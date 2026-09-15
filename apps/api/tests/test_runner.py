"""
Runner loop: turning schedules into pipeline runs.

The runner is the bridge between the scheduler (which decides *when*) and the pipeline
(which does *what*). Its job is to survive anything the pipeline throws, record the
outcome, and alert on it.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import entities as _entities  # noqa: F401 - FK targets
from app.models.base import Base, utcnow
from app.models.entities import Connection
from app.orchestration.alerting import AlertEvent, EventKind, LogAlertSink
from app.orchestration.models import Schedule
from app.orchestration.runner import _process_schedule, _tick


@pytest.fixture
def sessions():
    import tempfile

    path = tempfile.mktemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    yield factory
    engine.dispose()


@pytest.fixture
def db(sessions) -> Session:
    session = sessions()
    yield session
    session.close()


def _seed(db: Session, *, cron: str = "0 2 * * *", enabled: bool = True) -> tuple[str, str]:
    """Create a connection and a due schedule, return (schedule_id, connection_id)."""
    connection = Connection(id="conn-runner", name="Runner Test", source_id="files", config={})
    db.add(connection)
    db.commit()
    schedule = Schedule(
        connection_id="conn-runner",
        cron=cron,
        enabled=enabled,
        next_run_at=utcnow() - timedelta(minutes=1),
    )
    db.add(schedule)
    db.commit()
    return schedule.id, connection.id


class TestRunnerTick:
    @patch("app.orchestration.runner.session_scope")
    @patch("app.orchestration.runner.due_schedules")
    def test_no_due_schedules_does_nothing(self, mock_due, mock_scope):
        mock_db = MagicMock()
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        mock_due.return_value = []
        # Should not raise
        _tick()


class TestProcessSchedule:
    @patch("app.orchestration.runner.session_scope")
    def test_missing_schedule_is_skipped(self, mock_scope):
        mock_db = MagicMock()
        mock_db.get.return_value = None
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        # Should not raise
        _process_schedule("nonexistent")

    @patch("app.orchestration.runner.session_scope")
    def test_deleted_connection_disables_schedule(self, mock_scope):
        mock_db = MagicMock()
        mock_schedule = MagicMock(spec=Schedule)
        mock_schedule.connection_id = "gone"
        mock_schedule.id = "sched-1"
        # First call returns schedule, second call returns None (deleted connection)
        mock_db.get.side_effect = [mock_schedule, None]
        mock_scope.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_scope.return_value.__exit__ = MagicMock(return_value=False)
        _process_schedule("sched-1")
        assert mock_schedule.enabled is False


class TestAlerting:
    def test_log_sink_does_not_raise_on_any_event_kind(self):
        sink = LogAlertSink()
        for kind in EventKind:
            event = AlertEvent(
                kind=kind,
                connection_name="test",
                message="test message",
            )
            # Must not raise
            sink.send(event)

    def test_alert_event_is_immutable(self):
        event = AlertEvent(
            kind=EventKind.run_succeeded,
            connection_name="test",
        )
        with pytest.raises(AttributeError):
            event.kind = EventKind.run_failed  # type: ignore[misc]

    def test_webhook_sink_handles_failure_gracefully(self):
        from app.orchestration.alerting import WebhookAlertSink

        # A URL that will fail — the sink must not raise
        sink = WebhookAlertSink("http://localhost:1/nonexistent", timeout=0.5)
        event = AlertEvent(
            kind=EventKind.run_failed,
            connection_name="test",
            message="expected to fail",
        )
        # Must not raise
        sink.send(event)
