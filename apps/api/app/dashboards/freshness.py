"""
How stale a dashboard's data is allowed to look before it gets called out.

There is no fixed staleness bar: a table refreshed hourly is already a problem three
hours in, and a table refreshed nightly is unremarkable at three hours. So the threshold
is never a constant - it is derived from the connection's own `Schedule` (see
`app/orchestration/models.py`), at 1.5x its expected interval for `late` and 2x for
`stale`. A connection with no schedule has no cadence to judge against, and reporting one
anyway would be exactly the kind of approximation this platform refuses to make.

Freshness is judged against the run that actually *finished*, never the one that started -
a run stuck at ninety percent for six hours has produced nothing new, and timing it from
its start would call six-hour-old data current. A run that never finished, or never
happened at all, is `unknown`, never `fresh`.

"Stale" on its own gives a viewer nowhere to go, so every non-fresh result carries a
`reason` grounded in the schedule's own state - disabled, failing, or simply running
behind - rather than just a status label.

The decision itself (`assess`) is pure - it takes timestamps and a schedule in and returns
a verdict, with no clock read and no database access, so it can be tested without a
fixture. The lookup (`dashboard_freshness`) is the only part that touches storage, and it
goes through `TenantScope` like every other tenant-owned read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.models.base import ensure_utc, utcnow
from app.models.entities import DashboardRecord, PipelineRun
from app.orchestration.models import Schedule
from app.orchestration.scheduler import next_occurrence
from app.tenancy.scope import TenantScope

#: Multiples of the schedule's own expected interval. Chosen to give a run running behind
#: room for ordinary jitter (`late`) before calling out a real problem (`stale`).
LATE_FACTOR = 1.5
STALE_FACTOR = 2.0

NO_SCHEDULE_REASON = (
    "This connection has no refresh schedule, so freshness cannot be judged against an "
    "expected cadence."
)


class FreshnessStatus(StrEnum):
    fresh = "fresh"
    late = "late"
    stale = "stale"
    #: No run has ever finished, or there is no schedule to judge one against - kept
    #: apart from `stale` because "old" and "nothing to compare against" need different
    #: messages to a viewer.
    unknown = "unknown"


@dataclass(frozen=True, slots=True)
class FreshnessResult:
    status: FreshnessStatus
    finished_at: datetime | None
    age: timedelta | None
    #: Why, in terms a viewer can act on. `None` only when `status` is `fresh`.
    reason: str | None


def _expected_interval(cron: str, finished_at: datetime) -> timedelta:
    """
    The schedule's own step size, measured from the two occurrences after `finished_at`.

    Anchoring on `finished_at` rather than on some fixed epoch means an irregular cadence
    (business-hours-only, weekdays-only) reports the gap that was actually expected at
    that point in the schedule, not an average that smears weekends into weekdays.
    """
    first = next_occurrence(cron, after=finished_at)
    second = next_occurrence(cron, after=first)
    return second - first


def _reason(status: FreshnessStatus, schedule: Schedule) -> str | None:
    if status == FreshnessStatus.fresh:
        return None
    if status == FreshnessStatus.unknown:
        return "No pipeline run has completed yet."
    if not schedule.enabled:
        return "The schedule is disabled, so no further runs are expected until it is re-enabled."
    if schedule.last_status == "failed" and schedule.consecutive_failures > 0:
        run_word = "run" if schedule.consecutive_failures == 1 else "runs"
        return f"The last {schedule.consecutive_failures} {run_word} failed."
    return (
        "The schedule is enabled and healthy, but has not produced new data within the "
        "expected window."
    )


def assess(
    as_of: datetime,
    finished_at: datetime | None,
    schedule: Schedule,
    *,
    now: datetime,
) -> FreshnessResult:
    """
    Classify a run's finish time as fresh, late, stale, or unknown, as of `as_of`.

    `now` is required rather than defaulted to the wall clock: a "pure" function that
    quietly reads the real clock is not pure, and a caller who forgot to pass it would not
    notice until a test ran at an inconvenient minute. `as_of` is the point freshness is
    judged from and is normally identical to `now` - keeping them as two arguments means a
    caller that assesses freshness "as of" a moment later than the real present gets an
    error instead of a number that looks plausible but describes a time that has not
    happened yet.
    """
    for label, value in (("as_of", as_of), ("now", now), ("finished_at", finished_at)):
        if value is not None and value.tzinfo is None:
            raise ValueError(f"assess() needs a timezone-aware `{label}`, not a naive one.")

    if as_of > now:
        raise ValueError("as_of is after now - refusing to assess freshness from the future.")

    if finished_at is None:
        return FreshnessResult(
            status=FreshnessStatus.unknown,
            finished_at=None,
            age=None,
            reason=_reason(FreshnessStatus.unknown, schedule),
        )

    if finished_at > now:
        raise ValueError("finished_at is after now - a run cannot finish in the future.")

    age = as_of - finished_at
    interval = _expected_interval(schedule.cron, finished_at)

    if age > interval * STALE_FACTOR:
        status = FreshnessStatus.stale
    elif age > interval * LATE_FACTOR:
        status = FreshnessStatus.late
    else:
        status = FreshnessStatus.fresh

    return FreshnessResult(status=status, finished_at=finished_at, age=age,
                            reason=_reason(status, schedule))


def dashboard_freshness(
    scope: TenantScope,
    dashboard_id: str,
    *,
    now: datetime | None = None,
) -> FreshnessResult:
    """
    Freshness for one dashboard, judged by the newest *successful* run of its connection
    against that connection's own schedule.

    A run that is still going, or one that failed partway, has not produced the tables the
    dashboard reads from - only a `succeeded` run counts, and the most recently finished
    one wins when there is more than one.
    """
    dashboard = scope.get(DashboardRecord, dashboard_id)
    reference = now if now is not None else utcnow()

    run = scope.db.execute(
        scope.select(PipelineRun)
        .where(
            PipelineRun.connection_id == dashboard.connection_id,
            PipelineRun.status == "succeeded",
        )
        .order_by(PipelineRun.finished_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    finished_at = ensure_utc(run.finished_at) if run is not None else None

    schedule = scope.one_where(Schedule, Schedule.connection_id == dashboard.connection_id)
    if schedule is None:
        return FreshnessResult(
            status=FreshnessStatus.unknown, finished_at=finished_at, age=None,
            reason=NO_SCHEDULE_REASON,
        )

    return assess(reference, finished_at, schedule, now=reference)
