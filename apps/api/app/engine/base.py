from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any, Literal, TypeVar

import pyarrow as pa
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.errors import QueryError

_T = TypeVar("_T")

Layer = Literal["bronze", "silver", "gold"]
LAYERS: tuple[Layer, ...] = ("bronze", "silver", "gold")

WriteMode = Literal["replace", "append"]

logger = logging.getLogger("assarium.engine")


class TableRef(BaseModel):
    """A table the platform owns, inside one medallion layer."""

    layer: Layer
    name: str
    qualified: str
    row_count: int = 0
    column_names: list[str] = Field(default_factory=list)
    bytes_written: int | None = None


class QueryResult(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    elapsed_ms: int
    truncated: bool = False
    # Echoed back so a dashboard tile or chat answer can show exactly what ran.
    sql: str | None = None


class Engine(ABC):
    """
    Where the platform's own refined data lives and where its queries run.

    Two implementations sit behind this: DuckDB over local files, which runs the whole
    medallion on a laptop, and Databricks over Unity Catalog. Nothing above this layer
    knows which one is active.
    """

    name: str

    @abstractmethod
    def ensure_layers(self) -> None:
        """Create the three layer namespaces if they do not exist yet."""

    @abstractmethod
    def qualified(self, layer: Layer, name: str) -> str:
        """Fully-qualified, quoted table identifier for use inside SQL."""

    @abstractmethod
    def write(
        self,
        layer: Layer,
        name: str,
        batches: Iterator[pa.RecordBatch],
        schema: pa.Schema,
        mode: WriteMode = "replace",
    ) -> TableRef:
        """Materialise an Arrow stream as a table in `layer`."""

    @abstractmethod
    def execute(
        self, sql: str, limit: int | None = None, params: list[Any] | None = None
    ) -> QueryResult:
        """Run a read query and return bounded results.

        `params` are bound by the driver. Every value that originates outside the
        semantic model travels this way rather than being formatted into the SQL text.
        """

    @abstractmethod
    def execute_ddl(self, sql: str) -> None:
        """Run a statement that returns no rows."""

    @abstractmethod
    def cancel_running_query(self) -> None:
        """
        Ask the engine to abandon whatever it is running.

        Abstract rather than a no-op default: a new engine that quietly inherits "do
        nothing" would let the timeout bound how long the *caller* waits while the
        warehouse keeps working, and keeps billing. Making it a decision the author has
        to take is the point - an engine with no cancellation may implement `pass`, but
        it has to say so.
        """

    def run_bounded(self, work: Callable[[], _T], *, seconds: int | None = None) -> _T:
        """
        Run a query with a wall-clock ceiling.

        A query with no ceiling is one that can hold a connection, a warehouse slot and a
        request thread indefinitely - one user's mistake becoming everybody's outage. The
        cap was configured as `query_timeout_seconds` from the beginning and applied
        nowhere, which is the same as not having it.

        The work runs on a worker thread so the timeout is enforceable at all; on expiry
        the engine is asked to cancel, so the warehouse stops too rather than finishing
        the query for an audience that has left.
        """
        limit = seconds if seconds is not None else get_settings().query_timeout_seconds
        if not limit or limit <= 0:
            return work()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(work)
            try:
                return future.result(timeout=limit)
            except FuturesTimeout:
                self.cancel_running_query()
                raise QueryError(
                    f"That query ran longer than {limit} seconds and was stopped. "
                    "Narrow the time range, add a filter, or aggregate at a coarser "
                    "grain."
                ) from None

    @abstractmethod
    def table_exists(self, layer: Layer, name: str) -> bool: ...

    @abstractmethod
    def list_tables(self, layer: Layer) -> list[str]: ...

    @abstractmethod
    def describe(self, layer: Layer, name: str) -> list[tuple[str, str]]:
        """Column name and engine type for one of the platform's own tables."""

    @abstractmethod
    def row_count(self, layer: Layer, name: str) -> int: ...

    def merge(
        self,
        layer: Layer,
        name: str,
        batches: Iterator[pa.RecordBatch],
        schema: pa.Schema,
        keys: list[str],
    ) -> TableRef:
        """
        Upsert a stream into an existing table on `keys`.

        Incremental loads must be idempotent: a run that is retried after a crash, or a
        window that is deliberately re-read because the watermark did not advance, has to
        produce the same table either way. MERGE gives that; append does not.

        The incoming rows are staged as a real table rather than held in memory, so a
        large catch-up window does not have to fit in the process.
        """
        if not keys:
            raise QueryError(
                f"Cannot merge into {layer}.{name} without a key. A table with no "
                "column that identifies a row can only be replaced, not merged."
            )

        # No target yet: the first load is simply a write.
        if not self.table_exists(layer, name):
            return self.write(layer, name, batches, schema, mode="replace")

        staging_name = f"_staging_{name}_{uuid.uuid4().hex[:8]}"
        try:
            staged = self.write(layer, staging_name, batches, schema, mode="replace")
            self.execute_ddl(self.merge_sql(layer, name, staging_name, schema, keys))
        finally:
            # Always clean up: a failed merge must not leave a staging table behind that
            # the next run then tries to create again.
            try:
                self.drop(layer, staging_name)
            except Exception:  # noqa: BLE001 - cleanup is best effort
                logger.warning("Could not drop staging table %s.%s", layer, staging_name)

        return TableRef(
            layer=layer,
            name=name,
            qualified=self.qualified(layer, name),
            row_count=self.row_count(layer, name),
            column_names=[field.name for field in schema],
            bytes_written=staged.bytes_written,
        )

    # Columns SCD2 adds. Prefixed so they cannot collide with a source column, and
    # named consistently so a query can filter on _is_current without knowing the table.
    VALID_FROM = "_valid_from"
    VALID_TO = "_valid_to"
    IS_CURRENT = "_is_current"
    CHANGE_HASH = "_change_hash"

    #: Companion table recording which columns each SCD2 table hashes on.
    SCD_META = "_assarium_scd_meta"

    def _scd_tracked_before(self, layer: Layer, name: str) -> list[str] | None:
        """The tracked column set the existing hashes were built from, if known."""
        if not self.table_exists(layer, self.SCD_META):
            return None
        rows = self.execute(
            f"SELECT columns FROM {self.qualified(layer, self.SCD_META)} "
            f"WHERE table_name = '{name}'"
        ).rows
        return rows[0][0].split(",") if rows else None

    def _record_scd_tracked(self, layer: Layer, name: str, tracked: list[str]) -> None:
        meta = self.qualified(layer, self.SCD_META)
        self.execute_ddl(
            f"CREATE TABLE IF NOT EXISTS {meta} (table_name VARCHAR, columns VARCHAR)"
        )
        self.execute_ddl(f"DELETE FROM {meta} WHERE table_name = '{name}'")
        self.execute_ddl(
            f"INSERT INTO {meta} VALUES ('{name}', '{','.join(tracked)}')"
        )

    def hash_expr(self, columns: list[str]) -> str:
        """
        A fingerprint of the tracked columns, used to tell a real change from a re-load.

        Nulls are given a sentinel rather than being allowed to collapse the
        concatenation: without it, ("a", NULL) and (NULL, "a") would hash identically and
        a genuine change would be missed.
        """
        parts = ", ".join(
            f"COALESCE(CAST({self.quote(c)} AS VARCHAR), '<null>')" for c in columns
        )
        return f"MD5(CONCAT_WS('|', {parts}))"

    def merge_scd2(
        self,
        layer: Layer,
        name: str,
        batches: Iterator[pa.RecordBatch],
        schema: pa.Schema,
        keys: list[str],
        tracked: list[str] | None = None,
    ) -> dict[str, int]:
        """
        Load a dimension as slowly-changing type 2.

        A changed row does not overwrite its predecessor: the old version is closed off
        with an end date and the new one is inserted alongside it. Without this, "what
        was the rent last quarter" becomes unanswerable the moment the source updates the
        row - which for a lease book is the normal case, not an edge case.

        Rows whose tracked columns are unchanged are left completely alone, so re-running
        a load does not churn the table or invent history that did not happen.
        """
        if not keys:
            raise QueryError(
                f"Cannot load {layer}.{name} as SCD2 without a key: there is no way to "
                "tell a new row from a new version of an existing one."
            )

        source_columns = [field.name for field in schema]
        # Sorted, so the same set of columns always produces the same hash regardless of
        # the order the caller happened to pass them in.
        tracked = sorted(tracked or [c for c in source_columns if c not in keys])
        target = self.qualified(layer, name)

        staging_name = f"_scd_{name}_{uuid.uuid4().hex[:8]}"
        stats: dict[str, Any] = {"inserted": 0, "expired": 0, "unchanged": 0}

        # A hash computed over a different column set can never match the stored one, so
        # every row would look changed and the whole dimension would be re-versioned.
        # That is sometimes correct - a new column really is new information - but it is
        # never something that should happen quietly.
        previous_tracked = self._scd_tracked_before(layer, name)
        if previous_tracked is not None and previous_tracked != tracked:
            added = sorted(set(tracked) - set(previous_tracked))
            removed = sorted(set(previous_tracked) - set(tracked))
            changes = []
            if added:
                changes.append("added " + ", ".join(added))
            if removed:
                changes.append("removed " + ", ".join(removed))
            stats["tracked_columns_changed"] = "; ".join(changes)
            logger.warning(
                "SCD2 tracked columns for %s.%s changed (%s). Every row will be "
                "versioned once, because the stored change hashes were computed over a "
                "different set of columns.",
                layer, name, stats["tracked_columns_changed"],
            )

        try:
            self.write(layer, staging_name, batches, schema, mode="replace")
            staging = self.qualified(layer, staging_name)
            now = self.current_timestamp()
            hashed = self.hash_expr(tracked)

            if not self.table_exists(layer, name):
                # First load: everything is a new current version.
                columns = ", ".join(self.quote(c) for c in source_columns)
                self.execute_ddl(
                    f"CREATE TABLE {target} AS\n"
                    f"SELECT {columns},\n"
                    f"       {hashed} AS {self.quote(self.CHANGE_HASH)},\n"
                    f"       {now} AS {self.quote(self.VALID_FROM)},\n"
                    f"       CAST(NULL AS TIMESTAMP) AS {self.quote(self.VALID_TO)},\n"
                    f"       TRUE AS {self.quote(self.IS_CURRENT)}\n"
                    f"FROM {staging}"
                )
                stats["inserted"] = self.row_count(layer, name)
                self._record_scd_tracked(layer, name, tracked)
                return stats

            on = " AND ".join(f"t.{self.quote(k)} = s.{self.quote(k)}" for k in keys)
            before = self.row_count(layer, name)

            # Step 1: close off the versions that have actually changed. Ordering
            # matters - this must happen before the insert, or the new version and the
            # old one would both be current at the same instant.
            self.execute_ddl(
                f"MERGE INTO {target} AS t\n"
                f"USING (SELECT *, {hashed} AS {self.quote(self.CHANGE_HASH)} "
                f"FROM {staging}) AS s\n"
                f"ON {on} AND t.{self.quote(self.IS_CURRENT)}\n"
                f"WHEN MATCHED AND t.{self.quote(self.CHANGE_HASH)} "
                f"<> s.{self.quote(self.CHANGE_HASH)} THEN UPDATE SET\n"
                f"  {self.quote(self.VALID_TO)} = {now},\n"
                f"  {self.quote(self.IS_CURRENT)} = FALSE"
            )
            expired_at_step_one = self.execute(
                f"SELECT count(*) FROM {target} "
                f"WHERE NOT {self.quote(self.IS_CURRENT)}"
            ).rows[0][0]

            # Step 2: insert new keys and new versions of changed keys. The join on
            # is_current no longer matches anything expired above, so those rows fall
            # through and are inserted.
            columns = ", ".join(f"s.{self.quote(c)}" for c in source_columns)
            self.execute_ddl(
                f"INSERT INTO {target}\n"
                f"SELECT {columns},\n"
                f"       s.{self.quote(self.CHANGE_HASH)},\n"
                f"       {now}, CAST(NULL AS TIMESTAMP), TRUE\n"
                f"FROM (SELECT *, {hashed} AS {self.quote(self.CHANGE_HASH)} "
                f"FROM {staging}) AS s\n"
                f"LEFT JOIN {target} AS t\n"
                f"  ON {on} AND t.{self.quote(self.IS_CURRENT)}\n"
                f"WHERE t.{self.quote(keys[0])} IS NULL"
            )

            after = self.row_count(layer, name)
            stats["inserted"] = after - before
            stats["expired"] = expired_at_step_one
            stats["unchanged"] = self.execute(
                f"SELECT count(*) FROM {staging}"
            ).rows[0][0] - stats["inserted"]
            self._record_scd_tracked(layer, name, tracked)
        finally:
            try:
                self.drop(layer, staging_name)
            except Exception:  # noqa: BLE001 - cleanup is best effort
                logger.warning("Could not drop SCD staging table %s.%s", layer, staging_name)

        return stats

    def merge_sql(
        self,
        layer: Layer,
        target_name: str,
        staging_name: str,
        schema: pa.Schema,
        keys: list[str],
    ) -> str:
        """
        The MERGE statement. Shared, because DuckDB and Databricks accept the same shape.

        That overlap is deliberate: it means the merge path the tests exercise is the
        same one production runs, which is the main thing a local test fixture usually
        cannot promise.
        """
        target = self.qualified(layer, target_name)
        staging = self.qualified(layer, staging_name)
        columns = [field.name for field in schema]

        missing = [key for key in keys if key not in columns]
        if missing:
            raise QueryError(
                f"Merge key(s) {', '.join(missing)} are not columns of {target_name}."
            )

        on = " AND ".join(f"t.{self.quote(k)} = s.{self.quote(k)}" for k in keys)
        updatable = [c for c in columns if c not in keys]

        clauses = []
        if updatable:
            assignments = ", ".join(f"{self.quote(c)} = s.{self.quote(c)}" for c in updatable)
            clauses.append(f"WHEN MATCHED THEN UPDATE SET {assignments}")
        # A table whose columns are all keys has nothing to update; inserting the new
        # rows is the whole operation, and an empty UPDATE SET is a syntax error.
        insert_columns = ", ".join(self.quote(c) for c in columns)
        insert_values = ", ".join(f"s.{self.quote(c)}" for c in columns)
        clauses.append(
            f"WHEN NOT MATCHED THEN INSERT ({insert_columns}) VALUES ({insert_values})"
        )

        return (
            f"MERGE INTO {target} AS t\n"
            f"USING {staging} AS s\n"
            f"ON {on}\n" + "\n".join(clauses)
        )

    def drop(self, layer: Layer, name: str) -> None:
        self.execute_ddl(f"DROP TABLE IF EXISTS {self.qualified(layer, name)}")

    # -- SQL dialect -------------------------------------------------------------------
    #
    # The refinement steps generate SQL rather than pulling data into Python, so the work
    # happens where the data already sits. These few expressions are everything the
    # generated SQL needs that is not identical across engines.

    #: Logical type -> engine type name, used by generated CAST expressions.
    SQL_TYPES: dict[str, str] = {
        "string": "VARCHAR",
        "integer": "BIGINT",
        "float": "DOUBLE",
        "decimal": "DECIMAL(38, 9)",
        "boolean": "BOOLEAN",
        "date": "DATE",
        "timestamp": "TIMESTAMP",
    }

    @abstractmethod
    def quote(self, identifier: str) -> str:
        """Quote a single identifier for this engine."""

    def sql_type(self, logical_type: str) -> str:
        return self.SQL_TYPES.get(logical_type, "VARCHAR")

    def try_cast(self, expression: str, logical_type: str) -> str:
        """A cast that yields NULL rather than failing the whole load on one bad row."""
        return f"TRY_CAST({expression} AS {self.sql_type(logical_type)})"

    def cast_text(self, expression: str) -> str:
        """
        Cast to the engine's own text type.

        A dialect difference that matters: DuckDB spells it VARCHAR and Spark spells it
        STRING. `sql_type("string")` already knows which, so masking and any other
        string-shaped SQL can be written once and compile on both.
        """
        return f"CAST({expression} AS {self.sql_type('string')})"

    def blank_to_null(self, expression: str) -> str:
        return f"NULLIF(TRIM({expression}), '')"

    def strip_non_numeric(self, expression: str) -> str:
        """
        Remove currency symbols, thousands separators and percent signs.

        The input is cast to text first. The expression is not always applied to a text
        column - a quality rule can be evaluated against a column that has already been
        typed - and the regex function takes text only, so without the cast the SQL fails
        to bind rather than simply returning the value unchanged.
        """
        return f"REGEXP_REPLACE(CAST({expression} AS VARCHAR), '[^0-9eE.+-]', '', 'g')"

    def current_timestamp(self) -> str:
        return "CURRENT_TIMESTAMP"

    def month_trunc(self, expression: str) -> str:
        return self.date_trunc("month", expression)

    #: Time grains the engine can truncate to.
    GRAINS = {"day", "week", "month", "quarter", "year"}

    def date_trunc(self, grain: str, expression: str) -> str:
        if grain not in self.GRAINS:
            raise ValueError(f"Unsupported time grain '{grain}'.")
        return f"DATE_TRUNC('{grain}', {expression})"

    def close(self) -> None:  # noqa: B027 - optional hook, not every engine pools
        """Release engine resources. Safe to call more than once."""

    def __enter__(self) -> Engine:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def is_internal_table(name: str) -> bool:
    """
    Whether a table is the platform's own bookkeeping rather than the user's data.

    Staging tables, SCD metadata and checkpoints all live in the same schema as real
    tables. Without this they show up in the warehouse browser and get picked up by the
    semantic model builder, which is confusing at best and exposes internals at worst.
    """
    return name.startswith("_")


def safe_identifier(value: str) -> str:
    """
    Normalise a source object name into a table name the platform controls.

    Names arrive from other people's systems and go straight into DDL, so this is the
    single place that decides what a table may be called.
    """
    import re

    cleaned = re.sub(r"[^0-9a-zA-Z_]+", "_", value.strip()).strip("_").lower()
    cleaned = re.sub(r"_+", "_", cleaned)
    if not cleaned:
        cleaned = "unnamed"
    if cleaned[0].isdigit():
        cleaned = f"t_{cleaned}"
    return cleaned[:120]
