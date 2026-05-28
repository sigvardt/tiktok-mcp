from __future__ import annotations

# pyright: reportMissingTypeStubs=false
import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar, Protocol, cast

import keyring
import keyring.errors
import platformdirs
from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict, ValidationError

from tiktok_mcp.auth.redactor import register_token
from tiktok_mcp.types.accounts import Account, AccountTokens, ApiType
from tiktok_mcp.types.errors import (
    KeychainLockedError,
    KeychainUnavailableError,
    StoredCredentialError,
)

SERVICE_NAME = "tiktok-mcp"
CHUNK_THRESHOLD_BYTES = 2_000
CHUNK_SIZE = 1_900
CHUNK_SUFFIX_TEMPLATE = "::__part{index}__"
PENDING_SUFFIX = "::__pending__"
TOKENS_FILE_NAME = "tokens.json.enc"
FERNET_KEY_FILE_NAME = "fernet.key"
FERNET_BOOTSTRAP_ERROR = (
    "Both keyring and a stored fernet key were unavailable; encrypted file backend cannot "
    "bootstrap. See docs/security-model.md when present, or use an OS with keyring support."
)

logger = logging.getLogger(__name__)


class KeychainBackend(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def list_keys(self, prefix: str) -> list[str]: ...


class AccountRecord(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    account: Account
    tokens: AccountTokens


class KeyringBackend:
    def __init__(self) -> None:
        self.lock: asyncio.Lock = asyncio.Lock()

    async def get(self, key: str) -> str | None:
        async with self.lock:
            return await self.get_unlocked(key)

    async def set(self, key: str, value: str) -> None:
        async with self.lock:
            await self.set_unlocked(key, value)

    async def delete(self, key: str) -> None:
        async with self.lock:
            await self.delete_unlocked(key)

    async def list_keys(self, prefix: str) -> list[str]:
        async with self.lock:
            indexed_keys = await self._load_index_unlocked()
            return [key for key in indexed_keys if key.startswith(prefix)]

    async def probe(self) -> None:
        async with self.lock:
            await self._probe_unlocked()

    async def _probe_unlocked(self) -> None:
        _ = await self._get_raw(index_key_name())

    async def get_unlocked(self, key: str) -> str | None:
        value = await self._get_raw(key)
        part_count = _chunk_part_count(value)
        if part_count is None:
            return value

        parts: list[str] = []
        for index in range(part_count):
            part_key = _chunk_key(key, index)
            part = await self._get_raw(part_key)
            if part is None:
                raise KeychainUnavailableError(
                    "Chunked keychain entry is incomplete.",
                    context={"key": key, "part": index},
                )
            parts.append(part)

        try:
            return base64.b64decode("".join(parts).encode("ascii"), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise KeychainUnavailableError(
                "Chunked keychain entry could not be decoded.", context={"key": key}
            ) from exc

    async def set_unlocked(self, key: str, value: str) -> None:
        existing_value = await self._get_raw(key)
        existing_part_count = _chunk_part_count(existing_value)

        value_bytes = value.encode("utf-8")
        if len(value_bytes) > CHUNK_THRESHOLD_BYTES:
            encoded_value = base64.b64encode(value_bytes).decode("ascii")
            parts = [
                encoded_value[start : start + CHUNK_SIZE]
                for start in range(0, len(encoded_value), CHUNK_SIZE)
            ]
            for index, part in enumerate(parts):
                await self._set_raw(_chunk_key(key, index), part)
            await self._set_raw(
                key,
                json.dumps(
                    {"chunked": True, "n_parts": len(parts), "encoding": "base64"},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
            await self._delete_stale_parts_unlocked(key, existing_part_count, len(parts))
        else:
            await self._set_raw(key, value)
            await self._delete_stale_parts_unlocked(key, existing_part_count, 0)

        await self._add_index_key_unlocked(key)

    async def delete_unlocked(self, key: str) -> None:
        existing_value = await self._get_raw(key)
        existing_part_count = _chunk_part_count(existing_value)

        await self._delete_raw(key)
        if existing_part_count is not None:
            for index in range(existing_part_count):
                await self._delete_raw(_chunk_key(key, index))
        await self._remove_index_key_unlocked(key)

    async def _delete_stale_parts_unlocked(
        self, key: str, existing_part_count: int | None, retained_part_count: int
    ) -> None:
        if existing_part_count is None:
            return

        for index in range(retained_part_count, existing_part_count):
            await self._delete_raw(_chunk_key(key, index))

    async def _load_index_unlocked(self) -> list[str]:
        raw_index = await self._get_raw(index_key_name())
        if raw_index is None:
            return []

        try:
            loaded_index = cast(object, json.loads(raw_index))
        except json.JSONDecodeError as exc:
            raise KeychainUnavailableError("Keyring index is not valid JSON.") from exc

        if not isinstance(loaded_index, list):
            raise KeychainUnavailableError("Keyring index must be a JSON array.")

        indexed_keys: list[str] = []
        for item in cast(list[object], loaded_index):
            if not isinstance(item, str):
                raise KeychainUnavailableError("Keyring index contains a non-string key.")
            indexed_keys.append(item)
        return sorted(set(indexed_keys))

    async def _save_index_unlocked(self, indexed_keys: list[str]) -> None:
        await self._set_raw(
            index_key_name(),
            json.dumps(sorted(set(indexed_keys)), separators=(",", ":"), sort_keys=True),
        )

    async def _add_index_key_unlocked(self, key: str) -> None:
        if key == index_key_name():
            return

        indexed_keys = await self._load_index_unlocked()
        if key not in indexed_keys:
            indexed_keys.append(key)
            await self._save_index_unlocked(indexed_keys)

    async def _remove_index_key_unlocked(self, key: str) -> None:
        indexed_keys = await self._load_index_unlocked()
        next_index = [indexed_key for indexed_key in indexed_keys if indexed_key != key]
        if next_index != indexed_keys:
            await self._save_index_unlocked(next_index)

    async def _get_raw(self, key: str) -> str | None:
        try:
            return await asyncio.to_thread(keyring.get_password, SERVICE_NAME, key)
        except keyring.errors.NoKeyringError as exc:
            raise KeychainUnavailableError(
                str(exc), context={"operation": "get", "key": key}
            ) from exc
        except keyring.errors.KeyringLocked as exc:
            raise KeychainLockedError(context={"operation": "get", "key": key}) from exc

    async def _set_raw(self, key: str, value: str) -> None:
        try:
            await asyncio.to_thread(keyring.set_password, SERVICE_NAME, key, value)
        except keyring.errors.NoKeyringError as exc:
            raise KeychainUnavailableError(
                str(exc), context={"operation": "set", "key": key}
            ) from exc
        except keyring.errors.KeyringLocked as exc:
            raise KeychainLockedError(context={"operation": "set", "key": key}) from exc
        except keyring.errors.PasswordSetError as exc:
            raise KeychainUnavailableError(
                "Keyring refused to store a secret.", context={"operation": "set", "key": key}
            ) from exc

    async def _delete_raw(self, key: str) -> None:
        try:
            await asyncio.to_thread(keyring.delete_password, SERVICE_NAME, key)
        except keyring.errors.PasswordDeleteError as exc:
            logger.debug("Keyring delete skipped for absent key %s: %s", key, exc)
        except keyring.errors.NoKeyringError as exc:
            raise KeychainUnavailableError(
                str(exc), context={"operation": "delete", "key": key}
            ) from exc
        except keyring.errors.KeyringLocked as exc:
            raise KeychainLockedError(context={"operation": "delete", "key": key}) from exc


class EncryptedFileBackend:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir: Path = data_dir or Path(
            platformdirs.user_data_dir("tiktok-mcp", appauthor="Signikant", ensure_exists=True)
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path: Path = self.data_dir / TOKENS_FILE_NAME
        self.fernet_key_path: Path = self.data_dir / FERNET_KEY_FILE_NAME
        self.lock: asyncio.Lock = asyncio.Lock()
        self.fernet: Fernet = Fernet(self._load_or_create_fernet_key())

    async def get(self, key: str) -> str | None:
        async with self.lock:
            return await self.get_unlocked(key)

    async def set(self, key: str, value: str) -> None:
        async with self.lock:
            await self.set_unlocked(key, value)

    async def delete(self, key: str) -> None:
        async with self.lock:
            await self.delete_unlocked(key)

    async def list_keys(self, prefix: str) -> list[str]:
        async with self.lock:
            store = self._read_store_unlocked()
            return sorted(key for key in store if key.startswith(prefix))

    async def get_unlocked(self, key: str) -> str | None:
        store = self._read_store_unlocked()
        return store.get(key)

    async def set_unlocked(self, key: str, value: str) -> None:
        store = self._read_store_unlocked()
        store[key] = value
        self._write_store_unlocked(store)

    async def delete_unlocked(self, key: str) -> None:
        store = self._read_store_unlocked()
        if key in store:
            del store[key]
            self._write_store_unlocked(store)

    def _load_or_create_fernet_key(self) -> bytes:
        try:
            stored_key = keyring.get_password(SERVICE_NAME, fernet_key_name())
        except keyring.errors.NoKeyringError:
            return self._load_existing_fernet_key_file()
        except keyring.errors.KeyringLocked as exc:
            raise KeychainLockedError(context={"operation": "load_fernet_key"}) from exc

        if stored_key is not None:
            return stored_key.encode("ascii")

        generated_key = Fernet.generate_key()
        try:
            keyring.set_password(SERVICE_NAME, fernet_key_name(), generated_key.decode("ascii"))
        except keyring.errors.NoKeyringError:
            if self.fernet_key_path.exists():
                return self._load_existing_fernet_key_file()
            raise KeychainUnavailableError(FERNET_BOOTSTRAP_ERROR) from None
        except keyring.errors.KeyringLocked as exc:
            raise KeychainLockedError(context={"operation": "store_fernet_key"}) from exc
        except keyring.errors.PasswordSetError as exc:
            raise KeychainUnavailableError(
                "Keyring refused to store the encrypted file fernet key."
            ) from exc
        return generated_key

    def _load_existing_fernet_key_file(self) -> bytes:
        if not self.fernet_key_path.exists():
            raise KeychainUnavailableError(FERNET_BOOTSTRAP_ERROR)
        return self.fernet_key_path.read_bytes().strip()

    def _read_store_unlocked(self) -> dict[str, str]:
        if not self.path.exists():
            return {}

        decrypted = self.fernet.decrypt(self.path.read_bytes())
        try:
            loaded_store = cast(object, json.loads(decrypted.decode("utf-8")))
        except json.JSONDecodeError as exc:
            raise KeychainUnavailableError("Encrypted keychain file is not valid JSON.") from exc

        if not isinstance(loaded_store, dict):
            raise KeychainUnavailableError("Encrypted keychain file must contain a JSON object.")

        store: dict[str, str] = {}
        for key, value in cast(dict[object, object], loaded_store).items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise KeychainUnavailableError(
                    "Encrypted keychain file contains a non-string key or value."
                )
            store[key] = value
        return store

    def _write_store_unlocked(self, store: Mapping[str, str]) -> None:
        payload = json.dumps(dict(store), separators=(",", ":"), sort_keys=True).encode("utf-8")
        encrypted_payload = self.fernet.encrypt(payload)
        tmp_path = self.path.with_name(f"{self.path.name}.tmp")

        with tmp_path.open("wb") as file_handle:
            _ = file_handle.write(encrypted_payload)
            file_handle.flush()
            os.fsync(file_handle.fileno())

        os.replace(tmp_path, self.path)


_backend: KeychainBackend | None = None
_backend_selection_lock = asyncio.Lock()


async def get_backend() -> KeychainBackend:
    global _backend

    async with _backend_selection_lock:
        if _backend is None:
            try:
                keyring_backend = KeyringBackend()
                await keyring_backend.probe()
                backend: KeychainBackend = keyring_backend
            except KeychainUnavailableError:
                backend = EncryptedFileBackend()

            _backend = backend
            logger.info("Keychain backend selected: %s", type(backend).__name__)
        return _backend


def account_key(api: ApiType, sandbox: bool, alias: str) -> str:
    mode = "sandbox" if sandbox else "production"
    return f"tiktok-mcp::{api.value}::{mode}::account::{alias}"


def app_creds_key(api: ApiType, sandbox: bool) -> str:
    mode = "sandbox" if sandbox else "production"
    return f"tiktok-mcp::{api.value}::{mode}::app_creds"


def fernet_key_name() -> str:
    return "tiktok-mcp::__fernet_key__"


def index_key_name() -> str:
    return "tiktok-mcp::__index__"


def serialize_account_record(account: Account, tokens: AccountTokens) -> str:
    access_token = tokens.access_token.get_secret_value()
    refresh_token = (
        tokens.refresh_token.get_secret_value() if tokens.refresh_token is not None else None
    )
    register_token(access_token, "access_token")
    if refresh_token is not None:
        register_token(refresh_token, "refresh_token")

    account_payload = account.model_dump(mode="json")
    tokens_payload = tokens.model_dump(mode="json")
    tokens_payload["access_token"] = access_token
    tokens_payload["refresh_token"] = refresh_token
    return json.dumps(
        {"account": account_payload, "tokens": tokens_payload},
        separators=(",", ":"),
        sort_keys=True,
    )


def deserialize_account_record(blob: str) -> tuple[Account, AccountTokens]:
    try:
        payload = cast(object, json.loads(blob))
    except json.JSONDecodeError as exc:
        raise StoredCredentialError(
            "Stored account record is not valid JSON.",
            context={"record_type": "account"},
        ) from exc

    try:
        record = AccountRecord.model_validate(payload)
    except ValidationError as exc:
        raise StoredCredentialError(
            "Stored account record failed schema validation.",
            context={
                "record_type": "account",
                "details": exc.errors(include_url=False, include_input=False),
            },
        ) from exc
    register_token(record.tokens.access_token.get_secret_value(), "access_token")
    if record.tokens.refresh_token is not None:
        register_token(record.tokens.refresh_token.get_secret_value(), "refresh_token")
    return record.account, record.tokens


async def atomic_account_update(
    backend: KeychainBackend,
    api: ApiType,
    sandbox: bool,
    alias: str,
    new_account: Account,
    new_tokens: AccountTokens,
) -> None:
    key = account_key(api, sandbox, alias)
    pending_key = f"{key}{PENDING_SUFFIX}"
    serialized_record = serialize_account_record(new_account, new_tokens)
    expected_checksum = hashlib.sha256(serialized_record.encode("utf-8")).hexdigest()

    if isinstance(backend, KeyringBackend | EncryptedFileBackend):
        async with backend.lock:
            await backend.set_unlocked(pending_key, serialized_record)
            pending_record = await backend.get_unlocked(pending_key)
            verified_record = _verified_pending_record(pending_record, expected_checksum)
            await backend.set_unlocked(key, verified_record)
            await backend.delete_unlocked(pending_key)
        return

    await backend.set(pending_key, serialized_record)
    pending_record = await backend.get(pending_key)
    verified_record = _verified_pending_record(pending_record, expected_checksum)
    await backend.set(key, verified_record)
    await backend.delete(pending_key)


def _verified_pending_record(pending_record: str | None, expected_checksum: str) -> str:
    if pending_record is None:
        raise KeychainUnavailableError("Pending account write disappeared before verification.")

    actual_checksum = hashlib.sha256(pending_record.encode("utf-8")).hexdigest()
    if actual_checksum != expected_checksum:
        raise KeychainUnavailableError("Pending account write checksum verification failed.")
    return pending_record


def _chunk_key(key: str, index: int) -> str:
    return f"{key}{CHUNK_SUFFIX_TEMPLATE.format(index=index)}"


def _chunk_part_count(value: str | None) -> int | None:
    if value is None:
        return None

    try:
        sentinel_object = cast(object, json.loads(value))
    except json.JSONDecodeError:
        return None

    if not isinstance(sentinel_object, dict):
        return None
    sentinel = cast(dict[str, object], sentinel_object)
    if sentinel.get("chunked") is not True:
        return None

    part_count = sentinel.get("n_parts")
    if not isinstance(part_count, int) or part_count < 1:
        return None
    return part_count


__all__ = [
    "EncryptedFileBackend",
    "KeychainBackend",
    "KeyringBackend",
    "account_key",
    "app_creds_key",
    "atomic_account_update",
    "deserialize_account_record",
    "fernet_key_name",
    "get_backend",
    "index_key_name",
    "serialize_account_record",
]
