from __future__ import annotations

import os
from pathlib import Path
from threading import Lock

from app.core.config import get_settings
from app.core.security import decrypt_secrets, encrypt_secrets
from app.secrets.provider import SecretNotFound, SecretProvider


class LocalSecretProvider(SecretProvider):
    """
    Fernet-encrypted secrets in a file beside the metadata database.

    Development only - the production guards refuse to start with this backend outside a
    local environment. It exists so the product runs on a laptop without an Azure
    subscription, not as a supported way to hold customer credentials: a database or
    disk compromise here exposes every stored credential at once, which is exactly what
    Key Vault plus a Managed Identity avoids.
    """

    name = "local"

    def __init__(self, path: Path | None = None):
        self.path = path or (get_settings().data_dir / "secrets.enc")
        self._lock = Lock()

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        return decrypt_secrets(self.path.read_text())

    def _write(self, values: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(encrypt_secrets(values))
        os.chmod(self.path, 0o600)

    def put(self, reference: str, value: str) -> None:
        with self._lock:
            values = self._read()
            values[reference] = value
            self._write(values)

    def get(self, reference: str) -> str:
        with self._lock:
            values = self._read()
        if reference not in values:
            raise SecretNotFound(
                f"The credential '{reference}' is not stored. Re-enter it on the connection."
            )
        return values[reference]

    def delete(self, reference: str) -> None:
        with self._lock:
            values = self._read()
            if values.pop(reference, None) is not None:
                self._write(values)

    def health(self) -> dict[str, str]:
        try:
            count = len(self._read())
        except Exception as exc:  # noqa: BLE001
            return {"backend": "local", "status": "unreadable", "detail": str(exc)[:120]}
        return {
            "backend": "local",
            "status": "ok",
            "stored": str(count),
            "warning": "Development backend. Not for customer credentials.",
        }
