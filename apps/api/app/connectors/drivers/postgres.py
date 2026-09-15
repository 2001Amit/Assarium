from __future__ import annotations

from typing import Any

from app.connectors.sql_base import SQLConnector
from app.connectors.types import (
    AuthMethod,
    BrowseLevel,
    Capabilities,
    CredentialSpec,
    FieldOption,
    FieldType,
    SourceCategory,
    SpecField,
)

SSL_MODES = ["prefer", "require", "verify-ca", "verify-full", "disable"]


class PostgresConnector(SQLConnector):
    requires = ("psycopg", "psycopg[binary]", "postgres")
    has_catalogs = False
    paramstyle = "%s"
    quote_char = '"'

    spec = CredentialSpec(
        source_id="postgres",
        name="PostgreSQL",
        category=SourceCategory.DATABASE,
        summary="Open-source relational database, including Azure Database for PostgreSQL "
        "and Amazon RDS.",
        icon="database",
        docs_url="https://www.postgresql.org/docs/current/libpq-connect.html",
        capabilities=Capabilities(
            sql=True,
            incremental=True,
            row_count_estimate=True,
            levels=[
                BrowseLevel(key="schema", label="Schema", plural="Schemas"),
                BrowseLevel(key="table", label="Table", plural="Tables"),
            ],
        ),
        fields=[
            SpecField(name="host", label="Host", placeholder="db.example.com"),
            SpecField(name="port", label="Port", type=FieldType.NUMBER, default=5432),
            SpecField(name="database", label="Database", placeholder="postgres"),
            SpecField(
                name="sslmode",
                label="SSL mode",
                type=FieldType.SELECT,
                default="require",
                options=[FieldOption(value=m, label=m) for m in SSL_MODES],
                group="Network",
                help="Managed cloud instances normally require SSL.",
            ),
        ],
        auth_methods=[
            AuthMethod(
                id="password",
                label="Username and password",
                description="Standard libpq authentication.",
                recommended=True,
                fields=[
                    SpecField(name="user", label="Username", placeholder="readonly_user"),
                    SpecField(
                        name="password",
                        label="Password",
                        type=FieldType.PASSWORD,
                        secret=True,
                    ),
                ],
            ),
            AuthMethod(
                id="azure_entra",
                label="Microsoft Entra ID",
                description="Token authentication for Azure Database for PostgreSQL. No stored "
                "password; a token is fetched per connection.",
                fields=[
                    SpecField(
                        name="user",
                        label="Entra principal",
                        placeholder="app@contoso.com or the managed identity name",
                        help="Must already be mapped as a PostgreSQL role in the server.",
                    ),
                    SpecField(
                        name="tenant_id",
                        label="Tenant ID",
                        required=False,
                        group="Azure",
                        help="Leave blank to use the ambient Azure credential chain.",
                    ),
                ],
            ),
        ],
    )

    def _connect(self) -> Any:
        psycopg = self.ensure_driver()
        password = self.secrets.get("password")
        if self.auth_method == "azure_entra":
            password = self._entra_token()
        return psycopg.connect(
            host=self.config["host"],
            port=int(self.config.get("port") or 5432),
            dbname=self.config["database"],
            user=self.config["user"],
            password=password,
            sslmode=self.config.get("sslmode", "require"),
            connect_timeout=15,
            application_name="assarium",
        )

    def _entra_token(self) -> str:
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential()
        scope = "https://ossrdbms-aad.database.windows.net/.default"
        return credential.get_token(scope).token

    def probe(self) -> dict[str, Any]:
        _, rows = self.fetch("SELECT version(), current_database(), current_user")
        version, database, user = rows[0]
        return {
            "server_version": str(version).split(",")[0],
            "database": database,
            "user": user,
        }

    def list_tables(self, catalog: str | None, schema: str):
        # pg_class.reltuples gives a free row estimate that information_schema cannot.
        _, rows = self.fetch(
            "SELECT c.relname, "
            "       CASE c.relkind WHEN 'v' THEN 'View' WHEN 'm' THEN 'Materialized view' "
            "            WHEN 'f' THEN 'Foreign table' ELSE 'Table' END, "
            "       GREATEST(c.reltuples, 0)::bigint, "
            "       pg_total_relation_size(c.oid), "
            "       obj_description(c.oid) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE n.nspname = '{schema}' AND c.relkind IN ('r','v','m','p','f') "
            "ORDER BY 1"
        )
        from app.connectors.types import BrowseNode

        return [
            BrowseNode(
                id=f"{schema}.{r[0]}",
                name=r[0],
                kind="dataset",
                path=[schema, r[0]],
                row_estimate=int(r[2]) if r[2] is not None else None,
                size_bytes=int(r[3]) if r[3] is not None else None,
                meta={"object_type": r[1], "comment": r[4]},
            )
            for r in rows
        ]

    def foreign_keys(self, schema: str) -> list[dict[str, str]]:
        """Declared referential integrity, used to seed the ontology graph."""
        _, rows = self.fetch(
            "SELECT tc.table_name, kcu.column_name, ccu.table_name, ccu.column_name "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON tc.constraint_name = kcu.constraint_name "
            "JOIN information_schema.constraint_column_usage ccu "
            "  ON ccu.constraint_name = tc.constraint_name "
            f"WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = '{schema}'"
        )
        return [
            {
                "from_table": r[0],
                "from_column": r[1],
                "to_table": r[2],
                "to_column": r[3],
            }
            for r in rows
        ]
