"""
Deciding how much of a dataset to read.

The planner is deliberately separate from the reading. It produces a decision that can be
inspected, logged and unit-tested without a live source, and every decision carries the
reason it was made - including "I have to read everything, and here is why".
"""

from __future__ import annotations

import re

from app.connectors.types import ColumnSchema, DatasetSchema
from app.ingestion.types import IngestionPlan, WatermarkState
from app.profiling.naming import normalise

# Column names that conventionally carry a last-modified instant. Ordered: the first
# match wins, so the most specific and most trustworthy names come first.
WATERMARK_NAMES: tuple[str, ...] = (
    "systemmodstamp",
    "last_modified_date", "lastmodifieddate", "last_modified_at", "last_modified",
    "modified_at", "modified_date", "modified_on", "date_modified",
    "updated_at", "updated_on", "updated_date", "date_updated",
    "_ingested_at", "ingested_at", "loaded_at", "load_ts", "etl_timestamp",
    "changed_at", "change_date", "row_version", "rowversion", "sys_change_version",
    "created_at", "created_on", "creation_date", "date_created", "inserted_at",
)

# A created-at column only ever grows for insert-only tables. Using it where rows are
# updated in place would silently miss every update, so it is accepted but flagged.
INSERT_ONLY_NAMES = {
    "created_at", "created_on", "creation_date", "date_created", "inserted_at",
}

MONOTONIC_ID = re.compile(r"(^|_)(id|seq|sequence|version|revision|offset)$")


def watermark_candidates(schema: DatasetSchema) -> list[ColumnSchema]:
    """
    Columns that could serve as a high-water mark, best first.

    Temporal columns are preferred over identifiers: a timestamp catches updates, an
    auto-increment id only catches inserts.
    """
    by_name: dict[str, ColumnSchema] = {normalise(c.name): c for c in schema.columns}
    ordered: list[ColumnSchema] = []

    for name in WATERMARK_NAMES:
        column = by_name.get(name)
        if column and column.logical_type in {"timestamp", "date"}:
            ordered.append(column)

    # Any other temporal column, in declared order.
    ordered += [
        c for c in schema.columns
        if c.logical_type in {"timestamp", "date"} and c not in ordered
    ]

    # Monotonic integer keys last: insert-only, but better than a full reload.
    ordered += [
        c for c in schema.columns
        if c.logical_type == "integer"
        and MONOTONIC_ID.search(normalise(c.name))
        and c not in ordered
    ]
    return ordered


def plan_column_watermark(
    schema: DatasetSchema,
    state: WatermarkState,
    ceiling: str | None,
    *,
    preferred_column: str | None = None,
) -> IngestionPlan:
    """Plan a read bounded by a high-water-mark column."""
    candidates = watermark_candidates(schema)
    if preferred_column:
        candidates = [c for c in candidates if c.name == preferred_column] or candidates

    if not candidates:
        return IngestionPlan(
            strategy="full",
            full_refresh=True,
            reason=(
                "No column on this table records when a row last changed, so there is "
                "no safe way to read only what is new. Add a modified-at column, or "
                "accept a full reload each run."
            ),
        )

    column = candidates[0]
    notes = []
    if normalise(column.name) in INSERT_ONLY_NAMES:
        notes.append(
            f"'{column.name}' looks like a creation time, so updates to existing rows "
            "will not be picked up."
        )

    # A change of watermark column invalidates the stored value: the two columns are not
    # comparable, and carrying the old value over would skip rows.
    since = state.value if state.column == column.name else None
    reason = " ".join(
        [f"Using '{column.name}' as the high-water mark.", *notes]
    )
    if state.column and state.column != column.name:
        reason += (
            f" The previous watermark column ('{state.column}') no longer applies, "
            "so this run reads from the beginning."
        )

    return IngestionPlan(
        strategy="column",
        full_refresh=False,
        column=column.name,
        since=since,
        ceiling=ceiling,
        reason=reason,
    )
