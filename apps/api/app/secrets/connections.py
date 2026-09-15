"""
Reading and writing a connection's credentials through the secret backend.

One place, so no router or pipeline ever touches a raw credential value or decides for
itself where secrets live.
"""

from __future__ import annotations

import logging
from typing import Any

from app.secrets.factory import get_secret_provider
from app.secrets.provider import SecretNotFound, reference_for

logger = logging.getLogger("assarium.secrets")


def store_secrets(connection_id: str, secrets: dict[str, Any]) -> dict[str, str]:
    """
    Write a connection's credential fields and return the reference map to persist.

    Returns field -> reference. The values themselves are not returned, so a caller
    cannot accidentally log or echo them back.
    """
    if not secrets:
        return {}

    provider = get_secret_provider()
    references = {field: reference_for(connection_id, field) for field in secrets}
    provider.put_many({references[field]: str(value) for field, value in secrets.items()})
    logger.info(
        "Stored %d credential field(s) for connection %s in the %s backend",
        len(references), connection_id, provider.name,
    )
    return references


def load_secrets(references: dict[str, str]) -> dict[str, Any]:
    """Resolve a stored reference map into usable credential values."""
    if not references:
        return {}
    return get_secret_provider().get_many(references)


def merge_secrets(
    connection_id: str, existing_refs: dict[str, str], submitted: dict[str, Any]
) -> dict[str, Any]:
    """
    Combine a submitted form with already-stored credentials.

    Editing a connection should not mean re-typing a private key that has not changed,
    so a blank field falls back to the stored value rather than wiping it.
    """
    values = dict(submitted)
    if not existing_refs:
        return values

    provider = get_secret_provider()
    for field, reference in existing_refs.items():
        if values.get(field):
            continue
        try:
            values[field] = provider.get(reference)
        except SecretNotFound:
            # The vault no longer has it. Leave the field absent so validation asks the
            # user for it, rather than failing later with a confusing auth error.
            logger.warning(
                "Credential '%s' for connection %s is missing from the backend",
                field, connection_id,
            )
    return values


def forget_secrets(references: dict[str, str]) -> None:
    """Remove a connection's credentials when it is deleted."""
    if references:
        get_secret_provider().delete_many(references)
