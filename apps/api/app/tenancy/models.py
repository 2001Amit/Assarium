from __future__ import annotations

import re
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TimestampedBase

# Catalog and container names are derived from the slug, so it has to be safe for both
# Unity Catalog identifiers and Azure storage container names: lowercase, alphanumeric
# and underscores, starting with a letter.
SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,38}$")

ROLES = ("owner", "admin", "analyst", "viewer")

#: What each role may do. Checked in one place so a new endpoint cannot invent its own
#: interpretation of "admin".
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "owner": {
        "tenant:read", "tenant:write", "connection:read", "connection:write",
        "dataset:read", "dataset:write", "pipeline:read", "pipeline:run",
        "model:read", "model:write", "dashboard:read", "dashboard:write",
        "query:run", "export:run", "audit:read", "member:manage",
        # Seeing people, not just counts of them. Deliberately not given to analyst or
        # viewer: most analysis needs the shape of the data, not the individuals in it.
        "pii:view",
    },
    "admin": {
        "tenant:read", "connection:read", "connection:write",
        "dataset:read", "dataset:write", "pipeline:read", "pipeline:run",
        "model:read", "model:write", "dashboard:read", "dashboard:write",
        "query:run", "export:run", "audit:read", "pii:view",
    },
    "analyst": {
        "tenant:read", "connection:read", "dataset:read", "pipeline:read",
        "model:read", "dashboard:read", "dashboard:write", "query:run", "export:run",
    },
    "viewer": {
        "tenant:read", "connection:read", "dataset:read", "pipeline:read",
        "model:read", "dashboard:read", "query:run",
    },
}


def validate_slug(slug: str) -> str:
    if not SLUG_PATTERN.match(slug):
        raise ValueError(
            f"'{slug}' is not a usable tenant slug. Use 2-39 characters: lowercase "
            "letters, digits and underscores, starting with a letter."
        )
    return slug


class Tenant(TimestampedBase):
    """
    One customer of the platform.

    A tenant owns a Unity Catalog catalog and a storage container. Nothing is shared
    between tenants at the data layer - isolation is structural, not a filter predicate
    that could be got wrong.
    """

    __tablename__ = "tenants"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_tenant_slug"),
        UniqueConstraint("entra_tenant_id", name="uq_tenant_entra"),
    )

    slug: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(200))

    # The Entra ID directory (`tid` claim) whose users map to this tenant. The only
    # thing that decides which tenant a request belongs to.
    entra_tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    catalog: Mapped[str] = mapped_column(String(80))
    storage_container: Mapped[str] = mapped_column(String(80))

    status: Mapped[str] = mapped_column(String(20), default="active")  # active | suspended
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    members: Mapped[list[TenantMember]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    def schema_for(self, layer: str) -> str:
        """Fully-qualified schema for one medallion layer inside this tenant's catalog."""
        return f"{self.catalog}.{layer}"


class TenantMember(TimestampedBase):
    """A person's membership of one tenant, and what they may do in it."""

    __tablename__ = "tenant_members"
    __table_args__ = (
        UniqueConstraint("tenant_id", "subject", name="uq_member_subject"),
    )

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )

    #: The platform user this membership belongs to. This is the link that matters:
    #: a person is identified by their account, not by whichever directory happened to
    #: issue their token.
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    # An external subject, where one exists: the Entra object id (`oid`), or the SSO
    # provider's own subject claim. Stable across email changes, unlike a UPN. Kept
    # alongside `user_id` so a federated sign-in resolves without a second lookup.
    subject: Mapped[str] = mapped_column(String(64), index=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    role: Mapped[str] = mapped_column(String(20), default="viewer")
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    tenant: Mapped[Tenant] = relationship(back_populates="members")

    @property
    def permissions(self) -> set[str]:
        return ROLE_PERMISSIONS.get(self.role, set())
