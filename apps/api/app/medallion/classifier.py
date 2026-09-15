from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from app.connectors.type_map import NUMERIC_TYPES, TEMPORAL_TYPES
from app.connectors.types import DatasetSchema
from app.profiling.naming import MEASURE_NAMES, is_key_eligible
from app.profiling.types import DatasetProfile

Layer = Literal["bronze", "silver", "gold"]
Verdict = Literal["bronze", "silver", "gold", "neutral"]


class LayerSignal(BaseModel):
    """One measurable piece of evidence, and what it argues for."""

    id: str
    label: str
    observation: str
    verdict: Verdict
    weight: float
    detail: str | None = None


class LayerVerdict(BaseModel):
    layer: Layer
    confidence: float
    scores: dict[str, float]
    signals: list[LayerSignal] = Field(default_factory=list)
    summary: str
    recommended_action: str


# --------------------------------------------------------------------------------------
# Naming vocabulary. Names corroborate; they never decide on their own, and the total
# weight naming can contribute is capped below at NAMING_CAP.
# --------------------------------------------------------------------------------------

BRONZE_NAMES = re.compile(r"(^|_)(raw|landing|stg|staging|src|source|ingest|bronze|tmp|temp)(_|$)")
SILVER_NAMES = re.compile(r"(^|_)(silver|clean|cleansed|conformed|curated|std|standard|core)(_|$)")
GOLD_NAMES = re.compile(
    r"(^|_)(gold|dim|fact|f|d|agg|aggregate|mart|summary|report|kpi|metric|rpt|obt)(_|$)"
)

AUDIT_NAMES = re.compile(
    r"(^|_)(ingested_at|loaded_at|load_ts|extract_ts|_ingest|etl_|batch_id|file_name|"
    r"source_file|source_system|record_hash|op_type|is_deleted|cdc_)(_|$)"
)
GRAIN_NAMES = re.compile(
    r"(^|_)(year|quarter|month|week|day|date|period|region|country|segment|category|"
    r"channel|department|team|bucket|band|tier|grain)(_|$)"
)

NAMING_CAP = 1.5


def classify_layer(
    dataset_name: str, schema: DatasetSchema, profile: DatasetProfile
) -> LayerVerdict:
    """
    Decide which medallion layer a dataset's contents are already at.

    The classifier scores measurable properties of the data. It reports every signal it
    used so a data owner can audit the call rather than take it on trust, and the user's
    override always wins over the result.
    """
    signals: list[LayerSignal] = []
    columns = profile.columns or []
    column_count = len(columns) or len(schema.columns) or 1

    # Without profiled columns there is no data evidence, and signals such as "no column
    # uniquely identifies a row" would be vacuously true. Say so instead of guessing.
    if not columns:
        return LayerVerdict(
            layer="bronze",
            confidence=0.2,
            scores={"bronze": 0.0, "silver": 0.0, "gold": 0.0},
            signals=[],
            summary="This dataset has not been profiled, so it is treated as raw for now.",
            recommended_action="Profile the dataset, or set the layer yourself.",
        )

    _type_purity(signals, columns, column_count)
    _null_density(signals, columns, column_count)
    _duplicates(signals, profile)
    _keys(signals, schema, profile)
    _blank_values(signals, columns)
    _audit_columns(signals, columns)
    _shape(signals, columns, profile, column_count)
    _naming(signals, dataset_name)

    scores = {"bronze": 0.0, "silver": 0.0, "gold": 0.0}
    for signal in signals:
        if signal.verdict != "neutral":
            scores[signal.verdict] += signal.weight

    if not signals:
        return LayerVerdict(
            layer="bronze",
            confidence=0.2,
            scores=scores,
            signals=signals,
            summary="Not enough evidence to place this dataset, so it is treated as raw.",
            recommended_action="Profile the dataset, or set the layer yourself.",
        )

    # The layers are a ladder, not three competing buckets. Gold data is also clean, so
    # cleanliness evidence must not be weighed *against* aggregation evidence — it only
    # establishes that the data has left bronze. The decision is therefore two stages.
    #
    #   Stage 1  rawness vs. cleanliness   -> bronze, or at least silver
    #   Stage 2  business-readiness         -> silver, or gold
    raw, clean, business = scores["bronze"], scores["silver"], scores["gold"]

    if raw > clean:
        return LayerVerdict(
            layer="bronze",
            confidence=_margin(raw, clean),
            scores={key: round(value, 2) for key, value in scores.items()},
            signals=sorted(signals, key=lambda s: -s.weight),
            summary=_summary("bronze", signals),
            recommended_action=_action("bronze"),
        )

    # A single substantive gold signal clears this; naming alone (capped at NAMING_CAP)
    # cannot, which is what keeps a table called `sales_summary` from being promoted on
    # its name while its contents are plainly row-level.
    layer: Layer = "gold" if business > NAMING_CAP else "silver"
    confidence = (
        _margin(business, NAMING_CAP) if layer == "gold" else _margin(clean, max(raw, business))
    )

    return LayerVerdict(
        layer=layer,
        confidence=confidence,
        scores={key: round(value, 2) for key, value in scores.items()},
        signals=sorted(signals, key=lambda s: -s.weight),
        summary=_summary(layer, signals),
        recommended_action=_action(layer),
    )


def _margin(leader: float, runner_up: float) -> float:
    """Confidence from how decisively the winning evidence beats the alternative."""
    if leader <= 0:
        return 0.2
    return round(min(0.98, 0.45 + 0.55 * ((leader - runner_up) / leader)), 2)


# --------------------------------------------------------------------------------------
# individual signals
# --------------------------------------------------------------------------------------


def _type_purity(signals: list[LayerSignal], columns: list, column_count: int) -> None:
    mistyped = [c for c in columns if c.shadow_type]
    ratio = len(mistyped) / column_count
    if not columns:
        return
    if ratio >= 0.2:
        examples = ", ".join(f"{c.name} -> {c.shadow_type}" for c in mistyped[:3])
        signals.append(
            LayerSignal(
                id="type_purity",
                label="Types not applied",
                observation=f"{len(mistyped)} of {column_count} columns hold text that is really "
                f"numeric, date or boolean data",
                verdict="bronze",
                weight=2.5 if ratio >= 0.4 else 1.8,
                detail=examples,
            )
        )
    elif ratio == 0:
        signals.append(
            LayerSignal(
                id="type_purity",
                label="Types applied",
                observation="Every column carries a type that matches its values",
                verdict="silver",
                weight=1.5,
            )
        )


def _null_density(signals: list[LayerSignal], columns: list, column_count: int) -> None:
    if not columns:
        return
    mean_null = sum(c.null_pct for c in columns) / column_count
    very_sparse = [c for c in columns if c.null_pct > 0.6]
    if mean_null >= 0.25 or len(very_sparse) >= max(2, column_count // 4):
        signals.append(
            LayerSignal(
                id="null_density",
                label="Sparse columns",
                observation=f"{mean_null:.0%} of values are missing on average"
                + (f"; {len(very_sparse)} columns are over 60% empty" if very_sparse else ""),
                verdict="bronze",
                weight=1.6,
                detail=", ".join(c.name for c in very_sparse[:4]) or None,
            )
        )
    elif mean_null <= 0.05:
        signals.append(
            LayerSignal(
                id="null_density",
                label="Few gaps",
                observation=f"Only {mean_null:.1%} of values are missing",
                verdict="silver",
                weight=1.0,
            )
        )


def _duplicates(signals: list[LayerSignal], profile: DatasetProfile) -> None:
    ratio = profile.duplicate_row_ratio
    if ratio >= 0.01:
        signals.append(
            LayerSignal(
                id="duplicates",
                label="Duplicate rows",
                observation=f"{ratio:.1%} of sampled rows are exact duplicates of another row",
                verdict="bronze",
                weight=2.2 if ratio >= 0.05 else 1.4,
            )
        )
    elif profile.sampled_rows > 50:
        signals.append(
            LayerSignal(
                id="duplicates",
                label="No duplicate rows",
                observation="Every sampled row is distinct",
                verdict="silver",
                weight=0.9,
            )
        )


def _keys(signals: list[LayerSignal], schema: DatasetSchema, profile: DatasetProfile) -> None:
    declared = [c.name for c in schema.columns if c.primary_key]
    if declared:
        signals.append(
            LayerSignal(
                id="key",
                label="Primary key declared",
                observation=f"The source declares {', '.join(declared)} as the primary key",
                verdict="silver",
                weight=1.8,
            )
        )
        return
    if profile.key_candidates:
        key = profile.key_candidates[0]
        exact = any(c.exact_counts for c in profile.columns if c.name in key)
        signals.append(
            LayerSignal(
                id="key",
                label="Key identified",
                observation=f"{' + '.join(key)} uniquely identifies every row",
                verdict="silver",
                weight=1.4 if exact else 1.0,
                detail=None if exact else "Established from the sample rather than a full scan.",
            )
        )
        return
    signals.append(
        LayerSignal(
            id="key",
            label="No key",
            observation="No column or pair of columns uniquely identifies a row",
            verdict="bronze",
            weight=1.8,
        )
    )


def _blank_values(signals: list[LayerSignal], columns: list) -> None:
    untrimmed = [c for c in columns if c.blank_count > 0]
    if untrimmed:
        signals.append(
            LayerSignal(
                id="blanks",
                label="Whitespace-only values",
                observation=f"{len(untrimmed)} columns contain values that are blank but not null",
                verdict="bronze",
                weight=1.2,
                detail=", ".join(c.name for c in untrimmed[:4]),
            )
        )


def _audit_columns(signals: list[LayerSignal], columns: list) -> None:
    audit = [c for c in columns if AUDIT_NAMES.search(_norm(c.name))]
    if audit:
        signals.append(
            LayerSignal(
                id="ingestion_metadata",
                label="Ingestion metadata present",
                observation="Columns carrying load and change-capture metadata are still attached",
                verdict="bronze",
                weight=1.5,
                detail=", ".join(c.name for c in audit[:4]),
            )
        )


def _shape(
    signals: list[LayerSignal], columns: list, profile: DatasetProfile, column_count: int
) -> None:
    """Distinguish a normalised entity table from a wide, pre-aggregated reporting table."""
    measures = [
        c
        for c in columns
        if c.logical_type in NUMERIC_TYPES and MEASURE_NAMES.search(_norm(c.name))
    ]
    grains = [
        c
        for c in columns
        if GRAIN_NAMES.search(_norm(c.name)) or c.logical_type in TEMPORAL_TYPES
    ]
    # An aggregate has few distinct values per dimension relative to its measure count:
    # a handful of grain columns, several measures, and no row-level identifier.
    identifiers = [
        c for c in columns
        if c.is_unique and is_key_eligible(c.name, c.logical_type, c.shadow_type)
    ]

    if len(measures) >= 2 and grains and not identifiers:
        signals.append(
            LayerSignal(
                id="aggregated",
                label="Pre-aggregated measures",
                observation=f"{len(measures)} measure columns summarised by "
                f"{len(grains)} grain columns, with no row-level identifier",
                verdict="gold",
                weight=2.6,
                detail=", ".join(c.name for c in measures[:4]),
            )
        )
    elif len(measures) >= 2 and column_count >= 12:
        signals.append(
            LayerSignal(
                id="wide_reporting",
                label="Wide reporting shape",
                observation=f"{column_count} columns combining descriptive attributes with "
                f"{len(measures)} measures, which is a mart rather than a source table",
                verdict="gold",
                weight=1.6,
                detail=", ".join(c.name for c in measures[:4]),
            )
        )

    low_cardinality = [
        c for c in columns if c.distinct_count is not None and 1 < c.distinct_count <= 12
    ]
    if len(low_cardinality) >= 2 and profile.sampled_rows > 100 and not measures:
        signals.append(
            LayerSignal(
                id="normalised",
                label="Normalised entity shape",
                observation="Categorical attributes with stable, small domains and no "
                "pre-computed measures",
                verdict="silver",
                weight=1.1,
                detail=", ".join(c.name for c in low_cardinality[:4]),
            )
        )


def _naming(signals: list[LayerSignal], dataset_name: str) -> None:
    """
    Corroborate only.

    A name is a label someone chose, not a property of the data. It is capped well below
    any single data signal, so it can strengthen a verdict the data already supports but
    cannot produce one on its own.
    """
    name = _norm(dataset_name)
    candidates = ((BRONZE_NAMES, "bronze"), (GOLD_NAMES, "gold"), (SILVER_NAMES, "silver"))
    for pattern, verdict in candidates:
        match = pattern.search(name)
        if match:
            signals.append(
                LayerSignal(
                    id="naming",
                    label="Naming convention",
                    observation=f"The name contains '{match.group(0).strip('_')}', which "
                    f"conventionally marks a {verdict} table",
                    verdict=verdict,  # type: ignore[arg-type]
                    weight=NAMING_CAP,
                    detail="Naming only reinforces the data evidence; it never decides the layer.",
                )
            )
            return


# --------------------------------------------------------------------------------------
# narrative
# --------------------------------------------------------------------------------------


def _summary(layer: Layer, signals: list[LayerSignal]) -> str:
    supporting = [s for s in signals if s.verdict == layer]
    supporting.sort(key=lambda s: -s.weight)
    lead = supporting[0].observation.lower() if supporting else "the available evidence"
    if layer == "bronze":
        return f"Raw source data: {lead}."
    if layer == "silver":
        return f"Conformed data: {lead}."
    return f"Business-ready data: {lead}."


def _action(layer: Layer) -> str:
    return {
        "bronze": "Run the bronze-to-silver refinement to type, deduplicate and key this data.",
        "silver": "Run the silver-to-gold build to derive metrics and a reporting grain.",
        "gold": "Publish this straight to the semantic model and dashboards.",
    }[layer]


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
