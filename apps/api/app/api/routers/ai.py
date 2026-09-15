from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.ai import provider as llm
from app.ai.chat import chat, explain_kpi
from app.ai.provider import LLMUnavailableError
from app.ai.types import ChatRequest, ChatResponse, KpiExplanation
from app.auth.deps import get_scope, get_tenant_context
from app.core.errors import NotFoundError, ValidationError
from app.dashboards.runner import run_dashboard
from app.dashboards.types import Dashboard
from app.engine.factory import get_engine
from app.models.entities import Connection, DashboardRecord, SemanticModelRecord
from app.semantic.types import Filter, SemanticModel
from app.tenancy.scope import TenantScope

# Every route below resolves a tenant before the handler runs. Declared once here
# rather than per endpoint, so a new endpoint is guarded by default instead of by
# whoever remembers. `assert_routes_are_guarded` at startup proves none slipped past.
router = APIRouter(
    prefix="/api", tags=["ai"],
    dependencies=[Depends(get_tenant_context)],
)


def _model(scope: TenantScope, connection_id: str) -> SemanticModel:
    TenantScope.assert_is_scope(scope, "_model")
    record = scope.db.execute(
        scope.select(SemanticModelRecord).where(SemanticModelRecord.connection_id == connection_id)
    ).scalar_one_or_none()
    if record is None:
        raise NotFoundError("Build the semantic model before using AI features.")
    return SemanticModel.model_validate(record.document)


# --------------------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------------------


@router.get("/ai/status")
def ai_status() -> dict[str, Any]:
    """Check whether the LLM provider is configured."""
    return {"available": llm.is_available()}


# --------------------------------------------------------------------------------------
# chat
# --------------------------------------------------------------------------------------


@router.post("/connections/{connection_id}/chat", response_model=ChatResponse)
def chat_endpoint(
    connection_id: str, request: ChatRequest, scope: TenantScope = Depends(get_scope)
) -> ChatResponse:
    """Send a data question and receive a formatted answer with optional inline data."""
    scope.get(Connection, connection_id)

    try:
        model = _model(scope, connection_id)
        engine = get_engine(scope.tenant_id)
        return chat(
            model, engine, request.messages,
            permissions=scope.context.permissions,
        )
    except LLMUnavailableError as exc:
        raise ValidationError(str(exc)) from exc


# --------------------------------------------------------------------------------------
# KPI explanation
# --------------------------------------------------------------------------------------


class ExplainRequest(BaseModel):
    filters: list[Filter] = Field(default_factory=list)


@router.post(
    "/dashboards/{dashboard_id}/tiles/{tile_id}/explain",
    response_model=KpiExplanation,
)
def explain_tile(
    dashboard_id: str,
    tile_id: str,
    request: ExplainRequest,
    scope: TenantScope = Depends(get_scope),
) -> KpiExplanation:
    """Generate a plain-English explanation for one dashboard tile."""
    record = scope.get(DashboardRecord, dashboard_id)
    if record is None:
        raise NotFoundError("That dashboard does not exist.")

    dashboard = Dashboard.model_validate(record.document)
    tile = next((t for t in dashboard.tiles if t.id == tile_id), None)
    if tile is None:
        raise NotFoundError("That tile is not on this dashboard.")

    if not tile.measures:
        raise ValidationError("This tile has no measures to explain.")

    model = _model(scope, record.connection_id)
    engine = get_engine(scope.tenant_id)

    # Run just this tile to get the current value.
    data = run_dashboard(model, engine, dashboard, request.filters)
    tile_result = next((t for t in data.tiles if t.tile_id == tile_id), None)

    value = None
    delta = None
    direction = None
    breakdown: list[tuple[str, float | None]] | None = None

    if tile_result and tile_result.stats:
        stat = tile_result.stats[0]
        value = stat.value
        delta = stat.delta
        direction = stat.direction
    elif tile_result and tile_result.points:
        # A chart tile has no headline figure, so its largest contributors stand in.
        measure_column = (
            tile_result.measure_columns[0] if tile_result.measure_columns else None
        )
        if measure_column:
            breakdown = [
                (point.label, point.values.get(measure_column))
                for point in tile_result.points[:5]
            ]
            value = sum(
                amount for _, amount in breakdown if isinstance(amount, (int, float))
            )

    try:
        explanation = explain_kpi(
            model, engine, tile.measures[0], value, delta, direction, breakdown
        )
    except LLMUnavailableError as exc:
        raise ValidationError(str(exc)) from exc

    return KpiExplanation(
        tile_id=tile_id,
        measure=tile.measures[0],
        explanation=explanation,
    )
