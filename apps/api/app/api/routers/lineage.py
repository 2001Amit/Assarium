"""
Provenance and impact, over the SQL the pipeline actually ran.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.auth.deps import get_scope, get_tenant_context
from app.core.errors import ValidationError
from app.lineage.graph import ColumnRef
from app.lineage.service import build_for_connection, impact, provenance
from app.models.entities import Connection
from app.tenancy.scope import TenantScope

# Every route below resolves a tenant before the handler runs. Declared once here
# rather than per endpoint, so a new endpoint is guarded by default instead of by
# whoever remembers. `assert_routes_are_guarded` at startup proves none slipped past.
router = APIRouter(
    prefix="/api",
    tags=["lineage"],
    dependencies=[Depends(get_tenant_context)],
)

KINDS = ("source", "bronze", "silver", "gold", "measure", "tile")


def _ref(kind: str, table: str, column: str) -> ColumnRef:
    if kind not in KINDS:
        raise ValidationError(
            f"'{kind}' is not a lineage node kind. Use one of: {', '.join(KINDS)}."
        )
    return ColumnRef(kind=kind, table=table, column=column)


@router.get("/connections/{connection_id}/lineage")
def connection_lineage(
    connection_id: str, scope: TenantScope = Depends(get_scope)
) -> dict[str, Any]:
    """The whole graph, for drawing."""
    scope.get(Connection, connection_id)
    graph = build_for_connection(scope, connection_id)
    return {
        "edges": [
            {
                "source": {"kind": e.source.kind, "table": e.source.table,
                           "column": e.source.column, "id": e.source.id},
                "target": {"kind": e.target.kind, "table": e.target.table,
                           "column": e.target.column, "id": e.target.id},
                "via": e.via,
                "transform": e.transform,
            }
            for e in graph.edges
        ],
        "coverage": {
            "complete": not graph.unparsed,
            "unresolved_steps": graph.unparsed,
            "edge_count": len(graph.edges),
        },
    }


@router.get("/connections/{connection_id}/lineage/provenance")
def column_provenance(
    connection_id: str,
    kind: str = Query(...),
    table: str = Query(...),
    column: str = Query(...),
    scope: TenantScope = Depends(get_scope),
) -> dict[str, Any]:
    """Where one column or measure came from."""
    scope.get(Connection, connection_id)
    graph = build_for_connection(scope, connection_id)
    return provenance(graph, _ref(kind, table, column))


@router.get("/connections/{connection_id}/lineage/impact")
def column_impact(
    connection_id: str,
    kind: str = Query(...),
    table: str = Query(...),
    column: str = Query(...),
    scope: TenantScope = Depends(get_scope),
) -> dict[str, Any]:
    """
    What would be affected if this column changed.

    The question worth asking *before* a source rename lands, rather than after somebody
    notices a dashboard has gone blank.
    """
    scope.get(Connection, connection_id)
    graph = build_for_connection(scope, connection_id)
    return impact(graph, _ref(kind, table, column))
