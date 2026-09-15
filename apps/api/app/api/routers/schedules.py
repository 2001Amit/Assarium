from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.schemas import UtcDatetime
from app.auth.deps import get_scope, get_tenant_context
from app.core.errors import NotFoundError, ValidationError
from app.models.entities import Connection
from app.orchestration.models import Schedule
from app.orchestration.scheduler import next_occurrence, validate_cron
from app.tenancy.scope import TenantScope

# Every route below resolves a tenant before the handler runs. Declared once here
# rather than per endpoint, so a new endpoint is guarded by default instead of by
# whoever remembers. `assert_routes_are_guarded` at startup proves none slipped past.
router = APIRouter(
    prefix="/api", tags=["schedules"],
    dependencies=[Depends(get_tenant_context)],
)


# --------------------------------------------------------------------------------------
# schemas
# --------------------------------------------------------------------------------------


class ScheduleCreate(BaseModel):
    cron: str = Field(min_length=5, max_length=120, examples=["0 2 * * *"])
    timezone: str = Field(default="UTC", max_length=64)
    enabled: bool = True
    max_retries: int = Field(default=3, ge=0, le=10)


class ScheduleUpdate(BaseModel):
    cron: str | None = Field(default=None, min_length=5, max_length=120)
    timezone: str | None = None
    enabled: bool | None = None
    max_retries: int | None = Field(default=None, ge=0, le=10)


class ScheduleRead(BaseModel):
    id: str
    connection_id: str
    connection_name: str
    cron: str
    timezone: str
    enabled: bool
    next_run_at: UtcDatetime | None
    last_run_at: UtcDatetime | None
    last_run_id: str | None
    last_status: str | None
    last_error: str | None
    consecutive_failures: int
    max_retries: int
    locked_at: UtcDatetime | None
    locked_by: str | None
    created_at: UtcDatetime


class ScheduleOverview(BaseModel):
    total: int
    active: int
    paused: int
    failing: int
    schedules: list[ScheduleRead]


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _to_read(schedule: Schedule, connection_name: str) -> ScheduleRead:
    return ScheduleRead(
        id=schedule.id,
        connection_id=schedule.connection_id,
        connection_name=connection_name,
        cron=schedule.cron,
        timezone=schedule.timezone,
        enabled=schedule.enabled,
        next_run_at=schedule.next_run_at,
        last_run_at=schedule.last_run_at,
        last_run_id=schedule.last_run_id,
        last_status=schedule.last_status,
        last_error=schedule.last_error,
        consecutive_failures=schedule.consecutive_failures,
        max_retries=schedule.max_retries,
        locked_at=schedule.locked_at,
        locked_by=schedule.locked_by,
        created_at=schedule.created_at,
    )


# --------------------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------------------


@router.post(
    "/connections/{connection_id}/schedule",
    response_model=ScheduleRead,
    status_code=201,
)
def create_schedule(
    connection_id: str,
    request: ScheduleCreate,
    scope: TenantScope = Depends(get_scope),
) -> ScheduleRead:
    """Create or replace the schedule for a connection. One schedule per connection."""
    connection = scope.get(Connection, connection_id)
    validate_cron(request.cron)

    existing = scope.one_where(Schedule, Schedule.connection_id == connection_id)
    if existing is not None:
        raise ValidationError(
            "This connection already has a schedule. Use PATCH to modify it, "
            "or DELETE it first.",
            details={"schedule_id": existing.id},
        )

    schedule = scope.create(
        Schedule,
        connection_id=connection_id,
        cron=request.cron,
        timezone=request.timezone,
        enabled=request.enabled,
        max_retries=request.max_retries,
        next_run_at=next_occurrence(request.cron) if request.enabled else None,
    )
    scope.db.commit()
    return _to_read(schedule, connection.name)


@router.get("/connections/{connection_id}/schedule", response_model=ScheduleRead)
def get_schedule(
    connection_id: str,
    scope: TenantScope = Depends(get_scope),
) -> ScheduleRead:
    connection = scope.get(Connection, connection_id)
    schedule = scope.db.scalar(
        select(Schedule).where(Schedule.connection_id == connection_id)
    )
    if schedule is None:
        raise NotFoundError("This connection has no schedule.")
    return _to_read(schedule, connection.name)


@router.patch("/connections/{connection_id}/schedule", response_model=ScheduleRead)
def update_schedule(
    connection_id: str,
    request: ScheduleUpdate,
    scope: TenantScope = Depends(get_scope),
) -> ScheduleRead:
    """Update individual fields on an existing schedule."""
    connection = scope.get(Connection, connection_id)
    schedule = scope.db.scalar(
        select(Schedule).where(Schedule.connection_id == connection_id)
    )
    if schedule is None:
        raise NotFoundError("This connection has no schedule.")

    if request.cron is not None:
        validate_cron(request.cron)
        schedule.cron = request.cron
    if request.timezone is not None:
        schedule.timezone = request.timezone
    if request.enabled is not None:
        schedule.enabled = request.enabled
    if request.max_retries is not None:
        schedule.max_retries = request.max_retries

    # Recompute next run whenever the cron or enabled state changes.
    if schedule.enabled:
        schedule.next_run_at = next_occurrence(schedule.cron)
    else:
        schedule.next_run_at = None

    scope.db.commit()
    return _to_read(schedule, connection.name)


@router.delete("/connections/{connection_id}/schedule", status_code=204)
def delete_schedule(
    connection_id: str,
    scope: TenantScope = Depends(get_scope),
) -> None:
    scope.get(Connection, connection_id)
    schedule = scope.db.scalar(
        select(Schedule).where(Schedule.connection_id == connection_id)
    )
    if schedule is None:
        raise NotFoundError("This connection has no schedule.")
    scope.db.delete(schedule)
    scope.db.commit()


@router.get("/schedules", response_model=ScheduleOverview)
def list_schedules(scope: TenantScope = Depends(get_scope)) -> ScheduleOverview:
    """Overview of all schedules across all connections."""
    schedules = list(
        scope.db.execute(
            select(Schedule).order_by(Schedule.created_at.desc())
        ).scalars()
    )
    connection_names: dict[str, str] = {}
    for schedule in schedules:
        if schedule.connection_id not in connection_names:
            connection = scope.get(Connection, schedule.connection_id)
            connection_names[schedule.connection_id] = (
                connection.name if connection else "(deleted)"
            )

    reads = [
        _to_read(s, connection_names.get(s.connection_id, "(deleted)"))
        for s in schedules
    ]
    return ScheduleOverview(
        total=len(reads),
        active=sum(1 for s in schedules if s.enabled and s.consecutive_failures == 0),
        paused=sum(1 for s in schedules if not s.enabled),
        failing=sum(1 for s in schedules if s.enabled and s.consecutive_failures > 0),
        schedules=reads,
    )
