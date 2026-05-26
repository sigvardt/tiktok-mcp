from __future__ import annotations

import csv
import hashlib
import io
import os
import re
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

MAX_AUDIENCE_FILE_BYTES = 100 * 1024 * 1024
HASHED_COLUMN_SUFFIX = "_sha256"

_PHONE_SEPARATOR_RE = re.compile(r"\D+")


class HashedAudienceCSVStream:
    def __init__(self, source_path: Path, match_keys: Sequence[str]) -> None:
        self._source_path: Path = source_path
        self._match_keys: tuple[str, ...] = tuple(
            _normalized_match_key(match_key) for match_key in match_keys
        )
        self._chunks: Iterator[bytes] | None = None
        self._buffer: bytearray = bytearray()

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if offset != 0 or whence != os.SEEK_SET:
            raise io.UnsupportedOperation("HashedAudienceCSVStream only supports seek(0)")
        self._chunks = iter_hashed_audience_csv_bytes(self._source_path, self._match_keys)
        self._buffer.clear()
        return 0

    def read(self, size: int = -1) -> bytes:
        if self._chunks is None:
            _ = self.seek(0)

        if size < 0:
            return self._read_all()

        while len(self._buffer) < size:
            try:
                self._buffer.extend(next(self._require_chunks()))
            except StopIteration:
                break

        chunk = bytes(self._buffer[:size])
        del self._buffer[:size]
        return chunk

    def _read_all(self) -> bytes:
        chunks = [bytes(self._buffer)]
        self._buffer.clear()
        chunks.extend(self._require_chunks())
        return b"".join(chunks)

    def _require_chunks(self) -> Iterator[bytes]:
        if self._chunks is None:
            _ = self.seek(0)
        if self._chunks is None:
            raise AssertionError("unreachable stream state")
        return self._chunks


def validate_audience_source_path(
    source_file_path: str,
    *,
    home: Path | None = None,
    cwd: Path | None = None,
) -> Path | dict[str, object]:
    parsed = urlparse(source_file_path)
    if parsed.netloc or (parsed.scheme and not _looks_like_windows_drive_path(source_file_path)):
        return _invalid_path_error("source_file_path must be a local file-system path")

    raw_path = Path(source_file_path)
    if ".." in raw_path.parts:
        return _invalid_path_error("source_file_path must not contain traversal segments")

    try:
        resolved_path = raw_path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return _invalid_path_error("source_file_path does not point to a readable local file")

    if not resolved_path.is_file():
        return _invalid_path_error("source_file_path must point to a regular file")

    home_root = (home or Path.home()).expanduser().resolve()
    cwd_root = (cwd or Path.cwd()).expanduser().resolve()
    if not _is_relative_to(resolved_path, home_root):
        return _invalid_path_error(
            "source_file_path must be inside the current user's home directory"
        )
    if not _is_relative_to(resolved_path, cwd_root):
        return _invalid_path_error("source_file_path must be inside the current working directory")

    file_size = resolved_path.stat().st_size
    if file_size > MAX_AUDIENCE_FILE_BYTES:
        return {
            "error": "audience_file_too_large",
            "message": "Custom Audience source files must be 100MB or smaller.",
            "file_size_bytes": file_size,
            "max_file_size_bytes": MAX_AUDIENCE_FILE_BYTES,
        }

    return resolved_path


def hash_identifier(value: str, match_key: str) -> str:
    normalized_value = normalize_identifier(value, match_key)
    return hashlib.sha256(normalized_value.encode("utf-8")).hexdigest()


def normalize_identifier(value: str, match_key: str) -> str:
    normalized = value.strip().lower()
    if _normalized_match_key(match_key) == "phone":
        return _PHONE_SEPARATOR_RE.sub("", normalized)
    return normalized


def iter_hashed_audience_csv_bytes(source_path: Path, match_keys: Sequence[str]) -> Iterator[bytes]:
    for row in iter_hashed_audience_csv_rows(source_path, match_keys):
        yield row.encode("utf-8")


def iter_hashed_audience_csv_rows(source_path: Path, match_keys: Sequence[str]) -> Iterator[str]:
    normalized_match_keys = tuple(_normalized_match_key(match_key) for match_key in match_keys)
    with source_path.open("r", encoding="utf-8", newline="") as source_file:
        reader: csv.DictReader[str] = csv.DictReader(source_file)
        fieldnames = set(reader.fieldnames or [])
        missing_fields = [
            match_key for match_key in normalized_match_keys if match_key not in fieldnames
        ]
        if missing_fields:
            raise ValueError(
                "Audience CSV is missing required columns: " + ", ".join(missing_fields)
            )

        yield _csv_line(f"{match_key}{HASHED_COLUMN_SUFFIX}" for match_key in normalized_match_keys)
        for row in reader:
            hashed_values = [
                hash_identifier(row.get(match_key, ""), match_key)
                for match_key in normalized_match_keys
            ]
            if any(hashed_values):
                yield _csv_line(hashed_values)


def estimate_csv_row_count(source_path: Path) -> int:
    line_count = 0
    with source_path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            line_count += chunk.count(b"\n")
    return max(line_count - 1, 0)


def filename_hash(source_path: Path) -> str:
    return hashlib.sha256(source_path.name.encode("utf-8")).hexdigest()


def _csv_line(values: Sequence[str] | Iterator[str]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    rows_written = cast(int, writer.writerow(list(values)))
    if rows_written < 0:
        raise AssertionError("csv writer returned a negative row count")
    return output.getvalue()


def _normalized_match_key(match_key: str) -> str:
    return match_key.strip().lower()


def _invalid_path_error(message: str) -> dict[str, object]:
    return {"error": "invalid_path", "message": message}


def _looks_like_windows_drive_path(value: str) -> bool:
    return len(value) >= 3 and value[0].isalpha() and value[1] == ":" and value[2] in {"/", "\\"}


def _is_relative_to(path: Path, root: Path) -> bool:
    return path.is_relative_to(root)


__all__ = [
    "HASHED_COLUMN_SUFFIX",
    "MAX_AUDIENCE_FILE_BYTES",
    "HashedAudienceCSVStream",
    "estimate_csv_row_count",
    "filename_hash",
    "hash_identifier",
    "iter_hashed_audience_csv_bytes",
    "iter_hashed_audience_csv_rows",
    "normalize_identifier",
    "validate_audience_source_path",
]
