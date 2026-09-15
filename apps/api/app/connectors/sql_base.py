from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.connectors.base import Connector
from app.connectors.type_map import to_logical_type
from app.connectors.types import (
    BrowseNode,
    ColumnSchema,
    DatasetSchema,
    SampleResult,
)
from app.core.errors import QueryError


class SQLConnector(Connector):
    """
    Shared behaviour for every DBAPI-style source.

    A dialect supplies `_connect()` plus a handful of class attributes; the three-rung
    catalog / schema / table hierarchy, column introspection and sampling then come for
    free. Dialects override any piece where information_schema is not the right answer.
    """

    # Identifier quoting.
    quote_char: str = '"'
    #: Placeholder this driver's DBAPI expects. psycopg and pymysql use "%s"; DuckDB,
    #: SQLite and the Databricks connector use "?". Getting this wrong turns a bound
    #: parameter into a syntax error, so it is declared rather than guessed.
    paramstyle: str = "?"
    # Whether the source exposes a catalog (database) rung above schemas.
    has_catalogs: bool = True
    # Schemas that are engine internals and should never be offered to a user.
    system_schemas: set[str] = {
        "information_schema",
        "pg_catalog",
        "pg_toast",
        "sys",
        "mysql",
        "performance_schema",
        "sys_catalog",
    }
    supports_limit: bool = True  # False for T-SQL, which uses TOP

    _conn: Any = None

    # -- connection -------------------------------------------------------------------

    def _connect(self) -> Any:
        raise NotImplementedError

    @property
    def connection(self) -> Any:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    @contextmanager
    def cursor(self) -> Iterator[Any]:
        cur = self.connection.cursor()
        try:
            yield cur
        finally:
            try:
                cur.close()
            except Exception:  # noqa: BLE001 - a closed cursor is not an error worth raising
                pass

    def fetch(self, sql: str, params: tuple | dict | None = None) -> tuple[list[str], list[list]]:
        try:
            with self.cursor() as cur:
                cur.execute(sql, params) if params else cur.execute(sql)
                if cur.description is None:
                    return [], []
                columns = [d[0] for d in cur.description]
                rows = [list(r) for r in cur.fetchall()]
                return columns, rows
        except Exception as exc:  # noqa: BLE001
            raise QueryError(str(exc).splitlines()[0][:400]) from exc

    # -- identifiers ------------------------------------------------------------------

    def quote(self, identifier: str) -> str:
        q = self.quote_char
        return f"{q}{identifier.replace(q, q * 2)}{q}"

    def qualify(self, path: list[str]) -> str:
        return ".".join(self.quote(part) for part in path)

    # -- hierarchy --------------------------------------------------------------------

    def default_catalog(self) -> str | None:
        return self.config.get("database")

    def list_catalogs(self) -> list[BrowseNode]:
        raise NotImplementedError

    def list_schemas(self, catalog: str | None) -> list[BrowseNode]:
        prefix = [catalog] if catalog else []
        sql = "SELECT schema_name FROM information_schema.schemata ORDER BY 1"
        if catalog and self.has_catalogs:
            sql = (
                f"SELECT schema_name FROM {self.quote(catalog)}.information_schema.schemata "
                "ORDER BY 1"
            )
        _, rows = self.fetch(sql)
        return [
            BrowseNode(
                id=".".join([*prefix, r[0]]),
                name=r[0],
                kind="namespace",
                path=[*prefix, r[0]],
                has_children=True,
            )
            for r in rows
            if str(r[0]).lower() not in self.system_schemas
        ]

    def list_tables(self, catalog: str | None, schema: str) -> list[BrowseNode]:
        prefix = [catalog, schema] if catalog else [schema]
        qualifier = f"{self.quote(catalog)}." if catalog and self.has_catalogs else ""
        _, rows = self.fetch(
            f"SELECT table_name, table_type FROM {qualifier}information_schema.tables "
            f"WHERE table_schema = '{schema}' ORDER BY 1"
        )
        return [
            BrowseNode(
                id=".".join([*prefix, r[0]]),
                name=r[0],
                kind="dataset",
                path=[*prefix, r[0]],
                meta={"object_type": (r[1] or "TABLE").title()},
            )
            for r in rows
        ]

    def browse(self, path: list[str]) -> list[BrowseNode]:
        depth = len(path)
        if not self.has_catalogs:
            if depth == 0:
                return self.list_schemas(None)
            if depth == 1:
                return self.list_tables(None, path[0])
            return []
        if depth == 0:
            return self.list_catalogs()
        if depth == 1:
            return self.list_schemas(path[0])
        if depth == 2:
            return self.list_tables(path[0], path[1])
        return []

    # -- schema -----------------------------------------------------------------------

    def _columns_query(self, catalog: str | None, schema: str, table: str) -> str:
        qualifier = f"{self.quote(catalog)}." if catalog and self.has_catalogs else ""
        return (
            "SELECT column_name, data_type, is_nullable, ordinal_position "
            f"FROM {qualifier}information_schema.columns "
            f"WHERE table_schema = '{schema}' AND table_name = '{table}' "
            "ORDER BY ordinal_position"
        )

    def _primary_keys(self, catalog: str | None, schema: str, table: str) -> set[str]:
        qualifier = f"{self.quote(catalog)}." if catalog and self.has_catalogs else ""
        try:
            _, rows = self.fetch(
                "SELECT kcu.column_name "
                f"FROM {qualifier}information_schema.table_constraints tc "
                f"JOIN {qualifier}information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                " AND tc.table_schema = kcu.table_schema "
                "WHERE tc.constraint_type = 'PRIMARY KEY' "
                f"  AND tc.table_schema = '{schema}' AND tc.table_name = '{table}'"
            )
            return {r[0] for r in rows}
        except QueryError:
            # Several engines expose no constraint metadata at all; key inference in the
            # profiling stage covers that case.
            return set()

    def _split(self, path: list[str]) -> tuple[str | None, str, str]:
        if self.has_catalogs and len(path) == 3:
            return path[0], path[1], path[2]
        if len(path) == 2:
            return (self.default_catalog() if self.has_catalogs else None), path[0], path[1]
        raise QueryError(f"Cannot resolve dataset path {path!r} for {self.spec.name}.")

    def describe(self, path: list[str]) -> DatasetSchema:
        catalog, schema, table = self._split(path)
        _, rows = self.fetch(self._columns_query(catalog, schema, table))
        pks = self._primary_keys(catalog, schema, table)
        columns = [
            ColumnSchema(
                name=r[0],
                native_type=str(r[1]),
                logical_type=to_logical_type(str(r[1])),
                nullable=str(r[2]).upper() in {"YES", "TRUE", "1"},
                position=int(r[3]) if r[3] is not None else i,
                primary_key=r[0] in pks,
            )
            for i, r in enumerate(rows)
        ]
        return DatasetSchema(path=path, name=table, columns=columns)

    def sample(self, path: list[str], limit: int = 100) -> SampleResult:
        target = self.qualify(path)
        sql = (
            f"SELECT * FROM {target} LIMIT {int(limit)}"
            if self.supports_limit
            else f"SELECT TOP {int(limit)} * FROM {target}"
        )
        columns, rows = self.fetch(sql)
        return SampleResult(columns=columns, rows=rows, truncated=len(rows) >= limit)

    def read_batches(self, path: list[str], batch_size: int = 50_000):
        """Stream the table through the driver cursor, one Arrow batch at a time."""
        import pyarrow as pa  # noqa: F401 - imported for the caller's type expectations

        from app.engine.arrow import arrow_schema, rows_to_batch

        schema_info = self.describe(path)
        target = arrow_schema(schema_info.columns)

        with self.cursor() as cur:
            try:
                cur.execute(f"SELECT * FROM {self.qualify(path)}")
            except Exception as exc:  # noqa: BLE001
                raise QueryError(str(exc).splitlines()[0][:400]) from exc

            # Trust the cursor's own column order over information_schema: a view can
            # report columns in a different order than it returns them.
            positions = {d[0]: i for i, d in enumerate(cur.description or [])}
            ordered = [c for c in schema_info.columns if c.name in positions]
            ordered.sort(key=lambda c: positions[c.name])
            target = arrow_schema(ordered)

            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                yield rows_to_batch([list(r) for r in rows], ordered, target)

    # -- incremental reads ---------------------------------------------------------------

    def watermark_ceiling(self, path: list[str], column: str) -> str | None:
        """
        The highest value of the watermark column right now.

        Snapshotted before reading and used as an inclusive upper bound, so rows written
        while the read is in flight are picked up by the next run rather than being
        skipped by a watermark that ran ahead of what was actually read.
        """
        try:
            _, rows = self.fetch(
                f"SELECT MAX({self.quote(column)}) FROM {self.qualify(path)}"
            )
        except QueryError:
            return None
        if not rows or rows[0][0] is None:
            return None
        return str(rows[0][0])

    def plan_ingestion(self, path: list[str], schema, state):
        from app.ingestion.planner import plan_column_watermark

        preferred = state.column if state.strategy == "column" else None
        candidates = _watermark_names(schema, preferred)
        ceiling = self.watermark_ceiling(path, candidates[0]) if candidates else None
        return plan_column_watermark(schema, state, ceiling, preferred_column=preferred)

    def read_plan(self, path: list[str], plan, batch_size: int = 50_000):
        """Stream only the rows the plan selects."""
        from app.engine.arrow import arrow_schema, rows_to_batch

        schema_info = self.describe(path)
        target = arrow_schema(schema_info.columns)

        sql = f"SELECT * FROM {self.qualify(path)}"
        params: list = []
        if plan.strategy == "column" and plan.column:
            column = self.quote(plan.column)
            marker = self.paramstyle
            clauses = []
            if plan.since is not None:
                clauses.append(f"{column} > {marker}")
                params.append(plan.since)
            if plan.ceiling is not None:
                clauses.append(f"{column} <= {marker}")
                params.append(plan.ceiling)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            # Ordering by the watermark keeps a partial read resumable in watermark order.
            sql += f" ORDER BY {column}"

        with self.cursor() as cur:
            try:
                cur.execute(sql, tuple(params)) if params else cur.execute(sql)
            except Exception as exc:  # noqa: BLE001
                raise QueryError(str(exc).splitlines()[0][:400]) from exc

            positions = {d[0]: i for i, d in enumerate(cur.description or [])}
            ordered = [c for c in schema_info.columns if c.name in positions]
            ordered.sort(key=lambda c: positions[c.name])
            target = arrow_schema(ordered)

            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                yield rows_to_batch([list(r) for r in rows], ordered, target)


def _watermark_names(schema, preferred: str | None) -> list[str]:
    from app.ingestion.planner import watermark_candidates

    names = [c.name for c in watermark_candidates(schema)]
    if preferred and preferred in names:
        names.remove(preferred)
        names.insert(0, preferred)
    return names
