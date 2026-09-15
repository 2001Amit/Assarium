"""
Scheduling: when a pipeline runs, and making sure it runs once.

The failure that costs money is a double run - two workers ingesting the same window at
the same time. The failure that costs trust is a schedule that silently stops forever
because one worker crashed holding a lock. Both are tested here.
"""

from __future__ import annotations

import concurrent.futures as futures
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ValidationError
from app.models import entities as _entities  # noqa: F401 - registers the FK target
from app.models.base import Base, ensure_utc, utcnow
from app.orchestration.models import Schedule
from app.orchestration.scheduler import (
    LOCK_TIMEOUT,
    acquire,
    due_schedules,
    next_occurrence,
    release,
    validate_cron,
)


@pytest.fixture
def sessions():
    # A file-backed database so several sessions genuinely contend, which an in-memory
    # SQLite database would not reproduce.
    import tempfile

    path = tempfile.mktemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}")
    # Foreign keys must be enforced for the locking test to be meaningful, and every
    # referenced table has to exist for create_all to resolve them.
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    yield factory
    engine.dispose()


@pytest.fixture
def db(sessions) -> Session:
    session = sessions()
    yield session
    session.close()


#: Every row belongs to a tenant now, including in tests - the column is not nullable,
#: which is the point: a row with no owner cannot be created by accident.
TENANT = "tenant-under-test"


def make_schedule(db: Session, **kwargs) -> Schedule:
    from app.models.entities import Connection

    if db.get(Connection, "conn-1") is None:
        db.add(Connection(
            id="conn-1", name="c", source_id="files", config={}, tenant_id=TENANT
        ))
        db.commit()

    schedule = Schedule(
        connection_id="conn-1",
        tenant_id=TENANT,
        cron=kwargs.pop("cron", "0 2 * * *"),
        next_run_at=kwargs.pop("next_run_at", utcnow() - timedelta(minutes=1)),
        **kwargs,
    )
    db.add(schedule)
    db.commit()
    return schedule


class TestCron:
    def test_a_valid_expression_is_accepted(self):
        assert validate_cron("0 2 * * *") == "0 2 * * *"

    @pytest.mark.parametrize("bad", ["not a cron", "0 2 * *", "99 99 * * *", ""])
    def test_an_invalid_expression_is_refused_with_an_example(self, bad):
        with pytest.raises(ValidationError) as caught:
            validate_cron(bad)
        assert "0 2 * * *" in str(caught.value)

    def test_the_next_occurrence_is_in_the_future(self):
        assert next_occurrence("*/5 * * * *") > utcnow()


class TestDueness:
    def test_a_schedule_past_its_time_is_due(self, db):
        make_schedule(db)
        assert len(due_schedules(db)) == 1

    def test_a_future_schedule_is_not_due(self, db):
        make_schedule(db, next_run_at=utcnow() + timedelta(hours=1))
        assert due_schedules(db) == []

    def test_a_disabled_schedule_is_never_due(self, db):
        make_schedule(db, enabled=False)
        assert due_schedules(db) == []

    def test_a_schedule_with_no_next_run_is_not_due(self, db):
        make_schedule(db, next_run_at=None)
        assert due_schedules(db) == []

    def test_a_locked_schedule_is_not_picked_up_again(self, db):
        schedule = make_schedule(db)
        schedule.locked_at = utcnow()
        schedule.locked_by = "worker-1"
        db.commit()
        assert due_schedules(db) == []

    def test_a_stale_lock_is_reclaimed(self, db):
        """
        A worker that crashed holding a lock must not stop the schedule forever. Running
        twice is recoverable - the loads are idempotent. Never running again is not.
        """
        schedule = make_schedule(db)
        schedule.locked_at = utcnow() - LOCK_TIMEOUT - timedelta(minutes=1)
        schedule.locked_by = "dead-worker"
        db.commit()
        assert len(due_schedules(db)) == 1


class TestExactlyOnce:
    def test_only_one_worker_can_acquire_a_schedule(self, sessions):
        first = sessions()
        schedule = make_schedule(first)
        schedule_id = schedule.id

        second = sessions()
        reloaded = second.get(Schedule, schedule_id)

        assert acquire(first, schedule) is True
        assert acquire(second, reloaded) is False, "a second worker must not also claim it"
        first.close()
        second.close()

    def test_concurrent_workers_produce_exactly_one_winner(self, sessions):
        setup = sessions()
        schedule_id = make_schedule(setup).id
        setup.close()

        def attempt(_):
            session = sessions()
            try:
                return acquire(session, session.get(Schedule, schedule_id))
            finally:
                session.close()

        with futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(8)))

        assert sum(results) == 1, f"exactly one worker should win, got {sum(results)}"

    def test_releasing_frees_the_schedule_for_the_next_run(self, db):
        schedule = make_schedule(db)
        acquire(db, schedule)
        release(db, schedule, run_id="run-1", status="succeeded")
        assert schedule.locked_at is None
        assert schedule.locked_by is None


class TestOutcomes:
    def test_success_moves_to_the_next_scheduled_time(self, db):
        schedule = make_schedule(db, cron="0 2 * * *")
        release(db, schedule, run_id="r1", status="succeeded")
        assert ensure_utc(schedule.next_run_at) > utcnow()
        assert schedule.consecutive_failures == 0
        assert schedule.last_status == "succeeded"

    def test_a_failure_backs_off_rather_than_retrying_immediately(self, db):
        schedule = make_schedule(db)
        release(db, schedule, run_id="r1", status="failed", error="source unreachable")
        assert schedule.consecutive_failures == 1
        # Sooner than the daily cron, but not instant.
        assert utcnow() < ensure_utc(schedule.next_run_at) < utcnow() + timedelta(minutes=10)

    def test_backoff_lengthens_with_repeated_failures(self, db):
        schedule = make_schedule(db)
        delays = []
        for _ in range(3):
            before = utcnow()
            release(db, schedule, run_id="r", status="failed", error="down")
            delays.append(ensure_utc(schedule.next_run_at) - before)
        assert delays[0] < delays[1] < delays[2]

    def test_giving_up_returns_to_the_normal_cadence(self, db):
        """After max_retries the schedule waits for its next slot, not a tight loop."""
        schedule = make_schedule(db, cron="0 2 * * *", max_retries=2)
        for _ in range(4):
            release(db, schedule, run_id="r", status="failed", error="down")
        assert ensure_utc(schedule.next_run_at) > utcnow() + timedelta(hours=1)

    def test_a_success_after_failures_clears_the_backoff(self, db):
        schedule = make_schedule(db)
        release(db, schedule, run_id="r", status="failed", error="down")
        release(db, schedule, run_id="r", status="failed", error="down")
        assert schedule.consecutive_failures == 2
        release(db, schedule, run_id="r", status="succeeded")
        assert schedule.consecutive_failures == 0
        assert schedule.last_error is None

    def test_the_error_is_kept_for_diagnosis(self, db):
        schedule = make_schedule(db)
        release(db, schedule, run_id="r", status="failed", error="SharePoint 403")
        assert schedule.last_error == "SharePoint 403"
