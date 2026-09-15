from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase
from app.models.entities import TenantOwned


class Schedule(TenantOwned, TimestampedBase):
    """A recurring pipeline run for one connection."""

    __tablename__ = "schedules"
    __table_args__ = (UniqueConstraint("connection_id", name="uq_schedule_connection"),)

    connection_id: Mapped[str] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), index=True
    )

    cron: Mapped[str] = mapped_column(String(120), default="0 2 * * *")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Retries use exponential backoff. Reset to zero on any success, so a schedule that
    # recovers does not stay throttled.
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Held while a run is in flight so two workers cannot start the same pipeline. The
    # timestamp lets a lock left behind by a crashed process expire instead of wedging
    # the schedule forever.
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
