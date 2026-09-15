from __future__ import annotations

import logging
import time
from typing import Any

from app.core.errors import AssariumError
from app.dashboards.types import (
    MAX_DONUT_SLICES,
    DashboardData,
    Point,
    StatSummary,
    Tile,
    TileResult,
)
from app.engine.base import Engine
from app.semantic.compiler import QueryCompiler, SemanticError
from app.semantic.types import Filter, MetricQuery, SemanticModel

logger = logging.getLogger("assarium.dashboards")


#: Types drawn along a time axis, which must run left to right rather than largest-first.
TIMELINE_TYPES = frozenset({"line", "area", "combo"})

#: Types where too many categories stop the chart from meaning anything.
FOLDING_TYPES = frozenset({"donut", "stacked_bar", "bar"})

#: Past this many bands a stacked bar is unreadable, though it tolerates more than a donut
#: because the bands share a baseline and the eye can still compare lengths.
MAX_STACK_BANDS = 8


def run_dashboard(
    model: SemanticModel,
    engine: Engine,
    dashboard: Any,
    global_filters: list[Filter] | None = None,
    permissions: frozenset[str] | set[str] | None = None,
) -> DashboardData:
    """
    Execute every tile on a dashboard.

    Tiles are run independently and a failure is reported on the tile itself. One
    measure that no longer compiles should cost you that card, not the whole page.
    """
    started = time.perf_counter()
    compiler = QueryCompiler(model, engine, permissions=permissions)
    filters = list(global_filters or []) + list(dashboard.filters or [])

    results = [_run_tile(model, engine, compiler, tile, filters) for tile in dashboard.tiles]
    return DashboardData(
        dashboard_id=dashboard.id,
        tiles=results,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


def _run_tile(
    model: SemanticModel,
    engine: Engine,
    compiler: QueryCompiler,
    tile: Tile,
    global_filters: list[Filter],
) -> TileResult:
    result = TileResult(tile_id=tile.id, type=tile.type, title=tile.title)
    filters = [*global_filters, *tile.filters]

    try:
        if tile.type == "stat":
            _run_stat(model, engine, compiler, tile, filters, result)
        else:
            _run_grouped(model, engine, compiler, tile, filters, result)
    except SemanticError as exc:
        result.error = exc.message
    except AssariumError as exc:
        result.error = exc.message
    except Exception as exc:  # noqa: BLE001 - one bad tile must not take the page down
        logger.exception("Tile %s failed", tile.id)
        result.error = str(exc)[:300]
    return result


# --------------------------------------------------------------------------------------
# stat tiles
# --------------------------------------------------------------------------------------


def _run_stat(
    model: SemanticModel,
    engine: Engine,
    compiler: QueryCompiler,
    tile: Tile,
    filters: list[Filter],
    result: TileResult,
) -> None:
    """
    A headline number, its movement, and the shape behind it.

    The total is queried ungrouped rather than summed from the sparkline: adding up a
    series of monthly averages does not give you the overall average, and a stat tile
    that quietly does that is wrong in a way nobody notices.
    """
    measure_id = tile.measures[0]
    measure = model.measure(measure_id)
    if measure is None:
        raise SemanticError(f"There is no measure called '{measure_id}'.")

    total = compiler.compile(MetricQuery(measures=[measure_id], filters=filters, limit=1))
    total_result = engine.execute(total.sql, params=total.params)
    result.sql.append(total.sql)
    result.elapsed_ms += total_result.elapsed_ms

    summary = StatSummary(measure=measure_id)
    if total_result.rows and total_result.rows[0]:
        summary.value = _as_float(total_result.rows[0][0])

    if tile.time_dimension:
        series = compiler.compile(
            MetricQuery(
                measures=[measure_id],
                time_dimension=tile.time_dimension,
                time_grain=tile.time_grain,
                filters=filters,
                order_by=[{"field": measure.name, "direction": "asc"}],
                limit=60,
            )
        )
        # Ordering by the measure would scramble a timeline, so sort by period instead.
        sql = _order_by_first_column(series.sql)
        series_result = engine.execute(sql, params=series.params)
        result.sql.append(sql)
        result.elapsed_ms += series_result.elapsed_ms

        values = [_as_float(row[1]) for row in series_result.rows]
        summary.sparkline = values
        if len(values) >= 2:
            summary.value = summary.value if summary.value is not None else values[-1]
            current, previous = values[-1], values[-2]
            summary.previous = previous
            if current is not None and previous is not None:
                summary.delta = current - previous
                summary.delta_pct = (
                    (current - previous) / abs(previous) if previous else None
                )
                summary.direction = (
                    "up" if current > previous else "down" if current < previous else "flat"
                )
            summary.period_label = f"vs previous {tile.time_grain}"

    result.stats = [summary]
    result.measure_columns = [measure.name]
    result.measure_ids = [measure.id]


# --------------------------------------------------------------------------------------
# grouped tiles
# --------------------------------------------------------------------------------------


def _run_grouped(
    model: SemanticModel,
    engine: Engine,
    compiler: QueryCompiler,
    tile: Tile,
    filters: list[Filter],
    result: TileResult,
) -> None:
    is_timeline = tile.type in TIMELINE_TYPES
    query = MetricQuery(
        measures=tile.measures,
        dimensions=[tile.dimension] if tile.dimension else [],
        time_dimension=tile.time_dimension if is_timeline or tile.type == "table" else None,
        time_grain=tile.time_grain,
        filters=filters,
        limit=tile.limit,
    )
    compiled = compiler.compile(query)

    sql = compiled.sql
    if is_timeline:
        # A trend must run left to right; largest-first is meaningless on a timeline.
        sql = _order_by_first_column(sql)

    executed = engine.execute(sql, params=compiled.params)

    result.sql.append(sql)
    result.columns = executed.columns
    result.dimension_columns = compiled.dimension_columns
    result.measure_columns = compiled.measure_columns
    # The compiler emits one output column per requested measure, in order, so the tile's
    # own list lines up with it.
    result.measure_ids = list(tile.measures[: len(compiled.measure_columns)])
    result.secondary_measure_columns = [
        name for name in compiled.measure_columns
        if name in tile.secondary_measures or f"{tile.dimension}.{name}" in tile.secondary_measures
    ] or [
        # Fall back to matching on the measure id's trailing segment: the compiler names
        # output columns after the measure, the tile names them by id.
        name for name in compiled.measure_columns
        if any(ref.rsplit(".", 1)[-1] == name for ref in tile.secondary_measures)
    ]
    result.rows = executed.rows
    result.elapsed_ms = executed.elapsed_ms
    result.truncated = executed.truncated
    result.notes = compiled.notes

    dimension_count = len(compiled.dimension_columns)
    for row in executed.rows:
        label_parts = [_as_label(value) for value in row[:dimension_count]]
        result.points.append(
            Point(
                label=" · ".join(label_parts) if label_parts else "Total",
                raw=row[0] if dimension_count else None,
                values={
                    name: _as_float(row[dimension_count + index])
                    for index, name in enumerate(compiled.measure_columns)
                },
            )
        )

    if tile.type in FOLDING_TYPES and tile.fold_tail:
        _fold_tail(tile, result)


def _fold_tail(tile: Tile, result: TileResult) -> None:
    """
    Collapse the categories past the readable limit into one "Other" slice.

    A donut with twenty slices is not a chart, it is a colour wheel; a stacked bar with
    twenty bands is the same problem lying down. Folding keeps the total honest - the
    parts still sum to the whole - which dropping the tail would not.
    """
    keep = MAX_DONUT_SLICES if tile.type == "donut" else MAX_STACK_BANDS
    if len(result.points) <= keep + 1:
        return

    measure = result.measure_columns[0] if result.measure_columns else None
    if measure is None:
        return

    ordered = sorted(
        result.points,
        key=lambda point: abs(point.values.get(measure) or 0.0),
        reverse=True,
    )
    head, tail = ordered[:keep], ordered[keep:]

    folded = Point(label=f"Other ({len(tail)})", raw=None, values={})
    for name in result.measure_columns:
        values = [p.values.get(name) for p in tail if p.values.get(name) is not None]
        folded.values[name] = sum(values) if values else None

    result.points = [*head, folded]
    result.notes = [
        *result.notes,
        f"{len(tail)} smaller categories are grouped as Other so the chart stays "
        "readable. The totals still add up.",
    ]


def _order_by_first_column(sql: str) -> str:
    """Replace whatever ORDER BY the compiler chose with chronological order."""
    marker = "\nORDER BY "
    if marker not in sql:
        return sql
    head, _, tail = sql.partition(marker)
    _, _, after = tail.partition("\nLIMIT ")
    return f"{head}{marker}1 ASC\nLIMIT {after}"


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_label(value: Any) -> str:
    if value is None:
        return "Not set"
    text = str(value)
    # Period columns arrive as midnight timestamps; the time half is never the point.
    if text.endswith("T00:00:00") or text.endswith(" 00:00:00"):
        return text[:10]
    return text
