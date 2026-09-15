from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.deps import get_scope, get_tenant_context
from app.core.errors import NotFoundError, ValidationError
from app.engine.factory import get_engine
from app.models.entities import (
    Connection,
    Dataset,
    DatasetRelationship,
    SemanticModelRecord,
)
from app.ontology.graph import build_graph
from app.semantic.builder import build_model
from app.semantic.compiler import QueryCompiler
from app.semantic.types import Filter, MetricQuery, SemanticModel
from app.tenancy.scope import TenantScope

# Every route below resolves a tenant before the handler runs. Declared once here
# rather than per endpoint, so a new endpoint is guarded by default instead of by
# whoever remembers. `assert_routes_are_guarded` at startup proves none slipped past.
router = APIRouter(
    prefix="/api", tags=["semantic"],
    dependencies=[Depends(get_tenant_context)],
)


class BuildRequest(BaseModel):
    # Guards against silently discarding hand-authored labels and measures.
    overwrite_edits: bool = False


class QueryResponse(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    elapsed_ms: int
    truncated: bool
    sql: str
    dimension_columns: list[str]
    measure_columns: list[str]
    notes: list[str] = []


def _connection(scope: TenantScope, connection_id: str) -> Connection:
    TenantScope.assert_is_scope(scope, "_connection")
    connection = scope.get(Connection, connection_id)
    if connection is None:
        raise NotFoundError("That connection does not exist.")
    return connection


def _record(scope: TenantScope, connection_id: str) -> SemanticModelRecord:
    TenantScope.assert_is_scope(scope, "_record")
    record = scope.db.execute(
        scope.select(SemanticModelRecord).where(SemanticModelRecord.connection_id == connection_id)
    ).scalar_one_or_none()
    if record is None:
        raise NotFoundError(
            "No semantic model has been built for this connection yet.",
        )
    return record


def _model(scope: TenantScope, connection_id: str) -> SemanticModel:
    TenantScope.assert_is_scope(scope, "_model")
    return SemanticModel.model_validate(_record(scope, connection_id).document)


# --------------------------------------------------------------------------------------
# model lifecycle
# --------------------------------------------------------------------------------------


@router.post("/connections/{connection_id}/semantic/build", response_model=SemanticModel)
def build(
    connection_id: str, request: BuildRequest, scope: TenantScope = Depends(get_scope)
) -> SemanticModel:
    """Derive the semantic model from the silver layer, profiles and relationships."""
    _connection(scope, connection_id)

    existing = scope.db.execute(
        scope.select(SemanticModelRecord).where(SemanticModelRecord.connection_id == connection_id)
    ).scalar_one_or_none()
    if existing is not None and existing.edited and not request.overwrite_edits:
        raise ValidationError(
            "This model has hand-authored changes. Rebuilding replaces every label and "
            "measure definition. Confirm to overwrite them.",
            details={"requires": "overwrite_edits"},
        )

    datasets = [
        {"name": d.name, "profile": d.profile}
        for d in scope.db.execute(
            scope.select(Dataset).where(Dataset.connection_id == connection_id)
        ).scalars()
    ]
    names = {
        d.id: d.name
        for d in scope.db.execute(
            scope.select(Dataset).where(Dataset.connection_id == connection_id)
        ).scalars()
    }
    relationships = [
        {
            "from_dataset": names.get(r.from_dataset_id, ""),
            "from_column": r.from_column,
            "to_dataset": names.get(r.to_dataset_id, ""),
            "to_column": r.to_column,
            "cardinality": r.cardinality,
            "confidence": r.confidence,
            "kind": r.kind,
            "accepted": r.accepted,
        }
        for r in scope.db.execute(
            scope.select(DatasetRelationship).where(
                DatasetRelationship.connection_id == connection_id
            )
        ).scalars()
    ]

    model = build_model(connection_id, get_engine(scope.tenant_id), datasets, relationships)
    document = model.model_dump(mode="json")

    if existing is None:
        scope.create(
            SemanticModelRecord,
            connection_id=connection_id, document=document, edited=False,
        )
    else:
        existing.document = document
        existing.edited = False
    scope.db.commit()
    return model


@router.get("/connections/{connection_id}/semantic", response_model=SemanticModel)
def get_model(connection_id: str, scope: TenantScope = Depends(get_scope)) -> SemanticModel:
    return _model(scope, connection_id)


@router.put("/connections/{connection_id}/semantic", response_model=SemanticModel)
def update_model(
    connection_id: str, model: SemanticModel, scope: TenantScope = Depends(get_scope)
) -> SemanticModel:
    """Save a hand-edited model. Marked as edited so a rebuild has to ask first."""
    record = _record(scope, connection_id)
    model.connection_id = connection_id
    record.document = model.model_dump(mode="json")
    record.edited = True
    scope.db.commit()
    return model


# --------------------------------------------------------------------------------------
# querying
# --------------------------------------------------------------------------------------


@router.post("/connections/{connection_id}/semantic/query", response_model=QueryResponse)
def run_query(
    connection_id: str, query: MetricQuery, scope: TenantScope = Depends(get_scope)
) -> QueryResponse:
    """
    Answer a metric question.

    Compilation happens against the model, so an unknown measure or an unsafe join is
    rejected before anything reaches the warehouse.
    """
    model = _model(scope, connection_id)
    engine = get_engine(scope.tenant_id)
    compiled = QueryCompiler(
        model, engine, permissions=scope.context.permissions
    ).compile(query)
    result = engine.execute(compiled.sql, params=compiled.params)

    return QueryResponse(
        columns=result.columns,
        rows=result.rows,
        row_count=result.row_count,
        elapsed_ms=result.elapsed_ms,
        truncated=result.truncated,
        sql=compiled.sql,
        dimension_columns=compiled.dimension_columns,
        measure_columns=compiled.measure_columns,
        notes=compiled.notes,
    )


@router.post("/connections/{connection_id}/semantic/explain")
def explain_query(
    connection_id: str, query: MetricQuery, scope: TenantScope = Depends(get_scope)
) -> dict[str, Any]:
    """Compile without executing, so a caller can show the SQL before running it."""
    model = _model(scope, connection_id)
    compiled = QueryCompiler(
        model, get_engine(scope.tenant_id), permissions=scope.context.permissions
    ).compile(query)
    return compiled.model_dump(mode="json")


# --------------------------------------------------------------------------------------
# ontology
# --------------------------------------------------------------------------------------


@router.get("/connections/{connection_id}/ontology")
def ontology(connection_id: str, scope: TenantScope = Depends(get_scope)) -> dict[str, Any]:
    """The model as a graph: entities, how they join, and what can be measured where."""
    return build_graph(_model(scope, connection_id)).model_dump(mode="json")


class ValuesRequest(BaseModel):
    field: str
    limit: int = 200
    search: str | None = None


@router.post("/connections/{connection_id}/semantic/values")
def dimension_values(
    connection_id: str, request: ValuesRequest, scope: TenantScope = Depends(get_scope)
) -> dict[str, Any]:
    """Distinct values of one dimension, for populating a filter control."""
    model = _model(scope, connection_id)
    engine = get_engine(scope.tenant_id)
    compiler = QueryCompiler(model, engine, permissions=scope.context.permissions)
    entity, attribute = compiler.resolve_field(request.field)

    filters = []
    if request.search:
        filters.append(
            Filter(field=request.field, operator="contains", values=[request.search])
        )

    # Route through the compiler so the same identifier rules and binding apply here as
    # anywhere else; a filter control is not a reason to hand-write SQL.
    compiled = compiler.compile(
        MetricQuery(
            measures=[_any_measure(model, entity.id)],
            dimensions=[request.field],
            filters=filters,
            limit=min(request.limit, 1000),
        )
    )
    result = engine.execute(compiled.sql, params=compiled.params)
    return {
        "field": request.field,
        "label": attribute.label,
        "values": [row[0] for row in result.rows],
        "truncated": result.truncated,
    }


def _any_measure(model: SemanticModel, entity_id: str) -> str:
    """
    A grouped query needs a measure even when only the labels are wanted.

    The entity's own count is used where it exists; otherwise any measure reachable
    from it, so a dimension on a pure lookup table can still populate a filter.
    """
    own = [m for m in model.measures if m.entity_id == entity_id]
    if own:
        return next((m.id for m in own if m.aggregation == "count"), own[0].id)
    if model.measures:
        return model.measures[0].id
    raise ValidationError("This model defines no measures, so it cannot be queried yet.")
