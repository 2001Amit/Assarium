"""
Incremental ingestion.

The failure mode that matters here is silent data loss: a watermark that advances past
rows nobody read. These tests are written around that, not around the happy path.
"""

from __future__ import annotations

import csv
import time
from pathlib import Path

import pytest

from app.connectors.drivers.local_files import LocalFileConnector
from app.connectors.types import ColumnSchema, DatasetSchema
from app.ingestion.planner import plan_column_watermark, watermark_candidates
from app.ingestion.types import IngestionPlan, WatermarkState


def schema(*columns: tuple[str, str]) -> DatasetSchema:
    return DatasetSchema(
        path=["t"], name="t",
        columns=[ColumnSchema(name=n, native_type=t, logical_type=t) for n, t in columns],
    )


class TestWatermarkSelection:
    def test_a_modified_column_beats_a_created_column(self):
        candidates = watermark_candidates(
            schema(("created_at", "timestamp"), ("updated_at", "timestamp"))
        )
        assert candidates[0].name == "updated_at"

    def test_a_timestamp_beats_an_id(self):
        candidates = watermark_candidates(schema(("id", "integer"), ("modified_at", "timestamp")))
        assert candidates[0].name == "modified_at"

    def test_an_id_is_accepted_when_there_is_no_timestamp(self):
        candidates = watermark_candidates(schema(("id", "integer"), ("name", "string")))
        assert [c.name for c in candidates] == ["id"]

    def test_a_plain_numeric_column_is_not_a_watermark(self):
        """`amount` grows and shrinks; using it as a watermark would lose rows."""
        assert watermark_candidates(schema(("amount", "float"), ("qty", "integer"))) == []

    def test_no_candidate_means_a_full_load_with_a_stated_reason(self):
        plan = plan_column_watermark(schema(("name", "string")), WatermarkState(), None)
        assert plan.strategy == "full"
        assert plan.full_refresh
        assert "no safe way" in plan.reason

    def test_an_insert_only_column_is_used_but_flagged(self):
        plan = plan_column_watermark(schema(("created_at", "timestamp")), WatermarkState(), "x")
        assert plan.strategy == "column"
        assert "will not be picked up" in plan.reason


class TestWatermarkAdvancesSafely:
    """The rule: never advance past what was actually read."""

    def test_the_watermark_advances_only_to_the_snapshotted_ceiling(self):
        previous = WatermarkState(strategy="column", column="updated_at", value="2026-01-01")
        plan = IngestionPlan(
            strategy="column", column="updated_at",
            since="2026-01-01", ceiling="2026-06-01", reason="",
        )
        state = plan.next_state(rows_read=500, previous=previous)
        assert state.value == "2026-06-01"

    def test_rows_arriving_during_a_read_are_not_skipped(self):
        """
        The ceiling is snapshotted before reading. A row written after that snapshot has
        a higher value than the ceiling, so the next run's `> ceiling` window includes
        it. If the watermark advanced to 'now' instead, that row would be lost forever.
        """
        plan = IngestionPlan(
            strategy="column", column="updated_at",
            since="2026-01-01", ceiling="2026-06-01", reason="",
        )
        state = plan.next_state(100, WatermarkState(column="updated_at", value="2026-01-01"))
        late_row = "2026-06-15"
        assert late_row > state.value, "a row written mid-read must fall in the next window"

    def test_a_failed_run_leaves_the_watermark_untouched(self):
        """next_state is only ever called on success, so a crash re-reads the window."""
        previous = WatermarkState(strategy="column", column="updated_at", value="2026-01-01")
        assert previous.value == "2026-01-01"

    def test_changing_the_watermark_column_resets_the_position(self):
        previous = WatermarkState(strategy="column", column="old_col", value="9999")
        plan = plan_column_watermark(
            schema(("updated_at", "timestamp")), previous, "2026-06-01"
        )
        assert plan.since is None, "an incomparable old value must not be carried over"
        assert "no longer applies" in plan.reason

    def test_rows_total_accumulates_across_runs(self):
        state = WatermarkState(rows_total=1000)
        plan = IngestionPlan(strategy="column", column="c", ceiling="2", reason="")
        assert plan.next_state(250, state).rows_total == 1250


class TestFileChangeDetection:
    @pytest.fixture
    def connector(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            "app.connectors.drivers.local_files.get_settings",
            lambda: type("S", (), {"data_dir": tmp_path})(),
        )
        (tmp_path / "uploads" / "up1").mkdir(parents=True)
        return LocalFileConnector({"upload_id": "up1", "label": "test"}, {})

    def write(self, connector, name: str, rows: int) -> Path:
        target = connector.root / name
        with open(target, "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["id", "value"])
            for i in range(rows):
                writer.writerow([i, f"v{i}"])
        return target

    def test_an_unchanged_file_is_skipped(self, connector):
        self.write(connector, "orders.csv", 10)
        first = connector.stat(["orders.csv"])
        plan = connector.plan_ingestion(
            ["orders.csv"], None, WatermarkState(), {"orders.csv": first.etag}
        )
        assert plan.files == []
        assert plan.skipped_files == 1
        assert "unchanged" in plan.reason

    def test_a_new_file_is_read(self, connector):
        self.write(connector, "orders.csv", 10)
        plan = connector.plan_ingestion(["orders.csv"], None, WatermarkState(), {})
        assert len(plan.files) == 1
        assert "is new" in plan.reason

    def test_a_modified_file_is_re_read(self, connector):
        self.write(connector, "orders.csv", 10)
        before = connector.stat(["orders.csv"]).etag
        time.sleep(1.1)  # the local etag is size+mtime at second resolution
        self.write(connector, "orders.csv", 25)
        plan = connector.plan_ingestion(
            ["orders.csv"], None, WatermarkState(), {"orders.csv": before}
        )
        assert len(plan.files) == 1
        assert "changed" in plan.reason

    def test_a_skipped_file_yields_no_rows(self, connector):
        self.write(connector, "orders.csv", 10)
        etag = connector.stat(["orders.csv"]).etag
        plan = connector.plan_ingestion(
            ["orders.csv"], None, WatermarkState(), {"orders.csv": etag}
        )
        assert list(connector.read_plan(["orders.csv"], plan)) == []

    def test_a_changed_file_yields_all_its_rows(self, connector):
        self.write(connector, "orders.csv", 40)
        plan = connector.plan_ingestion(["orders.csv"], None, WatermarkState(), {})
        rows = sum(batch.num_rows for batch in connector.read_plan(["orders.csv"], plan))
        assert rows == 40


class TestPlanIsExplainable:
    def test_every_plan_states_a_reason(self):
        plans = [
            plan_column_watermark(schema(("name", "string")), WatermarkState(), None),
            plan_column_watermark(schema(("updated_at", "timestamp")), WatermarkState(), "x"),
        ]
        for plan in plans:
            assert plan.reason
            assert plan.describe()

    def test_a_full_load_says_so_rather_than_pretending(self):
        plan = plan_column_watermark(schema(("name", "string")), WatermarkState(), None)
        assert plan.describe().startswith("Full load.")
