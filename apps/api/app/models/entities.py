from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TimestampedBase


class TenantOwned:
    """
    Mixin for every row that belongs to exactly one customer.

    Denormalised onto each table rather than reached by joining back to `connections`.
    A join-based filter is one someone forgets to write; a column plus the mandatory
    `TenantScope` helper is something a test can prove is present on every table.

    Non-nullable with no default, deliberately. An empty-string default is worse than
    nothing: it is a real value that matches other empty strings, so every unscoped row
    silently collapses into one shared pseudo-tenant that a later filter would happily
    hand out.
    """

    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)


class Connection(TenantOwned, TimestampedBase):
    """A configured link to one source system."""

    __tablename__ = "connections"
    __table_args__ = (
        # Scoped to the tenant: one customer naming a connection "Salesforce" must not
        # stop every other customer from using the same obvious name.
        UniqueConstraint("tenant_id", "name", name="uq_connection_name"),
    )

    name: Mapped[str] = mapped_column(String(200))
    source_id: Mapped[str] = mapped_column(String(50), index=True)
    auth_method: Mapped[str] = mapped_column(String(50), default="default")

    # Non-secret settings, safe to return to the browser and to log.
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # Field name -> secret reference. The *reference* is stored here; the value lives in
    # the configured secret backend (Key Vault in production) and is fetched only at the
    # moment a connection is opened. A dump of this table yields no usable credential.
    secret_refs: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)


    status: Mapped[str] = mapped_column(String(20), default="untested")  # untested|ok|failed
    status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    datasets: Mapped[list[Dataset]] = relationship(
        back_populates="connection", cascade="all, delete-orphan"
    )


class Dataset(TenantOwned, TimestampedBase):
    """A table, object or file the user selected from a connection."""

    __tablename__ = "datasets"
    __table_args__ = (UniqueConstraint("connection_id", "path_key", name="uq_dataset_path"),)

    connection_id: Mapped[str] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(300))
    path: Mapped[list[str]] = mapped_column(JSON)
    # Joined form of `path`, so uniqueness can be enforced by the database.
    path_key: Mapped[str] = mapped_column(String(900), index=True)

    column_schema: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)
    row_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Layer classification: what the evidence says, and what the user decided.
    detected_layer: Mapped[str | None] = mapped_column(String(10), nullable=True)
    layer_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    layer_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    layer_override: Mapped[str | None] = mapped_column(String(10), nullable=True)

    profile: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    profiled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="selected")

    connection: Mapped[Connection] = relationship(back_populates="datasets")

    @property
    def effective_layer(self) -> str | None:
        return self.layer_override or self.detected_layer


class DatasetRelationship(TenantOwned, TimestampedBase):
    """A join path between two selected datasets, declared by the source or inferred."""

    __tablename__ = "dataset_relationships"
    __table_args__ = (
        UniqueConstraint(
            "from_dataset_id", "from_column", "to_dataset_id", "to_column", name="uq_relationship"
        ),
    )

    connection_id: Mapped[str] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), index=True
    )
    from_dataset_id: Mapped[str] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), index=True
    )
    from_column: Mapped[str] = mapped_column(String(300))
    to_dataset_id: Mapped[str] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), index=True
    )
    to_column: Mapped[str] = mapped_column(String(300))

    kind: Mapped[str] = mapped_column(String(10))  # declared | inferred
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    overlap: Mapped[float | None] = mapped_column(Float, nullable=True)
    cardinality: Mapped[str] = mapped_column(String(20), default="unknown")
    # A user can reject an inferred edge; rejected edges stay so they are not re-proposed.
    accepted: Mapped[bool] = mapped_column(Boolean, default=True)


class PipelineRun(TenantOwned, TimestampedBase):
    """One execution of the refinement pipeline over a connection's selected datasets."""

    __tablename__ = "pipeline_runs"

    connection_id: Mapped[str] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), index=True
    )
    engine: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dataset_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    steps: Mapped[list[PipelineStep]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="PipelineStep.sequence"
    )


class PipelineStep(TenantOwned, TimestampedBase):
    """One dataset moving one rung: source to bronze, bronze to silver, silver to gold."""

    __tablename__ = "pipeline_steps"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    dataset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dataset_name: Mapped[str] = mapped_column(String(300))

    kind: Mapped[str] = mapped_column(String(20))  # ingest | silver | gold
    layer: Mapped[str] = mapped_column(String(10))
    target_table: Mapped[str | None] = mapped_column(String(300), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="running")
    rows_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rows_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # The exact SQL that ran, so a result can always be traced back to its derivation.
    sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    actions: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    notes: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[PipelineRun] = relationship(back_populates="steps")


class SemanticModelRecord(TenantOwned, TimestampedBase):
    """The governed model for one connection, stored as a single editable document."""

    __tablename__ = "semantic_models"
    __table_args__ = (UniqueConstraint("connection_id", name="uq_semantic_connection"),)

    connection_id: Mapped[str] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), index=True
    )
    document: Mapped[dict[str, Any]] = mapped_column(JSON)
    # Set when a person has edited the model, so a rebuild can warn before overwriting.
    edited: Mapped[bool] = mapped_column(Boolean, default=False)


class DashboardRecord(TenantOwned, TimestampedBase):
    """A dashboard definition. Tiles hold metric queries, never SQL."""

    __tablename__ = "dashboards"

    connection_id: Mapped[str] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    document: Mapped[dict[str, Any]] = mapped_column(JSON)
    generated: Mapped[bool] = mapped_column(Boolean, default=True)
