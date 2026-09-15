from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase
from app.models.entities import TenantOwned


class DatasetWatermark(TenantOwned, TimestampedBase):
    """
    How far the last successful load of one dataset got.

    One row per dataset. Updated only after a load has been written, so a crash mid-load
    leaves the watermark where it was and the next run re-reads that window rather than
    skipping it. Re-reading is safe because the write is a MERGE; skipping would not be.
    """

    __tablename__ = "dataset_watermarks"
    __table_args__ = (UniqueConstraint("dataset_id", name="uq_watermark_dataset"),)

    connection_id: Mapped[str] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), index=True
    )
    dataset_id: Mapped[str] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), index=True
    )

    strategy: Mapped[str] = mapped_column(String(20), default="full")
    column_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    token: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rows_total: Mapped[int] = mapped_column(BigInteger, default=0)


class IngestedFile(TenantOwned, TimestampedBase):
    """
    One file version that has been landed.

    Keyed on (dataset, file, etag) so re-running never re-downloads a file whose content
    has not changed, and a genuinely modified file is treated as new work rather than
    being skipped because its name is familiar.
    """

    __tablename__ = "ingested_files"
    __table_args__ = (
        UniqueConstraint("dataset_id", "file_key", "etag", name="uq_file_version"),
    )

    connection_id: Mapped[str] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), index=True
    )
    dataset_id: Mapped[str] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), index=True
    )

    file_key: Mapped[str] = mapped_column(String(900), index=True)
    file_name: Mapped[str] = mapped_column(String(400))
    etag: Mapped[str] = mapped_column(String(200), default="")
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_modified_at: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # LANDED -> the bytes are in bronze. PROCESSED -> it has been through silver.
    # FAILED -> it errored; a later run will retry it rather than skip it.
    status: Mapped[str] = mapped_column(String(20), default="LANDED")
    rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
