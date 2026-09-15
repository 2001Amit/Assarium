"""
Slowly-changing dimension loading.

The two failure modes worth guarding: losing history when a row changes, and inventing
history when it did not. A load that re-runs cleanly is what makes retries safe.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from app.core.errors import QueryError
from app.engine.duckdb_engine import DuckDBEngine

SCHEMA = pa.schema([
    ("lease_id", pa.string()),
    ("tenant", pa.string()),
    ("monthly_rent", pa.float64()),
])


def batches(rows: list[dict]):
    return iter([pa.RecordBatch.from_pylist(rows, schema=SCHEMA)])


@pytest.fixture
def engine(tmp_path):
    instance = DuckDBEngine(tmp_path)
    instance.ensure_layers()
    yield instance
    instance.close()


BASE = [
    {"lease_id": "L1", "tenant": "Acme", "monthly_rent": 50000.0},
    {"lease_id": "L2", "tenant": "Globex", "monthly_rent": 42000.0},
]


def load(engine, rows, table="lease_dim"):
    return engine.merge_scd2("silver", table, batches(rows), SCHEMA, keys=["lease_id"])


def rows_for(engine, lease_id, table="lease_dim"):
    return engine.execute(
        f"SELECT monthly_rent, _is_current FROM silver.main.{table} "
        f"WHERE lease_id = '{lease_id}' ORDER BY _valid_from"
    ).rows


class TestHistoryIsPreserved:
    def test_a_changed_row_keeps_its_previous_version(self, engine):
        load(engine, BASE)
        load(engine, [{**BASE[0], "monthly_rent": 55000.0}, BASE[1]])
        versions = rows_for(engine, "L1")
        assert len(versions) == 2
        assert [v[0] for v in versions] == [50000.0, 55000.0]

    def test_exactly_one_version_is_current(self, engine):
        load(engine, BASE)
        load(engine, [{**BASE[0], "monthly_rent": 55000.0}, BASE[1]])
        current = [v for v in rows_for(engine, "L1") if v[1]]
        assert len(current) == 1
        assert current[0][0] == 55000.0

    def test_the_superseded_version_is_closed_off(self, engine):
        load(engine, BASE)
        load(engine, [{**BASE[0], "monthly_rent": 55000.0}, BASE[1]])
        closed = engine.execute(
            "SELECT count(*) FROM silver.main.lease_dim "
            "WHERE lease_id = 'L1' AND _valid_to IS NOT NULL"
        ).rows[0][0]
        assert closed == 1

    def test_a_past_value_remains_answerable(self, engine):
        """The question SCD2 exists for: what was it before the source changed?"""
        load(engine, BASE)
        load(engine, [{**BASE[0], "monthly_rent": 55000.0}, BASE[1]])
        previous = engine.execute(
            "SELECT monthly_rent FROM silver.main.lease_dim "
            "WHERE lease_id = 'L1' AND NOT _is_current"
        ).rows
        assert previous == [[50000.0]]

    def test_a_new_key_is_simply_inserted(self, engine):
        load(engine, BASE)
        stats = load(engine, [*BASE, {"lease_id": "L3", "tenant": "New", "monthly_rent": 1.0}])
        assert stats["inserted"] == 1
        assert len(rows_for(engine, "L3")) == 1


class TestNoInventedHistory:
    def test_replaying_the_same_load_changes_nothing(self, engine):
        """A retry after a crash must not fabricate a second version of every row."""
        load(engine, BASE)
        before = engine.row_count("silver", "lease_dim")
        stats = load(engine, BASE)
        assert engine.row_count("silver", "lease_dim") == before
        assert stats["inserted"] == 0
        assert stats["expired"] == 0

    def test_an_unchanged_row_is_left_alone_when_a_sibling_changes(self, engine):
        load(engine, BASE)
        load(engine, [{**BASE[0], "monthly_rent": 55000.0}, BASE[1]])
        assert len(rows_for(engine, "L2")) == 1, "L2 did not change and must not be versioned"

    def test_three_identical_loads_leave_one_version_each(self, engine):
        for _ in range(3):
            load(engine, BASE)
        assert engine.row_count("silver", "lease_dim") == 2


class TestChangeDetection:
    def test_a_null_moving_between_columns_is_detected(self, engine):
        """
        ("a", NULL) and (NULL, "a") must not hash alike. Without a null sentinel the
        concatenation collapses and a real change is missed.
        """
        rows_a = [{"lease_id": "L1", "tenant": "a", "monthly_rent": None}]
        rows_b = [{"lease_id": "L1", "tenant": None, "monthly_rent": None}]
        load(engine, rows_a, table="nulls")
        stats = engine.merge_scd2(
            "silver", "nulls", batches(rows_b), SCHEMA, keys=["lease_id"]
        )
        assert stats["expired"] == 1, "the change from 'a' to NULL must be seen"

    def test_only_tracked_columns_trigger_a_new_version(self, engine):
        """A change to a column outside the tracked set is not a new version."""
        tracked = ["monthly_rent"]
        engine.merge_scd2("silver", "tracked", batches(BASE), SCHEMA,
                          keys=["lease_id"], tracked=tracked)
        changed = [{**BASE[0], "tenant": "Acme Renamed"}, BASE[1]]
        stats = engine.merge_scd2("silver", "tracked", batches(changed), SCHEMA,
                                  keys=["lease_id"], tracked=tracked)
        assert stats["expired"] == 0

    def test_changing_the_tracked_set_is_reported_not_silent(self, engine):
        """
        Hashes built over different column sets can never match, so every row is
        re-versioned. That is sometimes right, but it must never happen quietly - a
        dimension silently doubling is very hard to notice after the fact.
        """
        engine.merge_scd2("silver", "drift", batches(BASE), SCHEMA,
                          keys=["lease_id"], tracked=["monthly_rent"])
        stats = engine.merge_scd2("silver", "drift", batches(BASE), SCHEMA,
                                  keys=["lease_id"], tracked=["monthly_rent", "tenant"])
        assert "tracked_columns_changed" in stats
        assert "added tenant" in stats["tracked_columns_changed"]

    def test_column_order_does_not_change_the_hash(self, engine):
        engine.merge_scd2("silver", "order", batches(BASE), SCHEMA,
                          keys=["lease_id"], tracked=["tenant", "monthly_rent"])
        stats = engine.merge_scd2("silver", "order", batches(BASE), SCHEMA,
                                  keys=["lease_id"], tracked=["monthly_rent", "tenant"])
        assert stats["expired"] == 0
        assert "tracked_columns_changed" not in stats


class TestGuards:
    def test_scd2_without_a_key_is_refused(self, engine):
        with pytest.raises(QueryError) as caught:
            engine.merge_scd2("silver", "no_key", batches(BASE), SCHEMA, keys=[])
        assert "without a key" in str(caught.value)

    def test_staging_tables_do_not_survive(self, engine):
        load(engine, BASE)
        load(engine, BASE)
        assert [t for t in engine.list_tables("silver") if t.startswith("_scd_")] == []

    def test_the_first_load_creates_the_history_columns(self, engine):
        load(engine, BASE)
        columns = {c for c, _ in engine.describe("silver", "lease_dim")}
        assert {"_valid_from", "_valid_to", "_is_current", "_change_hash"} <= columns
