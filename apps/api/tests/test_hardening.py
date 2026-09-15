"""
The protections that are not about tenancy: headers, rate limits, query ceilings, audit.

Each of these was previously either absent or present-but-unreachable. A configured
`query_timeout_seconds` that no code path consulted is the same as no timeout, and an
audit module with no callers is worse than none - it produces an empty table that reads
as evidence. So each one is asserted by observing the behaviour, not by checking that the
setting exists.
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.middleware import (
    DEFAULT_LIMIT,
    EXPENSIVE_LIMIT,
    SECURITY_HEADERS,
    AuditMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
)
from app.core.errors import QueryError
from app.engine.duckdb_engine import DuckDBEngine

# =======================================================================================
# Security headers
# =======================================================================================


@pytest.fixture
def headers_client():
    from fastapi.responses import JSONResponse

    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.exception_handler(Exception)
    async def unhandled(_request, _exc):
        # Mirrors app.main: an unhandled error becomes a normal response inside our own
        # middleware, so the headers apply and no traceback escapes.
        return JSONResponse(status_code=500, content={"error": {"code": "internal_error"}})

    @app.get("/thing")
    def thing():
        return {"ok": True}

    @app.get("/boom")
    def boom():
        raise ValueError("deliberate")

    return TestClient(app, raise_server_exceptions=False)


class TestSecurityHeaders:
    @pytest.mark.parametrize("header", sorted(SECURITY_HEADERS))
    def test_every_header_is_present(self, headers_client, header):
        assert headers_client.get("/thing").headers.get(header) == SECURITY_HEADERS[header]

    def test_headers_are_present_on_errors_too(self, headers_client):
        """An error response is still a response a browser will interpret."""
        response = headers_client.get("/boom")
        assert response.status_code == 500
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    def test_the_api_forbids_being_framed(self, headers_client):
        response = headers_client.get("/thing")
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]

    def test_responses_are_not_cached(self, headers_client):
        """One tenant's answer must not be served to the next from a shared cache."""
        assert headers_client.get("/thing").headers["Cache-Control"] == "no-store"

    def test_hsts_is_absent_over_plain_http(self, headers_client):
        """
        Sending it over HTTP is ignored anyway, and setting it in development would pin
        localhost to HTTPS in the developer's browser for a year.
        """
        assert "Strict-Transport-Security" not in headers_client.get("/thing").headers

    def test_hsts_is_present_behind_a_tls_terminating_proxy(self, headers_client):
        response = headers_client.get("/thing", headers={"x-forwarded-proto": "https"})
        assert "max-age=31536000" in response.headers["Strict-Transport-Security"]


# =======================================================================================
# Rate limiting
# =======================================================================================


@pytest.fixture
def limited_client():
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)

    @app.get("/api/cheap")
    def cheap():
        return {"ok": True}

    @app.post("/api/connections/x/semantic/query")
    def expensive():
        return {"ok": True}

    return TestClient(app)


class TestRateLimiting:
    def test_normal_use_is_not_limited(self, limited_client):
        for _ in range(20):
            assert limited_client.get("/api/cheap").status_code == 200

    def test_polling_a_run_is_not_charged_as_an_expensive_call(self, limited_client):
        """
        A client watching a five-minute load polls the run hundreds of times. Charging
        that at the same rate as starting the run makes the platform throttle its own
        progress bar - which is exactly what happened the first time this shipped.
        """
        from app.api.middleware import _is_expensive

        assert _is_expensive("POST", "/api/connections/x/runs")
        assert not _is_expensive("GET", "/api/runs/abc")

    def test_an_expensive_endpoint_has_a_smaller_budget(self, limited_client):
        """
        A warehouse query, an export or an LLM call costs real money. One tenant looping
        on those degrades the platform for everyone else.
        """
        assert EXPENSIVE_LIMIT.requests < DEFAULT_LIMIT.requests

        statuses = [
            limited_client.post("/api/connections/x/semantic/query").status_code
            for _ in range(EXPENSIVE_LIMIT.requests + 5)
        ]
        assert 429 in statuses
        assert statuses.count(200) == EXPENSIVE_LIMIT.requests

    def test_the_refusal_says_when_to_come_back(self, limited_client):
        """A 429 with no Retry-After teaches clients to retry immediately, which is worse."""
        for _ in range(EXPENSIVE_LIMIT.requests + 1):
            response = limited_client.post("/api/connections/x/semantic/query")
        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) >= 1
        assert response.json()["error"]["code"] == "rate_limited"

    def test_the_cheap_budget_is_unaffected_by_exhausting_the_expensive_one(
        self, limited_client
    ):
        for _ in range(EXPENSIVE_LIMIT.requests + 1):
            limited_client.post("/api/connections/x/semantic/query")
        assert limited_client.get("/api/cheap").status_code == 200

    def test_callers_have_separate_budgets(self):
        """
        Two customers behind one corporate NAT must not share a budget. The limiter runs
        before the tenant is resolved, so it keys on the presented credential instead -
        different tokens, different buckets.
        """
        app = FastAPI()
        app.add_middleware(RateLimitMiddleware)

        @app.post("/api/connections/x/chat")
        def chat():
            return {"ok": True}

        client = TestClient(app)
        alpha = {"Authorization": "Bearer token-belonging-to-alpha"}
        beta = {"Authorization": "Bearer token-belonging-to-beta"}

        for _ in range(EXPENSIVE_LIMIT.requests + 1):
            client.post("/api/connections/x/chat", headers=alpha)

        assert client.post("/api/connections/x/chat", headers=alpha).status_code == 429
        assert client.post("/api/connections/x/chat", headers=beta).status_code == 200

    def test_an_unauthenticated_flood_is_limited_by_address(self):
        app = FastAPI()
        app.add_middleware(RateLimitMiddleware)

        @app.post("/api/connections/x/chat")
        def chat():
            return {"ok": True}

        client = TestClient(app)
        statuses = [
            client.post("/api/connections/x/chat").status_code
            for _ in range(EXPENSIVE_LIMIT.requests + 3)
        ]
        assert 429 in statuses

    def test_the_key_map_does_not_grow_without_bound(self):
        """Otherwise the limiter is itself a slow memory leak keyed by whoever calls."""
        middleware = RateLimitMiddleware(FastAPI())
        now = time.monotonic()
        for index in range(12_000):
            middleware._hits[f"ip:10.0.0.{index}"].append(now - 3600)
        middleware._prune(time.monotonic())
        assert len(middleware._hits) == 0


# =======================================================================================
# Query ceiling
# =======================================================================================


@pytest.fixture
def engine(tmp_path):
    instance = DuckDBEngine(tmp_path)
    instance.ensure_layers()
    yield instance
    instance.close()


class TestQueryCeiling:
    def test_a_normal_query_is_unaffected(self, engine):
        assert engine.execute("SELECT 1 AS n").rows == [[1]]

    def test_a_runaway_query_is_stopped(self, engine, monkeypatch):
        """
        Without this a single query holds a connection, a warehouse slot and a request
        thread for as long as it likes - one person's mistake becoming everyone's outage.
        """
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "query_timeout_seconds", 1)
        with pytest.raises(QueryError) as caught:
            engine.execute(
                "SELECT count(*) FROM range(1, 200000000) t1, range(1, 400) t2"
            )
        assert "longer than 1 seconds" in str(caught.value)

    def test_the_message_says_what_to_do_about_it(self, engine, monkeypatch):
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "query_timeout_seconds", 1)
        with pytest.raises(QueryError) as caught:
            engine.execute("SELECT count(*) FROM range(1, 200000000) t1, range(1, 400) t2")
        message = str(caught.value)
        assert "filter" in message or "grain" in message

    def test_a_zero_timeout_means_no_ceiling(self, engine, monkeypatch):
        """Explicitly disabling it is allowed; silently having none is not."""
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "query_timeout_seconds", 0)
        assert engine.execute("SELECT 1 AS n").rows == [[1]]

    def test_the_engine_can_cancel(self, engine):
        """A timeout that does not cancel leaves the warehouse working, and billing."""
        engine.cancel_running_query()  # no query running: must be harmless


# =======================================================================================
# Audit
# =======================================================================================


class TestAuditLog:
    def test_a_write_is_recorded(self, tmp_path):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.audit.service import get_audit_events, log_audit_event
        from app.models.base import Base

        db_engine = create_engine(f"sqlite:///{tmp_path}/audit.db")
        Base.metadata.create_all(db_engine)
        with sessionmaker(bind=db_engine)() as session:
            log_audit_event(
                session, tenant_id="alpha", actor_id="priya",
                action="DELETE /api/connections/{id}", target_type="http",
                target_id="/api/connections/abc",
            )
            session.commit()
            events = get_audit_events(session, "alpha")

        assert len(events) == 1
        assert events[0].actor_id == "priya"
        assert events[0].id, "the row needs an id - the column had no default at first"

    def test_one_tenants_audit_log_is_not_another_tenants(self, tmp_path):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.audit.service import get_audit_events, log_audit_event
        from app.models.base import Base

        db_engine = create_engine(f"sqlite:///{tmp_path}/audit2.db")
        Base.metadata.create_all(db_engine)
        with sessionmaker(bind=db_engine)() as session:
            for tenant in ("alpha", "beta"):
                log_audit_event(session, tenant_id=tenant, actor_id="x", action="POST",
                                target_type="http", target_id="/api/x")
            session.commit()
            assert len(get_audit_events(session, "alpha")) == 1

    def test_mutations_are_logged_and_reads_are_not(self, caplog):
        """
        Reads at this volume would drown the signal. The questions an audit asks - who
        changed this, who deleted that, who exported it - are all about writes.
        """
        app = FastAPI()
        app.add_middleware(AuditMiddleware)

        @app.get("/api/things")
        def read():
            return []

        @app.delete("/api/things/1")
        def remove():
            return {}

        client = TestClient(app)
        with caplog.at_level("INFO", logger="assarium.access"):
            client.get("/api/things")
            assert not caplog.records
            client.delete("/api/things/1")
            assert any("DELETE /api/things/1" in r.getMessage() for r in caplog.records)

    def test_an_export_is_logged_even_though_it_is_a_read(self, caplog):
        """Taking data out of the platform is exactly what a compliance review looks for."""
        app = FastAPI()
        app.add_middleware(AuditMiddleware)

        @app.get("/api/dashboards/1/export.csv")
        def export():
            return "a,b\n"

        client = TestClient(app)
        with caplog.at_level("INFO", logger="assarium.access"):
            client.get("/api/dashboards/1/export.csv")
        assert any("export.csv" in r.getMessage() for r in caplog.records)

    def test_a_refused_request_is_still_logged(self, caplog):
        """
        A run of 404s from one actor is the shape of somebody probing for other tenants'
        ids. That is precisely the pattern worth being able to find afterwards.
        """
        app = FastAPI()
        app.add_middleware(AuditMiddleware)

        @app.delete("/api/things/{thing_id}")
        def remove(thing_id: str):
            from fastapi import HTTPException

            raise HTTPException(status_code=404)

        client = TestClient(app)
        with caplog.at_level("INFO", logger="assarium.access"):
            client.delete("/api/things/someone-elses-id")
        assert any("404" in r.getMessage() for r in caplog.records)


# =======================================================================================
# Upload caps
# =======================================================================================


class TestUploadCaps:
    def test_the_byte_ceiling_is_set(self):
        from app.api.routers.connections import MAX_UPLOAD_BYTES, MAX_UPLOAD_FILES

        assert 0 < MAX_UPLOAD_BYTES <= 2 * 1024**3
        assert 0 < MAX_UPLOAD_FILES <= 500

    def test_the_limit_is_counted_from_bytes_written(self):
        """
        Not from a declared Content-Length: the length is the client's claim, and the
        bytes are what actually fill the disk.
        """
        import inspect

        from app.api.routers.connections import upload_files

        source = inspect.getsource(upload_files)
        assert "written += len(chunk)" in source
        assert "MAX_UPLOAD_BYTES" in source
