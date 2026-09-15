"""
The local development tenant.

Tenancy is now enforced on every route, which is correct - and it means a fresh checkout
has nowhere to put anything and answers 403 to everything. That is a terrible first
experience for a platform whose pitch is "connect a source and go".

So local development gets exactly one tenant, created on startup. It is gated on
`auth_mode == "local"`, which `enforce_production_guards` refuses outside a local
environment, so this can never run in a deployment: there is no configuration in which
the guard passes and this function also does anything.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.tenancy.models import Tenant, TenantMember

logger = logging.getLogger("assarium.tenancy")

LOCAL_SLUG = "local"


def ensure_local_tenant(db: Session) -> Tenant | None:
    """
    Create the development workspace if it is missing. Returns None outside local mode.
    """
    settings = get_settings()
    if settings.auth_mode != "local" or settings.environment != "local":
        return None

    tenant = db.execute(
        select(Tenant).where(Tenant.entra_tenant_id == settings.local_dev_directory)
    ).scalar_one_or_none()

    if tenant is None:
        tenant = Tenant(
            slug=LOCAL_SLUG,
            name="Local development",
            entra_tenant_id=settings.local_dev_directory,
            catalog=f"{settings.catalog_prefix}_local_{LOCAL_SLUG}",
            storage_container=f"tenant-{LOCAL_SLUG}",
        )
        db.add(tenant)
        db.flush()
        logger.info("Created the local development workspace (%s)", tenant.id)

    member = db.execute(
        select(TenantMember).where(
            TenantMember.tenant_id == tenant.id,
            TenantMember.subject == settings.local_dev_subject,
        )
    ).scalar_one_or_none()

    if member is None:
        db.add(
            TenantMember(
                tenant_id=tenant.id,
                user_id=settings.local_dev_subject,
                subject=settings.local_dev_subject,
                email=settings.local_dev_email,
                display_name="Local developer",
                # Owner, because a developer on their own machine needs to be able to do
                # everything the product can do - including the things a real owner does.
                role="owner",
            )
        )
        db.flush()
        logger.info("Added the local developer to the %s workspace", tenant.slug)

    return tenant
