"""
Assembling one lineage graph for a connection, and answering questions with it.

The pipeline's SQL gives the warehouse half — source to bronze to silver to gold. The
semantic model and the dashboards give the half above it — column to measure to tile. Both
halves are needed for the questions people actually ask, which never stop at the warehouse
boundary: "where does this dashboard number come from" and "if this column changes, which
dashboards break".
"""

from __future__ import annotations

import logging
from typing import Any

from app.dashboards.types import Dashboard
from app.engine.base import safe_identifier
from app.lineage.graph import (
    ColumnRef,
    Edge,
    LineageGraph,
    bare_table,
    edges_from_step,
)
from app.models.entities import (
    DashboardRecord,
    Dataset,
    PipelineRun,
    PipelineStep,
    SemanticModelRecord,
)
from app.semantic.types import SemanticModel
from app.tenancy.scope import TenantScope

logger = logging.getLogger("assarium.lineage")

#: What a measure node is grouped under. The connection id was the obvious thing to reach
#: for and the wrong thing to show - a reader tracing a number does not need to read a
#: uuid, and every measure shares it anyway so it groups nothing.
MEASURE_NAMESPACE = "semantic model"

#: Steps that transform data. An ingest step has no SQL to read - the derivation there is
#: "the connector read the source" - so it contributes a source edge rather than a parse.
TRANSFORM_KINDS = ("silver", "gold")


def build_for_connection(scope: TenantScope, connection_id: str) -> LineageGraph:
    """
    The lineage graph for one connection, from its most recent successful run.

    The *most recent successful* run, not the most recent: a failed run's steps describe
    SQL that did not produce the tables anybody is looking at, and lineage drawn from it
    would explain a state that does not exist.
    """
    graph = LineageGraph()

    run = scope.one_where(
        PipelineRun,
        PipelineRun.connection_id == connection_id,
        PipelineRun.status == "succeeded",
    )
    if run is None:
        graph.unparsed.append({
            "step": "-",
            "reason": "This connection has no successful pipeline run yet, so there is "
                      "nothing to trace. Run the pipeline first.",
        })
        return graph

    # Built once and reused: without it, lineage cannot see through the nested
    # `SELECT *` wrappers the quality step emits, and every edge resolves to `*`.
    schema = _schema_for(scope, connection_id)

    steps = scope.all(PipelineStep, PipelineStep.run_id == run.id)
    for step in sorted(steps, key=lambda s: s.sequence):
        if step.status != "succeeded" or not step.target_table:
            continue

        if step.kind == "ingest":
            datasets = _datasets_by_name(scope, connection_id)
            # No SQL to parse: the derivation is "the connector read this object". The
            # edge still belongs in the graph, because a provenance chain that stops at
            # bronze answers the question only halfway.
            graph.edges.extend(_ingest_edges(step, datasets.get(step.dataset_name)))
            continue

        if not step.sql:
            # A step with no SQL is usually a deliberate passthrough - an already
            # aggregated table carried into gold, where nothing was transformed because
            # nothing needed to be. Those get identity edges, not a coverage warning:
            # reporting a designed no-op as an unreadable step both loses the edges and
            # cries wolf about how complete the graph is.
            dataset = _datasets_by_name(scope, connection_id).get(step.dataset_name)
            passthrough = _passthrough_edges(step, dataset)
            if passthrough:
                graph.edges.extend(passthrough)
            else:
                graph.unparsed.append({
                    "step": step.dataset_name,
                    "reason": "The step recorded no SQL and its columns are unknown, so "
                              "its derivations cannot be traced.",
                })
            continue

        edges, reason = edges_from_step(
            sql=step.sql,
            target_table=step.target_table,
            target_layer=step.layer,
            step_id=step.id,
            schema=schema,
        )
        graph.edges.extend(edges)
        if reason:
            graph.unparsed.append({"step": step.dataset_name, "reason": reason})

    graph.edges.extend(_semantic_edges(scope, connection_id))
    graph.edges.extend(_dashboard_edges(scope, connection_id))
    return graph


def _datasets_by_name(scope: TenantScope, connection_id: str) -> dict[str, Any]:
    return {
        dataset.name: dataset
        for dataset in scope.all(Dataset, Dataset.connection_id == connection_id)
    }


def _schema_for(scope: TenantScope, connection_id: str) -> dict[str, Any]:
    """
    The column list for every table the pipeline reads, in the shape sqlglot wants.

    Taken from the dataset's profiled schema rather than from the warehouse, so lineage
    can be answered without opening an engine connection - and so it still answers after
    a table has been dropped, which is exactly when somebody is asking what used to
    depend on it.
    """
    schema: dict[str, Any] = {}
    for dataset in scope.all(Dataset, Dataset.connection_id == connection_id):
        columns = {
            column["name"]: column.get("native_type", "VARCHAR")
            for column in (dataset.column_schema or [])
            if isinstance(column, dict) and column.get("name")
        }
        if not columns:
            continue
        table = safe_identifier(dataset.name)
        # Registered under every layer: the same table name exists in bronze and silver
        # with the same columns, and which one a step reads is decided by its SQL.
        for layer in ("bronze", "silver", "gold"):
            schema.setdefault(layer, {}).setdefault("main", {})[table] = columns
    return schema


def _ingest_edges(step: PipelineStep, dataset: Any | None = None) -> list[Edge]:
    """
    Source object to bronze table, one edge per bronze column.

    Column names are carried through an ingest unchanged - that is what makes bronze
    bronze - so the mapping is the identity, and saying so explicitly is more useful than
    leaving a gap where the source used to be.

    The columns come from the dataset's profiled schema. `step.actions` holds prose for
    the run timeline ("Trimmed whitespace in 4 columns"), and reading it as a column list
    produced a graph whose source nodes were sentences.
    """
    columns = [
        column["name"]
        for column in ((dataset.column_schema if dataset else None) or [])
        if isinstance(column, dict) and column.get("name")
    ]
    if not columns:
        return [
            Edge(
                source=ColumnRef("source", step.dataset_name, "*"),
                target=ColumnRef(
                    "bronze", bare_table(step.target_table or step.dataset_name), "*"
                ),
                via=step.id,
                transform="landed unchanged",
            )
        ]
    target_table = bare_table(step.target_table or step.dataset_name)
    return [
        Edge(
            source=ColumnRef("source", step.dataset_name, column),
            target=ColumnRef("bronze", target_table, safe_identifier(column)),
            via=step.id,
            transform="landed unchanged",
        )
        for column in columns
    ]


def _passthrough_edges(step: PipelineStep, dataset: Any | None) -> list[Edge]:
    """
    Identity edges for a step that moved a table up a layer without changing it.

    The column names are the same on both sides - that is what "carried through
    unchanged" means - so the mapping is the identity and the transform is named rather
    than left blank, so a reader can see the no-op was intended.
    """
    columns = [
        column["name"]
        for column in ((dataset.column_schema if dataset else None) or [])
        if isinstance(column, dict) and column.get("name")
    ]
    if not columns or step.layer not in ("silver", "gold"):
        return []

    upstream = {"silver": "bronze", "gold": "silver"}[step.layer]
    table = bare_table(step.target_table or step.dataset_name)
    return [
        Edge(
            source=ColumnRef(upstream, table, safe_identifier(column)),
            target=ColumnRef(step.layer, table, safe_identifier(column)),
            via=step.id,
            transform="carried through unchanged",
        )
        for column in columns
    ]


def _semantic_edges(scope: TenantScope, connection_id: str) -> list[Edge]:
    """Warehouse column to governed measure."""
    record = scope.one_where(
        SemanticModelRecord, SemanticModelRecord.connection_id == connection_id
    )
    if record is None:
        return []

    try:
        model = SemanticModel.model_validate(record.document)
    except Exception:  # noqa: BLE001 - a malformed model must not take lineage down
        logger.warning("Semantic model for %s did not validate", connection_id)
        return []

    edges: list[Edge] = []
    for measure in model.measures:
        entity = model.entity(measure.entity_id)
        if entity is None:
            continue
        # A count has no column of its own: it counts rows, so its provenance is the
        # table rather than any one field. Saying "*" is honest; picking a column
        # arbitrarily would not be.
        column = measure.column or "*"
        edges.append(
            Edge(
                source=ColumnRef(entity.layer, bare_table(entity.table), column),
                target=ColumnRef("measure", MEASURE_NAMESPACE, measure.id),
                via="semantic-model",
                transform=f"{measure.aggregation}({column})",
            )
        )
    return edges


def _dashboard_edges(scope: TenantScope, connection_id: str) -> list[Edge]:
    """Measure to the tiles that display it."""
    edges: list[Edge] = []
    records = scope.all(DashboardRecord, DashboardRecord.connection_id == connection_id)

    for record in records:
        try:
            dashboard = Dashboard.model_validate(record.document)
        except Exception:  # noqa: BLE001
            continue
        for tile in dashboard.tiles:
            for measure_id in tile.measures:
                edges.append(
                    Edge(
                        source=ColumnRef("measure", MEASURE_NAMESPACE, measure_id),
                        target=ColumnRef("tile", record.name, tile.title or tile.id),
                        via=record.id,
                        transform=tile.type,
                    )
                )
    return edges


# ---------------------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------------------


def _as_dict(edge: Edge) -> dict[str, Any]:
    return {
        "source": {"kind": edge.source.kind, "table": edge.source.table,
                   "column": edge.source.column, "id": edge.source.id},
        "target": {"kind": edge.target.kind, "table": edge.target.table,
                   "column": edge.target.column, "id": edge.target.id},
        "via": edge.via,
        "transform": edge.transform,
    }


def provenance(graph: LineageGraph, ref: ColumnRef) -> dict[str, Any]:
    """Where this came from, in the order a reader wants it: raw first, derived last."""
    edges = graph.upstream(ref)
    return {
        "of": {"kind": ref.kind, "table": ref.table, "column": ref.column, "id": ref.id},
        "direction": "upstream",
        "edges": [_as_dict(edge) for edge in edges],
        "origins": sorted(
            {edge.source.id for edge in edges if edge.source.kind == "source"}
        ),
        "coverage": _coverage(graph),
    }


def impact(graph: LineageGraph, ref: ColumnRef) -> dict[str, Any]:
    """
    What breaks if this changes.

    Dashboards and measures are called out separately from intermediate tables, because
    they are the part somebody has to be told about. A silver column changing is a fact
    about the pipeline; a dashboard going blank is a fact about somebody's morning.
    """
    edges = graph.downstream(ref)
    affected = [edge.target for edge in edges]
    return {
        "of": {"kind": ref.kind, "table": ref.table, "column": ref.column, "id": ref.id},
        "direction": "downstream",
        "edges": [_as_dict(edge) for edge in edges],
        "tiles": sorted({t.table + " · " + t.column for t in affected if t.kind == "tile"}),
        "measures": sorted({t.column for t in affected if t.kind == "measure"}),
        "tables": sorted(
            {f"{t.kind}.{t.table}" for t in affected if t.kind in ("bronze", "silver", "gold")}
        ),
        "coverage": _coverage(graph),
    }


def _coverage(graph: LineageGraph) -> dict[str, Any]:
    """
    How much of the pipeline this graph actually explains.

    Reported on every answer rather than hidden, because a lineage graph that silently
    omits what it could not parse is worse than one that says so - it looks complete, and
    the missing part is exactly the part somebody needed.
    """
    return {
        "complete": not graph.unparsed,
        "unresolved_steps": graph.unparsed,
        "edge_count": len(graph.edges),
    }
