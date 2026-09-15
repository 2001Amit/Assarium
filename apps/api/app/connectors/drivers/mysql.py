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


class MySQLConnector(SQLConnector):
    requires = ("pymysql", "pymysql", "mysql")
    has_catalogs = False
    paramstyle = "%s"
    quote_char = "`"

    spec = CredentialSpec(
        source_id="mysql",
        name="MySQL",
        category=SourceCategory.DATABASE,
        summary="MySQL and MariaDB, including Azure Database for MySQL and Amazon RDS.",
        icon="database",
        capabilities=Capabilities(
            sql=True,
            incremental=True,
            row_count_estimate=True,
            levels=[
                BrowseLevel(key="database", label="Database", plural="Databases"),
                BrowseLevel(key="table", label="Table", plural="Tables"),
            ],
        ),
        fields=[
            SpecField(name="host", label="Host", placeholder="mysql.example.com"),
            SpecField(name="port", label="Port", type=FieldType.NUMBER, default=3306),
            SpecField(
                name="database",
                label="Default database",
                required=False,
                help="Optional. Leave blank to browse every database the user can see.",
            ),
            SpecField(
                name="use_ssl",
                label="Require SSL",
                type=FieldType.BOOLEAN,
                default=True,
                required=False,
                group="Network",
            ),
        ],
        auth_methods=[
            AuthMethod(
                id="password",
                label="Username and password",
                description="Native MySQL authentication.",
                recommended=True,
                fields=[
                    SpecField(name="user", label="Username"),
                    SpecField(
                        name="password", label="Password", type=FieldType.PASSWORD, secret=True
                    ),
                ],
            )
        ],
    )

    def _connect(self) -> Any:
        pymysql = self.ensure_driver()
        kwargs: dict[str, Any] = {
            "host": self.config["host"],
            "port": int(self.config.get("port") or 3306),
            "user": self.config["user"],
            "password": self.secrets.get("password"),
            "connect_timeout": 15,
            "charset": "utf8mb4",
        }
        if self.config.get("database"):
            kwargs["database"] = self.config["database"]
        if self.config.get("use_ssl", True):
            kwargs["ssl"] = {"ssl": {}}
        return pymysql.connect(**kwargs)

    def probe(self) -> dict[str, Any]:
        _, rows = self.fetch("SELECT VERSION(), DATABASE(), CURRENT_USER()")
        return {"server_version": rows[0][0], "database": rows[0][1], "user": rows[0][2]}

    def list_schemas(self, catalog: str | None) -> list[BrowseNode]:
        # MySQL's "schema" and "database" are the same rung.
        _, rows = self.fetch(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT IN "
            "('information_schema','mysql','performance_schema','sys') ORDER BY 1"
        )
        return [
            BrowseNode(id=r[0], name=r[0], kind="namespace", path=[r[0]], has_children=True)
            for r in rows
        ]

    def list_tables(self, catalog: str | None, schema: str) -> list[BrowseNode]:
        _, rows = self.fetch(
            "SELECT table_name, table_type, table_rows, data_length + index_length, table_comment "
            "FROM information_schema.tables "
            f"WHERE table_schema = '{schema}' ORDER BY 1"
        )
        return [
            BrowseNode(
                id=f"{schema}.{r[0]}",
                name=r[0],
                kind="dataset",
                path=[schema, r[0]],
                row_estimate=int(r[2]) if r[2] is not None else None,
                size_bytes=int(r[3]) if r[3] is not None else None,
                meta={
                    "object_type": "View" if r[1] == "VIEW" else "Table",
                    "comment": r[4] or None,
                },
            )
            for r in rows
        ]
