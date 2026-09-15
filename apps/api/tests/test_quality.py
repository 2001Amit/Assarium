"""
Data quality and quarantine.

The behaviour being replaced here was `TRY_CAST`, which turned an unparseable value into
NULL and lost the fact that anything was wrong. These tests exist to make sure that never
comes back: a bad row must end up somewhere a person can find it, with the reason.
"""

from __future__ import annotations

import pytest

from app.engine.duckdb_engine import DuckDBEngine
from app.profiling.types import ColumnProfile, DatasetProfile, TopValue
from app.quality import sql as qsql
from app.quality.rules import QualityPolicy, Rule, derive_policy


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    instance = DuckDBEngine(tmp_path_factory.mktemp("dq"))
    instance.ensure_layers()
    yield instance
    instance.close()


@pytest.fixture
def raw(engine):
    engine.execute_ddl("""
        CREATE OR REPLACE TABLE bronze.main.raw AS SELECT * FROM (VALUES
          ('ORD-1', 'shipped',   '120.50'),
          ('ORD-2', 'pending',   'not-a-number'),
          ('ORD-3', NULL,        '80.00'),
          (NULL,    'shipped',   '60.00'),
          ('ORD-5', 'teleported','90.00'),
          ('ORD-6', 'shipped',   NULL)
        ) AS t(order_id, status, amount)
    """)
    return "SELECT * FROM bronze.main.raw"


def policy() -> QualityPolicy:
    return QualityPolicy(rules=[
        Rule(id="k", kind="not_null", column="order_id", severity="reject", reason="key"),
        Rule(id="t", kind="type", column="amount", severity="reject",
             params={"to": "decimal"}, reason="money"),
        Rule(id="a", kind="allowed_values", column="status", severity="warn",
             params={"values": ["shipped", "pending", "cancelled"]}, reason="domain"),
    ])


class TestBadRowsAreKeptNotDropped:
    def test_an_unconvertible_value_is_quarantined_not_nulled(self, engine, raw):
        """The whole point: 'not-a-number' must not silently become NULL."""
        built = qsql.build(engine, raw, policy(), run_id="r1")
        quarantined = engine.execute(built.quarantine).rows
        offending = [r for r in quarantined if r[0] == "ORD-2"]
        assert offending, "the row with an unparseable amount must be quarantined"
        assert "type:amount" in offending[0][3]

    def test_a_missing_key_is_quarantined(self, engine, raw):
        built = qsql.build(engine, raw, policy(), run_id="r1")
        rows = engine.execute(built.quarantine).rows
        assert any(r[0] is None and "not_null:order_id" in r[3] for r in rows)

    def test_clean_and_quarantine_together_account_for_every_row(self, engine, raw):
        built = qsql.build(engine, raw, policy(), run_id="r1")
        total = engine.execute("SELECT count(*) FROM bronze.main.raw").rows[0][0]
        clean = len(engine.execute(built.clean).rows)
        bad = len(engine.execute(built.quarantine).rows)
        assert clean + bad == total, "no row may vanish between the two outputs"

    def test_the_quarantine_row_carries_the_run_that_produced_it(self, engine, raw):
        built = qsql.build(engine, raw, policy(), run_id="run-abc")
        columns = engine.execute(built.quarantine).columns
        rows = engine.execute(built.quarantine).rows
        assert "_dq_run_id" in columns
        assert all(r[columns.index("_dq_run_id")] == "run-abc" for r in rows)

    def test_a_row_breaking_two_rules_reports_both(self, engine):
        engine.execute_ddl("""
            CREATE OR REPLACE TABLE bronze.main.double AS
            SELECT * FROM (VALUES (NULL, 'x', 'nope')) AS t(order_id, status, amount)
        """)
        built = qsql.build(engine, "SELECT * FROM bronze.main.double", policy(), "r")
        rule = engine.execute(built.quarantine).rows[0][3]
        assert "not_null:order_id" in rule and "type:amount" in rule

    def test_a_null_is_not_also_reported_as_a_type_failure(self, engine, raw):
        """ORD-6 has a NULL amount. That is missing, not malformed."""
        built = qsql.build(engine, raw, policy(), run_id="r1")
        clean_ids = [r[0] for r in engine.execute(built.clean).rows]
        assert "ORD-6" in clean_ids


class TestWarningsAreSurfaced:
    def test_a_warn_rule_does_not_reject_the_row(self, engine, raw):
        built = qsql.build(engine, raw, policy(), run_id="r1")
        clean_ids = [r[0] for r in engine.execute(built.clean).rows]
        assert "ORD-5" in clean_ids, "an unexpected status should warn, not reject"

    def test_warnings_are_counted_rather_than_discarded(self, engine, raw):
        built = qsql.build(engine, raw, policy(), run_id="r1")
        assert built.warning_summary is not None
        counts = engine.execute(built.warning_summary).rows
        assert ("allowed_values:status", 1) in [(r[0], r[1]) for r in counts]

    def test_the_clean_table_is_not_polluted_with_quality_columns(self, engine, raw):
        built = qsql.build(engine, raw, policy(), run_id="r1")
        columns = engine.execute(built.clean).columns
        assert not any(c.startswith("_dq") for c in columns)


class TestThresholds:
    def test_a_load_failing_too_many_rows_is_stopped(self):
        breach = QualityPolicy(max_reject_ratio=0.05).breached(1000, 200)
        assert breach and "20.0%" in breach and "stopped" in breach

    def test_a_load_within_tolerance_proceeds(self):
        assert QualityPolicy(max_reject_ratio=0.05).breached(1000, 10) is None

    def test_a_tiny_table_is_not_judged_on_ratio(self):
        """One bad row in five is 20%, but five rows is not a sample worth failing on."""
        assert QualityPolicy(max_reject_ratio=0.05).breached(5, 1) is None

    def test_no_rejections_never_breaches(self):
        assert QualityPolicy(max_reject_ratio=0.0).breached(1000, 0) is None


class TestDerivedRules:
    def column(self, name, **kwargs) -> ColumnProfile:
        defaults = dict(native_type="VARCHAR", logical_type="string", position=0)
        defaults.update(kwargs)
        return ColumnProfile(name=name, **defaults)

    def test_a_key_column_gets_a_rejecting_not_null_rule(self):
        profile = DatasetProfile(sampled_rows=500, columns=[self.column("order_id")])
        derived = derive_policy(profile, key_columns=["order_id"])
        rule = next(r for r in derived.rules if r.column == "order_id")
        assert rule.kind == "not_null" and rule.severity == "reject"

    def test_a_shadow_typed_column_gets_a_rejecting_type_rule(self):
        profile = DatasetProfile(sampled_rows=500, columns=[
            self.column("amount", shadow_type="decimal", shadow_ratio=0.99),
        ])
        rule = next(r for r in derive_policy(profile).rules if r.kind == "type")
        assert rule.severity == "reject" and rule.params["to"] == "decimal"

    def test_an_already_sparse_column_gets_no_not_null_rule(self):
        """Rejecting a third of the table on the first run teaches people to ignore quarantine."""
        profile = DatasetProfile(sampled_rows=500, columns=[
            self.column("notes", null_pct=0.3),
        ])
        assert not any(r.kind == "not_null" for r in derive_policy(profile).rules)

    def test_a_complete_column_only_warns(self):
        profile = DatasetProfile(sampled_rows=500, columns=[self.column("channel", null_pct=0.0)])
        rule = next(r for r in derive_policy(profile).rules if r.kind == "not_null")
        assert rule.severity == "warn"

    def test_a_small_domain_becomes_a_warn_rule(self):
        profile = DatasetProfile(sampled_rows=500, columns=[
            self.column("status", null_pct=0.0, distinct_count=2, top_values=[
                TopValue(value="shipped", count=400, pct=0.8),
                TopValue(value="pending", count=100, pct=0.2),
            ]),
        ])
        rule = next(r for r in derive_policy(profile).rules if r.kind == "allowed_values")
        assert rule.severity == "warn"
        assert set(rule.params["values"]) == {"shipped", "pending"}

    def test_every_derived_rule_explains_itself(self):
        profile = DatasetProfile(sampled_rows=500, columns=[
            self.column("order_id"),
            self.column("amount", shadow_type="decimal"),
        ])
        for rule in derive_policy(profile, key_columns=["order_id"]).rules:
            assert rule.reason, f"{rule.id} has no reason"


class TestRuleValuesAreEscaped:
    def test_a_quote_in_a_domain_value_cannot_break_the_sql(self, engine):
        """Allowed values come from the source's own data and can contain anything."""
        engine.execute_ddl("""
            CREATE OR REPLACE TABLE bronze.main.quoted AS
            SELECT * FROM (VALUES ('a', 'O''Brien'), ('b', 'x')) AS t(id, name)
        """)
        hostile = QualityPolicy(rules=[
            Rule(id="v", kind="allowed_values", column="name", severity="reject",
                 params={"values": ["O'Brien", "'; DROP TABLE bronze.main.quoted; --"]},
                 reason="hostile"),
        ])
        built = qsql.build(engine, "SELECT * FROM bronze.main.quoted", hostile, "r")
        clean = engine.execute(built.clean).rows
        assert [r[0] for r in clean] == ["a"]
        # The table is still there, so nothing was executed as SQL.
        assert engine.execute("SELECT count(*) FROM bronze.main.quoted").rows[0][0] == 2


class TestQualityRunsBeforeTheCast:
    """
    Regression: the rules must evaluate RAW bronze, not the transformed projection.

    This was wired the wrong way round once. Casting first turns an unconvertible value
    into NULL, so the type rule then inspects a clean column, quarantines nothing, and
    the row is silently corrupted - the exact behaviour quarantine exists to replace.
    """

    def build_case(self, engine):
        from app.medallion.transforms import plan_silver

        engine.execute_ddl("""
            CREATE OR REPLACE TABLE bronze.main.order_amounts AS SELECT * FROM (VALUES
              ('A', '120.50'), ('B', 'not-a-number'), ('C', '80.00')
            ) AS x(id, amount)
        """)
        profile = DatasetProfile(sampled_rows=200, columns=[
            ColumnProfile(name="id", native_type="VARCHAR", logical_type="string", position=0),
            ColumnProfile(name="amount", native_type="VARCHAR", logical_type="string",
                          position=1, shadow_type="decimal", shadow_ratio=1.0),
        ])
        derived = derive_policy(profile, key_columns=["id"])
        checked = qsql.build(engine, "SELECT * FROM bronze.main.order_amounts", derived, "r1")
        transform = plan_silver(engine, f"({checked.clean}) AS _clean", "t", profile)
        return checked, transform

    def test_the_unconvertible_row_is_caught(self, engine):
        checked, _ = self.build_case(engine)
        rows = engine.execute(checked.quarantine).rows
        assert len(rows) == 1
        assert rows[0][0] == "B"

    def test_the_quarantined_row_keeps_its_original_value(self, engine):
        """A NULL in quarantine tells you nothing. The offending value is the evidence."""
        checked, _ = self.build_case(engine)
        assert engine.execute(checked.quarantine).rows[0][1] == "not-a-number"

    def test_silver_contains_only_the_convertible_rows_properly_typed(self, engine):
        _, transform = self.build_case(engine)
        rows = engine.execute(transform.sql).rows
        assert [r[0] for r in rows] == ["A", "C"]
        assert all(r[1] is not None for r in rows)

    def test_the_wrong_order_would_have_been_caught_by_this(self, engine):
        """Casting before checking finds nothing wrong, which is the bug."""
        from app.medallion.transforms import plan_silver

        engine.execute_ddl("""
            CREATE OR REPLACE TABLE bronze.main.wrong_order AS SELECT * FROM (VALUES
              ('A', '120.50'), ('B', 'not-a-number')
            ) AS x(id, amount)
        """)
        profile = DatasetProfile(sampled_rows=200, columns=[
            ColumnProfile(name="id", native_type="VARCHAR", logical_type="string", position=0),
            ColumnProfile(name="amount", native_type="VARCHAR", logical_type="string",
                          position=1, shadow_type="decimal", shadow_ratio=1.0),
        ])
        transform = plan_silver(engine, "bronze.main.wrong_order", "t", profile)
        checked = qsql.build(engine, transform.sql, derive_policy(profile, ["id"]), "r1")
        assert len(engine.execute(checked.quarantine).rows) == 0, (
            "cast-then-check finds nothing; this is why the order is fixed"
        )


class TestRulesAreNoStricterThanTheTransform:
    """
    A rule that rejects a value the pipeline could have handled is a false positive, and
    a quarantine full of rows that were actually fine is one nobody reads.
    """

    @pytest.fixture
    def money(self, engine):
        engine.execute_ddl("""
            CREATE OR REPLACE TABLE bronze.main.money AS SELECT * FROM (VALUES
              ('A', '1200.00'),
              ('B', '1,850.00'),
              ('C', '$2,400.50'),
              ('D', 'N/A'),
              ('E', 'see addendum')
            ) AS t(id, amount)
        """)
        return "SELECT * FROM bronze.main.money"

    def rule_policy(self) -> QualityPolicy:
        return QualityPolicy(rules=[
            Rule(id="t", kind="type", column="amount", severity="reject",
                 params={"to": "decimal"}, reason="money"),
        ])

    def test_a_thousands_separator_is_not_a_data_error(self, engine, money):
        built = qsql.build(engine, money, self.rule_policy(), "r1")
        kept = [r[0] for r in engine.execute(built.clean).rows]
        assert "B" in kept, "1,850.00 converts once separators are stripped"

    def test_a_currency_symbol_is_not_a_data_error(self, engine, money):
        built = qsql.build(engine, money, self.rule_policy(), "r1")
        assert "C" in [r[0] for r in engine.execute(built.clean).rows]

    def test_genuinely_unconvertible_text_is_still_rejected(self, engine, money):
        built = qsql.build(engine, money, self.rule_policy(), "r1")
        rejected = {r[0] for r in engine.execute(built.quarantine).rows}
        assert rejected == {"D", "E"}
