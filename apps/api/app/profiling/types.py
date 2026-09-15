from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

PiiKind = Literal[
    "email", "phone", "national_id", "credit_card", "iban", "ip_address",
    "postal_code", "date_of_birth", "person_name", "street_address", "coordinates",
]


class PiiFinding(BaseModel):
    kind: PiiKind
    confidence: float
    # What led to the call, so a data owner can audit it rather than trust it.
    basis: Literal["name", "value", "name+value"]
    matched_ratio: float = 0.0


class TopValue(BaseModel):
    value: str
    count: int
    pct: float


class ColumnProfile(BaseModel):
    name: str
    native_type: str
    logical_type: str
    position: int

    null_count: int = 0
    null_pct: float = 0.0
    distinct_count: int | None = None
    distinct_pct: float | None = None
    is_unique: bool = False
    is_constant: bool = False

    minimum: str | None = None
    maximum: str | None = None
    mean: float | None = None
    stddev: float | None = None
    p25: float | None = None
    p50: float | None = None
    p75: float | None = None

    # Most decimal places seen. Two on a currency-named column means money stored as
    # a float, which is a precision problem waiting to appear in a total.
    decimal_places: int | None = None

    mean_length: float | None = None
    max_length: int | None = None
    blank_count: int = 0

    top_values: list[TopValue] = Field(default_factory=list)

    # A text column whose values are really numbers, dates or booleans: the single
    # strongest signal that a table has not been through a silver-layer conversion.
    shadow_type: str | None = None
    shadow_ratio: float = 0.0

    pii: PiiFinding | None = None
    # True when null/distinct counts came from the source rather than the sample.
    exact_counts: bool = False


class DatasetProfile(BaseModel):
    row_count: int | None = None
    row_count_exact: bool = False
    sampled_rows: int = 0
    duplicate_row_ratio: float = 0.0
    columns: list[ColumnProfile] = Field(default_factory=list)
    # Single columns, and the smallest composite found, that uniquely identify a row.
    key_candidates: list[list[str]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def column(self, name: str) -> ColumnProfile | None:
        return next((c for c in self.columns if c.name == name), None)

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class Relationship(BaseModel):
    """A join path between two selected datasets."""

    from_dataset: str
    from_column: str
    to_dataset: str
    to_column: str
    kind: Literal["declared", "inferred"]
    confidence: float
    # Fraction of the child's sampled values found in the parent's sampled key.
    overlap: float | None = None
    cardinality: Literal["many_to_one", "one_to_one", "unknown"] = "unknown"
