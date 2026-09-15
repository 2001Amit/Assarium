from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.connectors.type_map import NUMERIC_TYPES, TEMPORAL_TYPES
from app.engine.base import Engine, safe_identifier
from app.medallion.classifier import AUDIT_NAMES, GRAIN_NAMES
from app.profiling.naming import MEASURE_NAMES, normalise
from app.profiling.types import DatasetProfile

logger = logging.getLogger("assarium.medallion")

# Columns the platform adds itself. Prefixed so they never collide with source columns
# and can be stripped again on the way to gold.
INGESTED_AT = "_assarium_ingested_at"
REFINED_AT = "_assarium_refined_at"
SOURCE_COLUMN = "_assarium_source"
ASSARIUM_COLUMNS = {INGESTED_AT, REFINED_AT, SOURCE_COLUMN}


@dataclass
class TransformPlan:
    """The SQL a refinement step will run, plus a plain description of what it does."""

    target: str
    sql: str
    actions: list[str] = field(default_factory=list)
    dropped_columns: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# bronze -> silver
# --------------------------------------------------------------------------------------


def plan_silver(
    engine: Engine,
    source_table: str,
    target_name: str,
    profile: DatasetProfile,
    *,
    drop_ingestion_metadata: bool = True,
) -> TransformPlan:
    """
    Build the SQL that turns one bronze table into its silver form.

    Every decision here is driven by what profiling measured, not by guesswork: a column
    is cast because its values parse as that type, rows are deduplicated because exact
    duplicates were counted, and blanks become NULL because blanks were found.
    """
    projections: list[str] = []
    actions: list[str] = []
    dropped: list[str] = []
    notes: list[str] = []

    casts = 0
    trimmed = 0
    money: list[str] = []

    for column in profile.columns:
        name = column.name
        quoted = engine.quote(name)
        alias = safe_identifier(name)

        if name in ASSARIUM_COLUMNS:
            projections.append(f"{quoted} AS {engine.quote(alias)}")
            continue

        if drop_ingestion_metadata and AUDIT_NAMES.search(normalise(name)):
            dropped.append(name)
            continue

        expression = quoted

        if _is_money_as_float(column):
            # Currency held as a float will not sum exactly: 500 dollar amounts added as
            # float64 land on 52818.14999999999 rather than 52818.15, and that difference
            # reaches the dashboard. Silver is where it gets fixed, once, for everyone.
            places = max(column.decimal_places or 2, 2)
            expression = f"CAST({quoted} AS DECIMAL(38, {places}))"
            money.append(name)
        elif column.shadow_type:
            # The column is text that holds something else. Clean it, then cast in a way
            # that cannot fail the load: a value that will not convert becomes NULL and
            # shows up as a gap rather than taking the whole table down.
            cleaned = engine.blank_to_null(quoted)
            if column.shadow_type in {"integer", "float", "decimal"}:
                cleaned = engine.strip_non_numeric(cleaned)
            expression = engine.try_cast(cleaned, column.shadow_type)
            casts += 1
        elif column.logical_type in {"string", "unknown"}:
            expression = engine.blank_to_null(quoted)
            trimmed += 1

        projections.append(f"{expression} AS {engine.quote(alias)}")
        if alias != name:
            notes.append(f"{name} renamed to {alias}")

    projections.append(f"{engine.current_timestamp()} AS {engine.quote(REFINED_AT)}")

    if money:
        actions.append(
            f"Converted {len(money)} currency {'column' if len(money) == 1 else 'columns'} "
            f"from floating point to exact decimal ({', '.join(money)}), so totals add up "
            "to the cent"
        )
    if casts:
        types = ", ".join(
            f"{c.name} to {c.shadow_type}" for c in profile.columns if c.shadow_type
        )
        actions.append(
            f"Applied real types to {casts} text {'column' if casts == 1 else 'columns'} "
            f"({types})"
        )
    if trimmed:
        actions.append(
            f"Trimmed whitespace and turned blanks into NULL in {trimmed} "
            f"{'column' if trimmed == 1 else 'columns'}"
        )
    if dropped:
        actions.append(
            f"Removed {len(dropped)} ingestion metadata "
            f"{'column' if len(dropped) == 1 else 'columns'}: {', '.join(dropped)}"
        )

    deduplicate = profile.duplicate_row_ratio > 0
    if deduplicate:
        actions.append(
            f"Removed exact duplicate rows ({profile.duplicate_row_ratio:.1%} of the sample)"
        )

    # DISTINCT is applied to the source columns, before the refined-at stamp is added, or
    # every row would be unique by virtue of its timestamp.
    inner = f"SELECT {'DISTINCT ' if deduplicate else ''}* FROM {source_table}"
    sql = f"SELECT {', '.join(projections)} FROM ({inner}) AS _assarium_src"

    if not actions:
        actions.append("Copied through unchanged: profiling found nothing to correct")

    return TransformPlan(
        target=target_name, sql=sql, actions=actions, dropped_columns=dropped, notes=notes
    )


# --------------------------------------------------------------------------------------
# silver -> gold
# --------------------------------------------------------------------------------------


@dataclass
class GoldShape:
    """What a silver table looks like when read as a candidate for a reporting model."""

    measures: list[str]
    dimensions: list[str]
    date_column: str | None
    is_fact: bool


def read_shape(profile: DatasetProfile) -> GoldShape:
    """Work out which columns are measures and which describe the grain."""
    measures: list[str] = []
    dimensions: list[str] = []
    date_column: str | None = None

    for column in profile.columns:
        name = column.name
        if name in ASSARIUM_COLUMNS:
            continue

        if column.logical_type in TEMPORAL_TYPES:
            # Prefer the earliest-declared date column as the reporting timeline.
            if date_column is None:
                date_column = name
            continue

        if column.logical_type in NUMERIC_TYPES:
            # An identifier stored as a number is not something to sum.
            looks_like_key = column.is_unique or normalise(name).endswith(("_id", "_key", "id"))
            if not looks_like_key:
                measures.append(name)
            continue

        # A categorical column is a usable dimension only if its domain is small enough
        # to group by without producing one row per source row.
        distinct = column.distinct_count or 0
        if GRAIN_NAMES.search(normalise(name)) or (0 < distinct <= 50):
            dimensions.append(name)

    return GoldShape(
        measures=measures,
        dimensions=dimensions,
        date_column=date_column,
        is_fact=bool(measures) and bool(dimensions or date_column),
    )


def plan_gold(
    engine: Engine, source_table: str, target_name: str, profile: DatasetProfile
) -> TransformPlan | None:
    """
    Build a starting reporting table from a silver table.

    This produces a defensible default - measures summed and counted at the coarsest
    useful grain - not a finished business model. The semantic layer refines it; nothing
    here invents a metric definition the data does not support.
    """
    shape = read_shape(profile)
    if not shape.is_fact:
        return None

    group_expressions: list[str] = []
    group_aliases: list[str] = []

    if shape.date_column:
        alias = "period_month"
        group_expressions.append(
            f"{engine.month_trunc(engine.quote(shape.date_column))} AS {engine.quote(alias)}"
        )
        group_aliases.append(alias)

    # Cap the dimension count: every extra dimension multiplies the grain, and past a
    # handful the result stops being a summary at all.
    for dimension in shape.dimensions[:4]:
        alias = safe_identifier(dimension)
        group_expressions.append(f"{engine.quote(dimension)} AS {engine.quote(alias)}")
        group_aliases.append(alias)

    if not group_aliases:
        return None

    aggregates = ["COUNT(*) AS record_count"]
    for measure in shape.measures[:8]:
        quoted = engine.quote(measure)
        base = safe_identifier(measure)
        # A column that is already an average or a rate must not be summed; summing
        # percentages is one of the classic ways a dashboard ends up lying.
        if _is_ratio(measure):
            aggregates.append(f"AVG({quoted}) AS {engine.quote(f'avg_{base}')}")
        else:
            aggregates.append(f"SUM({quoted}) AS {engine.quote(f'total_{base}')}")
            aggregates.append(f"AVG({quoted}) AS {engine.quote(f'avg_{base}')}")

    group_by = ", ".join(str(i + 1) for i in range(len(group_aliases)))
    sql = (
        f"SELECT {', '.join(group_expressions + aggregates)} "
        f"FROM {source_table} "
        f"GROUP BY {group_by} "
        f"ORDER BY {group_by}"
    )

    summarised = len(shape.measures[:8])
    actions = [
        f"Grouped by {', '.join(group_aliases)}",
        f"Summarised {summarised} {'measure' if summarised == 1 else 'measures'}, "
        "plus a record count",
    ]
    notes = []
    ratios = [m for m in shape.measures[:8] if _is_ratio(m)]
    if ratios:
        notes.append(
            f"Averaged rather than summed: {', '.join(ratios)}. Summing a rate or "
            "percentage produces a meaningless total."
        )
    if len(shape.dimensions) > 4:
        notes.append(
            f"Grouped by the first 4 of {len(shape.dimensions)} candidate dimensions to keep "
            "the summary coarse; refine this in the semantic model."
        )

    return TransformPlan(target=target_name, sql=sql, actions=actions, notes=notes)


def _is_ratio(name: str) -> bool:
    normalised = normalise(name)
    return bool(
        MEASURE_NAMES.search(normalised)
        and any(
            token in normalised
            for token in (
                "pct", "percent", "rate", "ratio", "avg",
                "average", "mean", "margin", "score",
            )
        )
    )


# Names that mean the number is an amount of money rather than a measurement.
CURRENCY_NAMES = re.compile(
    r"(^|_)(amount|amt|revenue|sales|cost|price|value|spend|salary|balance|fee|charge|"
    r"payment|total|subtotal|discount|tax|profit|gmv|arr|mrr|usd|eur|gbp|inr)(_|$)"
)


def _is_money_as_float(column) -> bool:
    """
    Whether a column holds money in a type that cannot represent it exactly.

    Both halves are required. The name alone would re-type any column called `value`;
    the observed scale alone would re-type every measurement that happens to have two
    decimal places. Together they identify currency with reasonable confidence, and the
    conversion is reported in the run so it is never a silent change.
    """
    if column.logical_type != "float":
        return False
    if not CURRENCY_NAMES.search(normalise(column.name)):
        return False
    places = column.decimal_places
    # Money carries a small, fixed scale. A float with eight decimal places is a
    # measurement or a rate, whatever it is called.
    return places is not None and 0 < places <= 4
