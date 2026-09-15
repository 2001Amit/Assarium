from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any

import pyarrow as pa

from app.core.config import get_settings
from app.core.errors import ConfigurationError, DriverNotInstalledError, QueryError
from app.engine.base import LAYERS, Engine, Layer, QueryResult, TableRef, WriteMode

logger = logging.getLogger("assarium.engine.databricks")

# Arrow to Spark SQL. Anything unmapped is stored as STRING rather than guessed at.
ARROW_TO_SQL = {
    "bool": "BOOLEAN",
    "int8": "TINYINT",
    "int16": "SMALLINT",
    "int32": "INT",
    "int64": "BIGINT",
    "uint8": "SMALLINT",
    "uint16": "INT",
    "uint32": "BIGINT",
    "uint64": "BIGINT",
    "float": "FLOAT",
    "double": "DOUBLE",
    "date32[day]": "DATE",
    "date64[ms]": "DATE",
    "string": "STRING",
    "large_string": "STRING",
    "binary": "BINARY",
}

INSERT_BATCH_ROWS = 1_000


class DatabricksEngine(Engine):
    """
    Medallion warehouse on Databricks: one Unity Catalog catalog per layer, Delta tables.

    Writes go through multi-row INSERT, which is portable across every warehouse size and
    needs no extra grants. For high-volume production loads, staging Parquet in a Unity
    Catalog volume and running COPY INTO is substantially faster; that path needs volume
    privileges this engine does not assume it has.
    """

    name = "databricks"

    def __init__(self, tenant_id: str | None = None) -> None:
        settings = get_settings()

        missing = []
        if not settings.databricks_host:
            missing.append("ASSARIUM_DATABRICKS_HOST")
        if not settings.databricks_http_path:
            missing.append("ASSARIUM_DATABRICKS_HTTP_PATH")
        # OAuth M2M only. Personal access tokens are long-lived, belong to a person, and
        # do not rotate - the architecture rules them out, so leaving a fallback here
        # would just make the ruled-out path the easy one.
        if not (settings.databricks_client_id and settings.databricks_client_secret):
            missing.append("ASSARIUM_DATABRICKS_CLIENT_ID and _CLIENT_SECRET")

        if missing:
            raise ConfigurationError(
                "The Databricks engine needs " + ", ".join(missing) + " to be set."
            )

        self.settings = settings
        self.tenant_id = tenant_id
        self.catalogs: dict[str, str] = self._catalogs_for(tenant_id)
        self._connection: Any = None
        self._active_cursor: Any = None

    def _catalogs_for(self, tenant_id: str | None) -> dict[str, str]:
        """
        Which catalog each medallion layer lives in.

        One catalog per tenant, with the layers as schemas inside it. The catalog is the
        isolation boundary, so an engine built without a tenant has no catalog it could
        safely name - it refuses rather than falling back to a shared one, which is how
        every tenant ends up in the same warehouse.
        """
        if not tenant_id:
            raise ConfigurationError(
                "The Databricks engine needs a tenant. Building one without a tenant "
                "would have to point at a shared catalog, and the catalog is what keeps "
                "customers apart."
            )
        from app.engine.catalog_manager import sanitize_tenant_slug

        slug = sanitize_tenant_slug(tenant_id)
        catalog = f"{self.settings.catalog_prefix}_{self.settings.environment}_{slug}"
        return {"bronze": catalog, "silver": catalog, "gold": catalog}

    # -- lifecycle --------------------------------------------------------------------

    @property
    def connection(self) -> Any:
        if self._connection is None:
            try:
                from databricks import sql as dbsql
            except ImportError as exc:
                raise DriverNotInstalledError(
                    "Databricks engine", "databricks-sql-connector", "databricks"
                ) from exc
                
            kwargs: dict[str, Any] = {
                "server_hostname": self.settings.databricks_host.replace("https://", "").strip("/"),
                "http_path": self.settings.databricks_http_path,
                "_user_agent_entry": "Assarium",
            }
            
            # Use M2M if available, else PAT
            if self.settings.databricks_client_id and self.settings.databricks_client_secret:
                try:
                    from databricks.sdk.core import Config
                    cfg = Config(
                        host=kwargs["server_hostname"],
                        client_id=self.settings.databricks_client_id,
                        client_secret=self.settings.databricks_client_secret,
                        azure_tenant_id=self.settings.entra_audience,
                    )
                    kwargs["credentials_provider"] = lambda: cfg.authenticate()
                except ImportError:
                    logger.warning(
                        "databricks-sdk is not installed, so this falls back to basic "
                        "M2M. Install databricks-sdk to get token refresh."
                    )
            self._connection = dbsql.connect(**kwargs)
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None

    def ensure_layers(self) -> None:
        for layer in LAYERS:
            catalog = self.catalogs[layer]
            self.execute_ddl(f"CREATE CATALOG IF NOT EXISTS {self.quote(catalog)}")
            self.execute_ddl(
                f"CREATE SCHEMA IF NOT EXISTS {self.quote(catalog)}.{self.quote('assarium')}"
            )

    # -- identifiers and dialect -------------------------------------------------------

    def quote(self, identifier: str) -> str:
        return "`" + identifier.replace("`", "``") + "`"

    def strip_non_numeric(self, expression: str) -> str:
        # Spark's regexp_replace already replaces every match and accepts no flags
        # argument. The cast is for the same reason as the base implementation.
        return f"REGEXP_REPLACE(CAST({expression} AS STRING), '[^0-9eE.+-]', '')"

    def qualified(self, layer: Layer, name: str) -> str:
        return f"{self.quote(self.catalogs[layer])}.{self.quote('assarium')}.{self.quote(name)}"

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
        columns = ", ".join(
            f"{self.quote(field.name)} {ARROW_TO_SQL.get(str(field.type), 'STRING')}"
            for field in schema
        )

        if mode == "replace":
            self.execute_ddl(f"CREATE OR REPLACE TABLE {target} ({columns}) USING DELTA")
        else:
            self.execute_ddl(f"CREATE TABLE IF NOT EXISTS {target} ({columns}) USING DELTA")

        placeholders = "(" + ", ".join(["?"] * len(schema)) + ")"
        written = 0
        pending: list[list[Any]] = []

        def flush() -> None:
            nonlocal pending
            if not pending:
                return
            values = ", ".join([placeholders] * len(pending))
            flat = [value for row in pending for value in row]
            with self.connection.cursor() as cursor:
                cursor.execute(f"INSERT INTO {target} VALUES {values}", flat)
            pending = []

        for batch in batches:
            for row in batch.to_pylist():
                pending.append([row.get(field.name) for field in schema])
                written += 1
                if len(pending) >= INSERT_BATCH_ROWS:
                    flush()
        flush()

        logger.info("Wrote %s rows to %s", written, target)
        
        # Optimize Delta table asynchronously to merge small files created by inserts
        if written > 0:
            try:
                self.execute_ddl(f"OPTIMIZE {target}")
            except Exception as e:
                logger.warning("Failed to optimize %s: %s", target, e)
                
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
            with self.connection.cursor() as cursor:
                # Held so `cancel_running_query` can reach it when the timeout fires;
                # a cancel that cannot find the cursor leaves the warehouse working on
                # a result nobody is waiting for, and still billing for it.
                self._active_cursor = cursor
                try:
                    cursor.execute(effective, params or None)
                    description = cursor.description or []
                    return [d[0] for d in description], [list(r) for r in cursor.fetchall()]
                finally:
                    self._active_cursor = None

        try:
            columns, rows = self.run_bounded(run)
        except QueryError:
            raise
        except Exception as exc:  # noqa: BLE001 - driver raises a wide range of types
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
        """Stop the statement server-side, so the warehouse is not billed for a
        result nobody will read."""
        cursor = getattr(self, "_active_cursor", None)
        if cursor is None:
            return
        try:
            cursor.cancel()
        except Exception:  # noqa: BLE001 - it may already have finished
            pass

    def execute_ddl(self, sql: str) -> None:
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(sql)
        except Exception as exc:  # noqa: BLE001
            raise QueryError(str(exc).splitlines()[0][:400]) from exc

    def table_exists(self, layer: Layer, name: str) -> bool:
        result = self.execute(
            f"SELECT 1 FROM {self.quote(self.catalogs[layer])}.information_schema.tables "
            f"WHERE table_schema = 'assarium' AND table_name = '{name}'"
        )
        return result.row_count > 0

    def list_tables(self, layer: Layer) -> list[str]:
        result = self.execute(
            f"SELECT table_name FROM {self.quote(self.catalogs[layer])}.information_schema.tables "
            "WHERE table_schema = 'assarium' ORDER BY 1"
        )
        return [row[0] for row in result.rows]

    def describe(self, layer: Layer, name: str) -> list[tuple[str, str]]:
        result = self.execute(
            "SELECT column_name, data_type FROM "
            f"{self.quote(self.catalogs[layer])}.information_schema.columns "
            f"WHERE table_schema = 'assarium' AND table_name = '{name}' ORDER BY ordinal_position"
        )
        return [(row[0], row[1]) for row in result.rows]

    def row_count(self, layer: Layer, name: str) -> int:
        result = self.execute(f"SELECT count(*) FROM {self.qualified(layer, name)}")
        return int(result.rows[0][0]) if result.rows else 0
