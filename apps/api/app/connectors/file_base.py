from __future__ import annotations

import tempfile
from abc import abstractmethod
from pathlib import Path
from typing import Any

import duckdb

from app.connectors.base import Connector
from app.connectors.type_map import to_logical_type
from app.connectors.types import ColumnSchema, DatasetSchema, SampleResult
from app.core.errors import QueryError
from app.ingestion.types import FileChange

# Extensions the platform understands as tabular data.
TABULAR_SUFFIXES = {".csv", ".tsv", ".txt", ".parquet", ".pqt", ".json", ".jsonl", ".ndjson",
                    ".xlsx", ".xls"}

# A preview must never pull an unbounded object into memory.
PREVIEW_BYTE_CAP = 256 * 1024 * 1024


def duckdb_reader(local_path: Path) -> str:
    """Map a file suffix onto the DuckDB table function that reads it."""
    suffix = local_path.suffix.lower()
    quoted = str(local_path).replace("'", "''")
    if suffix in {".parquet", ".pqt"}:
        return f"read_parquet('{quoted}')"
    if suffix in {".json", ".jsonl", ".ndjson"}:
        return f"read_json_auto('{quoted}')"
    if suffix in {".xlsx", ".xls"}:
        # st_read via the spatial extension is unreliable for spreadsheets; pandas handles
        # these, and the caller converts to Parquet before this point.
        raise QueryError("Spreadsheets are converted before reading.")
    delimiter = "\t" if suffix in {".tsv", ".txt"} else ","
    return f"read_csv_auto('{quoted}', delim='{delimiter}', sample_size=20000)"


class FileConnector(Connector):
    """
    Shared behaviour for object stores and local uploads.

    Backends implement listing plus a byte fetch; format detection, schema inference and
    sampling are handled once here by DuckDB.
    """

    # DuckDB counts the file rather than estimating, so the total is exact.
    exact_row_counts = True

    @abstractmethod
    def _download(self, path: list[str], byte_cap: int = PREVIEW_BYTE_CAP) -> Path:
        """Materialise the object locally and return the path."""

    def _prepare(self, path: list[str]) -> Path:
        local = self._download(path)
        if local.suffix.lower() in {".xlsx", ".xls"}:
            return self._excel_to_parquet(local)
        return local

    def _excel_to_parquet(self, local: Path) -> Path:
        try:
            import pandas as pd
        except ImportError as exc:  # pragma: no cover
            raise QueryError("Reading spreadsheets requires pandas and openpyxl.") from exc
        frame = pd.read_excel(local)
        target = local.with_suffix(".parquet")
        frame.to_parquet(target, index=False)
        return target

    def _relation(self, path: list[str]) -> tuple[duckdb.DuckDBPyConnection, str]:
        local = self._prepare(path)
        return duckdb.connect(), duckdb_reader(local)

    def describe(self, path: list[str]) -> DatasetSchema:
        con, reader = self._relation(path)
        try:
            rows = con.execute(f"DESCRIBE SELECT * FROM {reader}").fetchall()
            count = con.execute(f"SELECT count(*) FROM {reader}").fetchone()[0]
        except duckdb.Error as exc:
            raise QueryError(str(exc).splitlines()[0][:400]) from exc
        finally:
            con.close()
        columns = [
            ColumnSchema(
                name=r[0],
                native_type=r[1],
                logical_type=to_logical_type(r[1]),
                nullable=True,
                position=i,
            )
            for i, r in enumerate(rows)
        ]
        return DatasetSchema(path=path, name=path[-1], columns=columns, row_estimate=int(count))

    def sample(self, path: list[str], limit: int = 100) -> SampleResult:
        con, reader = self._relation(path)
        try:
            cursor = con.execute(f"SELECT * FROM {reader} LIMIT {int(limit)}")
            columns = [d[0] for d in cursor.description]
            rows = [list(r) for r in cursor.fetchall()]
        except duckdb.Error as exc:
            raise QueryError(str(exc).splitlines()[0][:400]) from exc
        finally:
            con.close()
        return SampleResult(columns=columns, rows=rows, truncated=len(rows) >= limit)

    @staticmethod
    def _temp_path(name: str) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="assarium-"))
        return directory / Path(name).name

    @staticmethod
    def is_tabular(name: str) -> bool:
        return Path(name).suffix.lower() in TABULAR_SUFFIXES

    def probe(self) -> dict[str, Any]:
        raise NotImplementedError

    def read_batches(self, path: list[str], batch_size: int = 50_000):
        """DuckDB streams the file straight into Arrow, so no per-row conversion is needed."""
        import pyarrow as pa

        from app.engine.arrow import arrow_schema

        target = arrow_schema(self.describe(path).columns)
        con, relation = self._relation(path)
        try:
            # One reader, drained once. `to_arrow_reader` replaced `fetch_record_batch`
            # in DuckDB 1.4; fall back so an older pinned version still works.
            result = con.execute(f"SELECT * FROM {relation}")
            if hasattr(result, "to_arrow_reader"):
                reader = result.to_arrow_reader(batch_size)
            else:  # pragma: no cover - older DuckDB
                reader = result.fetch_record_batch(batch_size)
            while True:
                try:
                    batch = reader.read_next_batch()
                except StopIteration:
                    break
                if batch.num_rows == 0:
                    continue
                # Align DuckDB's inferred types with the platform's canonical schema, so
                # every layer table has the same shape whichever reader produced it.
                table = pa.Table.from_batches([batch]).rename_columns(target.names).cast(target)
                yield from table.to_batches()
        finally:
            con.close()

    # -- incremental reads ---------------------------------------------------------------

    def stat(self, path: list[str]) -> FileChange:
        """
        Current identity of one file: etag, size and last-modified.

        Backends override this with the store's own version marker. The default combines
        size and modified time, which is weaker than a real etag but still detects the
        common case of a file being replaced.
        """
        raise NotImplementedError(
            f"{self.spec.name} cannot report file versions, so every run reloads."
        )

    def plan_ingestion(self, path: list[str], schema, state, known_files=None):
        """
        Decide whether this file needs reading again.

        A dataset backed by a file is all-or-nothing: either the bytes changed and the
        whole file is re-read, or they did not and nothing is done. There is no partial
        read of a CSV that is meaningful.
        """
        from app.ingestion.types import IngestionPlan

        try:
            current = self.stat(path)
        except (NotImplementedError, Exception) as exc:  # noqa: BLE001
            return IngestionPlan(
                strategy="full",
                full_refresh=True,
                reason=(
                    f"Could not read this file's version marker ({exc}), so it is "
                    "reloaded in full."
                ),
            )

        file_key = "/".join(path)
        previous = (known_files or {}).get(file_key)

        if previous and current.etag and previous == current.etag:
            return IngestionPlan(
                strategy="file",
                full_refresh=False,
                files=[],
                skipped_files=1,
                reason=f"{current.name} is unchanged since the last load.",
            )

        return IngestionPlan(
            strategy="file",
            full_refresh=True,
            files=[current],
            reason=(
                f"{current.name} is new."
                if not previous
                else f"{current.name} changed since the last load."
            ),
        )

    def read_plan(self, path: list[str], plan, batch_size: int = 50_000):
        """Read the file, unless the plan decided it has not changed."""
        if plan.strategy == "file" and not plan.files:
            return
        yield from self.read_batches(path, batch_size=batch_size)
