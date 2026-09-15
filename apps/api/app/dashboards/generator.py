from __future__ import annotations

import math

from app.dashboards.types import MAX_DONUT_SLICES, Dashboard, Tile
from app.semantic.types import Entity, Measure, SemanticModel

# A dimension is only worth a chart if its domain is small enough to read.
MAX_CHART_CARDINALITY = 24
# Past this many stat tiles the row stops being a summary.
MAX_STATS = 4
MAX_BREAKDOWNS = 3


def generate_dashboard(dashboard_id: str, model: SemanticModel) -> Dashboard:
    """
    Build a first dashboard from the semantic model.

    The composition follows what the data can support, not a template: a headline row
    only if there are measures, a trend only if something carries a date, and a
    breakdown per dimension whose domain is small enough to read. Where the data does
    not support a chart, the dashboard says so instead of drawing an empty one.
    """
    fact = _primary_fact(model)
    if fact is None:
        return Dashboard(
            id=dashboard_id,
            connection_id=model.connection_id,
            name="Overview",
            tiles=[],
            notes=[
                "No table in this model carries measures, so there is nothing to chart. "
                "Add a measure to the semantic model first."
            ],
        )

    measures = _ranked_measures(model, fact)
    time_attribute = next((a for a in fact.attributes if a.role == "time"), None)
    time_reference = f"{fact.id}.{time_attribute.name}" if time_attribute else None

    dimensions = _chartable_dimensions(model, fact)
    tiles: list[Tile] = []
    notes: list[str] = []

    # 1. Headline row. One stat per measure, each with its own trend behind it.
    for index, measure in enumerate(measures[:MAX_STATS]):
        tiles.append(
            Tile(
                id=f"stat_{index}",
                type="stat",
                title=measure.label,
                subtitle=measure.description,
                measures=[measure.id],
                time_dimension=time_reference,
                width=3,
            )
        )

    # 2. The trend. A single series over time reads as one line, so no legend is needed.
    if time_reference and measures:
        tiles.append(
            Tile(
                id="trend",
                type="line",
                title=f"{_trend_measure(measures).label} over time",
                measures=[_trend_measure(measures).id],
                time_dimension=time_reference,
                width=12,
                height=2,
            )
        )
    elif measures:
        notes.append(
            "Nothing in this model carries a date, so there is no trend to show. "
            "A time column on the fact table would add one."
        )

    # 3. Breakdowns. One bar chart per readable dimension, ordered by how few values it
    #    has - the smallest domains make the clearest charts.
    #
    #    These lead with the business measure rather than the row count. "Revenue by
    #    channel" is a question somebody asks; "number of rows by channel" rarely is.
    breakdown_measure = next(
        (m for m in measures if m.aggregation != "count"), measures[0] if measures else None
    )
    for index, (entity, attribute) in enumerate(dimensions[:MAX_BREAKDOWNS]):
        if breakdown_measure is None:
            break
        reference = f"{entity.id}.{attribute.name}"
        others = [f"{e.id}.{a.name}" for e, a in dimensions if f"{e.id}.{a.name}" != reference]
        tiles.append(
            Tile(
                id=f"breakdown_{index}",
                type="bar",
                title=f"{breakdown_measure.label} by {attribute.label.lower()}",
                measures=[breakdown_measure.id],
                dimension=reference,
                drill_path=others,
                limit=MAX_CHART_CARDINALITY,
                width=4,
                height=2,
            )
        )

    # 3b. A composition chart, where one exists that is honest.
    #
    #     A donut is only offered for a dimension small enough that the eye can compare
    #     angles. Beyond that a bar chart is strictly better, so the generator does not
    #     produce a donut nobody should read.
    if dimensions and breakdown_measure is not None:
        smallest = min(dimensions, key=lambda pair: _cardinality(pair[1]))
        entity, attribute = smallest
        if _cardinality(attribute) <= MAX_DONUT_SLICES:
            tiles.append(
                Tile(
                    id="share",
                    type="donut",
                    title=f"Share of {breakdown_measure.label.lower()}",
                    subtitle=f"By {attribute.label.lower()}.",
                    measures=[breakdown_measure.id],
                    dimension=f"{entity.id}.{attribute.name}",
                    limit=MAX_DONUT_SLICES,
                    width=4,
                    height=2,
                )
            )

    # 3c. A combo chart when two measures move together but do not share a unit.
    #
    #     A currency and a percentage on one axis makes the percentage a flat line at
    #     zero. A second axis is the honest way to show them together - and the only
    #     reason to put them on one chart at all is that the relationship is the point.
    paired = _pair_for_combo(measures)
    if time_reference and paired:
        primary, secondary = paired
        tiles.append(
            Tile(
                id="combo",
                type="combo",
                title=f"{primary.label} and {secondary.label.lower()}",
                subtitle="Different units, so the second measure uses the right-hand axis.",
                measures=[primary.id, secondary.id],
                secondary_measures=[secondary.id],
                time_dimension=time_reference,
                width=8,
                height=2,
            )
        )

    if not dimensions:
        notes.append(
            "No dimension on this model has a small enough set of values to break the "
            "measures down by."
        )

    # 4. The detail table. Every chart's contrast relief and the export's source.
    if dimensions and measures:
        entity, attribute = dimensions[0]
        tiles.append(
            Tile(
                id="detail",
                type="table",
                title="Detail",
                subtitle="Every figure above, as numbers.",
                measures=[m.id for m in measures[:MAX_STATS]],
                dimension=f"{entity.id}.{attribute.name}",
                time_dimension=time_reference,
                limit=200,
                width=12,
                height=2,
            )
        )

    return Dashboard(
        id=dashboard_id,
        connection_id=model.connection_id,
        name=f"{fact.label} overview",
        description=f"Generated from the {fact.label} model.",
        tiles=tiles,
        time_dimension=time_reference,
        notes=notes,
    )


# --------------------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------------------


def _cardinality(attribute) -> int:
    """How many distinct values a dimension has, when the profile knew."""
    value = getattr(attribute, "cardinality", None)
    return value if isinstance(value, int) and value > 0 else MAX_CHART_CARDINALITY


def _pair_for_combo(measures: list[Measure]) -> tuple[Measure, Measure] | None:
    """
    Two measures worth showing on one chart with two axes.

    They have to differ in unit, otherwise a second axis invents a difference that is not
    there - two currencies on separate scales look like a relationship and are not one.
    """
    def candidates(pool: list[Measure]) -> tuple[Measure, Measure] | None:
        for primary in pool:
            for secondary in pool:
                if primary.id != secondary.id and primary.format != secondary.format:
                    return primary, secondary
        return None

    # A row count differs in unit from almost everything, so it would always win the
    # first match - and "revenue and number of rows" is the least interesting pair on
    # offer. Try the business measures first and fall back to including counts only if
    # nothing else pairs.
    business = [m for m in measures if m.aggregation != "count"]
    return candidates(business) or candidates(measures)


def _primary_fact(model: SemanticModel) -> Entity | None:
    """
    Choose the entity a dashboard should be built around.

    Measure count alone is a bad signal: a pre-aggregated table generates a measure for
    every numeric column it already summarised, so it can out-score the row-level table
    the business actually runs on. What makes an entity worth leading with is whether it
    can be trended, whether other entities hang off it, and how much of it there is.
    """
    facts = [entity for entity in model.entities if entity.is_fact]
    if not facts:
        return None

    counts: dict[str, int] = {}
    for measure in model.measures:
        counts[measure.entity_id] = counts.get(measure.entity_id, 0) + 1

    hubs = {join.from_entity for join in model.joins}

    def score(entity: Entity) -> float:
        total = 0.0
        # A fact you can put on a timeline is worth far more than one you cannot.
        if any(attribute.role == "time" for attribute in entity.attributes):
            total += 4
        # Being the "many" end of a join is what makes a table a fact.
        if entity.id in hubs:
            total += 3
        # Row count on a log scale: more grain means more to ask, with diminishing returns.
        total += min(3.0, math.log10(max(entity.row_count, 1)))
        # Measures still count, but capped so a wide aggregate cannot win on width alone.
        total += min(2.0, counts.get(entity.id, 0) * 0.4)
        return total

    return max(facts, key=lambda entity: (score(entity), entity.row_count))


def _trend_measure(measures: list[Measure]) -> Measure:
    """The measure a timeline should plot: the business figure, not the row count."""
    return next((m for m in measures if m.aggregation != "count"), measures[0])


def _ranked_measures(model: SemanticModel, fact: Entity) -> list[Measure]:
    """
    Order measures the way somebody would read them.

    A row count leads because it is always meaningful; money next, because that is
    usually what the question is about; averages last, since a total is the headline
    and its average is the follow-up.
    """
    order = {"count": 0, "sum": 1, "avg": 3, "min": 4, "max": 4, "median": 3}
    fact_measures = [m for m in model.measures if m.entity_id == fact.id]

    def rank(measure: Measure) -> tuple[int, int, str]:
        base = order.get(measure.aggregation, 5)
        # Currency outranks a bare number at the same aggregation.
        money = 0 if measure.format == "currency" else 1
        return (base, money, measure.label)

    return sorted(fact_measures, key=rank)


def _chartable_dimensions(model: SemanticModel, fact: Entity) -> list[tuple[Entity, object]]:
    """
    Dimensions worth drawing, nearest first.

    The fact's own columns come before a joined entity's, because a local breakdown
    needs no join and is what a reader reaches for first.
    """
    found: list[tuple[Entity, object, int, int]] = []

    reachable = {fact.id}
    for join in model.joins:
        if join.from_entity == fact.id and join.cardinality != "one_to_many":
            reachable.add(join.to_entity)

    for entity in model.entities:
        if entity.id not in reachable:
            continue
        distance = 0 if entity.id == fact.id else 1
        for attribute in entity.attributes:
            if attribute.hidden or attribute.contains_pii:
                continue
            if attribute.role not in {"dimension", "key"}:
                continue
            cardinality = attribute.cardinality or 0
            if cardinality < 2 or cardinality > MAX_CHART_CARDINALITY:
                continue
            found.append((entity, attribute, distance, cardinality))

    found.sort(key=lambda item: (item[2], item[3]))
    return [(entity, attribute) for entity, attribute, _, _ in found]
