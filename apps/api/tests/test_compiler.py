from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.engine.duckdb_engine import DuckDBEngine
from app.semantic.compiler import QueryCompiler, SemanticError
from app.semantic.types import (
    Attribute,
    Entity,
    Filter,
    Join,
    Measure,
    MetricQuery,
    OrderBy,
    SemanticModel,
)


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    instance = DuckDBEngine(tmp_path_factory.mktemp("warehouse"))
    yield instance
    instance.close()


@pytest.fixture
def model() -> SemanticModel:
    orders = Entity(
        id="orders",
        name="orders",
        label="Orders",
        layer="silver",
        table="orders",
        is_fact=True,
        attributes=[
            Attribute(name="order_id", label="Order ID", column="order_id",
                      logical_type="string", role="key"),
            Attribute(name="customer_id", label="Customer ID", column="customer_id",
                      logical_type="string", role="foreign_key"),
            Attribute(name="order_date", label="Order date", column="order_date",
                      logical_type="date", role="time"),
            Attribute(name="amount", label="Amount", column="amount",
                      logical_type="decimal", role="attribute"),
            Attribute(name="status", label="Status", column="status",
                      logical_type="string", role="dimension"),
        ],
    )
    customers = Entity(
        id="customers",
        name="customers",
        label="Customers",
        layer="silver",
        table="customers",
        attributes=[
            Attribute(name="customer_id", label="Customer ID", column="customer_id",
                      logical_type="string", role="key"),
            Attribute(name="country", label="Country", column="country",
                      logical_type="string", role="dimension"),
            Attribute(name="email", label="Email", column="email",
                      logical_type="string", role="dimension", contains_pii=True),
        ],
    )
    return SemanticModel(
        connection_id="c1",
        entities=[orders, customers],
        measures=[
            Measure(id="orders.total_amount", name="total_amount", label="Total amount",
                    entity_id="orders", aggregation="sum", column="amount"),
            Measure(id="orders.record_count", name="record_count", label="Order count",
                    entity_id="orders", aggregation="count"),
            Measure(id="customers.record_count", name="record_count", label="Customer count",
                    entity_id="customers", aggregation="count"),
        ],
        joins=[
            Join(id="j1", from_entity="orders", from_column="customer_id",
                 to_entity="customers", to_column="customer_id", cardinality="many_to_one"),
        ],
    )


def compile_query(model, engine, **kwargs):
    return QueryCompiler(model, engine).compile(MetricQuery(**kwargs))


class TestRefusals:
    """A semantic layer earns its keep by failing loudly instead of answering wrongly."""

    def test_unknown_measure_names_what_exists(self, model, engine):
        with pytest.raises(SemanticError) as caught:
            compile_query(model, engine, measures=["orders.total_profit"])
        assert "no measure called" in str(caught.value)
        assert "orders.total_amount" in str(caught.value)

    def test_unknown_attribute_names_what_exists(self, model, engine):
        with pytest.raises(SemanticError) as caught:
            compile_query(model, engine, measures=["orders.record_count"],
                          dimensions=["customers.region"])
        assert "has no attribute 'region'" in str(caught.value)
        assert "country" in str(caught.value)

    def test_measures_from_two_entities_are_refused(self, model, engine):
        with pytest.raises(SemanticError) as caught:
            compile_query(model, engine,
                          measures=["orders.total_amount", "customers.record_count"])
        assert "double-counting" in str(caught.value)

    def test_fan_out_is_refused(self, model, engine):
        """Counting customers by an order attribute would repeat each customer row."""
        with pytest.raises(SemanticError) as caught:
            compile_query(model, engine, measures=["customers.record_count"],
                          dimensions=["orders.status"])
        assert "inflate the totals" in str(caught.value)

    def test_no_measure_is_refused(self, model, engine):
        with pytest.raises(SemanticError):
            compile_query(model, engine, measures=[], dimensions=["orders.status"])

    def test_malformed_field_reference_is_refused(self, model, engine):
        with pytest.raises(SemanticError) as caught:
            compile_query(model, engine, measures=["orders.record_count"],
                          dimensions=["status"])
        assert "entity.attribute" in str(caught.value)

    def test_ordering_by_something_not_selected_is_refused(self, model, engine):
        with pytest.raises(SemanticError) as caught:
            compile_query(model, engine, measures=["orders.record_count"],
                          order_by=[OrderBy(field="orders.total_amount")])
        assert "not one of the columns" in str(caught.value)

    def test_unknown_filter_operator_never_reaches_sql(self, model, engine):
        """
        Operators are an allowlist twice over.

        The request schema rejects anything outside it, so a caller cannot smuggle an
        operator through the API; the compiler checks again for callers constructed
        in-process. Either way nothing unrecognised reaches the SQL builder.
        """
        with pytest.raises(ValidationError):
            Filter(field="orders.status", operator="regex", values=["x"])

        # Bypass the schema the way an internal caller could, and check the second gate.
        smuggled = Filter(field="orders.status", operator="eq", values=["x"])
        object.__setattr__(smuggled, "operator", "regex")
        with pytest.raises(SemanticError) as caught:
            compile_query(model, engine, measures=["orders.record_count"], filters=[smuggled])
        assert "not a supported filter" in str(caught.value)

    def test_filter_with_wrong_value_count_is_refused(self, model, engine):
        with pytest.raises(SemanticError) as caught:
            compile_query(
                model, engine,
                measures=["orders.record_count"],
                filters=[Filter(field="orders.amount", operator="between", values=[5])],
            )
        assert "needs 2 value" in str(caught.value)


class TestCompilation:
    def test_joins_only_from_many_to_one(self, model, engine):
        compiled = compile_query(model, engine, measures=["orders.total_amount"],
                                 dimensions=["customers.country"])
        assert "LEFT JOIN" in compiled.sql
        assert compiled.base_entity == "orders"
        assert compiled.joined_entities == ["customers"]

    def test_base_entity_takes_the_first_alias(self, model, engine):
        compiled = compile_query(model, engine, measures=["orders.total_amount"],
                                 dimensions=["customers.country"])
        assert 'AS e0' in compiled.sql.split("LEFT JOIN")[0]

    def test_filter_values_are_bound_not_interpolated(self, model, engine):
        compiled = compile_query(
            model, engine,
            measures=["orders.record_count"],
            filters=[Filter(field="orders.status", operator="in",
                            values=["shipped", "pending"])],
        )
        assert "IN (?, ?)" in compiled.sql
        assert compiled.params == ["shipped", "pending"]
        assert "shipped" not in compiled.sql

    def test_injection_attempt_stays_a_parameter(self, model, engine):
        """A hostile filter value is data, never SQL, however it is written."""
        payload = "'; DROP TABLE orders; --"
        compiled = compile_query(
            model, engine,
            measures=["orders.record_count"],
            filters=[Filter(field="orders.status", operator="eq", values=[payload])],
        )
        assert "DROP TABLE" not in compiled.sql
        assert compiled.params == [payload]

    def test_time_grain_is_applied(self, model, engine):
        compiled = compile_query(model, engine, measures=["orders.total_amount"],
                                 time_dimension="orders.order_date", time_grain="quarter")
        assert "DATE_TRUNC('quarter'" in compiled.sql
        assert compiled.dimension_columns == ["quarter_of_order_date"]

    def test_pii_grouping_is_flagged(self, model, engine):
        compiled = compile_query(model, engine, measures=["orders.record_count"],
                                 dimensions=["customers.email"])
        assert any("personal data" in note for note in compiled.notes)

    def test_limit_is_bounded(self, model, engine):
        compiled = compile_query(model, engine, measures=["orders.record_count"], limit=10**9)
        assert "LIMIT 50000" in compiled.sql

    def test_clashing_dimension_names_are_prefixed(self, model, engine):
        """Both entities have customer_id; the output columns must stay distinguishable."""
        compiled = compile_query(model, engine, measures=["orders.total_amount"],
                                 dimensions=["customers.customer_id"])
        assert compiled.dimension_columns == ["customers_customer_id"]

    def test_defaults_to_ordering_by_the_first_measure(self, model, engine):
        compiled = compile_query(model, engine, measures=["orders.total_amount"],
                                 dimensions=["orders.status"])
        assert 'ORDER BY "total_amount" DESC' in compiled.sql
