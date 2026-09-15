"""
Cross-tenant isolation, tested through the API rather than through the resolver.

This is the test the audit said was missing. The tenancy module was fully built and had
29 passing tests, and every one of them called `get_tenant_context()` directly - so they
proved the lock worked, not that it was on the door. It was not: 45 endpoints served data
with no authentication at all.

So these tests do the only thing that actually settles the question. Sign in as one
tenant, ask for another tenant's objects by id through the real HTTP surface, and assert
nothing comes back.

Two rules are asserted throughout:

- **Another tenant's row is 404, never 403.** A 403 confirms the row exists, which turns
  every id field into an oracle for enumerating other customers' objects.
- **No listing may contain a foreign row**, whatever filter is applied to it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.guard import PUBLIC_PATHS, iter_api_routes
from app.auth.deps import get_tenant_context
from app.db.session import get_db
from app.models.base import Base
from app.models.entities import (
    Connection,
    DashboardRecord,
    Dataset,
    DatasetRelationship,
    PipelineRun,
    SemanticModelRecord,
)
from app.tenancy.context import TenantContext
from app.tenancy.models import ROLE_PERMISSIONS

ALPHA = "tenant-alpha"
BETA = "tenant-beta"


def context_for(tenant_id: str, slug: str) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        slug=slug,
        name=slug.title(),
        catalog=f"assarium_test_{slug}",
        storage_container=f"tenant-{slug}",
        subject=f"user-{slug}",
        email=f"user@{slug}.example",
        role="owner",
        permissions=frozenset(ROLE_PERMISSIONS["owner"]),
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A live app over a throwaway database, with a switchable caller."""
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "secret_key", "test-key-for-tenant-isolation-suite")

    engine = create_engine(
        f"sqlite:///{tmp_path}/iso.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Sessions = sessionmaker(bind=engine, expire_on_commit=False)

    import app.main as main_module

    current = {"context": context_for(ALPHA, "alpha")}

    def override_db():
        session = Sessions()
        try:
            yield session
        finally:
            session.close()

    main_module.app.dependency_overrides[get_db] = override_db
    main_module.app.dependency_overrides[get_tenant_context] = lambda: current["context"]

    class Env:
        client = TestClient(main_module.app, raise_server_exceptions=False)
        sessions = Sessions

        @staticmethod
        def as_tenant(tenant_id: str, slug: str) -> None:
            current["context"] = context_for(tenant_id, slug)

        @staticmethod
        def seed(model, **values):
            """Insert a row directly, so a test does not depend on another endpoint."""
            with Sessions() as session:
                row = model(**values)
                session.add(row)
                session.commit()
                session.refresh(row)
                return row

    yield Env()

    main_module.app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture
def alpha_connection(env):
    return env.seed(
        Connection,
        tenant_id=ALPHA,
        name="Alpha warehouse",
        source_id="files",
        config={"upload_id": "alpha-uploads"},
        secret_refs={},
    )


def as_beta(env):
    env.as_tenant(BETA, "beta")


# =======================================================================================
# The structural guarantee
# =======================================================================================


class TestEveryRouteIsGuarded:
    def test_no_route_serves_data_without_a_tenant(self):
        """
        The check that runs at startup, asserted here too so a regression fails in CI
        rather than at deploy time.
        """
        import app.main as main_module

        unguarded = []
        for route in iter_api_routes(main_module.app):
            if route.path in PUBLIC_PATHS:
                continue
            names = set()
            stack = list(route.dependant.dependencies)
            while stack:
                dependency = stack.pop()
                call = getattr(dependency, "call", None)
                if call is not None:
                    names.add(getattr(call, "__name__", ""))
                stack.extend(dependency.dependencies)
            if not ({"get_tenant_context", "get_scope"} & names):
                unguarded.append(f"{sorted(route.methods)} {route.path}")
        assert unguarded == []

    def test_the_application_actually_has_routes_to_check(self):
        """
        The first version of this guard passed by inspecting an empty list, because
        this FastAPI includes routers lazily. A count nobody asserts is a count that can
        quietly become zero.
        """
        import app.main as main_module

        assert len(iter_api_routes(main_module.app)) >= 30


# =======================================================================================
# Connections
# =======================================================================================


class TestConnectionIsolation:
    def test_another_tenants_connection_is_not_readable(self, env, alpha_connection):
        as_beta(env)
        assert env.client.get(f"/api/connections/{alpha_connection.id}").status_code == 404

    def test_the_refusal_is_404_and_not_403(self, env, alpha_connection):
        """
        403 would confirm the row exists. That is enough to enumerate another customer's
        objects one id at a time, which is exactly what an id is for.
        """
        as_beta(env)
        response = env.client.get(f"/api/connections/{alpha_connection.id}")
        assert response.status_code == 404
        assert response.status_code != 403

    def test_a_foreign_row_and_a_missing_row_are_indistinguishable(self, env, alpha_connection):
        as_beta(env)
        foreign = env.client.get(f"/api/connections/{alpha_connection.id}")
        missing = env.client.get("/api/connections/does-not-exist-at-all")
        assert foreign.status_code == missing.status_code
        assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]

    def test_listing_excludes_other_tenants(self, env, alpha_connection):
        as_beta(env)
        assert env.client.get("/api/connections").json() == []

    def test_the_owner_still_sees_their_own(self, env, alpha_connection):
        listed = env.client.get("/api/connections").json()
        assert [c["id"] for c in listed] == [alpha_connection.id]

    def test_another_tenant_cannot_delete_it(self, env, alpha_connection):
        as_beta(env)
        assert env.client.delete(f"/api/connections/{alpha_connection.id}").status_code == 404
        with env.sessions() as session:
            assert session.get(Connection, alpha_connection.id) is not None

    def test_another_tenant_cannot_browse_it(self, env, alpha_connection):
        as_beta(env)
        assert env.client.get(f"/api/connections/{alpha_connection.id}/browse").status_code == 404

    def test_another_tenant_cannot_sample_from_it(self, env, alpha_connection):
        as_beta(env)
        response = env.client.get(
            f"/api/connections/{alpha_connection.id}/sample", params={"path": ["x"]}
        )
        assert response.status_code == 404

    def test_another_tenant_cannot_upload_into_it(self, env, alpha_connection):
        as_beta(env)
        response = env.client.post(
            f"/api/connections/{alpha_connection.id}/upload",
            files={"files": ("x.csv", b"a,b\n1,2\n", "text/csv")},
        )
        assert response.status_code == 404

    def test_another_tenant_cannot_retest_it(self, env, alpha_connection):
        as_beta(env)
        assert env.client.post(f"/api/connections/{alpha_connection.id}/test").status_code == 404

    def test_stored_secrets_cannot_be_borrowed_by_id(self, env):
        """
        `connection_id` on a test request merges that connection's stored secrets into
        the attempt. Unscoped, that is a way to use another tenant's credentials without
        ever seeing them.
        """
        env.seed(
            Connection, tenant_id=ALPHA, name="With secrets", source_id="postgres",
            config={"host": "alpha-db"}, secret_refs={"password": "source-x-password"},
        )
        with env.sessions() as session:
            alpha = session.query(Connection).filter_by(name="With secrets").one()

        as_beta(env)
        response = env.client.post(
            "/api/connections/test",
            json={"source_id": "postgres", "values": {"host": "evil"},
                  "connection_id": alpha.id},
        )
        # Whatever the connector then does, it must not have been handed alpha's secret.
        assert "source-x-password" not in response.text

    def test_both_tenants_may_use_the_same_connection_name(self, env):
        """
        Uniqueness belongs inside a tenant. Globally unique names leak the existence of
        other customers' connections and block the obvious names for everybody else.
        """
        env.seed(Connection, tenant_id=ALPHA, name="Salesforce", source_id="files",
                 config={"upload_id": "a"}, secret_refs={})
        as_beta(env)
        created = env.client.post(
            "/api/connections",
            json={"name": "Salesforce", "source_id": "files",
                  "values": {"label": "Beta uploads"}},
        )
        assert created.status_code == 201, created.text


# =======================================================================================
# Everything hanging off a connection
# =======================================================================================


class TestChildObjectIsolation:
    def test_datasets_are_not_listed_across_tenants(self, env, alpha_connection):
        env.seed(Dataset, tenant_id=ALPHA, connection_id=alpha_connection.id,
                 name="leases", path=["leases"], path_key="leases")
        as_beta(env)
        assert env.client.get("/api/datasets").json() == []

    def test_a_dataset_is_not_readable_by_id(self, env, alpha_connection):
        dataset = env.seed(Dataset, tenant_id=ALPHA, connection_id=alpha_connection.id,
                           name="leases", path=["leases"], path_key="leases")
        as_beta(env)
        assert env.client.get(f"/api/datasets/{dataset.id}").status_code == 404

    def test_a_dataset_cannot_be_deleted_by_another_tenant(self, env, alpha_connection):
        dataset = env.seed(Dataset, tenant_id=ALPHA, connection_id=alpha_connection.id,
                           name="leases", path=["leases"], path_key="leases")
        as_beta(env)
        assert env.client.delete(f"/api/datasets/{dataset.id}").status_code == 404
        with env.sessions() as session:
            assert session.get(Dataset, dataset.id) is not None

    def test_a_layer_override_cannot_be_applied_across_tenants(self, env, alpha_connection):
        """Writes matter more than reads: this one would change another tenant's pipeline."""
        dataset = env.seed(Dataset, tenant_id=ALPHA, connection_id=alpha_connection.id,
                           name="leases", path=["leases"], path_key="leases",
                           detected_layer="bronze")
        as_beta(env)
        response = env.client.patch(
            f"/api/datasets/{dataset.id}", json={"layer": "gold"}
        )
        assert response.status_code == 404
        with env.sessions() as session:
            assert session.get(Dataset, dataset.id).layer_override is None

    def test_relationships_are_not_listed_across_tenants(self, env, alpha_connection):
        as_beta(env)
        response = env.client.get(f"/api/connections/{alpha_connection.id}/relationships")
        assert response.status_code in (404, 200)
        if response.status_code == 200:
            assert response.json() == []

    def test_a_dashboard_is_not_readable_by_id(self, env, alpha_connection):
        dashboard = env.seed(
            DashboardRecord, tenant_id=ALPHA, connection_id=alpha_connection.id,
            name="Portfolio", document={"id": "d1", "name": "Portfolio", "tiles": []},
        )
        as_beta(env)
        assert env.client.get(f"/api/dashboards/{dashboard.id}").status_code == 404

    def test_a_dashboard_cannot_be_overwritten_across_tenants(self, env, alpha_connection):
        dashboard = env.seed(
            DashboardRecord, tenant_id=ALPHA, connection_id=alpha_connection.id,
            name="Portfolio", document={"id": "d1", "name": "Portfolio", "tiles": []},
        )
        as_beta(env)
        response = env.client.put(
            f"/api/dashboards/{dashboard.id}",
            json={"id": dashboard.id, "connection_id": alpha_connection.id,
                  "name": "Owned", "tiles": []},
        )
        assert response.status_code == 404
        with env.sessions() as session:
            assert session.get(DashboardRecord, dashboard.id).name == "Portfolio"

    def test_a_dashboard_cannot_be_deleted_across_tenants(self, env, alpha_connection):
        dashboard = env.seed(
            DashboardRecord, tenant_id=ALPHA, connection_id=alpha_connection.id,
            name="Portfolio", document={"id": "d1", "name": "Portfolio", "tiles": []},
        )
        as_beta(env)
        assert env.client.delete(f"/api/dashboards/{dashboard.id}").status_code == 404

    def test_a_semantic_model_is_not_readable_across_tenants(self, env, alpha_connection):
        env.seed(SemanticModelRecord, tenant_id=ALPHA, connection_id=alpha_connection.id,
                 document={"connection_id": alpha_connection.id, "entities": []})
        as_beta(env)
        response = env.client.get(f"/api/connections/{alpha_connection.id}/semantic")
        assert response.status_code == 404

    def test_a_semantic_model_cannot_be_replaced_across_tenants(self, env, alpha_connection):
        """A rewritten model changes what every number on their dashboards means."""
        env.seed(SemanticModelRecord, tenant_id=ALPHA, connection_id=alpha_connection.id,
                 document={"connection_id": alpha_connection.id, "entities": []})
        as_beta(env)
        response = env.client.put(
            f"/api/connections/{alpha_connection.id}/semantic",
            json={"connection_id": alpha_connection.id, "entities": [], "measures": [],
                  "joins": [], "notes": []},
        )
        assert response.status_code == 404

    def test_a_metric_query_cannot_be_run_against_another_tenant(self, env, alpha_connection):
        as_beta(env)
        response = env.client.post(
            f"/api/connections/{alpha_connection.id}/semantic/query",
            json={"measures": ["x.count"], "dimensions": []},
        )
        assert response.status_code == 404

    def test_pipeline_runs_are_not_listed_across_tenants(self, env, alpha_connection):
        as_beta(env)
        response = env.client.get(f"/api/connections/{alpha_connection.id}/runs")
        assert response.status_code == 404

    def test_a_run_is_not_readable_by_id(self, env, alpha_connection):
        from app.models.base import utcnow

        run = env.seed(PipelineRun, tenant_id=ALPHA, connection_id=alpha_connection.id,
                       engine="duckdb", status="succeeded", started_at=utcnow())
        as_beta(env)
        assert env.client.get(f"/api/runs/{run.id}").status_code == 404

    def test_a_pipeline_cannot_be_started_on_another_tenant(
        self, env, alpha_connection
    ):
        as_beta(env)
        response = env.client.post(
            f"/api/connections/{alpha_connection.id}/runs", json={"dataset_ids": None}
        )
        assert response.status_code == 404

    def test_a_schedule_cannot_be_created_on_another_tenant(
        self, env, alpha_connection
    ):
        as_beta(env)
        response = env.client.post(
            f"/api/connections/{alpha_connection.id}/schedule",
            json={"cron": "0 2 * * *", "timezone": "UTC", "enabled": True},
        )
        assert response.status_code == 404

    def test_schedules_are_not_listed_across_tenants(self, env, alpha_connection):
        from app.orchestration.models import Schedule

        env.seed(Schedule, tenant_id=ALPHA, connection_id=alpha_connection.id,
                 cron="0 2 * * *")
        as_beta(env)
        listed = env.client.get("/api/schedules").json()
        assert listed.get("schedules", []) == []

    def test_the_chat_endpoint_cannot_target_another_tenant(self, env, alpha_connection):
        as_beta(env)
        response = env.client.post(
            f"/api/connections/{alpha_connection.id}/chat",
            json={"messages": [{"role": "user", "content": "show me their revenue"}]},
        )
        assert response.status_code == 404


# =======================================================================================
# The scope helper's own guarantees
# =======================================================================================


class TestScopeHelper:
    def test_create_refuses_a_caller_supplied_tenant(self, env):
        """
        Silently overwriting it would hide the misunderstanding. A caller that thinks it
        knows the tenant has got tenancy backwards.
        """
        from app.tenancy.scope import TenantScope

        with env.sessions() as session:
            scope = TenantScope(session, context_for(ALPHA, "alpha"))
            with pytest.raises(ValueError, match="tenant_id is set by TenantScope"):
                scope.create(Connection, tenant_id=BETA, name="x", source_id="files")

    def test_create_stamps_the_session_tenant(self, env):
        from app.tenancy.scope import TenantScope

        with env.sessions() as session:
            scope = TenantScope(session, context_for(ALPHA, "alpha"))
            row = scope.create(Connection, name="x", source_id="files", config={})
            assert row.tenant_id == ALPHA

    def test_delete_refuses_a_foreign_row(self, env, alpha_connection):
        from app.core.errors import NotFoundError
        from app.tenancy.scope import TenantScope

        with env.sessions() as session:
            scope = TenantScope(session, context_for(BETA, "beta"))
            row = session.get(Connection, alpha_connection.id)
            with pytest.raises(NotFoundError):
                scope.delete(row)

    def test_get_with_no_id_is_a_clean_404(self, env):
        from app.core.errors import NotFoundError
        from app.tenancy.scope import TenantScope

        with env.sessions() as session:
            scope = TenantScope(session, context_for(ALPHA, "alpha"))
            with pytest.raises(NotFoundError):
                scope.get(Connection, None)

    def test_the_engine_is_keyed_by_tenant(self, env):
        """Two tenants must never be handed the same warehouse handle."""
        from app.engine.factory import close_all, get_engine

        close_all()
        try:
            assert get_engine(ALPHA) is not get_engine(BETA)
            assert get_engine(ALPHA) is get_engine(ALPHA)
        finally:
            close_all()


# =======================================================================================
# Every tenant-owned table really is owned
# =======================================================================================


class TestSchema:
    @pytest.mark.parametrize("model", [
        Connection, Dataset, DatasetRelationship, PipelineRun,
        SemanticModelRecord, DashboardRecord,
    ])
    def test_the_column_exists_and_cannot_be_null(self, model):
        column = model.__table__.columns["tenant_id"]
        assert column.nullable is False

    @pytest.mark.parametrize("model", [
        Connection, Dataset, DatasetRelationship, PipelineRun,
        SemanticModelRecord, DashboardRecord,
    ])
    def test_there_is_no_default_tenant(self, model):
        """
        An empty-string default is a real value that matches other empty strings, so
        every row created without an owner would collapse into one shared pseudo-tenant
        that a later filter would happily hand out.
        """
        column = model.__table__.columns["tenant_id"]
        assert column.default is None and column.server_default is None


# =======================================================================================
# The happy path
# =======================================================================================


class TestOwnTenantStillWorks:
    """
    Isolation tests alone are not enough.

    Every test above asserts a 404 for the wrong tenant - and a handler that raises before
    it reaches its own logic returns 404 for everyone, which passes all of them. Exactly
    that happened during the scoping refactor: three helpers ended up receiving a Session
    where a TenantScope belonged, and the isolation tests were green throughout because
    they never got far enough to notice.

    So these assert the other half: for its own tenant, each endpoint answers.
    """

    def test_reading_your_own_connection_works(self, env, alpha_connection):
        response = env.client.get(f"/api/connections/{alpha_connection.id}")
        assert response.status_code == 200
        assert response.json()["name"] == "Alpha warehouse"

    def test_reading_your_own_run_works(self, env, alpha_connection):
        """The endpoint that broke: it loads its steps through a helper."""
        from app.models.base import utcnow

        run = env.seed(PipelineRun, tenant_id=ALPHA, connection_id=alpha_connection.id,
                       engine="duckdb", status="succeeded", started_at=utcnow())
        response = env.client.get(f"/api/runs/{run.id}")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "succeeded"

    def test_listing_your_own_runs_works(self, env, alpha_connection):
        response = env.client.get(f"/api/connections/{alpha_connection.id}/runs")
        assert response.status_code == 200
        assert response.json() == []

    def test_reading_your_own_dataset_works(self, env, alpha_connection):
        dataset = env.seed(Dataset, tenant_id=ALPHA, connection_id=alpha_connection.id,
                           name="leases", path=["leases"], path_key="leases")
        response = env.client.get(f"/api/datasets/{dataset.id}")
        assert response.status_code == 200, response.text
        assert response.json()["name"] == "leases"

    def test_listing_your_own_datasets_works(self, env, alpha_connection):
        env.seed(Dataset, tenant_id=ALPHA, connection_id=alpha_connection.id,
                 name="leases", path=["leases"], path_key="leases")
        response = env.client.get("/api/datasets")
        assert response.status_code == 200
        assert len(response.json()) == 1

    def test_reading_your_own_dashboard_works(self, env, alpha_connection):
        dashboard = env.seed(
            DashboardRecord, tenant_id=ALPHA, connection_id=alpha_connection.id,
            name="Portfolio",
            document={"id": "d1", "connection_id": alpha_connection.id,
                      "name": "Portfolio", "tiles": []},
        )
        response = env.client.get(f"/api/dashboards/{dashboard.id}")
        assert response.status_code == 200, response.text
        assert response.json()["name"] == "Portfolio"

    def test_listing_your_own_dashboards_works(self, env, alpha_connection):
        env.seed(DashboardRecord, tenant_id=ALPHA, connection_id=alpha_connection.id,
                 name="Portfolio",
                 document={"id": "d1", "connection_id": alpha_connection.id,
                           "name": "Portfolio", "tiles": []})
        response = env.client.get(
            f"/api/connections/{alpha_connection.id}/dashboards"
        )
        assert response.status_code == 200
        assert len(response.json()) == 1

    def test_reading_your_own_semantic_model_works(self, env, alpha_connection):
        env.seed(SemanticModelRecord, tenant_id=ALPHA,
                 connection_id=alpha_connection.id,
                 document={"connection_id": alpha_connection.id, "entities": [],
                           "measures": [], "joins": [], "notes": []})
        response = env.client.get(f"/api/connections/{alpha_connection.id}/semantic")
        assert response.status_code == 200, response.text

    def test_listing_your_own_schedules_works(self, env, alpha_connection):
        from app.orchestration.models import Schedule

        env.seed(Schedule, tenant_id=ALPHA, connection_id=alpha_connection.id,
                 cron="0 2 * * *")
        response = env.client.get("/api/schedules")
        assert response.status_code == 200, response.text
        assert len(response.json()["schedules"]) == 1

    def test_creating_a_connection_works(self, env):
        response = env.client.post(
            "/api/connections",
            json={"name": "New uploads", "source_id": "files",
                  "values": {"label": "New uploads"}},
        )
        assert response.status_code == 201, response.text
        assert response.json()["dataset_count"] == 0

    def test_a_scope_refuses_a_session_in_its_place(self, env):
        """
        The guard for the mistake above. A Session has the same `.get(Model, id)`
        signature as a scope, so substituting one runs happily and looks up rows with no
        tenant filter.
        """
        from app.tenancy.scope import TenantScope

        with env.sessions() as session:
            with pytest.raises(TypeError, match="needs a TenantScope"):
                TenantScope.assert_is_scope(session, "_steps")
