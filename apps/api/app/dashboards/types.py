from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.semantic.types import Filter, TimeGrain

#: What a tile can be drawn as.
#:
#: Every one of these consumes the same result shape - labelled points carrying one value
#: per measure - so adding a type is a rendering decision, not a new query path. That is
#: deliberate: a chart that needed its own query would be a second way to compute a number,
#: which is the thing the semantic layer exists to prevent.
TileType = Literal[
    "stat",         # one headline number, its movement, and a sparkline
    "line",         # a measure over time
    "area",         # the same, filled - reads as composition rather than rate
    "bar",          # a measure across a dimension
    "stacked_bar",  # several measures across a dimension, as parts of a whole
    "combo",        # bars plus a line on a second axis, for measures of different units
    "donut",        # share of a total, only honest for a handful of categories
    "scatter",      # two measures against each other, to show correlation
    "table",        # the numbers themselves, with data bars
]

#: Types that plot exactly one measure. Offering more would silently drop the rest.
SINGLE_MEASURE_TYPES = frozenset({"line", "area", "bar", "donut"})

#: Types that need at least two, because the whole point is the comparison.
PAIRED_MEASURE_TYPES = frozenset({"combo", "scatter"})

#: A donut stops being readable well before a bar chart does - past this many slices the
#: eye cannot compare angles, and the chart is decoration.
MAX_DONUT_SLICES = 6


class Tile(BaseModel):
    """
    One card on a dashboard.

    A tile owns a measure, an optional dimension and an optional time axis - never SQL.
    Its numbers are produced by the semantic compiler like every other answer, so a tile
    cannot show a figure the model would refuse to defend.
    """

    id: str
    type: TileType
    title: str
    subtitle: str | None = None
    measures: list[str] = Field(default_factory=list)
    dimension: str | None = None
    time_dimension: str | None = None
    time_grain: TimeGrain = "month"
    # Tile-local filters, applied on top of the dashboard's.
    filters: list[Filter] = Field(default_factory=list)
    limit: int = 12
    # Dimensions this tile can be broken down by, in order. Powers drill-down.
    drill_path: list[str] = Field(default_factory=list)
    # Grid placement: 12-column layout.
    width: int = 4
    height: int = 1

    #: For combo tiles: which measures belong on the right-hand axis. A second axis is
    #: how a count and a currency share a chart without one flattening the other.
    secondary_measures: list[str] = Field(default_factory=list)

    #: For stacked and donut tiles: show the tail as a single "Other" slice rather than
    #: drawing categories too thin to read.
    fold_tail: bool = True

    def validate_shape(self) -> list[str]:
        """
        Reasons this tile cannot be drawn as asked.

        Checked when a dashboard is saved rather than when it is rendered, so a
        mis-specified tile is a refusal at edit time instead of an empty card at
        breakfast. Returning the reasons rather than raising lets one bad tile be
        reported alongside the others.
        """
        problems: list[str] = []
        count = len(self.measures)

        if count == 0 and self.type != "table":
            problems.append(f"A {self.type} tile needs at least one measure.")
        if self.type in SINGLE_MEASURE_TYPES and count > 1:
            problems.append(
                f"A {self.type} tile plots one measure; this one names {count}. "
                "Use a stacked bar or a combo tile to show several at once."
            )
        if self.type in PAIRED_MEASURE_TYPES and count < 2:
            problems.append(
                f"A {self.type} tile compares two measures against each other, so it "
                "needs two."
            )
        if self.type == "scatter" and count != 2:
            problems.append("A scatter tile takes exactly two measures: one per axis.")
        if self.type == "combo" and not self.secondary_measures:
            problems.append(
                "A combo tile needs at least one measure on the second axis, otherwise "
                "it is a bar chart with extra steps."
            )
        if self.type in {"bar", "stacked_bar", "donut", "scatter"} and not self.dimension:
            problems.append(f"A {self.type} tile needs a dimension to break the measure down by.")
        if self.type in {"line", "area"} and not (self.time_dimension or self.dimension):
            problems.append(
                f"A {self.type} tile needs a time column, or a dimension to run along "
                "the x axis."
            )
        if self.type == "donut" and self.limit > MAX_DONUT_SLICES and not self.fold_tail:
            problems.append(
                f"A donut with more than {MAX_DONUT_SLICES} slices cannot be read by "
                "eye. Lower the limit, fold the tail, or use a bar chart."
            )
        return problems


class Dashboard(BaseModel):
    id: str
    connection_id: str
    name: str
    description: str | None = None
    tiles: list[Tile] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    time_dimension: str | None = None
    time_grain: TimeGrain = "month"
    generated: bool = True
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------------------


class Point(BaseModel):
    label: str
    raw: Any = None
    values: dict[str, float | None] = Field(default_factory=dict)


class StatSummary(BaseModel):
    """The headline number, plus how it moved and what it looked like getting there."""

    measure: str
    value: float | None = None
    previous: float | None = None
    delta: float | None = None
    delta_pct: float | None = None
    # Whether an increase is a good thing is not knowable from the data alone, so the
    # tile reports direction and leaves the judgement to the reader.
    direction: Literal["up", "down", "flat", "unknown"] = "unknown"
    sparkline: list[float | None] = Field(default_factory=list)
    period_label: str | None = None


class TileResult(BaseModel):
    tile_id: str
    type: TileType
    title: str
    columns: list[str] = Field(default_factory=list)
    dimension_columns: list[str] = Field(default_factory=list)
    measure_columns: list[str] = Field(default_factory=list)
    #: The measure ids behind those columns, in the same order.
    #:
    #: Column names are not unique across the model - two entities can each have a
    #: `record_count` - so a renderer matching a label by column name will sometimes
    #: label a chart with a different entity's measure. The id is unambiguous.
    measure_ids: list[str] = Field(default_factory=list)
    #: Which of those belong on a second axis. Carried on the result so the renderer does
    #: not have to fetch the tile definition to know how to draw it.
    secondary_measure_columns: list[str] = Field(default_factory=list)
    points: list[Point] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    stats: list[StatSummary] = Field(default_factory=list)
    sql: list[str] = Field(default_factory=list)
    elapsed_ms: int = 0
    truncated: bool = False
    notes: list[str] = Field(default_factory=list)
    error: str | None = None


class DashboardData(BaseModel):
    dashboard_id: str
    tiles: list[TileResult] = Field(default_factory=list)
    elapsed_ms: int = 0
