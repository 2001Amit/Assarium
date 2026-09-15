from __future__ import annotations

import json

import pytest

from app.ai import chat as chat_module
from app.ai.chat import _chart_hint, _model_context, _to_query, chat
from app.ai.types import ChatMessage
from app.engine.duckdb_engine import DuckDBEngine
from app.semantic.types import Attribute, Entity, Join, Measure, MetricQuery, SemanticModel


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    instance = DuckDBEngine(tmp_path_factory.mktemp("chat-warehouse"))
    instance.ensure_layers()
    # A real table so a compiled query can actually run.
    instance.execute_ddl(
        "CREATE OR REPLACE TABLE silver.main.orders AS "
        "SELECT * FROM (VALUES "
        "  ('o1','c1',DATE '2026-01-05',100.00,'shipped'),"
        "  ('o2','c1',DATE '2026-01-20',250.50,'shipped'),"
        "  ('o3','c2',DATE '2026-02-11', 75.25,'pending')"
        ") AS t(order_id, customer_id, order_date, amount, status)"
    )
    instance.execute_ddl(
        "CREATE OR REPLACE TABLE silver.main.customers AS "
        "SELECT * FROM (VALUES ('c1','India'), ('c2','Brazil')) AS t(customer_id, country)"
    )
    yield instance
    instance.close()


@pytest.fixture
def model() -> SemanticModel:
    orders = Entity(
        id="orders", name="orders", label="Orders", layer="silver", table="orders",
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
                      logical_type="string", role="dimension", cardinality=2),
        ],
    )
    customers = Entity(
        id="customers", name="customers", label="Customers", layer="silver",
        table="customers",
        attributes=[
            Attribute(name="customer_id", label="Customer ID", column="customer_id",
                      logical_type="string", role="key"),
            Attribute(name="country", label="Country", column="country",
                      logical_type="string", role="dimension", cardinality=2),
        ],
    )
    return SemanticModel(
        connection_id="c1",
        entities=[orders, customers],
        measures=[
            Measure(id="orders.total_amount", name="total_amount", label="Total amount",
                    entity_id="orders", aggregation="sum", column="amount",
                    format="currency", decimals=2),
            Measure(id="orders.record_count", name="record_count", label="Order count",
                    entity_id="orders", aggregation="count"),
            Measure(id="customers.record_count", name="record_count",
                    label="Customer count", entity_id="customers", aggregation="count"),
        ],
        joins=[
            Join(id="j1", from_entity="orders", from_column="customer_id",
                 to_entity="customers", to_column="customer_id",
                 cardinality="many_to_one"),
        ],
    )


class FakeProvider:
    """Stands in for the LLM so the guardrails can be tested without credentials."""

    def __init__(self, replies: list[dict]):
        self.replies = [json.dumps(r) for r in replies]
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages, **_kwargs) -> str:
        self.calls.append(messages)
        return self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]


@pytest.fixture
def fake(monkeypatch):
    def install(replies: list[dict]) -> FakeProvider:
        stub = FakeProvider(replies)
        monkeypatch.setattr(chat_module.provider, "complete", stub.complete)
        return stub

    return install


class TestModelContext:
    """The prompt must speak in ids, because ids are what the compiler accepts."""

    def test_measure_ids_are_present(self, model):
        context = _model_context(model)
        assert "orders.total_amount" in context
        assert "orders.record_count" in context

    def test_dimension_references_are_fully_qualified(self, model):
        context = _model_context(model)
        assert "orders.status" in context
        assert "customers.country" in context

    def test_join_direction_is_stated(self, model):
        assert "orders -> customers" in _model_context(model)

    def test_personal_data_is_flagged(self, model):
        model.entities[1].attributes[1].contains_pii = True
        assert "[personal data]" in _model_context(model)


class TestGuardrails:
    def test_a_hallucinated_measure_is_refused_not_answered(self, model, engine, fake):
        """The compiler is the authority; a made-up measure never reaches the warehouse."""
        stub = fake([
            {"action": "query", "measures": ["orders.total_profit"]},
            {"action": "query", "measures": ["orders.total_profit"]},
        ])
        response = chat(model, engine, [ChatMessage(role="user", content="total profit?")])
        assert response.query_result is None
        assert "no measure called" in response.message.content
        # It was given one chance to correct itself before giving up.
        assert len(stub.calls) == 2

    def test_a_refusal_is_fed_back_for_one_repair(self, model, engine, fake):
        stub = fake([
            {"action": "query", "measures": ["orders.revenue"]},
            {"action": "query", "measures": ["orders.total_amount"],
             "intent": "Total amount"},
        ])
        response = chat(model, engine, [ChatMessage(role="user", content="revenue?")])
        assert response.query_result is not None
        assert response.query_result.rows[0][0] is not None
        # The second call carried the compiler's reason.
        repair = stub.calls[1][-1]["content"]
        assert "rejected" in repair and "no measure called" in repair

    def test_fan_out_stays_refused_through_the_chat(self, model, engine, fake):
        stub = fake([
            {"action": "query", "measures": ["customers.record_count"],
             "dimensions": ["orders.status"]},
        ])
        response = chat(model, engine, [ChatMessage(role="user", content="customers by status")])
        assert response.query_result is None
        assert "inflate the totals" in response.message.content
        assert len(stub.calls) == 2

    def test_a_non_data_question_is_answered_directly(self, model, engine, fake):
        fake([{"action": "answer", "text": "I work over your semantic model."}])
        response = chat(model, engine, [ChatMessage(role="user", content="what are you?")])
        assert response.query_result is None
        assert response.message.content == "I work over your semantic model."

    def test_non_json_output_is_passed_through_not_crashed(self, model, engine, monkeypatch):
        monkeypatch.setattr(chat_module.provider, "complete", lambda *a, **k: "plain text")
        response = chat(model, engine, [ChatMessage(role="user", content="hi")])
        assert response.message.content == "plain text"


class TestExecution:
    def test_a_valid_query_runs_and_returns_rows(self, model, engine, fake):
        fake([{
            "action": "query",
            "measures": ["orders.total_amount"],
            "dimensions": ["orders.status"],
            "intent": "Total amount by status",
        }])
        response = chat(model, engine, [ChatMessage(role="user", content="amount by status")])
        assert response.query_result is not None
        assert response.query_result.row_count == 2
        assert "SELECT" in response.query_result.sql
        assert response.query_result.chart_hint == "bar"

    def test_a_joined_query_runs(self, model, engine, fake):
        fake([{
            "action": "query",
            "measures": ["orders.total_amount"],
            "dimensions": ["customers.country"],
        }])
        response = chat(model, engine, [ChatMessage(role="user", content="amount by country")])
        assert response.query_result is not None
        assert response.query_result.row_count == 2

    def test_the_narrative_reports_the_actual_leading_row(self, model, engine, fake):
        fake([{
            "action": "query",
            "measures": ["orders.total_amount"],
            "dimensions": ["orders.status"],
        }])
        response = chat(model, engine, [ChatMessage(role="user", content="by status")])
        # shipped is 350.50 against pending's 75.25, so it must lead.
        assert "shipped" in response.message.content
        assert "350.50" in response.message.content

    def test_limits_are_clamped_however_the_model_asks(self):
        query = _to_query({"measures": ["m"], "limit": 100_000})
        assert query.limit == 200


class TestChartHint:
    def test_a_bare_aggregate_is_a_stat_not_a_one_bar_chart(self):
        assert _chart_hint(MetricQuery(measures=["m"]), 1) == "stat"

    def test_a_timeline_is_a_line(self):
        query = MetricQuery(measures=["m"], time_dimension="e.d", time_grain="month")
        assert _chart_hint(query, 12) == "line"

    def test_many_categories_fall_back_to_a_table(self):
        query = MetricQuery(measures=["m"], dimensions=["e.a"])
        assert _chart_hint(query, 80) == "table"

    def test_no_rows_draws_nothing(self):
        assert _chart_hint(MetricQuery(measures=["m"], dimensions=["e.a"]), 0) == "none"
