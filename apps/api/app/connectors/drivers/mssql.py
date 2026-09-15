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


class SQLServerConnector(SQLConnector):
    requires = ("pymssql", "pymssql", "mssql")
    has_catalogs = False
    paramstyle = "%s"
    quote_char = "["  # handled by the overridden quote()
    supports_limit = False

    spec = CredentialSpec(
        source_id="mssql",
        name="SQL Server",
        category=SourceCategory.DATABASE,
        summary="Microsoft SQL Server, Azure SQL Database and Azure SQL Managed Instance.",
        icon="database",
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
            SpecField(name="host", label="Server", placeholder="myserver.database.windows.net"),
            SpecField(name="port", label="Port", type=FieldType.NUMBER, default=1433),
            SpecField(name="database", label="Database", placeholder="AdventureWorks"),
        ],
        auth_methods=[
            AuthMethod(
                id="sql_login",
                label="SQL login",
                description="Server-managed username and password.",
                recommended=True,
                fields=[
                    SpecField(name="user", label="Login"),
                    SpecField(
                        name="password", label="Password", type=FieldType.PASSWORD, secret=True
                    ),
                ],
            )
        ],
    )

    def quote(self, identifier: str) -> str:
        return f"[{identifier.replace(']', ']]')}]"

    def _connect(self) -> Any:
        pymssql = self.ensure_driver()
        return pymssql.connect(
            server=self.config["host"],
            port=str(self.config.get("port") or 1433),
            database=self.config["database"],
            user=self.config["user"],
            password=self.secrets.get("password"),
            login_timeout=15,
            timeout=60,
        )

    def probe(self) -> dict[str, Any]:
        _, rows = self.fetch("SELECT @@VERSION, DB_NAME(), SUSER_SNAME()")
        return {
            "server_version": str(rows[0][0]).splitlines()[0],
            "database": rows[0][1],
            "user": rows[0][2],
        }

    def list_tables(self, catalog: str | None, schema: str) -> list[BrowseNode]:
        _, rows = self.fetch(
            "SELECT t.name, "
            "       CASE WHEN o.type = 'V' THEN 'View' ELSE 'Table' END, "
            "       SUM(p.rows) "
            "FROM sys.objects o "
            "JOIN sys.schemas s ON s.schema_id = o.schema_id "
            "JOIN sys.tables t ON t.object_id = o.object_id "
            "LEFT JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0,1) "
            f"WHERE s.name = '{schema}' AND o.type IN ('U','V') "
            "GROUP BY t.name, o.type ORDER BY 1"
        )
        return [
            BrowseNode(
                id=f"{schema}.{r[0]}",
                name=r[0],
                kind="dataset",
                path=[schema, r[0]],
                row_estimate=int(r[2]) if r[2] is not None else None,
                meta={"object_type": r[1]},
            )
            for r in rows
        ]
