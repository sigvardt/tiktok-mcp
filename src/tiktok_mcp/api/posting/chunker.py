from __future__ import annotations

import asyncio
import importlib
import importlib.util
from collections.abc import AsyncIterator
from dataclasses import dataclass
from os import PathLike
from types import TracebackType
from typing import Protocol, cast

MIN_CHUNK_BYTES = 5 * 1024 * 1024
MAX_CHUNK_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class ChunkBounds:
    index: int
    start: int
    end: int
    total: int

    @property
    def size(self) -> int:
        return self.end - self.start + 1

    @property
    def content_range(self) -> str:
        return f"bytes {self.start}-{self.end}/{self.total}"


class AsyncReadableFile(Protocol):
    async def read(self, size: int) -> bytes: ...


class AsyncFileContext(Protocol):
    async def __aenter__(self) -> AsyncReadableFile: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class AiofilesModule(Protocol):
    def open(self, path: str | PathLike[str], mode: str) -> AsyncFileContext: ...


def chunk_bytes_for_upload(
    file_size: int,
    chunk_size: int,
    total_chunk_count: int,
) -> list[ChunkBounds]:
    _validate_positive("file_size", file_size)
    _validate_positive("chunk_size", chunk_size)
    _validate_positive("total_chunk_count", total_chunk_count)
    if chunk_size > MAX_CHUNK_BYTES:
        raise ValueError("chunk_size must be at most 64MB")
    if total_chunk_count > 1 and chunk_size < MIN_CHUNK_BYTES:
        raise ValueError("non-final chunks must be at least 5MB")

    expected_count = (file_size + chunk_size - 1) // chunk_size
    if expected_count != total_chunk_count:
        raise ValueError(
            "total_chunk_count must match ceil(file_size / chunk_size) for FILE_UPLOAD"
        )

    chunks: list[ChunkBounds] = []
    for index in range(total_chunk_count):
        start = index * chunk_size
        end = min(start + chunk_size, file_size) - 1
        chunk = ChunkBounds(index=index, start=start, end=end, total=file_size)
        _validate_chunk_bounds(chunk, is_final=index == total_chunk_count - 1)
        chunks.append(chunk)
    return chunks


def chunk_bounds_for_index(
    file_size: int,
    chunk_size: int,
    total_chunk_count: int,
    chunk_index: int,
) -> ChunkBounds:
    if chunk_index < 0:
        raise ValueError("chunk_index must be non-negative")
    chunks = chunk_bytes_for_upload(file_size, chunk_size, total_chunk_count)
    try:
        return chunks[chunk_index]
    except IndexError as exc:
        raise ValueError("chunk_index is outside total_chunk_count") from exc


async def iter_file_chunks(
    path: str | PathLike[str],
    *,
    file_size: int,
    chunk_size: int,
    total_chunk_count: int,
) -> AsyncIterator[bytes]:
    chunks = chunk_bytes_for_upload(file_size, chunk_size, total_chunk_count)
    aiofiles_module = _aiofiles_module()
    if aiofiles_module is not None:
        async with aiofiles_module.open(path, "rb") as file_obj:
            for chunk in chunks:
                yield await file_obj.read(chunk.size)
        return

    with open(path, "rb") as file_obj:
        for chunk in chunks:
            yield await asyncio.to_thread(file_obj.read, chunk.size)


def _validate_positive(name: str, value: int) -> None:
    if isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _validate_chunk_bounds(chunk: ChunkBounds, *, is_final: bool) -> None:
    if chunk.size <= 0:
        raise ValueError("chunk bounds must have positive size")
    if chunk.size > MAX_CHUNK_BYTES:
        raise ValueError("chunks must be at most 64MB")
    if not is_final and chunk.size < MIN_CHUNK_BYTES:
        raise ValueError("non-final chunks must be at least 5MB")


def _aiofiles_module() -> AiofilesModule | None:
    if importlib.util.find_spec("aiofiles") is None:
        return None
    return cast(AiofilesModule, cast(object, importlib.import_module("aiofiles")))


__all__ = [
    "ChunkBounds",
    "MAX_CHUNK_BYTES",
    "MIN_CHUNK_BYTES",
    "chunk_bounds_for_index",
    "chunk_bytes_for_upload",
    "iter_file_chunks",
]
