"""Encrypted-at-rest secret store (DB passwords, DO/SMTP/Slack/SSH credentials).

Secrets never touch the SQLite DB or any run artifact. They live Fernet-encrypted
in ``secrets.enc`` (0600), keyed by a 0600 ``secret.key`` under the data dir; the
DB only ever stores an opaque *reference name*. Plaintext exists only in memory
and is injected into the child process environment at exec time, exactly like the
harness's existing ``PGPASSWORD`` handling. Losing ``secret.key`` means stored
secrets can't be decrypted (documented in OPERATIONS.md backup section).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


def _is_valid_fernet_key(raw: bytes) -> bool:
    try:
        Fernet(raw)
        return True
    except (ValueError, TypeError):
        return False


def _has_secrets(store_path: Path) -> bool:
    if not store_path.exists():
        return False
    try:
        return bool(json.loads(store_path.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        return False


def _write_key(key_path: Path, key: bytes) -> None:
    key_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)


def _load_key(key_path: Path, store_path: Path) -> bytes:
    """Load the Fernet key, generating a valid one if missing.

    If an existing key file is NOT a valid Fernet key (e.g. an installer wrote
    ``openssl rand -base64 48`` instead of a url-safe 32-byte key), self-heal by
    regenerating it — but ONLY when no secrets have been encrypted yet. If
    encrypted secrets already exist under a bad key, refuse loudly rather than
    silently orphaning them (restore the real key from backup).
    """
    if key_path.exists():
        raw = key_path.read_bytes().strip()
        if _is_valid_fernet_key(raw):
            return raw
        if _has_secrets(store_path):
            raise ValueError(
                f"{key_path} is not a valid Fernet key but encrypted secrets exist in "
                f"{store_path}. Restore the original secret.key from backup (see OPERATIONS.md).")
        # No secrets yet: replace the bad key with a valid one.
    key = Fernet.generate_key()
    _write_key(key_path, key)
    return key


class SecretStore:
    """Reference-keyed encrypted store. The DB holds only the reference names."""

    def __init__(self, key_path: Path, store_path: Path) -> None:
        self._fernet = Fernet(_load_key(key_path, store_path))
        self._path = store_path

    def _read(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            return dict(json.loads(self._path.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            return {}

    def _write(self, data: dict[str, str]) -> None:
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, self._path)

    def set(self, ref: str, value: str) -> None:
        data = self._read()
        data[ref] = self._fernet.encrypt(value.encode()).decode()
        self._write(data)

    def get(self, ref: str) -> Optional[str]:
        token = self._read().get(ref)
        if not token:
            return None
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            return None

    def delete(self, ref: str) -> None:
        data = self._read()
        if data.pop(ref, None) is not None:
            self._write(data)

    def refs(self) -> list[str]:
        return sorted(self._read())
