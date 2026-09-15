from __future__ import annotations

import re
from abc import ABC, abstractmethod

from app.core.errors import AssariumError

# Key Vault permits alphanumerics and dashes, 1-127 characters. The same shape is used
# for local references so a connection can move between backends unchanged.
REFERENCE_PATTERN = re.compile(r"^[a-zA-Z0-9-]{1,127}$")


class SecretError(AssariumError):
    status_code = 500
    code = "secret_error"


class SecretNotFound(SecretError):
    status_code = 404
    code = "secret_not_found"


def reference_for(connection_id: str, field: str) -> str:
    """
    Deterministic name for one credential field.

    Deterministic rather than random so a secret can be found and revoked from the vault
    by a human who only knows the connection, without a database lookup.
    """
    safe_field = re.sub(r"[^a-zA-Z0-9]+", "-", field).strip("-").lower()
    reference = f"source-{connection_id}-{safe_field}"
    if not REFERENCE_PATTERN.match(reference):
        raise SecretError(f"Cannot build a valid secret name from field '{field}'.")
    return reference


class SecretProvider(ABC):
    """
    Where credential values live.

    The application stores only *references*. A value is fetched at the moment it is
    needed and is never written to the metadata database, a log, or an API response.
    """

    name: str

    @abstractmethod
    def put(self, reference: str, value: str) -> None:
        """Store or replace a secret. Overwrites are expected on credential rotation."""

    @abstractmethod
    def get(self, reference: str) -> str:
        """Fetch a secret value. Raises SecretNotFound if it is gone."""

    @abstractmethod
    def delete(self, reference: str) -> None:
        """Remove a secret. Must not raise if it is already absent."""

    @abstractmethod
    def health(self) -> dict[str, str]:
        """Cheap reachability check for the health endpoint."""

    def put_many(self, values: dict[str, str]) -> dict[str, str]:
        """Store several fields for one connection. Returns field -> reference."""
        written: dict[str, str] = {}
        try:
            for reference, value in values.items():
                self.put(reference, value)
                written[reference] = reference
        except Exception:
            # Do not leave half a credential behind: a connection with three of four
            # fields stored fails at connect time in a way that is hard to diagnose.
            for reference in written:
                try:
                    self.delete(reference)
                except Exception:  # noqa: BLE001 - best effort cleanup
                    pass
            raise
        return written

    def get_many(self, references: dict[str, str]) -> dict[str, str]:
        """Resolve field -> reference into field -> value."""
        return {field: self.get(reference) for field, reference in references.items()}

    def delete_many(self, references: dict[str, str]) -> None:
        for reference in references.values():
            try:
                self.delete(reference)
            except Exception:  # noqa: BLE001 - deletion is best effort
                pass
