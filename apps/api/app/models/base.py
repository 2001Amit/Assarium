from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime | None) -> datetime | None:
    """
    Attach UTC to a timestamp that came back from the store without it.

    SQLite has no timezone type and returns naive datetimes; a Postgres column declared
    without a timezone does the same. Comparing one of those against an aware datetime
    raises TypeError, so every stored timestamp goes through here before it is compared.
    """
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class Base(DeclarativeBase):
    pass


class TimestampedBase(Base):
    __abstract__ = True

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
