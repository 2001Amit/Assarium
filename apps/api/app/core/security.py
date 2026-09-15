from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings
from app.core.errors import ConfigurationError

_KEY_FILENAME = "secret.key"


@lru_cache
def _fernet() -> Fernet:
    settings = get_settings()
    if settings.secret_key:
        return Fernet(settings.secret_key.encode())

    # Local convenience only: persist a generated key with owner-only permissions.
    key_path = settings.data_dir / _KEY_FILENAME
    if key_path.exists():
        return Fernet(key_path.read_bytes())

    if settings.environment != "local":
        raise ConfigurationError(
            "ASSARIUM_SECRET_KEY must be set outside local development; refusing to "
            "generate an ephemeral encryption key."
        )
    key = Fernet.generate_key()
    key_path.write_bytes(key)
    os.chmod(key_path, 0o600)
    return Fernet(key)


def encrypt_secrets(payload: dict[str, Any]) -> str:
    """Encrypt a credential bundle at rest. Returns a urlsafe token."""
    return _fernet().encrypt(json.dumps(payload).encode()).decode()


def decrypt_secrets(token: str) -> dict[str, Any]:
    try:
        return json.loads(_fernet().decrypt(token.encode()).decode())
    except InvalidToken as exc:  # pragma: no cover - depends on key rotation
        raise ConfigurationError(
            "Stored credentials could not be decrypted. The encryption key has changed."
        ) from exc


def redact(value: str, keep: int = 4) -> str:
    """Render a secret for display without leaking it."""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]
