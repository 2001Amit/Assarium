from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Strategy = Literal["full", "column", "file", "delta_token"]


@dataclass
class WatermarkState:
    """Where the last successful load of one dataset got to."""

    strategy: Strategy = "full"
    #: Column used as the high-water mark, for the "column" strategy.
    column: str | None = None
    #: The highest value already loaded. Exclusive lower bound for the next read.
    value: str | None = None
    #: Opaque cursor from the source, for sources that issue one (Graph delta).
    token: str | None = None
    last_success_at: datetime | None = None
    rows_total: int = 0


@dataclass
class FileChange:
    """One file the source says is new or modified since the last load."""

    path: list[str]
    name: str
    etag: str | None = None
    size_bytes: int | None = None
    modified_at: str | None = None
    #: Set when the source reports a deletion rather than a change.
    deleted: bool = False


@dataclass
class IngestionPlan:
    """
    What the next load will actually read, and why.

    Built before any data moves so the decision can be shown to a user, logged, and
    tested without touching the source. A plan that reads everything says so, and says
    why - "no usable watermark column" is a fact the user can act on, whereas silently
    reloading a billion rows is not.
    """

    strategy: Strategy
    reason: str
    #: True when this load replaces the target rather than merging into it.
    full_refresh: bool = True

    column: str | None = None
    #: Exclusive lower bound. Rows at or below this were loaded already.
    since: str | None = None
    #: Inclusive upper bound, snapshotted before reading so rows arriving mid-read are
    #: picked up by the *next* run instead of being skipped by an advanced watermark.
    ceiling: str | None = None

    token: str | None = None
    files: list[FileChange] = field(default_factory=list)
    skipped_files: int = 0

    estimated_rows: int | None = None

    @property
    def has_work(self) -> bool:
        if self.strategy == "file":
            return bool(self.files)
        if self.strategy in {"column", "delta_token"}:
            return True
        return True

    def describe(self) -> str:
        if self.strategy == "full":
            return f"Full load. {self.reason}"
        if self.strategy == "file":
            changed = len(self.files)
            skipped = f", {self.skipped_files} unchanged" if self.skipped_files else ""
            return f"Incremental by file: {changed} new or modified{skipped}. {self.reason}"
        if self.strategy == "column":
            window = f"{self.column} > {self.since}" if self.since else f"all of {self.column}"
            return f"Incremental by column: {window}. {self.reason}"
        return f"Incremental by source cursor. {self.reason}"

    def next_state(self, rows_read: int, previous: WatermarkState) -> WatermarkState:
        """The watermark to persist after this plan has been read successfully."""
        return WatermarkState(
            strategy=self.strategy,
            column=self.column,
            # Advance to the snapshotted ceiling, never to "now": anything written after
            # the snapshot has not been read yet and must not be skipped.
            value=self.ceiling if self.strategy == "column" else previous.value,
            token=self.token if self.strategy == "delta_token" else previous.token,
            rows_total=previous.rows_total + rows_read,
        )


@dataclass
class IngestionResult:
    plan: IngestionPlan
    rows_read: int = 0
    files_processed: int = 0
    state: WatermarkState | None = None
    notes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
