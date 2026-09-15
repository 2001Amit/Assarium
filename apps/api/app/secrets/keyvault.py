from __future__ import annotations

import logging
import threading
import time

from app.core.config import get_settings
from app.core.errors import ConfigurationError
from app.secrets.provider import SecretError, SecretNotFound, SecretProvider

logger = logging.getLogger("assarium.secrets")

# Values are cached briefly so a pipeline that opens many connections does not make a
# vault call per connection. Short enough that a rotation takes effect within a run.
CACHE_TTL_SECONDS = 300


class KeyVaultSecretProvider(SecretProvider):
    """
    Azure Key Vault, reached with the ambient Azure credential.

    In Azure this resolves to a Managed Identity, which means no secret has to exist to
    read the secrets - the thing most likely to leak is the thing we never issue.
    Locally it falls back to the developer's `az login`.
    """

    name = "keyvault"

    def __init__(self, vault_url: str | None = None):
        settings = get_settings()
        self.vault_url = vault_url or settings.key_vault_url
        if not self.vault_url:
            raise ConfigurationError(
                "ASSARIUM_KEY_VAULT_URL must be set to use the Key Vault secret backend."
            )
        self._client = None
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[str, float]] = {}

    @property
    def client(self):
        if self._client is None:
            with self._lock:
                if self._client is None:
                    try:
                        from azure.identity import DefaultAzureCredential
                        from azure.keyvault.secrets import SecretClient
                    except ImportError as exc:  # pragma: no cover
                        raise ConfigurationError(
                            "Key Vault support needs 'azure-identity' and "
                            "'azure-keyvault-secrets'."
                        ) from exc
                    self._client = SecretClient(
                        vault_url=self.vault_url, credential=DefaultAzureCredential()
                    )
        return self._client

    def put(self, reference: str, value: str) -> None:
        try:
            self.client.set_secret(reference, value)
        except Exception as exc:  # noqa: BLE001
            raise SecretError(f"Could not write '{reference}' to Key Vault.") from exc
        with self._lock:
            self._cache[reference] = (value, time.monotonic() + CACHE_TTL_SECONDS)

    def get(self, reference: str) -> str:
        with self._lock:
            cached = self._cache.get(reference)
            if cached and cached[1] > time.monotonic():
                return cached[0]

        try:
            secret = self.client.get_secret(reference)
        except Exception as exc:  # noqa: BLE001
            # The message deliberately names only the reference, never the vault
            # response, which can echo values in some error shapes.
            if "SecretNotFound" in str(exc) or "was not found" in str(exc):
                raise SecretNotFound(
                    f"The credential '{reference}' is no longer in the vault. "
                    "Re-enter it on the connection."
                ) from exc
            raise SecretError(f"Could not read '{reference}' from Key Vault.") from exc

        value = secret.value or ""
        with self._lock:
            self._cache[reference] = (value, time.monotonic() + CACHE_TTL_SECONDS)
        return value

    def delete(self, reference: str) -> None:
        with self._lock:
            self._cache.pop(reference, None)
        try:
            self.client.begin_delete_secret(reference)
        except Exception as exc:  # noqa: BLE001 - absent is an acceptable outcome
            logger.info("Delete of secret '%s' did not complete: %s", reference, exc)

    def health(self) -> dict[str, str]:
        try:
            next(iter(self.client.list_properties_of_secrets(max_page_size=1)), None)
            return {"backend": "keyvault", "vault": self.vault_url, "status": "ok"}
        except Exception as exc:  # noqa: BLE001
            return {
                "backend": "keyvault",
                "vault": self.vault_url,
                "status": "unreachable",
                "detail": str(exc).splitlines()[0][:160],
            }
