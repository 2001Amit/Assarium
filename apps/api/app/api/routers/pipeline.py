from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel

from app.api.schemas import UtcDatetime
from app.auth.deps import get_scope, get_tenant_context
from app.core.errors import NotFoundError, ValidationError
from app.engine.base import LAYERS, is_internal_table
from app.engine.factory import get_engine
from app.medallion.pipeline import PipelineOptions, execute_run, start_run
from app.models.entities import Connection, Dataset, PipelineRun, PipelineStep
from app.tenancy.scope import TenantScope

# Every route below resolves a tenant before the handler runs. Declared once here
# rather than per endpoint, so a new endpoint is guarded by default instead of by
# whoever remembers. `assert_routes_are_guarded` at startup proves none slipped past.
router = APIRouter(
    prefix="/api", tags=["pipeline"],
    dependencies=[Depends(get_tenant_context)],
)


class RunRequest(BaseModel):
    dataset_ids: list[str] | None = None
    drop_ingestion_metadata: bool = True
    build_gold: bool = True


class StepRead(BaseModel):
    id: str
    sequence: int
    dataset_name: str
    kind: str
    layer: str
    status: str
    target_table: str | None
    rows_out: int | None
    duration_ms: int | None
    actions: list[str] | None
    notes: list[str] | None
    message: str | None
    sql: str | None


class RunRead(BaseModel):
    id: str
    connection_id: str
    engine: str
    status: str
    started_at: UtcDatetime
    finished_at: UtcDatetime | None
    dataset_count: int
    summary: dict[str, Any] | None
    steps: list[StepRead] = []


def _to_read(run: PipelineRun, steps: list[PipelineStep]) -> RunRead:
    return RunRead(
        id=run.id,
        connection_id=run.connection_id,
        engine=run.engine,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        dataset_count=run.dataset_count,
        summary=run.summary,
        steps=[
            StepRead(
                id=s.id,
                sequence=s.sequence,
                dataset_name=s.dataset_name,
                kind=s.kind,
                layer=s.layer,
                status=s.status,
                target_table=s.target_table,
                rows_out=s.rows_out,
                duration_ms=s.duration_ms,
                actions=s.actions,
                notes=s.notes,
                message=s.message,
                sql=s.sql,
            )
            for s in steps
        ],
    )


def _steps(scope: TenantScope, run_id: str) -> list[PipelineStep]:
    TenantScope.assert_is_scope(scope, "_steps")
    return list(
        scope.db.execute(
            scope.select(PipelineStep)
            .where(PipelineStep.run_id == run_id)
            .order_by(PipelineStep.sequence)
        ).scalars()
    )


@router.post("/connections/{connection_id}/runs", response_model=RunRead, status_code=202)
def create_run(
    connection_id: str,
    request: RunRequest,
    background: BackgroundTasks,
    scope: TenantScope = Depends(get_scope),
) -> RunRead:
    """Start a refinement run. Returns immediately; poll the run for progress."""
    scope.get(Connection, connection_id)

    unprofiled = scope.db.execute(
        scope.select(Dataset).where(
            Dataset.connection_id == connection_id, Dataset.profile.is_(None)
        )
    ).scalars().first()
    if unprofiled is not None:
        raise ValidationError(
            "Every selected dataset needs profiling first. Refinement is driven by what "
            "profiling measured, so an unprofiled table has nothing to act on.",
            details={"dataset": unprofiled.name},
        )

    run_id = start_run(connection_id, scope.tenant_id, request.dataset_ids)
    options = PipelineOptions(
        drop_ingestion_metadata=request.drop_ingestion_metadata,
        build_gold=request.build_gold,
    )
    background.add_task(execute_run, run_id, request.dataset_ids, options)

    run = scope.get(PipelineRun, run_id)
    scope.db.refresh(run)
    return _to_read(run, [])


@router.get("/connections/{connection_id}/runs", response_model=list[RunRead])
def list_runs(
    connection_id: str,
    limit: int = Query(default=20, le=100),
    scope: TenantScope = Depends(get_scope),
) -> list[RunRead]:
    # Check the connection first. Filtering only the runs would answer 200 with an empty
    # list for somebody else's connection, which reads as "no runs yet" rather than
    # "not yours" - the same answer for two very different situations.
    scope.get(Connection, connection_id)
    runs = list(
        scope.db.execute(
            scope.select(PipelineRun)
            .where(PipelineRun.connection_id == connection_id)
            .order_by(PipelineRun.started_at.desc())
            .limit(limit)
        ).scalars()
    )
    return [_to_read(run, []) for run in runs]


@router.get("/runs/{run_id}", response_model=RunRead)
def get_run(run_id: str, scope: TenantScope = Depends(get_scope)) -> RunRead:
    run = scope.get(PipelineRun, run_id)
    if run is None:
        raise NotFoundError("That run does not exist.")
    scope.db.expire(run)
    return _to_read(run, _steps(scope, run_id))


@router.get("/warehouse")
def warehouse_contents(scope: TenantScope = Depends(get_scope)) -> dict[str, Any]:
    """What this tenant currently holds in each medallion layer."""
    engine = get_engine(scope.tenant_id)
    engine.ensure_layers()
    return {
        "engine": engine.name,
        "layers": {
            layer: [
                {
                    "name": name,
                    "rows": engine.row_count(layer, name),
                    "columns": [
                        {"name": column, "type": kind}
                        for column, kind in engine.describe(layer, name)
                    ],
                }
                for name in engine.list_tables(layer)
                if not is_internal_table(name)
            ]
            for layer in LAYERS
        },
    }


@router.get("/warehouse/{layer}/{table}/preview")
def preview_table(
    layer: str,
    table: str,
    limit: int = Query(default=100, ge=1, le=1000),
    scope: TenantScope = Depends(get_scope),
) -> dict[str, Any]:
    engine = get_engine(scope.tenant_id)
    if layer not in LAYERS:
        raise ValidationError(f"'{layer}' is not a medallion layer.")
    if table not in engine.list_tables(layer):  # type: ignore[arg-type]
        raise NotFoundError(f"{layer}.{table} does not exist.")
    result = engine.execute(
        f"SELECT * FROM {engine.qualified(layer, table)}",  # type: ignore[arg-type]
        limit=limit,
    )
    return result.model_dump(mode="json")
