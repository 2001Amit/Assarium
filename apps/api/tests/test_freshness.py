from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.errors import NotFoundError
from app.dashboards.freshness import (
    NO_SCHEDULE_REASON,
    FreshnessStatus,
    assess,
    dashboard_freshness,
)
from app.models.base import Base
from app.models.entities import Connection, DashboardRecord, PipelineRun
from app.orchestration.models import Schedule
from app.tenancy.context import TenantContext
from app.tenancy.models import ROLE_PERMISSIONS
from app.tenancy.scope import TenantScope


def make_schedule(
    *, cron: str = "0 * * * *", enabled: bool = True,
    last_status: str | None = "succeeded", consecutive_failures: int = 0,
) -> Schedule:
    return Schedule(
        cron=cron, enabled=enabled, last_status=last_status,
        consecutive_failures=consecutive_failures,
    )


def test_data_that_landed_this_morning_is_fresh():
    now = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)
    schedule = make_schedule(cron="0 2 * * *")
    result = assess(now, datetime(2026, 3, 10, 2, 5, tzinfo=UTC), schedule, now = now)
    assert result.status == "fresh"

def test_data_from_three_days_ago_is_stale():
    now = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)
    schedule = make_schedule(cron="0 2 * * *")
    result = assess(now, datetime(2026, 3, 7, 2, 5, tzinfo=UTC), schedule, now = now)
    assert result.status == "stale"


# =======================================================================================
# assess() - edge cases, each proving one thing
# =======================================================================================


NOW = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)
HOURLY = make_schedule(cron="0 * * * *")


class TestNoRunHasEverFinished:
    def test_a_missing_finished_at_is_unknown_not_fresh(self):
        """Absence must never be the permissive case: no run means `unknown`, not `fresh`."""
        result = assess(NOW, None, HOURLY, now=NOW)
        assert result.status == FreshnessStatus.unknown
        assert result.age is None

    def test_a_missing_finished_at_explains_itself(self):
        result = assess(NOW, None, HOURLY, now=NOW)
        assert result.reason == "No pipeline run has completed yet."


class TestNaiveTimestampsAreRejected:
    """Refuse rather than approximate: a naive timestamp's timezone is a guess, not a fact."""

    def test_a_naive_as_of_is_rejected(self):
        with pytest.raises(ValueError, match="as_of"):
            assess(
                datetime(2026, 3, 10, 9, 0), datetime(2026, 3, 10, 2, 5, tzinfo=UTC),
                HOURLY, now=NOW,
            )

    def test_a_naive_now_is_rejected(self):
        with pytest.raises(ValueError, match="now"):
            assess(
                NOW, datetime(2026, 3, 10, 2, 5, tzinfo=UTC), HOURLY,
                now=datetime(2026, 3, 10, 9, 0),
            )

    def test_a_naive_finished_at_is_rejected(self):
        with pytest.raises(ValueError, match="finished_at"):
            assess(NOW, datetime(2026, 3, 10, 2, 5), HOURLY, now=NOW)


class TestClockSkewIsRejected:
    """A wrong number that looks right is worse than no number - these refuse instead."""

    def test_as_of_after_now_is_rejected(self):
        with pytest.raises(ValueError, match="as_of is after now"):
            assess(NOW + timedelta(minutes=1), NOW, HOURLY, now=NOW)

    def test_a_run_finishing_after_now_is_rejected(self):
        with pytest.raises(ValueError, match="finished_at is after now"):
            assess(NOW, NOW + timedelta(minutes=1), HOURLY, now=NOW)


class TestTheCadenceDerivedBoundary:
    """
    The threshold comes from the schedule's own step size, not a constant: for the hourly
    schedule here that is 1.5h (late) and 2h (stale). Each boundary gets its own test
    rather than one test asserting both sides.
    """

    def test_well_within_the_interval_is_fresh(self):
        result = assess(NOW, NOW - timedelta(hours=1), HOURLY, now=NOW)
        assert result.status == FreshnessStatus.fresh

    def test_exactly_at_the_late_threshold_is_still_fresh(self):
        result = assess(NOW, NOW - timedelta(hours=1, minutes=30), HOURLY, now=NOW)
        assert result.status == FreshnessStatus.fresh

    def test_one_second_past_the_late_threshold_is_late(self):
        result = assess(
            NOW, NOW - timedelta(hours=1, minutes=30, seconds=1), HOURLY, now=NOW,
        )
        assert result.status == FreshnessStatus.late

    def test_exactly_at_the_stale_threshold_is_still_late(self):
        result = assess(NOW, NOW - timedelta(hours=2), HOURLY, now=NOW)
        assert result.status == FreshnessStatus.late

    def test_one_second_past_the_stale_threshold_is_stale(self):
        result = assess(NOW, NOW - timedelta(hours=2, seconds=1), HOURLY, now=NOW)
        assert result.status == FreshnessStatus.stale


class TestTheIntervalComesFromTheScheduleNotAConstant:
    """The bug report this change fixes: one fixed bar cannot fit every cadence."""

    def test_the_same_age_is_stale_on_an_hourly_schedule_and_fresh_on_a_daily_one(self):
        finished_at = NOW - timedelta(hours=5)
        hourly = assess(NOW, finished_at, make_schedule(cron="0 * * * *"), now=NOW)
        daily = assess(NOW, finished_at, make_schedule(cron="0 2 * * *"), now=NOW)
        assert hourly.status == FreshnessStatus.stale
        assert daily.status == FreshnessStatus.fresh


class TestTheReasonReflectsScheduleState:
    """`stale` alone gives a viewer nowhere to go, so every non-fresh result explains why."""

    STALE_AGE = NOW - timedelta(hours=5)  # well past the hourly schedule's 2h stale bar

    def test_fresh_data_carries_no_reason(self):
        result = assess(NOW, NOW - timedelta(hours=1), HOURLY, now=NOW)
        assert result.reason is None

    def test_a_disabled_schedule_names_itself_as_the_reason(self):
        schedule = make_schedule(enabled=False)
        result = assess(NOW, self.STALE_AGE, schedule, now=NOW)
        assert "disabled" in result.reason

    def test_repeated_failures_are_named_with_their_count(self):
        schedule = make_schedule(last_status="failed", consecutive_failures=3)
        result = assess(NOW, self.STALE_AGE, schedule, now=NOW)
        assert result.reason == "The last 3 runs failed."

    def test_a_single_failure_uses_singular_wording(self):
        schedule = make_schedule(last_status="failed", consecutive_failures=1)
        result = assess(NOW, self.STALE_AGE, schedule, now=NOW)
        assert result.reason == "The last 1 run failed."

    def test_a_healthy_but_slow_schedule_gets_a_generic_reason(self):
        schedule = make_schedule(last_status="succeeded", consecutive_failures=0)
        result = assess(NOW, self.STALE_AGE, schedule, now=NOW)
        assert "enabled and healthy" in result.reason

    def test_a_disabled_and_failing_schedule_leads_with_disabled(self):
        """Disabled is the more fundamental fact: retries would not help even if tried."""
        schedule = make_schedule(enabled=False, last_status="failed", consecutive_failures=2)
        result = assess(NOW, self.STALE_AGE, schedule, now=NOW)
        assert "disabled" in result.reason
        assert "failed" not in result.reason


# =======================================================================================
# dashboard_freshness() - the TenantScope-backed lookup
# =======================================================================================

ALPHA = "tenant-alpha"
BETA = "tenant-beta"


def context_for(tenant_id: str) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        slug=tenant_id,
        name=tenant_id.title(),
        catalog=f"assarium_test_{tenant_id}",
        storage_container=f"container-{tenant_id}",
        subject=f"user-{tenant_id}",
        email=f"user@{tenant_id}.example",
        role="owner",
        permissions=frozenset(ROLE_PERMISSIONS["owner"]),
    )


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/freshness.db")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    yield factory
    engine.dispose()


@pytest.fixture
def db(sessions):
    session = sessions()
    yield session
    session.close()


def scope_for(db, tenant_id: str) -> TenantScope:
    return TenantScope(db, context_for(tenant_id))


@pytest.fixture
def alpha_dashboard(db):
    connection = Connection(
        tenant_id=ALPHA, name="Warehouse", source_id="files", config={}, secret_refs={},
    )
    db.add(connection)
    db.commit()
    dashboard = DashboardRecord(
        tenant_id=ALPHA, connection_id=connection.id, name="Portfolio",
        document={"id": "d1", "name": "Portfolio", "tiles": []},
    )
    db.add(dashboard)
    db.commit()
    return dashboard


@pytest.fixture
def alpha_daily_schedule(db, alpha_dashboard):
    """A healthy, enabled, once-a-day schedule for the dashboard's connection."""
    schedule = Schedule(
        tenant_id=ALPHA, connection_id=alpha_dashboard.connection_id, cron="0 2 * * *",
        enabled=True, last_status="succeeded", consecutive_failures=0,
    )
    db.add(schedule)
    db.commit()
    return schedule


def _run(connection_id: str, *, status: str, started_at: datetime, finished_at: datetime | None):
    return PipelineRun(
        tenant_id=ALPHA, connection_id=connection_id, engine="duckdb", status=status,
        started_at=started_at, finished_at=finished_at,
    )


class TestDashboardFreshnessHasNoRuns:
    def test_a_dashboard_with_no_runs_at_all_is_unknown(
        self, db, alpha_dashboard, alpha_daily_schedule,
    ):
        scope = scope_for(db, ALPHA)
        result = dashboard_freshness(scope, alpha_dashboard.id, now=NOW)
        assert result.status == FreshnessStatus.unknown


class TestDashboardFreshnessHasNoSchedule:
    """No cadence to judge against is its own kind of `unknown`, not a guessed default."""

    def test_a_dashboard_whose_connection_has_no_schedule_is_unknown(
        self, db, alpha_dashboard,
    ):
        db.add(_run(alpha_dashboard.connection_id, status="succeeded",
                     started_at=NOW - timedelta(hours=2),
                     finished_at=NOW - timedelta(hours=1)))
        db.commit()
        scope = scope_for(db, ALPHA)
        result = dashboard_freshness(scope, alpha_dashboard.id, now=NOW)
        assert result.status == FreshnessStatus.unknown
        assert result.reason == NO_SCHEDULE_REASON


class TestDashboardFreshnessIgnoresRunsThatDidNotProduceData:
    def test_a_still_running_run_does_not_count(
        self, db, alpha_dashboard, alpha_daily_schedule,
    ):
        db.add(_run(alpha_dashboard.connection_id, status="running",
                     started_at=NOW - timedelta(hours=1), finished_at=None))
        db.commit()
        scope = scope_for(db, ALPHA)
        result = dashboard_freshness(scope, alpha_dashboard.id, now=NOW)
        assert result.status == FreshnessStatus.unknown

    def test_a_failed_run_does_not_count_even_though_it_finished(
        self, db, alpha_dashboard, alpha_daily_schedule,
    ):
        """A failed run has a `finished_at`, but produced no tables worth dating."""
        db.add(_run(alpha_dashboard.connection_id, status="failed",
                     started_at=NOW - timedelta(hours=2),
                     finished_at=NOW - timedelta(hours=1)))
        db.commit()
        scope = scope_for(db, ALPHA)
        result = dashboard_freshness(scope, alpha_dashboard.id, now=NOW)
        assert result.status == FreshnessStatus.unknown


class TestDashboardFreshnessPicksTheNewestSuccessfulRun:
    def test_an_older_successful_run_does_not_shadow_a_newer_one(
        self, db, alpha_dashboard, alpha_daily_schedule,
    ):
        db.add(_run(alpha_dashboard.connection_id, status="succeeded",
                     started_at=NOW - timedelta(days=3, hours=1),
                     finished_at=NOW - timedelta(days=3)))
        db.add(_run(alpha_dashboard.connection_id, status="succeeded",
                     started_at=NOW - timedelta(hours=1),
                     finished_at=NOW - timedelta(minutes=30)))
        db.commit()
        scope = scope_for(db, ALPHA)
        result = dashboard_freshness(scope, alpha_dashboard.id, now=NOW)
        assert result.status == FreshnessStatus.fresh
        assert result.age == timedelta(minutes=30)

    def test_a_later_started_but_still_running_run_does_not_shadow_the_last_success(
        self, db, alpha_dashboard, alpha_daily_schedule,
    ):
        db.add(_run(alpha_dashboard.connection_id, status="succeeded",
                     started_at=NOW - timedelta(hours=2),
                     finished_at=NOW - timedelta(hours=1)))
        db.add(_run(alpha_dashboard.connection_id, status="running",
                     started_at=NOW - timedelta(minutes=5), finished_at=None))
        db.commit()
        scope = scope_for(db, ALPHA)
        result = dashboard_freshness(scope, alpha_dashboard.id, now=NOW)
        assert result.status == FreshnessStatus.fresh
        assert result.age == timedelta(hours=1)


class TestDashboardFreshnessReasonWiring:
    """Proves the schedule's fields reach the top-level result, not just `assess()`."""

    def test_a_disabled_schedule_explains_a_stale_dashboard(self, db, alpha_dashboard):
        schedule = Schedule(
            tenant_id=ALPHA, connection_id=alpha_dashboard.connection_id, cron="0 * * * *",
            enabled=False, last_status="succeeded", consecutive_failures=0,
        )
        db.add(schedule)
        db.add(_run(alpha_dashboard.connection_id, status="succeeded",
                     started_at=NOW - timedelta(hours=6),
                     finished_at=NOW - timedelta(hours=5)))
        db.commit()
        scope = scope_for(db, ALPHA)
        result = dashboard_freshness(scope, alpha_dashboard.id, now=NOW)
        assert result.status == FreshnessStatus.stale
        assert "disabled" in result.reason


class TestDashboardFreshnessTenantIsolation:
    """Another tenant's dashboard is 404, never 403 - see app/tenancy/scope.py."""

    def test_another_tenants_dashboard_is_not_found(
        self, db, alpha_dashboard, alpha_daily_schedule,
    ):
        scope = scope_for(db, BETA)
        with pytest.raises(NotFoundError):
            dashboard_freshness(scope, alpha_dashboard.id, now=NOW)


class TestDashboardFreshnessOwnTenantStillWorks:
    """
    The happy-path twin to the isolation test above: a handler that raises before reaching
    its own logic would return 404 for everybody, passing the isolation test for the wrong
    reason. See TestOwnTenantStillWorks in test_tenant_isolation.py.
    """

    def test_the_owning_tenant_still_gets_an_answer(
        self, db, alpha_dashboard, alpha_daily_schedule,
    ):
        db.add(_run(alpha_dashboard.connection_id, status="succeeded",
                     started_at=NOW - timedelta(hours=2),
                     finished_at=NOW - timedelta(hours=1)))
        db.commit()
        scope = scope_for(db, ALPHA)
        result = dashboard_freshness(scope, alpha_dashboard.id, now=NOW)
        assert result.status == FreshnessStatus.fresh
