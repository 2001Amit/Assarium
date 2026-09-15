from __future__ import annotations

import logging

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.entra import AuthenticationError, TokenClaims, get_validator, local_claims
from app.core.config import get_settings
from app.db.session import get_db
from app.tenancy.context import TenantContext, TenantResolutionError
from app.tenancy.models import ROLE_PERMISSIONS, Tenant, TenantMember
from app.tenancy.scope import TenantScope

logger = logging.getLogger("assarium.auth")

# Request fields that must never influence which tenant a caller is placed in. Their
# presence is logged, because a client sending them is either broken or probing.
TENANT_OVERRIDE_FIELDS = ("x-tenant-id", "x-tenant", "tenant_id", "tenant")


def _claims_from_request(request: Request) -> TokenClaims:
    settings = get_settings()

    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return get_validator().validate(header[7:].strip())

    if settings.auth_mode == "local":
        # Local development has no directory. The identity is fixed by configuration,
        # never taken from the request, so it cannot be used to impersonate anyone.
        return local_claims(
            subject=settings.local_dev_subject,
            email=settings.local_dev_email,
            directory=settings.local_dev_directory,
        )

    raise AuthenticationError("This endpoint needs a bearer token.")


def get_tenant_context(
    request: Request, db: Session = Depends(get_db)
) -> TenantContext:
    """
    Resolve who is calling and which tenant they are calling as.

    The tenant comes from the validated token's `tid` claim and the membership table -
    never from a header, query parameter or body field. This is the platform's single
    isolation boundary, so it has exactly one implementation and no override path.
    """
    claims = _claims_from_request(request)

    # Log, but do not honour, any attempt to name a tenant in the request itself.
    supplied = [f for f in TENANT_OVERRIDE_FIELDS if f in request.headers]
    if supplied:
        logger.warning(
            "Ignoring client-supplied tenant hint(s) %s from subject=%s directory=%s",
            supplied, claims.subject, claims.tenant_id,
        )

    tenant = db.execute(
        select(Tenant).where(Tenant.entra_tenant_id == claims.tenant_id)
    ).scalar_one_or_none()

    if tenant is None:
        logger.warning("No tenant registered for directory %s", claims.tenant_id)
        raise TenantResolutionError(
            "Your organisation is not registered with this platform."
        )
    if not tenant.is_active:
        raise TenantResolutionError(f"The {tenant.name} workspace is currently suspended.")

    member = db.execute(
        select(TenantMember).where(
            TenantMember.tenant_id == tenant.id,
            TenantMember.subject == claims.subject,
        )
    ).scalar_one_or_none()

    if member is None or not member.active:
        logger.warning(
            "Subject %s is in directory %s but is not a member of tenant %s",
            claims.subject, claims.tenant_id, tenant.slug,
        )
        raise TenantResolutionError(
            f"You do not have access to the {tenant.name} workspace. "
            "Ask an owner to add you."
        )

    context = TenantContext(
        tenant_id=tenant.id,
        slug=tenant.slug,
        name=tenant.name,
        catalog=tenant.catalog,
        storage_container=tenant.storage_container,
        subject=member.subject,
        email=member.email,
        role=member.role,
        permissions=frozenset(ROLE_PERMISSIONS.get(member.role, set())),
        is_service=claims.is_service,
    )
    # Published for the middleware: the rate limiter keys on the tenant so one customer
    # cannot spend another's budget, and the audit log needs to name the actor.
    request.state.tenant_id = context.tenant_id
    request.state.audit_context = context
    return context


def get_scope(
    context: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
) -> TenantScope:
    """
    A tenant-scoped database session.

    This is what routers depend on. Taking it as a parameter is what makes a handler
    tenant-aware, and `assert_routes_are_guarded` at startup refuses to serve any route
    that does not.
    """
    return TenantScope(db, context)


def require(permission: str):
    """
    Dependency factory for endpoints that need a specific permission.

        @router.post("/connections", dependencies=[Depends(require("connection:write"))])
    """

    def dependency(context: TenantContext = Depends(get_tenant_context)) -> TenantContext:
        context.require(permission)
        return context

    return dependency
