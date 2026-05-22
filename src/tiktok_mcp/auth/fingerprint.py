from __future__ import annotations


def client_key_fingerprint(client_key: str) -> str:
    return f"{client_key[:4]}…{client_key[-4:]}"
