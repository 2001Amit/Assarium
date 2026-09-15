from __future__ import annotations

import json
from typing import Any

from app.connectors.base import Connector
from app.connectors.type_map import to_logical_type
from app.connectors.types import (
    AuthMethod,
    BrowseLevel,
    BrowseNode,
    Capabilities,
    ColumnSchema,
    CredentialSpec,
    DatasetSchema,
    FieldOption,
    FieldType,
    SampleResult,
    SourceCategory,
    SpecField,
)
from app.core.errors import ConnectionFailedError

LOCATIONS = ["US", "EU", "us-central1", "europe-west1", "asia-south1", "australia-southeast1"]


class BigQueryConnector(Connector):
    requires = ("google.cloud.bigquery", "google-cloud-bigquery", "bigquery")

    spec = CredentialSpec(
        source_id="bigquery",
        name="BigQuery",
        category=SourceCategory.WAREHOUSE,
        summary="Google BigQuery serverless data warehouse.",
        icon="database",
        docs_url="https://cloud.google.com/bigquery/docs/authentication",
        capabilities=Capabilities(
            sql=True,
            incremental=True,
            row_count_estimate=True,
            levels=[
                BrowseLevel(key="dataset", label="Dataset", plural="Datasets"),
                BrowseLevel(key="table", label="Table", plural="Tables"),
            ],
        ),
        fields=[
            SpecField(name="project_id", label="Project ID", placeholder="my-gcp-project"),
            SpecField(
                name="location",
                label="Location",
                type=FieldType.SELECT,
                default="US",
                required=False,
                options=[FieldOption(value=loc, label=loc) for loc in LOCATIONS],
                group="Network",
            ),
        ],
        auth_methods=[
            AuthMethod(
                id="service_account",
                label="Service account key",
                description="A JSON key for a service account holding BigQuery Data Viewer and "
                "Job User.",
                recommended=True,
                fields=[
                    SpecField(
                        name="service_account_json",
                        label="Service account JSON",
                        type=FieldType.TEXTAREA,
                        secret=True,
                        placeholder='{"type": "service_account", ...}',
                    )
                ],
            ),
            AuthMethod(
                id="adc",
                label="Application default credentials",
                description="Use the ambient Google credential on the host. Nothing is stored.",
                fields=[],
            ),
        ],
    )

    _client: Any = None

    @property
    def client(self) -> Any:
        if self._client is None:
            self.ensure_driver()
            from google.cloud import bigquery

            if self.auth_method == "service_account":
                from google.oauth2 import service_account

                try:
                    info = json.loads(self.secrets["service_account_json"])
                except (KeyError, json.JSONDecodeError) as exc:
                    raise ConnectionFailedError(
                        "The service account key is not valid JSON."
                    ) from exc
                creds = service_account.Credentials.from_service_account_info(info)
                self._client = bigquery.Client(
                    project=self.config["project_id"],
                    credentials=creds,
                    location=self.config.get("location") or None,
                )
            else:
                self._client = bigquery.Client(
                    project=self.config["project_id"], location=self.config.get("location") or None
                )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def probe(self) -> dict[str, Any]:
        datasets = list(self.client.list_datasets(max_results=1))
        return {
            "server_version": "BigQuery",
            "project": self.client.project,
            "datasets_visible": "yes" if datasets else "none found",
        }

    def browse(self, path: list[str]) -> list[BrowseNode]:
        if not path:
            return [
                BrowseNode(
                    id=ds.dataset_id,
                    name=ds.dataset_id,
                    kind="namespace",
                    path=[ds.dataset_id],
                    has_children=True,
                )
                for ds in self.client.list_datasets()
            ]
        if len(path) == 1:
            return [
                BrowseNode(
                    id=f"{path[0]}.{t.table_id}",
                    name=t.table_id,
                    kind="dataset",
                    path=[path[0], t.table_id],
                    meta={"object_type": (t.table_type or "TABLE").title()},
                )
                for t in self.client.list_tables(path[0])
            ]
        return []

    def describe(self, path: list[str]) -> DatasetSchema:
        table = self.client.get_table(f"{self.config['project_id']}.{path[0]}.{path[1]}")
        columns = [
            ColumnSchema(
                name=f.name,
                native_type=f.field_type,
                logical_type=to_logical_type(f.field_type),
                nullable=f.mode != "REQUIRED",
                position=i,
                comment=f.description,
            )
            for i, f in enumerate(table.schema)
        ]
        return DatasetSchema(
            path=path,
            name=path[-1],
            columns=columns,
            row_estimate=table.num_rows,
            comment=table.description,
        )

    def sample(self, path: list[str], limit: int = 100) -> SampleResult:
        # list_rows reads storage directly, so a preview costs no query bytes.
        table = self.client.get_table(f"{self.config['project_id']}.{path[0]}.{path[1]}")
        rows = list(self.client.list_rows(table, max_results=limit))
        columns = [f.name for f in table.schema]
        return SampleResult(
            columns=columns,
            rows=[[row.get(c) for c in columns] for row in rows],
            truncated=len(rows) >= limit,
        )

    def read_batches(self, path: list[str], batch_size: int = 50_000):
        """
        Stream the table as Arrow via the BigQuery storage path.

        `list_rows` reads the table directly rather than running a query, so a full
        ingest costs no query bytes - which matters, because BigQuery bills for them.
        """
        import pyarrow as pa

        from app.engine.arrow import arrow_schema

        target = arrow_schema(self.describe(path).columns)
        table = self.client.get_table(f"{self.config['project_id']}.{path[0]}.{path[1]}")
        rows = self.client.list_rows(table, page_size=batch_size)

        for batch in rows.to_arrow_iterable():
            if batch.num_rows == 0:
                continue
            aligned = (
                pa.Table.from_batches([batch]).rename_columns(target.names).cast(target)
            )
            yield from aligned.to_batches()
