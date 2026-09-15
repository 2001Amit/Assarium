from __future__ import annotations

import logging
import math
from collections import Counter
from typing import Any

from app.connectors.base import Connector
from app.connectors.sql_base import SQLConnector
from app.connectors.type_map import NUMERIC_TYPES, TEMPORAL_TYPES
from app.connectors.types import DatasetSchema
from app.core.errors import QueryError
from app.profiling.naming import IDENTIFIER_NAMES, is_key_eligible, normalise
from app.profiling.pii import detect_pii
from app.profiling.shadow import infer_shadow_type
from app.profiling.types import ColumnProfile, DatasetProfile, TopValue

logger = logging.getLogger("assarium.profiling")

# Counting distinct values across every column is a full scan. Above this estimate the
# profiler stays on the sample and says so, rather than issuing an expensive query
# against someone's production warehouse without being asked.
PUSHDOWN_ROW_CEILING = 20_000_000
PUSHDOWN_COLUMN_CEILING = 80
# Types no engine reliably supports in COUNT(DISTINCT).
UNCOUNTABLE = {"json", "binary"}


def profile_dataset(
    connector: Connector,
    path: list[str],
    schema: DatasetSchema,
    sample_rows: int = 50_000,
) -> DatasetProfile:
    """Compute a quality and shape profile for one dataset."""
    sample = connector.sample(path, limit=sample_rows)
    row_count = schema.row_estimate
    profile = DatasetProfile(
        row_count=row_count,
        row_count_exact=connector.exact_row_counts and row_count is not None,
        sampled_rows=len(sample.rows),
    )

    if not sample.rows:
        profile.warnings.append("The dataset returned no rows, so no statistics were computed.")
        profile.columns = [
            ColumnProfile(
                name=c.name,
                native_type=c.native_type,
                logical_type=c.logical_type,
                position=c.position,
            )
            for c in schema.columns
        ]
        return profile

    index = {name: i for i, name in enumerate(sample.columns)}
    columns_by_name = {c.name: c for c in schema.columns}

    exact = _pushdown_counts(connector, path, schema, row_count)
    if exact:
        profile.row_count = exact["__rows"]
        profile.row_count_exact = True
    elif row_count is None:
        profile.row_count = len(sample.rows)
        profile.warnings.append(
            "The source reports no row count, so the sample size is shown instead."
        )

    denominator = profile.row_count or len(sample.rows)

    for name in sample.columns:
        declared = columns_by_name.get(name)
        values = [row[index[name]] for row in sample.rows]
        column = _profile_column(
            name=name,
            values=values,
            native_type=declared.native_type if declared else "unknown",
            logical_type=declared.logical_type if declared else "unknown",
            position=declared.position if declared else index[name],
            sample_size=len(sample.rows),
        )
        if exact and f"n_{name}" in exact:
            non_null = exact[f"n_{name}"]
            column.null_count = max(exact["__rows"] - non_null, 0)
            column.null_pct = column.null_count / denominator if denominator else 0.0
            distinct = exact.get(f"d_{name}")
            if distinct is not None:
                column.distinct_count = distinct
                column.distinct_pct = distinct / denominator if denominator else None
                column.is_unique = distinct == exact["__rows"] and column.null_count == 0
                column.is_constant = distinct <= 1
            column.exact_counts = True
        profile.columns.append(column)

    profile.duplicate_row_ratio = _duplicate_ratio(sample.rows)
    profile.key_candidates = _key_candidates(profile, sample, index)

    if not profile.row_count_exact and sample.truncated:
        profile.warnings.append(
            f"Statistics are estimated from a {len(sample.rows):,}-row sample."
        )
    return profile


# --------------------------------------------------------------------------------------
# per-column statistics
# --------------------------------------------------------------------------------------


def _profile_column(
    *,
    name: str,
    values: list[Any],
    native_type: str,
    logical_type: str,
    position: int,
    sample_size: int,
) -> ColumnProfile:
    non_null = [v for v in values if v is not None]
    null_count = len(values) - len(non_null)

    column = ColumnProfile(
        name=name,
        native_type=native_type,
        logical_type=logical_type,
        position=position,
        null_count=null_count,
        null_pct=null_count / sample_size if sample_size else 0.0,
    )

    distinct = len({_hashable(v) for v in non_null})
    column.distinct_count = distinct
    column.distinct_pct = distinct / sample_size if sample_size else None
    column.is_unique = distinct == len(non_null) and null_count == 0 and len(non_null) > 1
    column.is_constant = distinct <= 1 and len(non_null) > 0

    if logical_type in NUMERIC_TYPES:
        _numeric_stats(column, non_null)
    elif logical_type in TEMPORAL_TYPES:
        _ordered_stats(column, non_null)
    else:
        _text_stats(column, non_null)
        shadow, ratio = infer_shadow_type(non_null)
        # A column that is genuinely one value is uniform, not mistyped.
        if shadow and not column.is_constant:
            column.shadow_type = shadow
            column.shadow_ratio = ratio

    # Low-cardinality columns get a value breakdown; a near-unique one would just be noise.
    if distinct and distinct <= 40 and logical_type not in NUMERIC_TYPES | TEMPORAL_TYPES:
        counts = Counter(_hashable(v) for v in non_null).most_common(10)
        column.top_values = [
            TopValue(value=str(value), count=count, pct=count / sample_size)
            for value, count in counts
        ]

    column.pii = detect_pii(name, non_null, logical_type)
    return column


def _numeric_stats(column: ColumnProfile, values: list[Any]) -> None:
    numbers = [float(v) for v in values if _is_number(v)]
    if not numbers:
        return
    numbers.sort()
    count = len(numbers)
    mean = sum(numbers) / count
    variance = sum((n - mean) ** 2 for n in numbers) / count if count > 1 else 0.0
    column.minimum = _short(numbers[0])
    column.maximum = _short(numbers[-1])
    column.mean = round(mean, 6)
    column.stddev = round(math.sqrt(variance), 6)
    column.p25 = round(_quantile(numbers, 0.25), 6)
    column.p50 = round(_quantile(numbers, 0.50), 6)
    column.p75 = round(_quantile(numbers, 0.75), 6)

    # Measured from the raw values, not the floats, so formatting is not lost.
    places = 0
    for value in values[:2000]:
        text = str(value)
        if "." in text and "e" not in text.lower():
            places = max(places, len(text.split(".", 1)[1].rstrip("0")))
    column.decimal_places = places


def _ordered_stats(column: ColumnProfile, values: list[Any]) -> None:
    try:
        ordered = sorted(values)
    except TypeError:
        ordered = sorted(str(v) for v in values)
    if ordered:
        column.minimum = str(ordered[0])[:40]
        column.maximum = str(ordered[-1])[:40]


def _text_stats(column: ColumnProfile, values: list[Any]) -> None:
    strings = [str(v) for v in values]
    if not strings:
        return
    lengths = [len(s) for s in strings]
    column.mean_length = round(sum(lengths) / len(lengths), 2)
    column.max_length = max(lengths)
    # Whitespace-only values read as present but carry nothing: a distinct quality problem.
    column.blank_count = sum(1 for s in strings if s.strip() == "")
    ordered = sorted(strings)
    column.minimum = ordered[0][:40]
    column.maximum = ordered[-1][:40]


def _quantile(ordered: list[float], q: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _short(value: float) -> str:
    return f"{value:.6g}"


def _hashable(value: Any) -> Any:
    """Lists and dicts appear in JSON columns and cannot go into a set as-is."""
    if isinstance(value, (list, dict, set)):
        return str(value)
    try:
        hash(value)
        return value
    except TypeError:
        return str(value)


def _duplicate_ratio(rows: list[list[Any]]) -> float:
    if not rows:
        return 0.0
    seen = {tuple(_hashable(v) for v in row) for row in rows}
    return round(1 - len(seen) / len(rows), 4)


# --------------------------------------------------------------------------------------
# exact counts, pushed down where the source can answer cheaply
# --------------------------------------------------------------------------------------


def _pushdown_counts(
    connector: Connector, path: list[str], schema: DatasetSchema, row_estimate: int | None
) -> dict[str, int] | None:
    if not isinstance(connector, SQLConnector):
        return None
    if row_estimate is not None and row_estimate > PUSHDOWN_ROW_CEILING:
        return None

    countable = [c for c in schema.columns if c.logical_type not in UNCOUNTABLE]
    if not countable or len(countable) > PUSHDOWN_COLUMN_CEILING:
        return None

    selects = ["COUNT(*) AS __rows"]
    aliases: list[tuple[str, str]] = []
    for i, column in enumerate(countable):
        quoted = connector.quote(column.name)
        selects.append(f"COUNT({quoted}) AS n{i}")
        selects.append(f"COUNT(DISTINCT {quoted}) AS d{i}")
        aliases.append((f"n{i}", f"n_{column.name}"))
        aliases.append((f"d{i}", f"d_{column.name}"))

    sql = f"SELECT {', '.join(selects)} FROM {connector.qualify(path)}"
    try:
        columns, rows = connector.fetch(sql)
    except QueryError as exc:
        # An unsupported aggregate or a permission gap is not a profiling failure;
        # the sample-based numbers still stand.
        logger.info("Exact count pushdown unavailable for %s: %s", ".".join(path), exc)
        return None

    if not rows:
        return None
    raw = {name.lower(): value for name, value in zip(columns, rows[0], strict=False)}
    result: dict[str, int] = {"__rows": int(raw.get("__rows") or 0)}
    for alias, target in aliases:
        value = raw.get(alias)
        if value is not None:
            result[target] = int(value)
    return result


# --------------------------------------------------------------------------------------
# key detection
# --------------------------------------------------------------------------------------


def _key_candidates(profile: DatasetProfile, sample: Any, index: dict[str, int]) -> list[list[str]]:
    """Single-column keys first; only look for a composite when no single column works."""
    eligible = [
        c for c in profile.columns
        if is_key_eligible(c.name, c.logical_type, c.shadow_type)
    ]
    unique = [c for c in eligible if c.is_unique]
    if unique:
        # Several columns can be unique at once; an email address is unique but it is not
        # what the table is keyed on. Rank by how much each looks like a real identifier,
        # then offer alternates only when their names say they are keys too - otherwise a
        # coincidentally-unique name column is presented as something to join on.
        unique.sort(key=_key_rank)
        keys = [[unique[0].name]]
        keys += [
            [c.name] for c in unique[1:3] if IDENTIFIER_NAMES.search(normalise(c.name))
        ]
        return keys

    # Restrict the pair search to columns with enough cardinality to plausibly combine,
    # so this stays O(n²) over a handful of columns rather than all of them.
    candidates = [
        c.name
        for c in sorted(eligible, key=lambda c: -(c.distinct_pct or 0))
        if (c.distinct_pct or 0) > 0.05 and c.null_count == 0
    ][:8]

    total = len(sample.rows)
    for i, left in enumerate(candidates):
        for right in candidates[i + 1 :]:
            pairs = {
                (_hashable(row[index[left]]), _hashable(row[index[right]]))
                for row in sample.rows
            }
            if len(pairs) == total:
                return [[left, right]]
    return []


def _key_rank(column: ColumnProfile) -> tuple[int, int, int]:
    """Sort key: identifier-shaped names first, personal data last, then column order."""
    name = normalise(column.name)
    looks_like_id = 0 if IDENTIFIER_NAMES.search(name) else 1
    is_personal = 1 if column.pii else 0
    return (looks_like_id, is_personal, column.position)
