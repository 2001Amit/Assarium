from __future__ import annotations

import itertools
import logging
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from app.core.config import get_settings
from app.core.errors import QueryError
from app.engine.base import LAYERS, Engine, Layer, QueryResult, TableRef, WriteMode

logger = logging.getLogger("assarium.engine.duckdb")


class DuckDBEngine(Engine):
    """
    Local medallion warehouse.

    Each layer is a separate database file, attached under its own name. That keeps the
    layers on genuinely separate storage - a bronze file can be archived or dropped
    without touching gold - while still allowing a single query to join across them.
    """

    name = "duckdb"

    def __init__(self, directory: Path | None = None, tenant_id: str | None = None):
        base_dir = directory or get_settings().warehouse_dir
        if tenant_id:
            from app.engine.catalog_manager import sanitize_tenant_slug
            self.directory = base_dir / sanitize_tenant_slug(tenant_id)
        else:
            self.directory = base_dir
            
        self.directory.mkdir(parents=True, exist_ok=True)
        self._connection: duckdb.DuckDBPyConnection | None = None
        self._lock = threading.Lock()
        self._names = itertools.count()

    # -- lifecycle --------------------------------------------------------------------

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        # Double-checked under a lock: FastAPI runs sync endpoints in a threadpool, so
        # two requests can arrive here at once on a cold engine.
        if self._connection is None:
            with self._lock:
                if self._connection is None:
                    self._connection = duckdb.connect()
                    self.ensure_layers()
        return self._connection

    def _cursor(self) -> duckdb.DuckDBPyConnection:
        """
        A private connection for this call, sharing the same database instance.

        A DuckDB connection object cannot be used by two threads at once - results
        interleave and rows come back attached to the wrong query. `cursor()` returns an
        independent connection over the same instance, so attached layers and any
        registered data stay visible while each request gets its own execution state.
        """
        return self.connection.cursor()

    def ensure_layers(self) -> None:
        connection = self._connection
        if connection is None:  # pragma: no cover - only via the property above
            return
        attached = {
            row[0]
            for row in connection.execute(
                "SELECT database_name FROM duckdb_databases()"
            ).fetchall()
        }
        for layer in LAYERS:
            if layer in attached:
                continue
            path = str(self.directory / f"{layer}.duckdb").replace("'", "''")
            connection.execute(f"ATTACH IF NOT EXISTS '{path}' AS {layer}")

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    # -- identifiers ------------------------------------------------------------------

    def quote(self, identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def qualified(self, layer: Layer, name: str) -> str:
        return f"{layer}.main.{self.quote(name)}"

    # -- writes -----------------------------------------------------------------------

    def write(
        self,
        layer: Layer,
        name: str,
        batches: Iterator[pa.RecordBatch],
        schema: pa.Schema,
        mode: WriteMode = "replace",
    ) -> TableRef:
        target = self.qualified(layer, name)
        connection = self._cursor()
        # Registered views live on the connection, so two concurrent writes must not
        # share a name.
        token = next(self._names)
        shell, batch_view = f"_assarium_shell_{token}", f"_assarium_batch_{token}"
        written = 0

        try:
            if mode == "replace":
                # Create the shell from the schema so an empty stream still yields a table
                # with the right columns rather than nothing at all.
                empty = pa.Table.from_batches([], schema=schema)
                connection.register(shell, empty)
                connection.execute(
                    f"CREATE OR REPLACE TABLE {target} AS SELECT * FROM {shell}"
                )
                connection.unregister(shell)

            for batch in batches:
                if batch.num_rows == 0:
                    continue
                table = pa.Table.from_batches([batch], schema=batch.schema)
                connection.register(batch_view, table)
                connection.execute(f"INSERT INTO {target} SELECT * FROM {batch_view}")
                connection.unregister(batch_view)
                written += batch.num_rows
        except duckdb.Error as exc:
            raise QueryError(f"Could not write {layer}.{name}: {str(exc).splitlines()[0]}") from exc

        return TableRef(
            layer=layer,
            name=name,
            qualified=target,
            row_count=self.row_count(layer, name),
            column_names=[field.name for field in schema],
        )

    # -- reads ------------------------------------------------------------------------

    def execute(
        self, sql: str, limit: int | None = None, params: list[Any] | None = None
    ) -> QueryResult:
        started = time.perf_counter()
        effective = (
            sql
            if limit is None
            else f"SELECT * FROM ({sql}) AS _assarium_q LIMIT {int(limit) + 1}"
        )
        def run() -> tuple[list[str], list[list[Any]]]:
            cursor = self._cursor().execute(effective, params or [])
            names = [description[0] for description in (cursor.description or [])]
            return names, [list(row) for row in cursor.fetchall()]

        try:
            columns, rows = self.run_bounded(run)
        except duckdb.Error as exc:
            raise QueryError(str(exc).splitlines()[0][:400]) from exc

        truncated = limit is not None and len(rows) > limit
        if truncated:
            rows = rows[:limit]

        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            truncated=truncated,
            sql=sql,
        )

    def cancel_running_query(self) -> None:
        """DuckDB interrupts whatever the connection is currently executing."""
        connection = self._connection
        if connection is None:
            return
        try:
            connection.interrupt()
        except Exception:  # noqa: BLE001 - the query may already have finished
            pass

    def execute_ddl(self, sql: str) -> None:
        try:
            self._cursor().execute(sql)
        except duckdb.Error as exc:
            raise QueryError(str(exc).splitlines()[0][:400]) from exc

    def table_exists(self, layer: Layer, name: str) -> bool:
        rows = self._cursor().execute(
            "SELECT 1 FROM duckdb_tables() WHERE database_name = ? AND table_name = ?",
            [layer, name],
        ).fetchall()
        return bool(rows)

    def list_tables(self, layer: Layer) -> list[str]:
        rows = self._cursor().execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = ? ORDER BY 1", [layer]
        ).fetchall()
        return [row[0] for row in rows]

    def describe(self, layer: Layer, name: str) -> list[tuple[str, str]]:
        rows = self._cursor().execute(
            "SELECT column_name, data_type FROM duckdb_columns() "
            "WHERE database_name = ? AND table_name = ? ORDER BY column_index",
            [layer, name],
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def row_count(self, layer: Layer, name: str) -> int:
        if not self.table_exists(layer, name):
            return 0
        result = self._cursor().execute(
            f"SELECT count(*) FROM {self.qualified(layer, name)}"
        ).fetchone()
        return int(result[0]) if result else 0
