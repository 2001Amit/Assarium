from __future__ import annotations

from typing import Any

from app.connectors.sql_base import SQLConnector
from app.connectors.types import (
    AuthMethod,
    BrowseLevel,
    BrowseNode,
    Capabilities,
    CredentialSpec,
    FieldType,
    SourceCategory,
    SpecField,
)


class DatabricksConnector(SQLConnector):
    """Databricks SQL warehouse over Unity Catalog (catalog / schema / table)."""

    requires = ("databricks.sql", "databricks-sql-connector", "databricks")
    has_catalogs = True
    quote_char = "`"

    spec = CredentialSpec(
        source_id="databricks",
        name="Databricks",
        category=SourceCategory.LAKEHOUSE,
        summary="Databricks SQL warehouse governed by Unity Catalog.",
        icon="layers",
        docs_url="https://docs.databricks.com/aws/en/dev-tools/python-sql-connector",
        capabilities=Capabilities(
            sql=True,
            incremental=True,
            row_count_estimate=False,
            levels=[
                BrowseLevel(key="catalog", label="Catalog", plural="Catalogs"),
                BrowseLevel(key="schema", label="Schema", plural="Schemas"),
                BrowseLevel(key="table", label="Table", plural="Tables"),
            ],
        ),
        fields=[
            SpecField(
                name="host",
                label="Workspace host",
                placeholder="adb-1234567890.12.azuredatabricks.net",
                help="Without the https:// prefix.",
            ),
            SpecField(
                name="http_path",
                label="HTTP path",
                placeholder="/sql/1.0/warehouses/abc123def456",
                help="SQL Warehouses -> your warehouse -> Connection details.",
            ),
            SpecField(name="catalog", label="Default catalog", required=False, default="main"),
        ],
        auth_methods=[
            AuthMethod(
                id="oauth_m2m",
                label="OAuth machine-to-machine",
                description="Service principal client credentials. Tokens are short-lived and "
                "rotate automatically.",
                recommended=True,
                fields=[
                    SpecField(name="client_id", label="Client ID"),
                    SpecField(
                        name="client_secret",
                        label="Client secret",
                        type=FieldType.PASSWORD,
                        secret=True,
                    ),
                ],
            ),
            AuthMethod(
                id="pat",
                label="Personal access token",
                description="A workspace token. Simple, but tied to a human identity and "
                "long-lived.",
                fields=[
                    SpecField(
                        name="token",
                        label="Access token",
                        type=FieldType.PASSWORD,
                        secret=True,
                        placeholder="dapi...",
                    )
                ],
            ),
        ],
    )

    def _connect(self) -> Any:
        self.ensure_driver()
        from databricks import sql as dbsql

        kwargs: dict[str, Any] = {
            "server_hostname": self.config["host"].replace("https://", "").strip("/"),
            "http_path": self.config["http_path"],
            "_user_agent_entry": "Assarium",
        }
        if self.config.get("catalog"):
            kwargs["catalog"] = self.config["catalog"]

        if self.auth_method == "oauth_m2m":
            from databricks.sdk.core import Config, oauth_service_principal

            host = f"https://{kwargs['server_hostname']}"
            cfg = Config(
                host=host,
                client_id=self.config["client_id"],
                client_secret=self.secrets.get("client_secret"),
            )
            kwargs["credentials_provider"] = lambda: oauth_service_principal(cfg)
        else:
            kwargs["access_token"] = self.secrets.get("token")
        return dbsql.connect(**kwargs)

    def probe(self) -> dict[str, Any]:
        _, rows = self.fetch("SELECT current_catalog(), current_user()")
        return {"server_version": "Databricks SQL", "catalog": rows[0][0], "user": rows[0][1]}

    def default_catalog(self) -> str | None:
        return self.config.get("catalog") or "main"

    def list_catalogs(self) -> list[BrowseNode]:
        _, rows = self.fetch("SHOW CATALOGS")
        names = [r[0] for r in rows if str(r[0]).lower() not in {"system"}]
        return [
            BrowseNode(id=n, name=n, kind="container", path=[n], has_children=True) for n in names
        ]

    def list_schemas(self, catalog: str | None) -> list[BrowseNode]:
        _, rows = self.fetch(f"SHOW SCHEMAS IN {self.quote(catalog)}")
        return [
            BrowseNode(
                id=f"{catalog}.{r[0]}",
                name=r[0],
                kind="namespace",
                path=[catalog, r[0]],
                has_children=True,
            )
            for r in rows
            if str(r[0]).lower() != "information_schema"
        ]

    def list_tables(self, catalog: str | None, schema: str) -> list[BrowseNode]:
        _, rows = self.fetch(
            "SELECT table_name, table_type, comment "
            f"FROM {self.quote(catalog)}.information_schema.tables "
            f"WHERE table_schema = '{schema}' ORDER BY 1"
        )
        return [
            BrowseNode(
                id=f"{catalog}.{schema}.{r[0]}",
                name=r[0],
                kind="dataset",
                path=[catalog, schema, r[0]],
                meta={
                    "object_type": "View" if "VIEW" in str(r[1]).upper() else "Table",
                    "comment": r[2] or None,
                },
            )
            for r in rows
        ]
