from __future__ import annotations

# pyright: reportMissingTypeStubs=false
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import keyring
import keyring.errors
import platformdirs
import pytest
from cryptography.fernet import Fernet, InvalidToken
from jaraco.classes import properties
from keyring.backend import KeyringBackend as BaseKeyringBackend
from pydantic import SecretStr
from typing_extensions import override

import tiktok_mcp.auth.keychain as keychain_module
from tiktok_mcp.auth.keychain import (
    EncryptedFileBackend,
    KeyringBackend,
    account_key,
    atomic_account_update,
    deserialize_account_record,
    get_backend,
    serialize_account_record,
)
from tiktok_mcp.types.accounts import Account, AccountStatus, AccountTokens, ApiType
from tiktok_mcp.types.errors import KeychainUnavailableError

SERVICE_NAME = "tiktok-mcp"
NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


class MemoryKeyring(BaseKeyringBackend):
    @properties.classproperty
    def priority(self) -> float:
        return 1

    def __init__(self) -> None:
        super().__init__()
        self.values: dict[tuple[str, str], str] = {}

    @override
    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    @override
    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    @override
    def delete_password(self, service: str, username: str) -> None:
        try:
            del self.values[(service, username)]
        except KeyError as exc:
            raise keyring.errors.PasswordDeleteError("not found") from exc


@pytest.fixture(autouse=True)
def reset_backend_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keychain_module, "_backend", None)


@pytest.fixture
def memory_keyring() -> Iterator[MemoryKeyring]:
    original_keyring = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(original_keyring)


@pytest.fixture
def no_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_no_keyring(*_args: object, **_kwargs: object) -> None:
        raise keyring.errors.NoKeyringError("no keyring available")

    monkeypatch.setattr(keyring, "get_password", raise_no_keyring)
    monkeypatch.setattr(keyring, "set_password", raise_no_keyring)


@pytest.mark.asyncio
async def test_keyring_roundtrip(memory_keyring: MemoryKeyring) -> None:
    backend = KeyringBackend()
    key = "unit::roundtrip"

    await backend.set(key, "stored-value")

    assert await backend.get(key) == "stored-value"
    assert memory_keyring.values[(SERVICE_NAME, key)] == "stored-value"

    await backend.delete(key)


    assert await backend.get(key) is None


@pytest.mark.asyncio
async def test_keyring_list_keys_via_index(memory_keyring: MemoryKeyring) -> None:
    backend = KeyringBackend()

    await backend.set("prefix::one", "one")
    await backend.set("prefix::two", "two")
    await backend.set("other::three", "three")

    assert await backend.list_keys("prefix::") == ["prefix::one", "prefix::two"]
    assert (SERVICE_NAME, keychain_module.index_key_name()) in memory_keyring.values


@pytest.mark.asyncio
async def test_chunked_large_value(memory_keyring: MemoryKeyring) -> None:
    backend = KeyringBackend()
    key = "unit::large"
    large_value = "x" * 5_000

    await backend.set(key, large_value)

    part_keys = [
        username
        for service, username in memory_keyring.values
        if service == SERVICE_NAME and username.startswith(f"{key}::__part")
    ]
    sentinel = memory_keyring.values[(SERVICE_NAME, key)]

    assert len(part_keys) > 1
    assert '"chunked":true' in sentinel
    assert await backend.get(key) == large_value


@pytest.mark.asyncio
async def test_encrypted_file_roundtrip(tmp_path: Path, no_keyring: None) -> None:
    _ = no_keyring
    data_dir = tmp_path
    _ = _write_fernet_key(data_dir)
    backend = EncryptedFileBackend(data_dir=data_dir)
    key = "unit::encrypted"

    await backend.set(key, "encrypted-value")

    assert await backend.get(key) == "encrypted-value"
    assert b"encrypted-value" not in backend.path.read_bytes()

    await backend.delete(key)

    assert await backend.get(key) is None


@pytest.mark.asyncio
async def test_encrypted_file_decryption_requires_correct_key(
    tmp_path: Path, no_keyring: None
) -> None:
    _ = no_keyring
    data_dir = tmp_path
    _ = _write_fernet_key(data_dir)
    backend = EncryptedFileBackend(data_dir=data_dir)

    await backend.set("unit::encrypted", "encrypted-value")

    _ = _write_fernet_key(data_dir, Fernet.generate_key())
    wrong_key_backend = EncryptedFileBackend(data_dir=data_dir)

    with pytest.raises((InvalidToken, KeychainUnavailableError)):
        _ = await wrong_key_backend.get("unit::encrypted")


@pytest.mark.asyncio
async def test_fallback_to_encrypted_file(
    tmp_path: Path,
    no_keyring: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = no_keyring
    data_dir = tmp_path
    _ = _write_fernet_key(data_dir)

    def tmp_user_data_dir(*_args: object, **_kwargs: object) -> str:
        return str(data_dir)

    monkeypatch.setattr(
        platformdirs,
        "user_data_dir",
        tmp_user_data_dir,
    )

    backend = await get_backend()

    assert isinstance(backend, EncryptedFileBackend)


@pytest.mark.asyncio
async def test_atomic_write_crash_preserves_prior_value(
    tmp_path: Path,
    no_keyring: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = no_keyring
    data_dir = tmp_path
    _ = _write_fernet_key(data_dir)
    backend = EncryptedFileBackend(data_dir=data_dir)
    alias = "demo-display"
    canonical_key = account_key(ApiType.DISPLAY, sandbox=True, alias=alias)
    original_account = _make_account(alias=alias)
    original_tokens = _make_tokens("original-access-token", "original-refresh-token")
    original_blob = serialize_account_record(original_account, original_tokens)
    new_account = _make_account(alias=alias, display_name="New Display")
    new_tokens = _make_tokens("new-access-token", "new-refresh-token")

    await backend.set(canonical_key, original_blob)

    original_set_unlocked = backend.set_unlocked

    async def crash_on_canonical(write_key: str, value: str) -> None:
        if write_key == canonical_key:
            raise RuntimeError("simulated crash")
        await original_set_unlocked(write_key, value)

    monkeypatch.setattr(backend, "set_unlocked", crash_on_canonical)

    with pytest.raises(RuntimeError, match="simulated crash"):
        await atomic_account_update(
            backend,
            ApiType.DISPLAY,
            True,
            alias,
            new_account,
            new_tokens,
        )

    assert await backend.get(canonical_key) == original_blob


@pytest.mark.asyncio
async def test_sandbox_production_isolation(memory_keyring: MemoryKeyring) -> None:
    backend = KeyringBackend()
    alias = "same-alias"
    sandbox_key = account_key(ApiType.DISPLAY, sandbox=True, alias=alias)
    production_key = account_key(ApiType.DISPLAY, sandbox=False, alias=alias)

    await backend.set(sandbox_key, "sandbox-value")
    await backend.set(production_key, "production-value")

    assert sandbox_key != production_key
    assert await backend.get(sandbox_key) == "sandbox-value"
    assert await backend.get(production_key) == "production-value"
    assert await backend.get(sandbox_key) != await backend.get(production_key)
    assert memory_keyring.values[(SERVICE_NAME, sandbox_key)] == "sandbox-value"
    assert memory_keyring.values[(SERVICE_NAME, production_key)] == "production-value"


@pytest.mark.asyncio
async def test_get_backend_idempotent(memory_keyring: MemoryKeyring) -> None:
    first_backend = await get_backend()
    second_backend = await get_backend()

    assert isinstance(first_backend, KeyringBackend)
    assert first_backend is second_backend
    assert memory_keyring.values == {}


def test_account_record_serialization_redacts_tokens(caplog: pytest.LogCaptureFixture) -> None:
    account = _make_account()
    tokens = _make_tokens("redaction-access-token-123456", "redaction-refresh-token-123456")
    blob = serialize_account_record(account, tokens)

    restored_account, restored_tokens = deserialize_account_record(blob)

    assert restored_account == account
    assert restored_tokens.access_token.get_secret_value() == "redaction-access-token-123456"
    with caplog.at_level(logging.INFO):
        logging.getLogger().info(
            "round-tripped token %s",
            tokens.access_token.get_secret_value(),
        )
    assert "redaction-access-token-123456" not in caplog.text
    assert "<REDACTED:access_token>" in caplog.text


def _make_account(alias: str = "demo-display", display_name: str = "Demo Display") -> Account:
    return Account(
        alias=alias,
        api_type=ApiType.DISPLAY,
        sandbox=True,
        tiktok_id="abcd1234abcd1234abcd1234",
        display_name=display_name,
        avatar_url="https://example.com/avatar.png",
        scopes=["user.info.basic"],
        created_at=NOW,
        last_used_at=NOW + timedelta(minutes=5),
        status=AccountStatus.OK,
    )


def _make_tokens(access_token: str, refresh_token: str) -> AccountTokens:
    return AccountTokens(
        access_token=SecretStr(access_token),
        refresh_token=SecretStr(refresh_token),
        access_token_expires_at=NOW + timedelta(hours=1),
        refresh_token_expires_at=NOW + timedelta(days=30),
        last_rotated_at=NOW,
    )


def _write_fernet_key(data_dir: Path, key: bytes | None = None) -> bytes:
    fernet_key = key or Fernet.generate_key()
    key_path = data_dir / keychain_module.FERNET_KEY_FILE_NAME
    _ = key_path.write_bytes(fernet_key)
    return fernet_key
