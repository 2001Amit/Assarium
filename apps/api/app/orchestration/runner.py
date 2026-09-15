"""
The loop that turns schedules into pipeline runs.

Runs as a single asyncio task inside the API process. It polls on a fixed
interval rather than sleeping until the next due time, because schedules can be
created or modified at any moment and an event-driven wakeup adds complexity
that is not justified until there are thousands of schedules.

Design constraints:
  - The runner must never crash the API. Every exception inside the loop is
    caught, logged, and continued.
  - The runner must never start a run that is already running. The scheduler's
    conditional-update lock guarantees this even if two API processes exist.
  - A schedule that fails is backed off, not retried immediately. The
    scheduler's exponential backoff handles this.
"""

from __future__ import annotations

import asyncio
import logging
import traceback

from app.db.session import session_scope
from app.medallion.pipeline import PipelineOptions, execute_run, start_run
from app.models.entities import Connection
from app.orchestration.alerting import AlertEvent, EventKind, send_alert
from app.orchestration.scheduler import acquire, due_schedules, release

logger = logging.getLogger("assarium.runner")


async def run_loop(*, poll_seconds: int = 30) -> None:
    """Poll for due schedules and execute them.

    Intended to be launched as an asyncio background task from the FastAPI lifespan.
    """
    logger.info("Orchestration runner started — polling every %ds", poll_seconds)
    while True:
        try:
            await asyncio.sleep(poll_seconds)
            _tick()
        except asyncio.CancelledError:
            logger.info("Orchestration runner stopped")
            return
        except Exception:
            # The runner must survive anything. Log the traceback and continue.
            logger.error("Runner tick failed:\n%s", traceback.format_exc())
            await asyncio.sleep(poll_seconds)


def _tick() -> None:
    """One pass: find due schedules, acquire, execute, release."""
    with session_scope() as db:
        due = due_schedules(db)
        if not due:
            return
        logger.info("Found %d due schedule(s)", len(due))

    # Process each schedule in its own session so a failure in one does not
    # roll back the outcome of another.
    for schedule in due:
        _process_schedule(schedule.id)


def _process_schedule(schedule_id: str) -> None:
    """Acquire, run the pipeline, and release a single schedule."""
    run_id: str | None = None
    connection_name = "(unknown)"
    with session_scope() as db:
        from app.orchestration.models import Schedule

        schedule = db.get(Schedule, schedule_id)
        if schedule is None:
            return

        connection = db.get(Connection, schedule.connection_id)
        if connection is None:
            logger.warning("Schedule %s references a deleted connection", schedule_id)
            schedule.enabled = False
            db.commit()
            return
        connection_name = connection.name

        if not acquire(db, schedule):
            logger.debug("Schedule %s already claimed by another worker", schedule_id)
            return

        logger.info(
            "Acquired schedule %s for connection '%s' (cron: %s)",
            schedule_id, connection_name, schedule.cron,
        )

    # Run outside the session that holds the lock. The pipeline opens its own sessions
    # internally, and a long-running transaction on the lock row is unnecessary.
    status = "succeeded"
    error_message: str | None = None
    try:
        run_id = start_run(schedule.connection_id, schedule.tenant_id)
        execute_run(run_id, dataset_ids=None, options=PipelineOptions())
        logger.info("Schedule %s completed successfully (run %s)", schedule_id, run_id)
    except Exception as exc:
        status = "failed"
        error_message = f"{type(exc).__name__}: {exc}"
        logger.error(
            "Schedule %s failed (run %s): %s\n%s",
            schedule_id, run_id, error_message, traceback.format_exc(),
        )

    # Release the lock and record the outcome.
    with session_scope() as db:
        from app.orchestration.models import Schedule

        schedule = db.get(Schedule, schedule_id)
        if schedule is None:
            return
        release(db, schedule, run_id=run_id, status=status, error=error_message)

    # Alert after the lock is released so a slow webhook does not hold it.
    _send_run_alert(status, connection_name, schedule_id, run_id, error_message)


def _send_run_alert(
    status: str,
    connection_name: str,
    schedule_id: str,
    run_id: str | None,
    error: str | None,
) -> None:
    """Dispatch an alert for the completed run."""
    try:
        if status == "succeeded":
            send_alert(AlertEvent(
                kind=EventKind.run_succeeded,
                connection_name=connection_name,
                schedule_id=schedule_id,
                run_id=run_id,
                message=f"Pipeline run {run_id} completed successfully.",
            ))
        else:
            send_alert(AlertEvent(
                kind=EventKind.run_failed,
                connection_name=connection_name,
                schedule_id=schedule_id,
                run_id=run_id,
                message=f"Pipeline run failed: {error}",
            ))
    except Exception:
        logger.exception("Failed to send alert for schedule %s", schedule_id)
