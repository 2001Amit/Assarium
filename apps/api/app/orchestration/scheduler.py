"""
Deciding when a pipeline should run, and making sure it only runs once.

Kept free of any transport so the same logic serves an in-process loop today and an
external trigger (Databricks Workflows, a cron container) later without change.
"""

from __future__ import annotations

import logging
import os
import socket
from datetime import UTC, datetime, timedelta

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ValidationError
from app.models.base import utcnow
from app.orchestration.models import Schedule

logger = logging.getLogger("assarium.orchestration")

# A lock older than this is assumed to belong to a process that died mid-run. Long
# enough that a genuinely slow load is never stolen from itself.
LOCK_TIMEOUT = timedelta(hours=6)

# Backoff between retries, by consecutive failure count. A source that is down stays
# down for a while, and hammering it every minute helps nobody.
BACKOFF = [timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2)]


def worker_id() -> str:
    """Identifies which process holds a lock, so a stuck run can be traced."""
    return f"{socket.gethostname()}:{os.getpid()}"


def validate_cron(expression: str) -> str:
    if not croniter.is_valid(expression):
        raise ValidationError(
            f"'{expression}' is not a valid cron expression. Use five fields, for "
            "example '0 2 * * *' for 02:00 every day."
        )
    return expression


def next_occurrence(cron: str, after: datetime | None = None) -> datetime:
    base = after or utcnow()
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    return croniter(cron, base).get_next(datetime)


def due_schedules(db: Session, limit: int = 20) -> list[Schedule]:
    """
    Schedules that should run now and are not already running.

    A stale lock is treated as free: without that, one crashed worker would stop a
    schedule from ever running again, which is a worse failure than running it twice.
    """
    now = utcnow()
    cutoff = now - LOCK_TIMEOUT
    rows = db.execute(
        select(Schedule)
        .where(
            Schedule.enabled.is_(True),
            Schedule.next_run_at.isnot(None),
            Schedule.next_run_at <= now,
        )
        .order_by(Schedule.next_run_at)
        .limit(limit)
    ).scalars()

    ready = []
    for schedule in rows:
        if schedule.locked_at is None:
            ready.append(schedule)
            continue
        locked_at = schedule.locked_at
        if locked_at.tzinfo is None:
            locked_at = locked_at.replace(tzinfo=UTC)
        if locked_at < cutoff:
            logger.warning(
                "Reclaiming a stale lock on schedule %s held by %s since %s",
                schedule.id, schedule.locked_by, locked_at,
            )
            ready.append(schedule)
    return ready


def acquire(db: Session, schedule: Schedule) -> bool:
    """
    Claim a schedule for this worker.

    The claim is a conditional UPDATE, so if two workers race, exactly one row is
    modified and the other sees zero and backs off. Checking then writing would let both
    through.
    """
    now = utcnow()
    cutoff = now - LOCK_TIMEOUT
    result = db.execute(
        Schedule.__table__.update()
        .where(
            Schedule.id == schedule.id,
            (Schedule.locked_at.is_(None)) | (Schedule.locked_at < cutoff),
        )
        .values(locked_at=now, locked_by=worker_id())
    )
    db.commit()
    return result.rowcount == 1


def release(
    db: Session,
    schedule: Schedule,
    *,
    run_id: str | None,
    status: str,
    error: str | None = None,
) -> None:
    """Record the outcome and decide when to try again."""
    now = utcnow()
    schedule.locked_at = None
    schedule.locked_by = None
    schedule.last_run_at = now
    schedule.last_run_id = run_id
    schedule.last_status = status
    schedule.last_error = error

    if status == "succeeded":
        schedule.consecutive_failures = 0
        schedule.next_run_at = next_occurrence(schedule.cron, now)
        schedule.last_error = None
    else:
        schedule.consecutive_failures += 1
        if schedule.consecutive_failures > schedule.max_retries:
            # Stop retrying, but keep the schedule on its normal cadence: the next
            # scheduled time is a fresh attempt, not an immediate hammering.
            schedule.next_run_at = next_occurrence(schedule.cron, now)
            logger.error(
                "Schedule %s has failed %d times; waiting for the next scheduled slot.",
                schedule.id, schedule.consecutive_failures,
            )
        else:
            delay = BACKOFF[min(schedule.consecutive_failures - 1, len(BACKOFF) - 1)]
            schedule.next_run_at = now + delay
            logger.warning(
                "Schedule %s failed (%d/%d); retrying in %s.",
                schedule.id, schedule.consecutive_failures, schedule.max_retries, delay,
            )
    db.commit()
