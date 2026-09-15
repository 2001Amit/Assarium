from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

settings = get_settings()
_url = settings.resolved_database_url
_is_sqlite = _url.startswith("sqlite")

engine = create_engine(
    _url,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    pool_pre_ping=True,
    future=True,
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver level
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """For background work that runs outside a request."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    # Importing a module is what registers its tables on the shared metadata, so every
    # model module has to be listed here or its table silently never gets created.
    from app.identity import models as identity_models  # noqa: F401
    from app.ingestion import models as ingestion_models  # noqa: F401
    from app.models import (
        audit,  # noqa: F401
        base,  # noqa: F401 - registers metadata
        entities,  # noqa: F401
    )
    from app.orchestration import models as orchestration_models  # noqa: F401
    from app.tenancy import models as tenancy_models  # noqa: F401

    base.Base.metadata.create_all(bind=engine)

    # create_all adds missing tables but never alters existing ones, so a column added
    # to a model after first run is silently absent. Catch that here rather than at the
    # first request that needs it.
    from app.db.schema_check import assert_schema_matches

    assert_schema_matches(engine)
