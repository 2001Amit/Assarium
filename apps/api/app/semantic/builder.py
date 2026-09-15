from __future__ import annotations

import re

from app.engine.base import Engine, is_internal_table, safe_identifier
from app.profiling.naming import IDENTIFIER_NAMES, MEASURE_NAMES, normalise
from app.profiling.types import DatasetProfile
from app.semantic.types import Attribute, Entity, Join, Measure, SemanticModel

# Suffixes that come from the file the data arrived in, not from the business.
FILE_SUFFIXES = re.compile(r"_(csv|tsv|parquet|pqt|json|jsonl|ndjson|xlsx|xls)$")
LAYER_PREFIXES = re.compile(r"^(raw|stg|staging|src|source|bronze|silver|gold|dim|fact|agg)_")

CURRENCY_HINTS = re.compile(
    r"(^|_)(amount|amt|revenue|sales|cost|price|value|spend|salary|balance|fee|charge|"
    r"gmv|arr|mrr|profit|margin_amount)(_|$)"
)
PERCENT_HINTS = re.compile(r"(^|_)(pct|percent|rate|ratio|margin|share|conversion)(_|$)")
RATIO_SHAPED = re.compile(r"(^|_)(pct|percent|rate|ratio|avg|average|mean|margin|score|index)(_|$)")

# A categorical column is only useful to group by if its domain stays small.
MAX_DIMENSION_CARDINALITY = 200


ACRONYMS = {"id", "usd", "eur", "gbp", "inr", "url", "ip", "sku", "kpi", "vat", "gst", "api"}

# Column names are written for a database, not for a reader. These are the abbreviations
# common enough that expanding them makes a label better rather than surprising.
EXPANSIONS = {
    "avg": "average", "amt": "amount", "qty": "quantity", "cnt": "count", "num": "number",
    "pct": "percent", "desc": "description", "addr": "address", "dt": "date", "ts": "timestamp",
    "yr": "year", "mth": "month", "org": "organisation", "cust": "customer", "prod": "product",
}

# Aggregation words a column name may already carry, so a measure is not named twice.
SUM_PREFIX = re.compile(r"^(total|sum|gross|net)_")
AVG_PREFIX = re.compile(r"^(avg|average|mean)_")


def humanise(value: str) -> str:
    """Turn a table or column name into something a person would write on a slide."""
    cleaned = FILE_SUFFIXES.sub("", normalise(value))
    cleaned = LAYER_PREFIXES.sub("", cleaned)
    words = [EXPANSIONS.get(w, w) for w in cleaned.split("_") if w]
    if not words:
        return value
    # Sentence case, not title case: a screen full of Title Case Everywhere reads as shouting.
    rendered = [words[0].upper() if words[0] in ACRONYMS else words[0].capitalize()]
    rendered += [w.upper() if w in ACRONYMS else w for w in words[1:]]
    return " ".join(rendered)


def lower_first(value: str) -> str:
    """Lowercase the leading word so a label can be embedded mid-sentence."""
    if not value:
        return value
    head, _, tail = value.partition(" ")
    if head.isupper() and len(head) > 1:
        return value
    return head.lower() + (" " + tail if tail else "")


def build_model(
    connection_id: str,
    engine: Engine,
    datasets: list[dict],
    relationships: list[dict],
) -> SemanticModel:
    """
    Derive a starting semantic model from what the pipeline already produced.

    Entities are the silver tables, not the gold ones. Silver is row-level and joinable,
    so a measure defined there can be asked at any grain with any filter. Defining the
    same measure over a pre-aggregated gold table as well would give the organisation two
    ways to compute revenue, which is the problem this layer exists to remove.
    """
    entities: list[Entity] = []
    measures: list[Measure] = []
    notes: list[str] = []

    # Columns that participate in a relationship are foreign keys, not free dimensions.
    foreign_keys: dict[str, set[str]] = {}
    for relationship in relationships:
        table = safe_identifier(relationship["from_dataset"])
        foreign_keys.setdefault(table, set()).add(relationship["from_column"])

    by_dataset_name: dict[str, str] = {}

    for dataset in datasets:
        table = safe_identifier(dataset["name"])
        if is_internal_table(table):
            continue
        if not engine.table_exists("silver", table):
            notes.append(
                f"{dataset['name']} is not in the silver layer yet, so it is not in the model. "
                "Run the pipeline first."
            )
            continue

        raw_profile = dataset.get("profile")
        profile = DatasetProfile.model_validate(raw_profile) if raw_profile else None
        engine_columns = dict(engine.describe("silver", table))
        entity_id = table
        by_dataset_name[dataset["name"]] = entity_id

        key_columns = set()
        if profile and profile.key_candidates:
            key_columns = {safe_identifier(c) for c in profile.key_candidates[0]}

        attributes: list[Attribute] = []
        for column_name in engine_columns:
            if column_name.startswith("_assarium_"):
                continue
            column_profile = _match_profile(profile, column_name)
            logical = column_profile.logical_type if column_profile else "string"
            if column_profile and column_profile.shadow_type:
                logical = column_profile.shadow_type

            role = _role(column_name, logical, key_columns, foreign_keys.get(table, set()))
            cardinality = column_profile.distinct_count if column_profile else None

            attributes.append(
                Attribute(
                    name=column_name,
                    label=humanise(column_name),
                    column=column_name,
                    logical_type=logical,
                    role=role,
                    cardinality=cardinality,
                    contains_pii=bool(column_profile and column_profile.pii),
                    pii_kind=(
                        column_profile.pii.kind
                        if column_profile and column_profile.pii
                        else None
                    ),
                    # A near-unique column is noise in a grouping menu. Keys, foreign
                    # keys and time columns stay visible: they are how you join and trend.
                    hidden=role in {"dimension", "attribute"}
                    and cardinality is not None
                    and cardinality > MAX_DIMENSION_CARDINALITY,
                )
            )

        entity_measures = _measures_for(entity_id, table, attributes)
        entity = Entity(
            id=entity_id,
            name=table,
            label=humanise(dataset["name"]),
            layer="silver",
            table=table,
            primary_key=sorted(key_columns),
            attributes=attributes,
            is_fact=bool(entity_measures) and len(entity_measures) > 1,
            row_count=engine.row_count("silver", table),
        )
        entities.append(entity)
        measures.extend(entity_measures)

    joins = _joins(relationships, by_dataset_name)

    facts = [e for e in entities if e.is_fact]
    if not facts:
        notes.append(
            "No table in this model carries measures, so there is nothing to aggregate yet. "
            "Add a measure by hand, or bring in a table with numeric columns."
        )

    return SemanticModel(
        connection_id=connection_id,
        entities=entities,
        measures=measures,
        joins=joins,
        notes=notes,
    )


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _match_profile(profile: DatasetProfile | None, column: str):
    """Silver renames columns, so profile entries are matched on the normalised name."""
    if profile is None:
        return None
    return next((c for c in profile.columns if safe_identifier(c.name) == column), None)


def _role(column: str, logical: str, keys: set[str], foreign: set[str]) -> str:
    if column in keys:
        return "key"
    if column in foreign or safe_identifier(column) in {safe_identifier(f) for f in foreign}:
        return "foreign_key"
    if logical in {"date", "timestamp"}:
        return "time"
    if logical in {"integer", "float", "decimal"}:
        # Numerics are candidate measure sources; _measures_for decides which of them
        # actually read as measurements rather than as codes.
        return "attribute"
    return "dimension"


def _measures_for(entity_id: str, table: str, attributes: list[Attribute]) -> list[Measure]:
    """
    Propose measures from the numeric columns that are genuinely measurements.

    Ratio-shaped columns get an average only. Offering `total_margin_pct` would invite
    somebody to put the sum of a percentage on a dashboard.
    """
    measures: list[Measure] = [
        Measure(
            id=f"{entity_id}.record_count",
            name="record_count",
            label=f"{humanise(table)} count",
            description=f"Number of rows in {humanise(table)}.",
            entity_id=entity_id,
            aggregation="count",
            format="number",
        )
    ]

    for attribute in attributes:
        if attribute.logical_type not in {"integer", "float", "decimal"}:
            continue
        if attribute.role in {"key", "foreign_key"}:
            continue
        name = normalise(attribute.name)
        if IDENTIFIER_NAMES.search(name):
            continue
        # Require the column to read as a measurement, so a stray numeric code does not
        # become a metric nobody asked for.
        if not MEASURE_NAMES.search(name) and attribute.logical_type == "integer":
            continue

        is_percent = bool(PERCENT_HINTS.search(name))
        is_currency = bool(CURRENCY_HINTS.search(name))
        fmt = "percent" if is_percent else "currency" if is_currency else "number"
        decimals = 1 if is_percent else 2 if is_currency else 0

        if RATIO_SHAPED.search(name):
            measure_name, label = _name_measure(attribute, "avg")
            measures.append(
                Measure(
                    id=f"{entity_id}.{measure_name}",
                    name=measure_name,
                    label=label,
                    description=f"Mean {lower_first(attribute.label)}. This column is a rate, "
                    "so it is averaged rather than summed.",
                    entity_id=entity_id,
                    aggregation="avg",
                    column=attribute.name,
                    format=fmt,
                    decimals=max(decimals, 1),
                )
            )
            continue

        for aggregation in ("sum", "avg"):
            measure_name, label = _name_measure(attribute, aggregation)
            measures.append(
                Measure(
                    id=f"{entity_id}.{measure_name}",
                    name=measure_name,
                    label=label,
                    entity_id=entity_id,
                    aggregation=aggregation,
                    column=attribute.name,
                    format=fmt,
                    decimals=decimals if aggregation == "sum" else max(decimals, 2),
                )
            )

    return measures


def _name_measure(attribute: Attribute, aggregation: str) -> tuple[str, str]:
    """
    Name a measure without repeating an aggregation the column name already states.

    A column called `total_revenue` summed is `total_revenue`, not `total_total_revenue`;
    `avg_order_value` averaged is `avg_order_value`, not `avg_avg_order_value`.
    """
    column = attribute.name
    if aggregation == "sum":
        if SUM_PREFIX.match(normalise(column)):
            return column, humanise(column)
        return f"total_{column}", f"Total {lower_first(attribute.label)}"

    if AVG_PREFIX.match(normalise(column)):
        return column, humanise(column)
    return f"avg_{column}", f"Average {lower_first(attribute.label)}"


def _joins(relationships: list[dict], by_name: dict[str, str]) -> list[Join]:
    joins: list[Join] = []
    for relationship in relationships:
        if not relationship.get("accepted", True):
            continue
        from_entity = by_name.get(relationship["from_dataset"])
        to_entity = by_name.get(relationship["to_dataset"])
        if not from_entity or not to_entity or from_entity == to_entity:
            continue
        joins.append(
            Join(
                id=f"{from_entity}.{relationship['from_column']}->{to_entity}",
                from_entity=from_entity,
                from_column=safe_identifier(relationship["from_column"]),
                to_entity=to_entity,
                to_column=safe_identifier(relationship["to_column"]),
                cardinality=relationship.get("cardinality") or "many_to_one",
                confidence=relationship.get("confidence", 1.0),
                kind=relationship.get("kind", "inferred"),
            )
        )
    return joins
