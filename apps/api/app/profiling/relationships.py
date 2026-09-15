from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.connectors.base import Connector
from app.connectors.types import DatasetSchema
from app.core.errors import AssariumError
from app.profiling.types import DatasetProfile, Relationship

logger = logging.getLogger("assarium.relationships")

# Value overlap below this is coincidence, not a join path.
MIN_OVERLAP = 0.75
RELATIONSHIP_SAMPLE = 5_000
JOINABLE_TYPES = {"string", "integer", "decimal"}


@dataclass
class DatasetContext:
    id: str
    name: str
    path: list[str]
    schema: DatasetSchema
    profile: DatasetProfile


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _singularise(value: str) -> str:
    """Enough English plural handling to match `orders` against `order_id`."""
    if value.endswith("ies") and len(value) > 4:
        return value[:-3] + "y"
    if value.endswith("ses") or value.endswith("xes") or value.endswith("zes"):
        return value[:-2]
    if value.endswith("s") and not value.endswith("ss"):
        return value[:-1]
    return value


def _name_affinity(child_column: str, parent_dataset: str, parent_column: str) -> float:
    """How strongly a column's name suggests it points at a given parent key."""
    child = _normalise(child_column)
    parent_table = _singularise(_normalise(parent_dataset))
    parent_key = _normalise(parent_column)

    if child == parent_key and child not in {"id", "key", "code"}:
        return 0.9
    # The classic shape: customer_id -> customers.id
    if child in {f"{parent_table}_id", f"{parent_table}_key", f"{parent_table}_code"}:
        return 1.0
    if child.startswith(parent_table) and child.endswith(("_id", "_key", "_code")):
        return 0.85
    # A renamed lookup: billing_customer_id -> customers.customer_id
    if parent_table in child and child.endswith(("_id", "_key", "_code")):
        return 0.7
    if child == parent_key:
        return 0.45
    return 0.0


def _parent_keys(context: DatasetContext) -> list[str]:
    keys = [c.name for c in context.profile.columns if c.is_unique]
    if keys:
        return keys
    return [group[0] for group in context.profile.key_candidates if len(group) == 1]


def infer_relationships(
    connector: Connector, contexts: list[DatasetContext]
) -> list[Relationship]:
    """
    Find join paths between selected datasets.

    Declared foreign keys are taken as given. Everything else is proposed from name
    affinity confirmed by value overlap — a name alone is a guess, and overlap alone
    matches every pair of integer columns in the warehouse.
    """
    relationships: list[Relationship] = []
    seen: set[tuple[str, str, str, str]] = set()

    for relationship in _declared(connector, contexts):
        key = (
            relationship.from_dataset,
            relationship.from_column,
            relationship.to_dataset,
            relationship.to_column,
        )
        seen.add(key)
        relationships.append(relationship)

    values = _sample_values(connector, contexts)

    for child in contexts:
        for parent in contexts:
            if child.id == parent.id:
                continue
            for parent_key in _parent_keys(parent):
                parent_values = values.get((parent.id, parent_key))
                if not parent_values:
                    continue
                for column in child.schema.columns:
                    if column.logical_type not in JOINABLE_TYPES:
                        continue
                    affinity = _name_affinity(column.name, parent.name, parent_key)
                    if affinity < 0.45:
                        continue
                    key = (child.id, column.name, parent.id, parent_key)
                    if key in seen:
                        continue
                    child_values = values.get((child.id, column.name))
                    if not child_values:
                        continue
                    overlap = len(child_values & parent_values) / len(child_values)
                    if overlap < MIN_OVERLAP:
                        continue

                    child_profile = child.profile.column(column.name)
                    seen.add(key)
                    relationships.append(
                        Relationship(
                            from_dataset=child.id,
                            from_column=column.name,
                            to_dataset=parent.id,
                            to_column=parent_key,
                            kind="inferred",
                            # Overlap dominates; the name breaks ties between candidates.
                            confidence=round(min(0.99, 0.65 * overlap + 0.35 * affinity), 3),
                            overlap=round(overlap, 3),
                            cardinality=(
                                "one_to_one"
                                if child_profile and child_profile.is_unique
                                else "many_to_one"
                            ),
                        )
                    )

    relationships.sort(key=lambda r: (-r.confidence, r.from_dataset))
    return relationships


def _sample_values(
    connector: Connector, contexts: list[DatasetContext]
) -> dict[tuple[str, str], set[Any]]:
    """Read a bounded sample per dataset and index joinable columns as value sets."""
    values: dict[tuple[str, str], set[Any]] = {}
    for context in contexts:
        try:
            sample = connector.sample(context.path, limit=RELATIONSHIP_SAMPLE)
        except AssariumError as exc:
            logger.info("Skipping %s during relationship inference: %s", context.name, exc)
            continue
        index = {name: i for i, name in enumerate(sample.columns)}
        for column in context.schema.columns:
            if column.logical_type not in JOINABLE_TYPES or column.name not in index:
                continue
            position = index[column.name]
            column_values = {
                # Compare as text so integer 42 and string "42" still join.
                str(row[position]).strip()
                for row in sample.rows
                if row[position] is not None and str(row[position]).strip() != ""
            }
            if column_values:
                values[(context.id, column.name)] = column_values
    return values


def _declared(connector: Connector, contexts: list[DatasetContext]) -> list[Relationship]:
    """Referential integrity the source already knows about."""
    by_name = {_normalise(c.name): c for c in contexts}
    found: list[Relationship] = []

    if hasattr(connector, "foreign_keys"):
        schemas = {c.path[-2] for c in contexts if len(c.path) >= 2}
        for schema in schemas:
            try:
                declared = connector.foreign_keys(schema)  # type: ignore[attr-defined]
            except AssariumError as exc:
                logger.info("Could not read declared keys for %s: %s", schema, exc)
                continue
            found.extend(_match(declared, by_name))

    elif hasattr(connector, "relationships"):
        for context in contexts:
            try:
                declared = connector.relationships(context.path[-1])  # type: ignore[attr-defined]
            except AssariumError as exc:
                logger.info("Could not read relationships for %s: %s", context.name, exc)
                continue
            found.extend(_match(declared, by_name))

    return found


def _match(
    declared: list[dict[str, str]], by_name: dict[str, DatasetContext]
) -> list[Relationship]:
    matched: list[Relationship] = []
    for link in declared:
        child = by_name.get(_normalise(link["from_table"]))
        parent = by_name.get(_normalise(link["to_table"]))
        # A declared key pointing outside the user's selection has nothing to join to.
        if not child or not parent or child.id == parent.id:
            continue
        child_profile = child.profile.column(link["from_column"])
        matched.append(
            Relationship(
                from_dataset=child.id,
                from_column=link["from_column"],
                to_dataset=parent.id,
                to_column=link["to_column"],
                kind="declared",
                confidence=1.0,
                cardinality=(
                    "one_to_one" if child_profile and child_profile.is_unique else "many_to_one"
                ),
            )
        )
    return matched
