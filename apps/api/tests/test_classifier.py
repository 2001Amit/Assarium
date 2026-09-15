from __future__ import annotations

import pytest

from app.connectors.types import ColumnSchema, DatasetSchema
from app.medallion.classifier import classify_layer
from app.profiling.types import ColumnProfile, DatasetProfile


def column(
    name: str,
    logical_type: str = "string",
    *,
    null_pct: float = 0.0,
    distinct_pct: float | None = 0.5,
    unique: bool = False,
    shadow: str | None = None,
    blanks: int = 0,
    distinct_count: int | None = None,
) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        native_type=logical_type,
        logical_type=logical_type,
        position=0,
        null_pct=null_pct,
        null_count=int(null_pct * 100),
        distinct_pct=distinct_pct,
        distinct_count=distinct_count,
        is_unique=unique,
        shadow_type=shadow,
        shadow_ratio=1.0 if shadow else 0.0,
        blank_count=blanks,
    )


def build(name: str, columns: list[ColumnProfile], **profile_kwargs):
    schema = DatasetSchema(
        path=[name],
        name=name,
        columns=[
            ColumnSchema(name=c.name, native_type=c.native_type, logical_type=c.logical_type)
            for c in columns
        ],
    )
    profile = DatasetProfile(
        row_count=1000,
        sampled_rows=1000,
        columns=columns,
        key_candidates=[[c.name] for c in columns if c.is_unique],
        **profile_kwargs,
    )
    return classify_layer(name, schema, profile)


CLEAN_ROW_LEVEL = [
    column("customer_id", "string", unique=True, distinct_pct=1.0),
    column("customer_name", "string"),
    column("country", "string", distinct_count=5, distinct_pct=0.005),
    column("segment", "string", distinct_count=4, distinct_pct=0.004),
    column("signup_date", "date"),
]

RAW_EXTRACT = [
    column("order_id", "string", distinct_pct=0.9),
    column("amount", "string", shadow="float"),
    column("order_date", "string", shadow="date"),
    column("notes", "string", null_pct=0.7, blanks=12),
    column("ingested_at", "timestamp"),
]

AGGREGATE = [
    column("region", "string", distinct_count=5, distinct_pct=0.005),
    column("month", "date", distinct_count=12, distinct_pct=0.012),
    column("total_revenue", "float", distinct_pct=1.0, unique=True),
    column("order_count", "integer"),
    column("avg_order_value", "float"),
]


class TestNamingCannotDecide:
    """A table's name is a label someone chose. Only its contents settle the layer."""

    def test_gold_name_does_not_promote_row_level_data(self):
        verdict = build("gold_customer_mart", CLEAN_ROW_LEVEL)
        assert verdict.layer == "silver"

    def test_bronze_name_does_not_demote_clean_data(self):
        verdict = build("raw_customers", CLEAN_ROW_LEVEL)
        assert verdict.layer == "silver"

    def test_gold_name_does_not_promote_raw_data(self):
        verdict = build("dim_customer", RAW_EXTRACT)
        assert verdict.layer == "bronze"

    def test_naming_signal_is_reported_but_capped(self):
        verdict = build("gold_customer_mart", CLEAN_ROW_LEVEL)
        naming = next(s for s in verdict.signals if s.id == "naming")
        data_signals = [s for s in verdict.signals if s.id != "naming"]
        assert naming.weight <= max(s.weight for s in data_signals)


class TestLayerDetection:
    def test_clean_keyed_table_is_silver(self):
        assert build("customers", CLEAN_ROW_LEVEL).layer == "silver"

    def test_untyped_duplicated_extract_is_bronze(self):
        verdict = build("orders", RAW_EXTRACT, duplicate_row_ratio=0.08)
        assert verdict.layer == "bronze"
        assert {s.id for s in verdict.signals} >= {"type_purity", "duplicates"}

    def test_aggregated_table_is_gold(self):
        assert build("sales_by_region", AGGREGATE).layer == "gold"

    def test_aggregate_that_is_still_dirty_stays_bronze(self):
        """Pre-computed measures do not make raw data business-ready."""
        dirty_aggregate = AGGREGATE + [
            column("total_cost", "string", shadow="float"),
            column("loaded_at", "timestamp"),
            column("stale", "string", null_pct=0.8, blanks=30),
        ]
        verdict = build("revenue_summary", dirty_aggregate, duplicate_row_ratio=0.09)
        assert verdict.layer == "bronze"


class TestVerdictQuality:
    def test_every_signal_carries_a_readable_observation(self):
        verdict = build("orders", RAW_EXTRACT, duplicate_row_ratio=0.08)
        for signal in verdict.signals:
            first = signal.observation[0]
            assert signal.observation and (first.isupper() or first.isdigit())
            assert signal.label

    def test_confidence_is_bounded(self):
        for name, columns in [
            ("customers", CLEAN_ROW_LEVEL),
            ("orders", RAW_EXTRACT),
            ("sales", AGGREGATE),
        ]:
            verdict = build(name, columns)
            assert 0.0 <= verdict.confidence <= 0.98

    def test_no_evidence_falls_back_to_bronze_with_low_confidence(self):
        verdict = build("mystery", [])
        assert verdict.layer == "bronze"
        assert verdict.confidence <= 0.3

    @pytest.mark.parametrize("layer", ["bronze", "silver", "gold"])
    def test_every_layer_has_a_next_action(self, layer):
        columns = {"bronze": RAW_EXTRACT, "silver": CLEAN_ROW_LEVEL, "gold": AGGREGATE}[layer]
        names = {"bronze": "orders", "silver": "customers", "gold": "sales"}
        verdict = build(names[layer], columns)
        assert verdict.layer == layer
        assert verdict.recommended_action
