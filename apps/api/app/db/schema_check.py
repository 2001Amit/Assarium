"""
Detect a database whose shape no longer matches the models.

`create_all` creates missing tables. It does not alter existing ones, so a column added
to a model after the database was first created simply is not there - and nothing says
so until a query fails at request time with `no such column`, which looks like a bug in
the feature rather than a stale database.

This turns that into a startup failure that names the missing columns and says what to
do about it.
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine, inspect

from app.core.errors import ConfigurationError
from app.models.base import Base

logger = logging.getLogger("assarium.db")


def find_drift(engine: Engine) -> dict[str, list[str]]:
    """Columns each existing table is missing, compared with the models."""
    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    drift: dict[str, list[str]] = {}

    for table in Base.metadata.sorted_tables:
        if table.name not in present:
            continue  # create_all will make it
        actual = {column["name"] for column in inspector.get_columns(table.name)}
        missing = [c.name for c in table.columns if c.name not in actual]
        if missing:
            drift[table.name] = missing
    return drift


def assert_schema_matches(engine: Engine) -> None:
    """
    Refuse to serve traffic against a stale database.

    Failing at startup is the whole point. The alternative is a process that looks
    healthy and returns a 500 the first time somebody touches the affected feature -
    which is discovered by a customer rather than by a deploy.
    """
    drift = find_drift(engine)
    if not drift:
        return

    lines = [f"  {table}: missing {', '.join(columns)}" for table, columns in sorted(drift.items())]
    is_sqlite = engine.url.get_backend_name() == "sqlite"
    remedy = (
        "This is a local SQLite database from before those columns existed. Delete it and "
        "let it be recreated:\n    rm ~/.assarium/assarium.db"
        if is_sqlite
        else "Apply the migration that adds these columns before deploying this version."
    )

    raise ConfigurationError(
        "Refusing to start: the database is missing columns the code expects, so "
        "requests would fail at run time with errors that look like feature bugs.\n"
        + "\n".join(lines)
        + f"\n\n{remedy}"
    )
