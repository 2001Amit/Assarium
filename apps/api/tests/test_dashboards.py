"""
Dashboard tiles: what may be drawn, and what must be refused.

The theme running through these is that a chart is a claim about the data. A donut with
twenty slices, a line with two measures on one axis, a share made of negative parts - each
of them renders happily and says something untrue. So the shape rules are enforced when a
dashboard is saved, while somebody is still editing, rather than discovered as an empty
card the next morning.
"""

from __future__ import annotations

import pytest

from app.dashboards.runner import MAX_STACK_BANDS, _fold_tail
from app.dashboards.types import MAX_DONUT_SLICES, Point, Tile, TileResult


def tile(**kwargs) -> Tile:
    base = {"id": "t1", "type": "bar", "title": "A tile", "measures": ["m.total"]}
    base.update(kwargs)
    return Tile(**base)


class TestShapeRules:
    def test_a_well_formed_bar_tile_is_accepted(self):
        assert tile(type="bar", dimension="e.region").validate_shape() == []

    def test_a_bar_without_a_dimension_is_refused(self):
        problems = tile(type="bar", dimension=None).validate_shape()
        assert any("needs a dimension" in p for p in problems)

    def test_a_line_with_two_measures_is_refused(self):
        """
        Two measures on one axis means the smaller one is a flat line along the bottom.
        The fix is a second axis or a second chart, and the message says so.
        """
        problems = tile(
            type="line", measures=["m.revenue", "m.occupancy"], time_dimension="e.month"
        ).validate_shape()
        assert any("plots one measure" in p for p in problems)
        assert any("combo" in p for p in problems)

    def test_a_line_with_nothing_on_the_x_axis_is_refused(self):
        problems = tile(type="line", dimension=None, time_dimension=None).validate_shape()
        assert any("needs a time column" in p for p in problems)

    def test_a_combo_with_one_measure_is_refused(self):
        problems = tile(type="combo", measures=["m.revenue"], time_dimension="e.m").validate_shape()
        assert any("needs two" in p for p in problems)

    def test_a_combo_with_no_second_axis_is_refused(self):
        """Otherwise it is a bar chart wearing a different name."""
        problems = tile(
            type="combo", measures=["m.a", "m.b"], time_dimension="e.m",
            secondary_measures=[],
        ).validate_shape()
        assert any("second axis" in p for p in problems)

    def test_a_well_formed_combo_is_accepted(self):
        assert tile(
            type="combo", measures=["m.revenue", "m.occupancy"],
            secondary_measures=["m.occupancy"], time_dimension="e.month",
        ).validate_shape() == []

    def test_a_scatter_needs_exactly_two_measures(self):
        assert any(
            "exactly two" in p
            for p in tile(
                type="scatter", measures=["m.a", "m.b", "m.c"], dimension="e.unit"
            ).validate_shape()
        )

    def test_a_well_formed_scatter_is_accepted(self):
        assert tile(
            type="scatter", measures=["m.rent", "m.sqft"], dimension="e.unit"
        ).validate_shape() == []

    def test_an_unfoldable_donut_with_too_many_slices_is_refused(self):
        problems = tile(
            type="donut", dimension="e.region", limit=30, fold_tail=False
        ).validate_shape()
        assert any("cannot be read by eye" in p for p in problems)

    def test_a_donut_that_folds_its_tail_is_accepted(self):
        assert tile(
            type="donut", dimension="e.region", limit=30, fold_tail=True
        ).validate_shape() == []

    def test_a_tile_with_no_measures_is_refused(self):
        assert any("at least one measure" in p for p in tile(measures=[]).validate_shape())

    def test_a_table_may_have_no_measures(self):
        """A table is the raw grid; it is legitimate to list dimensions alone."""
        assert tile(type="table", measures=[]).validate_shape() == []


class TestTailFolding:
    def result_with(self, count: int) -> TileResult:
        return TileResult(
            tile_id="t1", type="donut", title="Share",
            measure_columns=["total"],
            points=[
                Point(label=f"cat-{i}", values={"total": float(100 - i)})
                for i in range(count)
            ],
        )

    def test_a_short_list_is_left_alone(self):
        result = self.result_with(4)
        _fold_tail(tile(type="donut", dimension="e.x"), result)
        assert len(result.points) == 4
        assert not any("Other" in p.label for p in result.points)

    def test_a_long_list_is_folded(self):
        result = self.result_with(30)
        _fold_tail(tile(type="donut", dimension="e.x"), result)
        assert len(result.points) == MAX_DONUT_SLICES + 1
        assert result.points[-1].label.startswith("Other")

    def test_folding_preserves_the_total(self):
        """
        Dropping the tail would silently shrink the whole, so every percentage on the
        chart would be wrong while looking perfectly reasonable.
        """
        result = self.result_with(30)
        before = sum(p.values["total"] or 0 for p in result.points)
        _fold_tail(tile(type="donut", dimension="e.x"), result)
        after = sum(p.values["total"] or 0 for p in result.points)
        assert after == pytest.approx(before)

    def test_the_biggest_categories_survive(self):
        result = self.result_with(30)
        _fold_tail(tile(type="donut", dimension="e.x"), result)
        assert result.points[0].label == "cat-0"

    def test_the_reader_is_told_it_happened(self):
        result = self.result_with(30)
        _fold_tail(tile(type="donut", dimension="e.x"), result)
        assert any("grouped as Other" in note for note in result.notes)

    def test_a_stacked_bar_tolerates_more_bands_than_a_donut(self):
        """Bands share a baseline, so lengths stay comparable where angles do not."""
        assert MAX_STACK_BANDS > MAX_DONUT_SLICES
        result = self.result_with(30)
        result.type = "stacked_bar"
        _fold_tail(tile(type="stacked_bar", dimension="e.x"), result)
        assert len(result.points) == MAX_STACK_BANDS + 1


class TestGeneratedDashboards:
    def build(self, model):
        from app.dashboards.generator import generate_dashboard

        return generate_dashboard("d1", model)

    def model(self):
        """A REIT-shaped model: money, a rate, a date, and two readable dimensions."""
        from app.semantic.types import Attribute, Entity, Measure, SemanticModel

        fact = Entity(
            id="leases", name="leases", label="Leases", layer="silver", table="leases",
            is_fact=True, row_count=5000,
            attributes=[
                Attribute(name="lease_id", label="Lease", column="lease_id",
                          logical_type="string", role="key"),
                Attribute(name="signed_on", label="Signed on", column="signed_on",
                          logical_type="date", role="time"),
                Attribute(name="region", label="Region", column="region",
                          logical_type="string", role="dimension", cardinality=5),
                Attribute(name="asset_class", label="Asset class", column="asset_class",
                          logical_type="string", role="dimension", cardinality=12),
                Attribute(name="monthly_rent", label="Monthly rent", column="monthly_rent",
                          logical_type="decimal", role="attribute"),
                Attribute(name="occupancy_pct", label="Occupancy percent",
                          column="occupancy_pct", logical_type="float", role="attribute"),
            ],
        )
        return SemanticModel(
            connection_id="c1",
            entities=[fact],
            measures=[
                Measure(id="leases.total_monthly_rent", name="total_monthly_rent",
                        label="Total monthly rent", entity_id="leases", aggregation="sum",
                        column="monthly_rent", format="currency"),
                Measure(id="leases.avg_occupancy_pct", name="avg_occupancy_pct",
                        label="Average occupancy percent", entity_id="leases",
                        aggregation="avg", column="occupancy_pct", format="percent"),
                Measure(id="leases.record_count", name="record_count",
                        label="Leases count", entity_id="leases", aggregation="count",
                        format="number"),
            ],
        )

    def test_every_generated_tile_passes_its_own_shape_rules(self):
        """
        The generator and the validator must agree. If they drift, the product proposes
        dashboards it will then refuse to save - which is the worst of both.
        """
        dashboard = self.build(self.model())
        problems = {t.id: t.validate_shape() for t in dashboard.tiles}
        assert all(not v for v in problems.values()), problems

    def test_the_generated_page_uses_more_than_one_kind_of_chart(self):
        """
        A page of nine bar charts answers one question nine times. Variety here is not
        decoration - each chart type answers a different shape of question.
        """
        kinds = {t.type for t in self.build(self.model()).tiles}
        assert {"stat", "line", "bar", "table"} <= kinds
        assert {"donut", "combo"} & kinds, "no composition or dual-axis tile was proposed"

    def test_the_combo_pairs_business_measures_rather_than_a_row_count(self):
        """
        A row count differs in unit from nearly everything, so it wins any naive pairing
        - and "revenue against number of rows" is the least useful chart on the page.
        """
        combo = next(t for t in self.build(self.model()).tiles if t.type == "combo")
        assert not any("record_count" in m for m in combo.measures)

    def test_a_donut_is_only_proposed_for_a_small_dimension(self):
        """Region has five values; asset class has twelve and must not become a donut."""
        donut = next(t for t in self.build(self.model()).tiles if t.type == "donut")
        assert donut.dimension is not None and donut.dimension.endswith("region")

    def test_a_model_with_no_measures_produces_a_reason_not_an_empty_page(self):
        from app.semantic.types import SemanticModel

        dashboard = self.build(SemanticModel(connection_id="c1", entities=[], measures=[]))
        assert dashboard.tiles == []
        assert dashboard.notes, "an empty dashboard has to say why it is empty"


class TestMeasureIdentity:
    def test_the_result_carries_measure_ids_alongside_columns(self):
        """
        Column names are not unique across a model - two entities can each have a
        `record_count`. A renderer matching a label by column name alone will sometimes
        label a chart with a different entity's measure, which is how a combo chart ended
        up saying "Customers count" over an orders bar.
        """
        from app.dashboards.types import TileResult

        result = TileResult(
            tile_id="t", type="combo", title="x",
            measure_columns=["record_count", "total_amount_usd"],
            measure_ids=["orders.record_count", "orders.total_amount_usd"],
        )
        assert len(result.measure_ids) == len(result.measure_columns)
        assert result.measure_ids[0].startswith("orders.")
