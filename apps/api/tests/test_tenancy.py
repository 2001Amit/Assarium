"""
Tenant isolation guarantees.

These are the most important tests in the codebase. A bug anywhere else costs a wrong
number; a bug here shows one customer another customer's data.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.auth.deps import get_tenant_context
from app.core.config import Settings
from app.core.errors import ConfigurationError
from app.models.base import Base
from app.tenancy.context import (
    PermissionDenied,
    TenantContext,
    TenantResolutionError,
    current,
    current_or_none,
    reset_current,
    set_current,
)
from app.tenancy.models import ROLE_PERMISSIONS, Tenant, TenantMember, validate_slug


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture
def two_tenants(db: Session):
    """Two tenants in two different Entra directories, each with one member."""
    acme = Tenant(
        slug="acme", name="Acme REIT", entra_tenant_id="dir-acme",
        catalog="assarium_dev_acme", storage_container="acme",
    )
    globex = Tenant(
        slug="globex", name="Globex Properties", entra_tenant_id="dir-globex",
        catalog="assarium_dev_globex", storage_container="globex",
    )
    db.add_all([acme, globex])
    db.flush()
    db.add_all([
        TenantMember(tenant_id=acme.id, subject="user-a", email="a@acme.com", role="admin"),
        TenantMember(tenant_id=globex.id, subject="user-g", email="g@globex.com", role="viewer"),
    ])
    db.commit()
    return acme, globex


def make_request(headers: dict[str, str]) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/api/datasets",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "query_string": b"",
    })


def claims(directory: str, subject: str, email: str = "x@example.com"):
    from app.auth.entra import TokenClaims

    return TokenClaims({"tid": directory, "oid": subject, "preferred_username": email})


class TestTenantComesFromTheTokenOnly:
    """The single isolation boundary: a caller may never name their own tenant."""

    def test_header_cannot_override_the_tenant(self, db, two_tenants, monkeypatch):
        acme, globex = two_tenants
        # A member of Acme, asking to be treated as Globex.
        monkeypatch.setattr(
            "app.auth.deps._claims_from_request",
            lambda _r: claims("dir-acme", "user-a"),
        )
        request = make_request({
            "X-Tenant-Id": globex.id,
            "X-Tenant": "globex",
        })
        context = get_tenant_context(request, db)
        assert context.tenant_id == acme.id
        assert context.catalog == "assarium_dev_acme"

    def test_a_directory_with_no_tenant_is_refused(self, db, two_tenants, monkeypatch):
        monkeypatch.setattr(
            "app.auth.deps._claims_from_request",
            lambda _r: claims("dir-unknown", "user-x"),
        )
        with pytest.raises(TenantResolutionError) as caught:
            get_tenant_context(make_request({}), db)
        assert "not registered" in str(caught.value)

    def test_a_directory_member_who_is_not_a_platform_member_is_refused(
        self, db, two_tenants, monkeypatch
    ):
        """Being in the right Entra directory is not the same as having access."""
        monkeypatch.setattr(
            "app.auth.deps._claims_from_request",
            lambda _r: claims("dir-acme", "someone-else"),
        )
        with pytest.raises(TenantResolutionError) as caught:
            get_tenant_context(make_request({}), db)
        assert "do not have access" in str(caught.value)

    def test_a_deactivated_member_is_refused(self, db, two_tenants, monkeypatch):
        acme, _ = two_tenants
        member = db.query(TenantMember).filter_by(tenant_id=acme.id).one()
        member.active = False
        db.commit()
        monkeypatch.setattr(
            "app.auth.deps._claims_from_request",
            lambda _r: claims("dir-acme", "user-a"),
        )
        with pytest.raises(TenantResolutionError):
            get_tenant_context(make_request({}), db)

    def test_a_suspended_tenant_is_refused(self, db, two_tenants, monkeypatch):
        acme, _ = two_tenants
        acme.status = "suspended"
        db.commit()
        monkeypatch.setattr(
            "app.auth.deps._claims_from_request",
            lambda _r: claims("dir-acme", "user-a"),
        )
        with pytest.raises(TenantResolutionError) as caught:
            get_tenant_context(make_request({}), db)
        assert "suspended" in str(caught.value)

    def test_each_directory_resolves_to_its_own_catalog(self, db, two_tenants, monkeypatch):
        for directory, subject, expected in (
            ("dir-acme", "user-a", "assarium_dev_acme"),
            ("dir-globex", "user-g", "assarium_dev_globex"),
        ):
            monkeypatch.setattr(
                "app.auth.deps._claims_from_request",
                lambda _r, d=directory, s=subject: claims(d, s),
            )
            assert get_tenant_context(make_request({}), db).catalog == expected


class TestNoUnscopedDataPath:
    def test_asking_for_the_tenant_without_one_raises(self):
        token = set_current(None)
        try:
            with pytest.raises(TenantResolutionError):
                current()
        finally:
            reset_current(token)

    def test_absence_is_reportable_without_raising(self):
        token = set_current(None)
        try:
            assert current_or_none() is None
        finally:
            reset_current(token)

    def test_every_table_name_is_inside_the_tenant_catalog(self):
        context = TenantContext(
            tenant_id="t1", slug="acme", name="Acme", catalog="assarium_dev_acme",
            storage_container="acme", subject="s", email=None, role="admin",
            permissions=frozenset(),
        )
        assert context.qualified("silver", "orders") == "assarium_dev_acme.silver.orders"

    def test_context_cannot_be_mutated_to_widen_access(self):
        context = TenantContext(
            tenant_id="t1", slug="acme", name="Acme", catalog="assarium_dev_acme",
            storage_container="acme", subject="s", email=None, role="viewer",
            permissions=frozenset(ROLE_PERMISSIONS["viewer"]),
        )
        # A frozen dataclass raises FrozenInstanceError, not a generic Exception.
        with pytest.raises(dataclasses.FrozenInstanceError):
            context.catalog = "assarium_dev_globex"  # type: ignore[misc]


class TestPermissions:
    def test_a_viewer_cannot_write(self):
        context = TenantContext(
            tenant_id="t", slug="s", name="n", catalog="c", storage_container="sc",
            subject="u", email=None, role="viewer",
            permissions=frozenset(ROLE_PERMISSIONS["viewer"]),
        )
        with pytest.raises(PermissionDenied):
            context.require("connection:write")
        context.require("dataset:read")

    def test_only_an_owner_manages_members(self):
        for role in ("admin", "analyst", "viewer"):
            assert "member:manage" not in ROLE_PERMISSIONS[role]
        assert "member:manage" in ROLE_PERMISSIONS["owner"]

    def test_every_role_can_at_least_read_its_tenant(self):
        for role, permissions in ROLE_PERMISSIONS.items():
            assert "tenant:read" in permissions, role


class TestSlugSafety:
    """The slug becomes a catalog name and a storage container, so it goes into DDL."""

    @pytest.mark.parametrize("bad", [
        "Acme",                 # uppercase
        "1acme",                # leading digit
        "a",                    # too short
        "acme-corp",            # hyphen is not valid in a UC identifier
        "acme; DROP TABLE x",   # injection attempt
        "acme catalog",         # space
        "",                     # empty
        "a" * 40,               # too long
    ])
    def test_unsafe_slugs_are_rejected(self, bad):
        with pytest.raises(ValueError):
            validate_slug(bad)

    @pytest.mark.parametrize("good", ["acme", "acme_reit", "globex_properties_2"])
    def test_safe_slugs_are_accepted(self, good):
        assert validate_slug(good) == good


class TestProductionGuards:
    """A misconfigured deployment must fail at boot, not serve traffic unsafely."""

    def test_local_auth_is_refused_in_production(self):
        settings = Settings(
            environment="production", auth_mode="local", engine="databricks",
            secrets_backend="keyvault", key_vault_url="https://kv", secret_key="k",
            entra_audience="api://x",
        )
        with pytest.raises(ConfigurationError) as caught:
            settings.enforce_production_guards()
        assert "ASSARIUM_AUTH_MODE" in str(caught.value)

    def test_duckdb_is_refused_in_production(self):
        settings = Settings(
            environment="production", auth_mode="entra", entra_audience="api://x",
            engine="duckdb", secrets_backend="keyvault", key_vault_url="https://kv",
            secret_key="k",
        )
        with pytest.raises(ConfigurationError) as caught:
            settings.enforce_production_guards()
        assert "no tenant isolation" in str(caught.value)

    def test_local_secrets_backend_is_refused_in_production(self):
        settings = Settings(
            environment="production", auth_mode="entra", entra_audience="api://x",
            engine="databricks", secrets_backend="local", secret_key="k",
        )
        with pytest.raises(ConfigurationError) as caught:
            settings.enforce_production_guards()
        assert "SECRETS_BACKEND" in str(caught.value)

    def test_a_correct_production_configuration_starts(self):
        settings = Settings(
            environment="production", auth_mode="entra", entra_audience="api://assarium",
            engine="databricks", secrets_backend="keyvault",
            key_vault_url="https://kv.vault.azure.net", secret_key="a-real-key",
        )
        settings.enforce_production_guards()

    def test_local_development_is_unaffected(self):
        Settings(environment="local").enforce_production_guards()
