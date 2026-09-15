"""
Column-level lineage, built from the SQL the pipeline actually ran.

Two questions, one graph, opposite directions:

- **Provenance.** "This number on my dashboard — where did it come from?" Walk upstream
  from a measure to the gold column, the silver column, the bronze column, and the source
  object the bytes arrived in.
- **Impact.** "The source is renaming this column — what breaks?" Walk downstream and get
  the list, before the change lands rather than after somebody notices a dashboard is
  empty.

The lineage is derived from `PipelineStep.sql`, which the pipeline already records for
every step precisely so that a result can be traced back to its derivation. That matters:
this is lineage of what *ran*, not of what a model file *says* should run. A hand-edited
step, a schema that drifted, a transform that silently changed — all of them show up here,
because the SQL is the evidence.

Where a step's SQL cannot be parsed, the edge is recorded as `unresolved` with the reason
rather than dropped. A lineage graph that quietly omits what it could not understand is
worse than one that says so: it looks complete, and the missing part is exactly the part
somebody needed.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.lineage import lineage as sqlglot_lineage
from sqlglot.optimizer.qualify import qualify

logger = logging.getLogger("assarium.lineage")

#: Where a node sits. Ordered from raw to consumed, which is the order a reader expects
#: to see a provenance chain laid out.
NodeKind = str
KIND_ORDER = ("source", "bronze", "silver", "gold", "measure", "tile")


@dataclass(frozen=True)
class ColumnRef:
    """One column, somewhere. `table` is layer-qualified so bronze and silver differ."""

    kind: NodeKind
    table: str
    column: str

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.table}.{self.column}"

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.table}.{self.column}"


@dataclass
class Edge:
    """`source` feeds `target`. `via` names the step, so a reader can go and look."""

    source: ColumnRef
    target: ColumnRef
    via: str
    transform: str | None = None
    #: Set when the step's SQL could not be parsed. The edge is still recorded, because
    #: an omission would look like "nothing depends on this".
    unresolved_reason: str | None = None


@dataclass
class LineageGraph:
    edges: list[Edge] = field(default_factory=list)
    #: Steps whose SQL did not parse, so the UI can say how complete this graph is
    #: instead of implying it is complete.
    unparsed: list[dict[str, str]] = field(default_factory=list)

    def _index(self, forward: bool) -> dict[str, list[Edge]]:
        index: dict[str, list[Edge]] = defaultdict(list)
        for edge in self.edges:
            key = edge.source.id if forward else edge.target.id
            index[key].append(edge)
        return index

    def upstream(self, ref: ColumnRef, limit: int = 500) -> list[Edge]:
        """Everything this column was derived from, transitively."""
        return self._walk(ref, forward=False, limit=limit)

    def downstream(self, ref: ColumnRef, limit: int = 500) -> list[Edge]:
        """Everything that would be affected if this column changed."""
        return self._walk(ref, forward=True, limit=limit)

    def _walk(self, start: ColumnRef, *, forward: bool, limit: int) -> list[Edge]:
        index = self._index(forward)
        seen: set[str] = {start.id}
        found: list[Edge] = []
        queue: deque[str] = deque([start.id])

        while queue and len(found) < limit:
            current = queue.popleft()
            for edge in index.get(current, []):
                if edge in found:
                    continue
                found.append(edge)
                next_ref = edge.target if forward else edge.source
                # A cycle is not expected in a medallion pipeline, but a self-referencing
                # MERGE would make one, and an unbounded walk would hang the request.
                if next_ref.id not in seen:
                    seen.add(next_ref.id)
                    queue.append(next_ref.id)
        return found

    def columns(self) -> set[ColumnRef]:
        refs: set[ColumnRef] = set()
        for edge in self.edges:
            refs.add(edge.source)
            refs.add(edge.target)
        return refs


# ---------------------------------------------------------------------------------------
# Building the graph from executed SQL
# ---------------------------------------------------------------------------------------


def bare_table(name: str) -> str:
    """
    `silver.main."customers_csv"` -> `customers_csv`.

    References arrive fully qualified and quoted, and the layer is carried separately on
    the node - so keeping the prefix would file the same table under two different ids
    depending on how the SQL happened to spell it.
    """
    return (name or "").split(".")[-1].strip('"').strip("`").strip("[]")


def layer_of(name: str) -> NodeKind | None:
    """The medallion layer a qualified reference names, when it names one."""
    head = (name or "").split(".")[0].strip('"').lower()
    return head if head in ("bronze", "silver", "gold") else None


def _kind_for_layer(layer: str) -> NodeKind:
    return layer if layer in ("bronze", "silver", "gold") else "source"


def _upstream_layer(kind: NodeKind) -> NodeKind:
    return {"bronze": "source", "silver": "bronze", "gold": "silver"}.get(kind, "source")


def edges_from_step(
    *,
    sql: str,
    target_table: str,
    target_layer: str,
    step_id: str,
    dialect: str = "duckdb",
    schema: dict[str, Any] | None = None,
) -> tuple[list[Edge], str | None]:
    """
    Column edges for one pipeline step. Returns (edges, unparsed reason).

    `sqlglot.lineage` is asked one column at a time because that is the shape of its API,
    and its answer is a tree whose leaves are the source columns. Only the leaves are kept
    here: intermediate CTE nodes are an artefact of how the SQL was written, and a reader
    asking "where did this come from" wants the table it came from, not the alias it
    passed through on the way.

    `schema` is not optional in practice. The silver step this platform emits reads
    through three nested `SELECT *` subqueries - the quality-check wrapper - and a `*`
    says nothing about where its columns came from unless the real column list is
    supplied. Without it every edge resolves to `*` and the graph is decorative.
    """
    target_kind = _kind_for_layer(target_layer)
    source_kind = _upstream_layer(target_kind)

    try:
        statement = sqlglot.parse_one(sql, dialect=dialect)
    except Exception as exc:  # noqa: BLE001 - any parse failure is the same outcome
        return [], f"could not parse the step's SQL: {str(exc).splitlines()[0][:160]}"

    select = statement.find(exp.Select)
    if select is None:
        return [], "the step is not a SELECT, so it has no column derivations to read"

    # `SELECT *` has no column list to trace - unless the table's real columns are known,
    # in which case qualifying expands the star into them. Worth doing rather than
    # refusing: a passthrough step is exactly the kind that gets written as `SELECT *`,
    # and it is still part of the chain.
    if schema and any(isinstance(e, exp.Star) for e in select.expressions):
        try:
            statement = qualify(
                statement,
                schema=schema,
                dialect=dialect,
                # A column the schema does not know about must not abort the whole
                # expansion - the rest of the step is still traceable.
                validate_qualify_columns=False,
            )
            select = statement.find(exp.Select) or select
        except Exception as exc:  # noqa: BLE001
            logger.debug("Qualifying %s failed: %s", target_table, exc)

    output_columns = [
        projection.alias_or_name
        for projection in select.expressions
        if projection.alias_or_name and projection.alias_or_name != "*"
    ]
    if not output_columns:
        return [], (
            "the step selects * and the source table's columns are not known, so they "
            "cannot be resolved"
        )

    # The transform belongs to the projection, not to the leaf. A leaf node's expression
    # is the table it was read from, which is how the first version of this ended up
    # labelling every edge with `FROM bronze.main.orders_raw`.
    transforms = {
        projection.alias_or_name: _render_transform(projection)
        for projection in select.expressions
        if projection.alias_or_name
    }

    edges: list[Edge] = []
    for column in output_columns:
        try:
            node = sqlglot_lineage(column, statement, dialect=dialect, schema=schema)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Lineage for %s.%s failed: %s", target_table, column, exc)
            continue

        target = ColumnRef(kind=target_kind, table=bare_table(target_table), column=column)
        for leaf in _leaves(node):
            qualified, source_column = _split(leaf)
            if not source_column or source_column == "*":
                continue
            edges.append(
                Edge(
                    source=ColumnRef(
                        kind=layer_of(qualified or "") or source_kind,
                        table=bare_table(qualified or target_table),
                        column=source_column,
                    ),
                    target=target,
                    via=step_id,
                    transform=transforms.get(column),
                )
            )
    return edges, None


def _leaves(node: Any) -> list[Any]:
    """The bottom of the lineage tree: real columns on real tables."""
    if not getattr(node, "downstream", None):
        return [node]
    out: list[Any] = []
    for child in node.downstream:
        out.extend(_leaves(child))
    return out


def _split(node: Any) -> tuple[str | None, str | None]:
    """
    (qualified table, column) for a leaf.

    The qualified form is kept rather than reduced here, because the caller needs the
    prefix to work out which layer the reference names - `bronze.main.orders` and
    `silver.main.orders` are different tables with the same bare name.
    """
    name = getattr(node, "name", "") or ""
    if "." in name:
        table, _, column = name.rpartition(".")
        return table, column
    return (getattr(node, "source_name", "") or None), (name or None)


def _render_transform(projection: exp.Expression) -> str | None:
    """
    What was done to the column on its way through, when anything was.

    A plain passthrough returns None: labelling `status -> status` with "status" tells the
    reader nothing they cannot see. A cast, a trim, a currency strip or a CASE is worth
    showing, because it is the difference between "the value travelled unchanged" and
    "the value was changed here, and this is how".
    """
    inner = projection.this if isinstance(projection, exp.Alias) else projection
    if isinstance(inner, exp.Column):
        return None
    try:
        rendered = inner.sql()
    except Exception:  # noqa: BLE001
        return None
    return rendered if len(rendered) <= 240 else rendered[:237] + "..."
