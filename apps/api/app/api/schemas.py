from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field, PlainSerializer


def _as_utc(value: datetime | None) -> str | None:
    """SQLite discards timezone information, so naive values come back as bare UTC.

    Stamping UTC on the way out keeps the browser from reading them as local time.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


UtcDatetime = Annotated[datetime, PlainSerializer(_as_utc, return_type=str | None)]


class ConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source_id: str
    auth_method: str = "default"
    # Raw form values, keyed by the spec's field names. Split server-side.
    values: dict[str, Any] = Field(default_factory=dict)


class ConnectionTestRequest(BaseModel):
    source_id: str
    auth_method: str = "default"
    values: dict[str, Any] = Field(default_factory=dict)
    # When set, unfilled secret fields fall back to this saved connection's stored values,
    # so editing a connection never requires re-typing a password.
    connection_id: str | None = None


class ConnectionRead(BaseModel):
    id: str
    name: str
    source_id: str
    source_name: str
    auth_method: str
    config: dict[str, Any]
    status: str
    status_message: str | None = None
    last_tested_at: UtcDatetime | None = None
    created_at: UtcDatetime
    dataset_count: int = 0


class DatasetSelection(BaseModel):
    paths: list[list[str]]


class DatasetRead(BaseModel):
    id: str
    connection_id: str
    name: str
    path: list[str]
    row_estimate: int | None = None
    column_count: int = 0
    detected_layer: str | None = None
    layer_confidence: float | None = None
    layer_override: str | None = None
    effective_layer: str | None = None
    status: str
    profiled_at: UtcDatetime | None = None


class DatasetDetail(DatasetRead):
    columns: list[dict[str, Any]] = Field(default_factory=list)
    profile: dict[str, Any] | None = None
    layer_evidence: dict[str, Any] | None = None


class LayerOverride(BaseModel):
    # None clears the override and hands the decision back to the classifier.
    layer: str | None = None
