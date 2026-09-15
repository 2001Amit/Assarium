from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """One turn in a conversation."""

    role: Literal["user", "assistant", "system"]
    content: str
    # When the assistant compiled a metric query to answer, it is attached so the
    # frontend can render an inline result.
    query_result: ChatQueryResult | None = None


class ChatQueryResult(BaseModel):
    """The metric data that backs an assistant answer."""

    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    elapsed_ms: int = 0
    truncated: bool = False
    sql: str = ""
    dimension_columns: list[str] = Field(default_factory=list)
    measure_columns: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    # Hint for the frontend on how best to present the data.
    chart_hint: Literal["bar", "line", "table", "stat", "none"] = "table"


class ChatRequest(BaseModel):
    messages: list[ChatMessage]


class ChatResponse(BaseModel):
    message: ChatMessage
    # Returned so the frontend can render inline charts without a second round-trip.
    query_result: ChatQueryResult | None = None


class KpiExplanation(BaseModel):
    tile_id: str
    measure: str
    explanation: str
    suggestions: list[str] = Field(default_factory=list)
