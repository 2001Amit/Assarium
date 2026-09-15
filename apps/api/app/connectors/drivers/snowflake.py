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
from app.core.errors import ConnectionFailedError


class SnowflakeConnector(SQLConnector):
    requires = ("snowflake.connector", "snowflake-connector-python", "snowflake")
    has_catalogs = True
    quote_char = '"'

    spec = CredentialSpec(
        source_id="snowflake",
        name="Snowflake",
        category=SourceCategory.WAREHOUSE,
        summary="Snowflake Data Cloud warehouse.",
        icon="snowflake",
        docs_url="https://docs.snowflake.com/en/user-guide/key-pair-auth",
        capabilities=Capabilities(
            sql=True,
            incremental=True,
            row_count_estimate=True,
            levels=[
                BrowseLevel(key="database", label="Database", plural="Databases"),
                BrowseLevel(key="schema", label="Schema", plural="Schemas"),
                BrowseLevel(key="table", label="Table", plural="Tables"),
            ],
        ),
        fields=[
            SpecField(
                name="account",
                label="Account identifier",
                placeholder="orgname-account_name",
                help="From your Snowflake URL, without .snowflakecomputing.com.",
            ),
            SpecField(name="user", label="User", placeholder="ASSARIUM_SVC"),
            SpecField(
                name="warehouse",
                label="Warehouse",
                placeholder="COMPUTE_WH",
                help="A dedicated, auto-suspending warehouse keeps this workload's cost visible.",
            ),
            SpecField(name="role", label="Role", required=False, placeholder="ASSARIUM_READER"),
            SpecField(
                name="database",
                label="Default database",
                required=False,
                help="Optional. Leave blank to browse every database the role can see.",
            ),
        ],
        auth_methods=[
            AuthMethod(
                id="key_pair",
                label="Key-pair (RSA)",
                description="Signed key-pair authentication for service accounts.",
                recommended=True,
                notice="Snowflake is blocking single-factor password sign-in for service "
                "connections through 2026. Key-pair is the supported path.",
                fields=[
                    SpecField(
                        name="private_key",
                        label="Private key (PKCS#8 PEM)",
                        type=FieldType.TEXTAREA,
                        secret=True,
                        placeholder="-----BEGIN ENCRYPTED PRIVATE KEY-----",
                        help="The matching public key must be assigned to the user with "
                        "ALTER USER ... SET RSA_PUBLIC_KEY.",
                    ),
                    SpecField(
                        name="private_key_passphrase",
                        label="Key passphrase",
                        type=FieldType.PASSWORD,
                        secret=True,
                        required=False,
                        help="Only for an encrypted private key.",
                    ),
                ],
            ),
            AuthMethod(
                id="oauth",
                label="OAuth token",
                description="A bearer token issued by your identity provider.",
                fields=[
                    SpecField(
                        name="token", label="Access token", type=FieldType.PASSWORD, secret=True
                    )
                ],
            ),
            AuthMethod(
                id="password",
                label="Password",
                description="Username and password.",
                deprecated=True,
                notice="Snowflake is phasing out single-factor password sign-in. Existing "
                "connections keep working; use key-pair for anything new.",
                fields=[
                    SpecField(
                        name="password", label="Password", type=FieldType.PASSWORD, secret=True
                    )
                ],
            ),
        ],
    )

    def _private_key_der(self) -> bytes:
        from cryptography.hazmat.primitives import serialization

        pem = (self.secrets.get("private_key") or "").strip().encode()
        passphrase = self.secrets.get("private_key_passphrase") or None
        try:
            key = serialization.load_pem_private_key(
                pem, password=passphrase.encode() if passphrase else None
            )
        except (ValueError, TypeError) as exc:
            raise ConnectionFailedError(
                "The private key could not be read. Check that it is PKCS#8 PEM and that the "
                "passphrase is correct."
            ) from exc
        return key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def _connect(self) -> Any:
        self.ensure_driver()
        import snowflake.connector as sf

        kwargs: dict[str, Any] = {
            "account": self.config["account"],
            "user": self.config["user"],
            "warehouse": self.config.get("warehouse"),
            "application": "Assarium",
            "login_timeout": 20,
            "client_session_keep_alive": False,
        }
        if self.config.get("role"):
            kwargs["role"] = self.config["role"]
        if self.config.get("database"):
            kwargs["database"] = self.config["database"]

        if self.auth_method == "key_pair":
            kwargs["private_key"] = self._private_key_der()
        elif self.auth_method == "oauth":
            kwargs["authenticator"] = "oauth"
            kwargs["token"] = self.secrets.get("token")
        else:
            kwargs["password"] = self.secrets.get("password")
        return sf.connect(**kwargs)

    def probe(self) -> dict[str, Any]:
        _, rows = self.fetch(
            "SELECT CURRENT_VERSION(), CURRENT_ACCOUNT(), CURRENT_ROLE(), CURRENT_WAREHOUSE()"
        )
        return {
            "server_version": f"Snowflake {rows[0][0]}",
            "account": rows[0][1],
            "role": rows[0][2],
            "warehouse": rows[0][3],
        }

    def default_catalog(self) -> str | None:
        return self.config.get("database")

    def list_catalogs(self) -> list[BrowseNode]:
        _, rows = self.fetch("SHOW TERSE DATABASES")
        # SHOW TERSE returns: created_on, name, kind, database_name, schema_name
        return [
            BrowseNode(id=r[1], name=r[1], kind="container", path=[r[1]], has_children=True)
            for r in rows
        ]

    def list_tables(self, catalog: str | None, schema: str) -> list[BrowseNode]:
        _, rows = self.fetch(
            "SELECT table_name, table_type, row_count, bytes, comment "
            f"FROM {self.quote(catalog)}.information_schema.tables "
            f"WHERE table_schema = '{schema}' ORDER BY 1"
        )
        return [
            BrowseNode(
                id=f"{catalog}.{schema}.{r[0]}",
                name=r[0],
                kind="dataset",
                path=[catalog, schema, r[0]],
                row_estimate=int(r[2]) if r[2] is not None else None,
                size_bytes=int(r[3]) if r[3] is not None else None,
                meta={
                    "object_type": "View" if "VIEW" in str(r[1]).upper() else "Table",
                    "comment": r[4] or None,
                },
            )
            for r in rows
        ]
