from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings
from app.core.errors import ConfigurationError
from app.secrets.provider import SecretProvider


@lru_cache
def get_secret_provider() -> SecretProvider:
    """The configured secret backend. One instance per process."""
    backend = get_settings().secrets_backend.lower()
    if backend == "keyvault":
        from app.secrets.keyvault import KeyVaultSecretProvider

        return KeyVaultSecretProvider()
    if backend == "local":
        from app.secrets.local import LocalSecretProvider

        return LocalSecretProvider()
    raise ConfigurationError(
        f"Unknown secret backend '{backend}'. Use 'keyvault' or 'local'."
    )
