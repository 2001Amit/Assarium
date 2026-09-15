from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.engine.base import Layer

AttributeRole = Literal["key", "foreign_key", "dimension", "time", "attribute"]
Aggregation = Literal["sum", "avg", "min", "max", "count", "count_distinct", "median", "ratio"]
TimeGrain = Literal["day", "week", "month", "quarter", "year"]
Cardinality = Literal["many_to_one", "one_to_one", "one_to_many"]

FilterOperator = Literal[
    "eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte",
    "contains", "starts_with", "between", "is_null", "is_not_null",
]


class Attribute(BaseModel):
    """A column exposed to the model as something you can group or filter by."""

    name: str
    label: str
    column: str
    logical_type: str
    role: AttributeRole = "attribute"
    description: str | None = None
    # Distinct-value count from profiling, used to decide what is worth grouping by.
    cardinality: int | None = None
    contains_pii: bool = False
    #: Which kind, when profiling could tell. The masking strategy depends on it - an
    #: email keeps its domain, a card number keeps nothing - so "it is personal" is not
    #: enough on its own.
    pii_kind: str | None = None
    hidden: bool = False


class Entity(BaseModel):
    """A business object, backed by exactly one table in the warehouse."""

    id: str
    name: str
    label: str
    description: str | None = None
    layer: Layer
    table: str
    primary_key: list[str] = Field(default_factory=list)
    attributes: list[Attribute] = Field(default_factory=list)
    # True when this table carries the measures rather than describing something.
    is_fact: bool = False
    row_count: int = 0

    def attribute(self, name: str) -> Attribute | None:
        return next((a for a in self.attributes if a.name == name), None)


class Measure(BaseModel):
    """
    A governed metric definition.

    A measure exists here or it does not exist at all. Nothing downstream - not a
    dashboard tile, not the chat - may invent an aggregation over a raw column, because
    that is precisely how two people end up with two different revenue numbers.
    """

    id: str
    name: str
    label: str
    description: str | None = None
    entity_id: str
    aggregation: Aggregation
    column: str | None = None
    # For ratio measures: numerator and denominator measure ids.
    numerator: str | None = None
    denominator: str | None = None
    format: Literal["number", "currency", "percent", "duration"] = "number"
    decimals: int = 0
    # Filter applied to every use of this measure, e.g. status <> 'cancelled'.
    constraint: str | None = None
    generated: bool = True


class Join(BaseModel):
    """A traversable relationship between two entities."""

    id: str
    from_entity: str
    from_column: str
    to_entity: str
    to_column: str
    cardinality: Cardinality
    confidence: float = 1.0
    kind: Literal["declared", "inferred", "manual"] = "inferred"


class SemanticModel(BaseModel):
    connection_id: str
    entities: list[Entity] = Field(default_factory=list)
    measures: list[Measure] = Field(default_factory=list)
    joins: list[Join] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def entity(self, entity_id: str) -> Entity | None:
        return next((e for e in self.entities if e.id == entity_id), None)

    def measure(self, measure_id: str) -> Measure | None:
        return next((m for m in self.measures if m.id == measure_id), None)


# --------------------------------------------------------------------------------------
# querying
# --------------------------------------------------------------------------------------


class Filter(BaseModel):
    # "entity_id.attribute_name"
    field: str
    operator: FilterOperator
    values: list[Any] = Field(default_factory=list)


class OrderBy(BaseModel):
    # A measure id or a dimension reference.
    field: str
    direction: Literal["asc", "desc"] = "desc"


class MetricQuery(BaseModel):
    """
    The only way to ask the warehouse a question.

    Everything named here must already exist in the model. There is no free-text SQL
    path, so an unanswerable question fails loudly instead of returning a number that
    looks right.
    """

    measures: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    time_dimension: str | None = None
    time_grain: TimeGrain | None = None
    order_by: list[OrderBy] = Field(default_factory=list)
    limit: int = 500


class CompiledQuery(BaseModel):
    sql: str
    params: list[Any] = Field(default_factory=list)
    # Output column names in order, and what each one is.
    dimension_columns: list[str] = Field(default_factory=list)
    measure_columns: list[str] = Field(default_factory=list)
    base_entity: str
    joined_entities: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
