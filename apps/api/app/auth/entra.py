from __future__ import annotations

import logging
import time
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient

from app.core.config import get_settings
from app.core.errors import AssariumError

logger = logging.getLogger("assarium.auth")


class AuthenticationError(AssariumError):
    status_code = 401
    code = "authentication_failed"


class TokenClaims:
    """The handful of claims the platform actually uses, already validated."""

    def __init__(self, raw: dict[str, Any]):
        self.raw = raw
        self.tenant_id: str = raw.get("tid", "")
        # `oid` is the stable object id; `sub` is pairwise per-application. Prefer oid,
        # because an email or UPN can change and must never be an identity key.
        self.subject: str = raw.get("oid") or raw.get("sub") or ""
        self.email: str | None = (
            raw.get("preferred_username") or raw.get("upn") or raw.get("email")
        )
        self.name: str | None = raw.get("name")
        self.scopes: set[str] = set((raw.get("scp") or "").split())
        self.roles: set[str] = set(raw.get("roles") or [])
        # A token with no user identity is an application token: a service principal.
        self.is_service: bool = "sub" in raw and not raw.get("preferred_username") and (
            raw.get("idtyp") == "app" or "roles" in raw and not raw.get("scp")
        )


class EntraValidator:
    """
    Validates Microsoft Entra ID access tokens.

    Signature, issuer, audience and expiry are all checked against the tenant's published
    JWKS. The `tid` claim is returned for tenant resolution but is *not* trusted for
    authorisation on its own - the caller must still match a registered tenant.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self.audience = settings.entra_audience
        self.allowed_tenants = set(settings.entra_allowed_tenant_ids or [])
        self._clients: dict[str, PyJWKClient] = {}
        self._issuers: dict[str, str] = {}

    # -- discovery ---------------------------------------------------------------------

    def _jwks_for(self, tenant_id: str) -> tuple[PyJWKClient, str]:
        """
        JWKS client and expected issuer for one directory.

        Cached per directory: fetching OpenID metadata on every request would add a
        round trip to Microsoft to every single API call.
        """
        if tenant_id in self._clients:
            return self._clients[tenant_id], self._issuers[tenant_id]

        discovery = (
            f"https://login.microsoftonline.com/{tenant_id}/v2.0/.well-known/openid-configuration"
        )
        try:
            metadata = httpx.get(discovery, timeout=10).raise_for_status().json()
        except Exception as exc:  # noqa: BLE001
            raise AuthenticationError(
                "Could not reach the Microsoft Entra ID discovery endpoint."
            ) from exc

        client = PyJWKClient(metadata["jwks_uri"], cache_keys=True, lifespan=3600)
        self._clients[tenant_id] = client
        self._issuers[tenant_id] = metadata["issuer"]
        return client, metadata["issuer"]

    # -- validation --------------------------------------------------------------------

    def validate(self, token: str) -> TokenClaims:
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            raise AuthenticationError("That is not a readable token.") from exc

        directory = unverified.get("tid")
        if not directory:
            raise AuthenticationError("The token carries no tenant (tid) claim.")

        # Reject unknown directories before spending a network call on their JWKS.
        if self.allowed_tenants and directory not in self.allowed_tenants:
            logger.warning("Rejected token from unregistered directory %s", directory)
            raise AuthenticationError("This directory is not registered with the platform.")

        client, issuer = self._jwks_for(directory)

        try:
            signing_key = client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=issuer,
                options={
                    "require": ["exp", "iat", "iss", "aud", "tid"],
                    "verify_exp": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
                leeway=30,
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError("That token has expired.") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthenticationError("That token was issued for a different application.") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthenticationError("That token came from an unexpected issuer.") from exc
        except jwt.PyJWTError as exc:
            raise AuthenticationError(f"The token failed validation: {exc}") from exc

        return TokenClaims(claims)


_validator: EntraValidator | None = None


def get_validator() -> EntraValidator:
    global _validator  # noqa: PLW0603
    if _validator is None:
        _validator = EntraValidator()
    return _validator


# --------------------------------------------------------------------------------------
# Local development identity
# --------------------------------------------------------------------------------------


def local_claims(subject: str, email: str, directory: str) -> TokenClaims:
    """
    A synthetic identity for local development, where there is no Entra tenant.

    Only reachable when `auth_mode` is "local", which `Settings` refuses to allow outside
    a local environment. It exists so the product can be run and demoed without an Azure
    directory, not as a way to skip authentication.
    """
    return TokenClaims(
        {
            "tid": directory,
            "oid": subject,
            "preferred_username": email,
            "name": email,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        }
    )
