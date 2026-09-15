"""
Column-level lineage and impact analysis.

The product's claim is that every number can be traced back to the bytes it came from.
That claim is only worth making if the trace is derived from what actually ran, so these
tests parse the same shape of SQL the pipeline emits, and check the two directions people
ask about: where did this come from, and what breaks if I change it.

The other thing tested here is honesty about gaps. A lineage graph that silently omits
what it could not parse is worse than one that says so, because it looks complete — and
the missing part is exactly the part somebody went looking for.
"""

from __future__ import annotations

import pytest

from app.lineage.graph import ColumnRef, Edge, LineageGraph, edges_from_step
from app.lineage.service import impact, provenance

SILVER_SQL = """
SELECT
  CAST(order_id AS VARCHAR) AS order_id,
  UPPER(TRIM(status)) AS status,
  CAST(REGEXP_REPLACE(CAST(amount AS VARCHAR), '[^0-9.-]', '', 'g') AS DECIMAL(18,2))
    AS amount_usd,
  created_at AS ordered_at,
  region
FROM bronze.main.orders_raw
"""

GOLD_SQL = """
SELECT
  region,
  DATE_TRUNC('month', ordered_at) AS month,
  SUM(amount_usd) AS total_amount_usd,
  COUNT(*) AS order_count
FROM silver.main.orders
GROUP BY 1, 2
"""


def build(sql: str, table: str, layer: str, step: str = "s1"):
    return edges_from_step(sql=sql, target_table=table, target_layer=layer, step_id=step)


class TestColumnMapping:
    def test_a_renamed_column_is_followed(self):
        """`created_at AS ordered_at` is where a naive table-level lineage stops helping."""
        edges, _ = build(SILVER_SQL, "orders", "silver")
        edge = next(e for e in edges if e.target.column == "ordered_at")
        assert edge.source.column == "created_at"
        assert edge.source.table == "orders_raw"

    def test_a_transformed_column_is_followed_through_its_expression(self):
        edges, _ = build(SILVER_SQL, "orders", "silver")
        edge = next(e for e in edges if e.target.column == "amount_usd")
        assert edge.source.column == "amount"

    def test_the_transform_is_recorded_so_the_change_is_visible(self):
        """
        The difference between "the value travelled unchanged" and "the value was changed
        here, and this is how".
        """
        edges, _ = build(SILVER_SQL, "orders", "silver")
        edge = next(e for e in edges if e.target.column == "amount_usd")
        assert edge.transform and "REGEXP_REPLACE" in edge.transform

    def test_a_passthrough_records_no_transform(self):
        """Labelling `region -> region` with "region" tells the reader nothing."""
        edges, _ = build(SILVER_SQL, "orders", "silver")
        edge = next(e for e in edges if e.target.column == "region")
        assert edge.transform is None

    def test_an_aggregate_traces_to_the_column_it_aggregates(self):
        edges, _ = build(GOLD_SQL, "orders_by_month", "gold")
        edge = next(e for e in edges if e.target.column == "total_amount_usd")
        assert edge.source.column == "amount_usd"
        assert edge.source.kind == "silver"

    def test_the_layer_above_is_inferred_from_the_target(self):
        silver, _ = build(SILVER_SQL, "orders", "silver")
        gold, _ = build(GOLD_SQL, "orders_by_month", "gold")
        assert all(e.source.kind == "bronze" for e in silver)
        assert all(e.source.kind == "silver" for e in gold)


class TestUnparseableSql:
    def test_a_broken_statement_returns_a_reason_rather_than_raising(self):
        edges, reason = build("this is not sql at all", "orders", "silver")
        assert edges == []
        assert reason and "could not parse" in reason

    def test_a_select_star_says_why_it_cannot_be_resolved(self):
        """
        `SELECT *` has no column list to trace. Saying so beats returning an empty graph
        that reads as "nothing depends on this".
        """
        edges, reason = build("SELECT * FROM bronze.main.orders_raw", "orders", "silver")
        assert edges == []
        assert reason and "cannot be resolved" in reason

    def test_a_non_select_statement_is_reported(self):
        edges, reason = build("DELETE FROM silver.main.orders", "orders", "silver")
        assert edges == []
        assert reason


class TestTraversal:
    @pytest.fixture
    def graph(self) -> LineageGraph:
        graph = LineageGraph()
        silver, _ = build(SILVER_SQL, "orders", "silver", step="silver-step")
        gold, _ = build(GOLD_SQL, "orders_by_month", "gold", step="gold-step")
        graph.edges.extend(silver)
        graph.edges.extend(gold)
        graph.edges.append(
            Edge(
                source=ColumnRef("source", "orders_raw.csv", "amount"),
                target=ColumnRef("bronze", "orders_raw", "amount"),
                via="ingest", transform="landed unchanged",
            )
        )
        graph.edges.append(
            Edge(
                source=ColumnRef("gold", "orders_by_month", "total_amount_usd"),
                target=ColumnRef("measure", "c1", "orders.total_amount_usd"),
                via="semantic-model", transform="sum(total_amount_usd)",
            )
        )
        graph.edges.append(
            Edge(
                source=ColumnRef("measure", "c1", "orders.total_amount_usd"),
                target=ColumnRef("tile", "Overview", "Total amount USD"),
                via="dash-1", transform="stat",
            )
        )
        return graph

    def test_a_dashboard_number_traces_all_the_way_to_the_source_file(self, graph):
        """The claim the product makes, asserted end to end."""
        result = provenance(graph, ColumnRef("tile", "Overview", "Total amount USD"))
        assert "source:orders_raw.csv.amount" in result["origins"]

    def test_the_chain_passes_through_every_layer(self, graph):
        result = provenance(graph, ColumnRef("tile", "Overview", "Total amount USD"))
        kinds = {e["source"]["kind"] for e in result["edges"]}
        assert {"measure", "gold", "silver", "bronze", "source"} <= kinds

    def test_changing_a_source_column_names_the_dashboard_it_breaks(self, graph):
        """
        The question worth asking before a rename lands, rather than after somebody
        notices a tile has gone blank.
        """
        result = impact(graph, ColumnRef("source", "orders_raw.csv", "amount"))
        assert "Overview · Total amount USD" in result["tiles"]
        assert "orders.total_amount_usd" in result["measures"]

    def test_impact_lists_the_intermediate_tables_too(self, graph):
        result = impact(graph, ColumnRef("source", "orders_raw.csv", "amount"))
        assert any("silver.orders" in t for t in result["tables"])
        assert any("gold.orders_by_month" in t for t in result["tables"])

    def test_an_unrelated_column_has_no_impact(self, graph):
        result = impact(graph, ColumnRef("source", "orders_raw.csv", "nothing_uses_this"))
        assert result["tiles"] == []
        assert result["edges"] == []

    def test_a_cycle_does_not_hang_the_walk(self):
        """A self-referencing MERGE would make one, and an unbounded walk hangs a request."""
        graph = LineageGraph(edges=[
            Edge(ColumnRef("silver", "a", "x"), ColumnRef("silver", "b", "x"), via="1"),
            Edge(ColumnRef("silver", "b", "x"), ColumnRef("silver", "a", "x"), via="2"),
        ])
        assert len(graph.downstream(ColumnRef("silver", "a", "x"))) == 2

    def test_the_walk_is_bounded(self):
        graph = LineageGraph(edges=[
            Edge(ColumnRef("silver", f"t{i}", "x"), ColumnRef("silver", f"t{i + 1}", "x"),
                 via=str(i))
            for i in range(50)
        ])
        assert len(graph.downstream(ColumnRef("silver", "t0", "x"), limit=10)) == 10


class TestCoverageHonesty:
    def test_a_complete_graph_says_so(self):
        graph = LineageGraph(edges=[
            Edge(ColumnRef("bronze", "a", "x"), ColumnRef("silver", "b", "x"), via="1")
        ])
        assert provenance(graph, ColumnRef("silver", "b", "x"))["coverage"]["complete"]

    def test_an_incomplete_graph_reports_which_steps_it_could_not_read(self):
        """
        The important one. A graph that omits what it could not parse looks complete, and
        the missing part is exactly the part somebody went looking for.
        """
        graph = LineageGraph(
            edges=[Edge(ColumnRef("bronze", "a", "x"), ColumnRef("silver", "b", "x"), via="1")],
            unparsed=[{"step": "weird_table", "reason": "the step selects *"}],
        )
        coverage = impact(graph, ColumnRef("bronze", "a", "x"))["coverage"]
        assert coverage["complete"] is False
        assert coverage["unresolved_steps"][0]["step"] == "weird_table"


# The SQL the silver step actually emits: the quality-check wrapper nests the real table
# three subqueries deep behind `SELECT *`. This is the shape that broke the first version.
REAL_SILVER_SQL = """
SELECT NULLIF(TRIM("customer_id"), '') AS "customer_id",
       NULLIF(TRIM("email"), '') AS "email",
       CURRENT_TIMESTAMP AS "_assarium_refined_at"
FROM (SELECT * FROM (SELECT * EXCLUDE ("_dq_rule")
      FROM (SELECT *, CONCAT_WS('; ', CASE WHEN "customer_id" IS NULL
                                      THEN 'not_null:customer_id' END) AS "_dq_rule"
            FROM bronze.main."customers_csv")))
"""

SCHEMA = {
    "bronze": {"main": {"customers_csv": {"customer_id": "VARCHAR", "email": "VARCHAR"}}},
    "silver": {"main": {"customers_csv": {"customer_id": "VARCHAR", "email": "VARCHAR"}}},
}


class TestNestedSelectStar:
    def test_a_bare_select_star_is_unresolvable_without_a_schema(self):
        """
        Why the schema is plumbed through at all. A `*` says nothing about where its
        columns came from, and a passthrough step is exactly the kind written that way.
        """
        edges, reason = build("SELECT * FROM bronze.main.customers_csv", "customers", "silver")
        assert edges == []
        assert reason and "not known" in reason

    def test_a_schema_lets_the_star_be_expanded(self):
        edges, reason = edges_from_step(
            sql="SELECT * FROM bronze.main.customers_csv",
            target_table="customers_csv", target_layer="silver", step_id="s1",
            schema=SCHEMA,
        )
        assert reason is None
        assert {e.target.column for e in edges} == {"customer_id", "email"}

    def test_with_a_schema_it_sees_through_all_three_wrappers(self):
        edges, reason = edges_from_step(
            sql=REAL_SILVER_SQL,
            target_table='silver.main."customers_csv"',
            target_layer="silver",
            step_id="s1",
            schema=SCHEMA,
        )
        assert reason is None
        by_target = {e.target.column: e for e in edges}
        assert by_target["customer_id"].source.table == "customers_csv"
        assert by_target["customer_id"].source.column == "customer_id"
        assert by_target["customer_id"].source.kind == "bronze"

    def test_the_qualified_target_name_is_reduced_to_the_bare_table(self):
        """
        `silver.main."customers_csv"` and `customers_csv` must be one node, not two -
        otherwise the chain breaks at every layer boundary.
        """
        edges, _ = edges_from_step(
            sql=REAL_SILVER_SQL,
            target_table='silver.main."customers_csv"',
            target_layer="silver",
            step_id="s1",
            schema=SCHEMA,
        )
        assert all(e.target.table == "customers_csv" for e in edges)

    def test_the_source_layer_comes_from_the_reference_not_an_assumption(self):
        """
        The step is a silver step reading bronze, but a gold step reading silver and a
        silver step reading silver are both legitimate - so the layer is read off the
        qualified name rather than assumed from the target.
        """
        edges, _ = edges_from_step(
            sql=REAL_SILVER_SQL,
            target_table='silver.main."customers_csv"',
            target_layer="silver",
            step_id="s1",
            schema=SCHEMA,
        )
        assert {e.source.kind for e in edges if e.source.column != "_assarium_refined_at"} == {
            "bronze"
        }
