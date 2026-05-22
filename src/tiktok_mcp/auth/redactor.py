"""Logging filter that removes TikTok tokens and secret-bearing values."""

from __future__ import annotations

import logging
import re
import threading
from _thread import LockType
from collections.abc import Mapping
from typing import ClassVar

from typing_extensions import override

LogArgs = tuple[object, ...] | Mapping[str, object] | None


class SecretRedactor(logging.Filter):
    """Redact registered token values and common secret-bearing log patterns."""

    MASK_REPLACEMENT_TEMPLATE: ClassVar[str] = "<REDACTED:{name}>"
    DEFAULT_SEED_PATTERNS: ClassVar[tuple[str, ...]] = (
        "access_token",
        "refresh_token",
        "code=",
        "client_secret",
        "auth_code",
        "secret",
        "Authorization:",
        "Bearer ",
    )

    _VALUE_PATTERN: ClassVar[str] = r"[^\s&\"'\)\]\}<%]+"
    _JSON_VALUE_PATTERN: ClassVar[str] = r"[^\"<%]+"
    _SPECIAL_SEED_KEYS: ClassVar[frozenset[str]] = frozenset({"authorization", "bearer"})

    def __init__(self, seed_patterns: list[str] | None = None) -> None:
        super().__init__()
        self._lock: LockType = threading.Lock()
        self._tokens: set[str] = set()
        self._token_names: dict[str, str] = {}
        self._patterns: list[re.Pattern[str]] = self._compile_patterns(seed_patterns)

    def register_token(self, token: str, name: str) -> None:
        if not token:
            return
        with self._lock:
            self._tokens.add(token)
            self._token_names[token] = name

    def unregister_token(self, token: str) -> None:
        with self._lock:
            self._tokens.discard(token)
            _ = self._token_names.pop(token, None)

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact_text(str(record.msg))
        record.args = self._redact_args(record.args)
        formatted_message = record.getMessage()
        redacted_message = self._redact_text(formatted_message)
        if redacted_message != formatted_message:
            record.msg = redacted_message
            record.args = ()
        return True

    def _redact_args(self, args: LogArgs) -> LogArgs:
        if args is None:
            return None
        if isinstance(args, tuple):
            return tuple(self._redact_value(arg) for arg in args)
        return {key: self._redact_value(value) for key, value in args.items()}

    def _redact_value(self, value: object) -> object:
        if isinstance(value, str):
            return self._redact_text(value)

        text = str(value)
        redacted = self._redact_text(text)
        if redacted == text:
            return value
        return redacted

    def _redact_text(self, text: str) -> str:
        redacted = self._redact_registered_tokens(text)
        for pattern in self._patterns:
            redacted = pattern.sub(self._mask_pattern_match, redacted)
        return redacted

    def _redact_registered_tokens(self, text: str) -> str:
        with self._lock:
            token_names = tuple(
                sorted(self._token_names.items(), key=lambda item: len(item[0]), reverse=True)
            )

        redacted = text
        for token, name in token_names:
            redacted = redacted.replace(
                token,
                self.MASK_REPLACEMENT_TEMPLATE.format(name=name),
            )
        return redacted

    @staticmethod
    def _mask_pattern_match(match: re.Match[str]) -> str:
        return f"{match.group(1)}<REDACTED>{match.group(3)}"

    @classmethod
    def _compile_patterns(cls, seed_patterns: list[str] | None) -> list[re.Pattern[str]]:
        pattern_sources = [
            rf"(Authorization:\s*Bearer\s+)({cls._VALUE_PATTERN})()",
            rf"(Authorization:\s*(?!Bearer\s+)(?:[A-Za-z]+\s+)?)({cls._VALUE_PATTERN})()",
            rf"(Bearer\s+)({cls._VALUE_PATTERN})()",
            rf'("access_token"\s*:\s*")({cls._JSON_VALUE_PATTERN})(")',
            rf"(access_token=)({cls._VALUE_PATTERN})()",
            rf"(Access-Token:\s*)({cls._VALUE_PATTERN})()",
        ]

        seeds = seed_patterns if seed_patterns is not None else list(cls.DEFAULT_SEED_PATTERNS)
        for seed in seeds:
            pattern_sources.extend(cls._pattern_sources_for_seed(seed))

        unique_sources = list(dict.fromkeys(pattern_sources))
        return [re.compile(source) for source in unique_sources]

    @classmethod
    def _pattern_sources_for_seed(cls, seed: str) -> list[str]:
        key = cls._normalise_seed_key(seed)
        if not key or key.lower() in cls._SPECIAL_SEED_KEYS:
            return []

        escaped_key = re.escape(key)
        return [
            rf"({escaped_key}\s*=\s*)({cls._VALUE_PATTERN})()",
            rf"({escaped_key}:\s+)({cls._VALUE_PATTERN})()",
            rf'("{escaped_key}"\s*:\s*")({cls._JSON_VALUE_PATTERN})(")',
        ]

    @staticmethod
    def _normalise_seed_key(seed: str) -> str:
        return seed.strip().rstrip("=:").strip()


_redactor: SecretRedactor | None = None
_redactor_lock: LockType = threading.Lock()


def install_redactor() -> SecretRedactor:
    """Install a singleton SecretRedactor on the root logger idempotently."""
    global _redactor

    with _redactor_lock:
        root_logger = logging.getLogger()
        for existing_filter in root_logger.filters:
            if isinstance(existing_filter, SecretRedactor):
                _redactor = existing_filter
                return existing_filter

        if _redactor is None:
            _redactor = SecretRedactor()

        root_logger.addFilter(_redactor)
        return _redactor


def get_redactor() -> SecretRedactor:
    if _redactor is None:
        msg = "SecretRedactor has not been installed"
        raise RuntimeError(msg)
    return _redactor


def register_token(token: str, name: str = "token") -> None:
    install_redactor().register_token(token, name)


def unregister_token(token: str) -> None:
    install_redactor().unregister_token(token)
