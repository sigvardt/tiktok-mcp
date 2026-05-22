from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

MIN_CHUNK_SIZE = 5 * 1024 * 1024
MAX_CHUNK_SIZE = 64 * 1024 * 1024
DEFAULT_CHUNK_SIZE = MIN_CHUNK_SIZE
HASH_READ_SIZE = 8192


def chunk_file(path: Path, chunk_size: int) -> Iterator[bytes]:
    if chunk_size < MIN_CHUNK_SIZE or chunk_size > MAX_CHUNK_SIZE:
        raise ValueError("chunk_size must be between 5MB and 64MB")

    with path.open("rb") as source:
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            yield chunk


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(HASH_READ_SIZE), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
