from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx

from app.connectors.base import Connector
from app.connectors.types import (
    AuthMethod,
    BrowseLevel,
    BrowseNode,
    Capabilities,
    ColumnSchema,
    CredentialSpec,
    DatasetSchema,
    FieldType,
    SampleResult,
    SourceCategory,
    SpecField,
)
from app.core.errors import ConnectionFailedError

# Salesforce field types mapped onto the platform's logical set.
SF_TYPE_MAP = {
    "id": "string", "string": "string", "textarea": "string", "picklist": "string",
    "multipicklist": "string", "reference": "string", "email": "string", "phone": "string",
    "url": "string", "encryptedstring": "string", "combobox": "string", "address": "json",
    "int": "integer", "long": "integer", "double": "float", "percent": "float",
    "currency": "decimal", "boolean": "boolean", "date": "date", "datetime": "timestamp",
    "time": "string", "base64": "binary", "location": "json", "anyType": "unknown",
}

# Objects that are platform plumbing rather than business data.
NOISE_SUFFIXES = ("__History", "__Share", "__Feed", "__Tag", "__ChangeEvent", "__e", "__mdt")


class SalesforceConnector(Connector):
    spec = CredentialSpec(
        source_id="salesforce",
        name="Salesforce",
        category=SourceCategory.SAAS,
        summary="Salesforce standard and custom objects through the REST API.",
        icon="cloud",
        docs_url="https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_flows.htm",
        capabilities=Capabilities(
            sql=False,
            levels=[BrowseLevel(key="object", label="Object", plural="Objects")],
        ),
        fields=[
            SpecField(
                name="instance_url",
                label="Instance URL",
                placeholder="https://myorg.my.salesforce.com",
                help="Your My Domain URL. Use the sandbox domain for a sandbox org.",
            ),
            SpecField(
                name="api_version", label="API version", default="v62.0", required=False,
                group="Advanced",
            ),
            SpecField(
                name="include_custom_only",
                label="Custom objects only",
                type=FieldType.BOOLEAN,
                default=False,
                required=False,
                group="Advanced",
                help="Hide standard objects when only your own are relevant.",
            ),
        ],
        auth_methods=[
            AuthMethod(
                id="client_credentials",
                label="Client credentials",
                description="Server-to-server with a consumer key and secret, running as a "
                "designated integration user.",
                recommended=True,
                notice="Create an External Client App. New Connected Apps are restricted from "
                "the Spring '26 release onward.",
                fields=[
                    SpecField(name="client_id", label="Consumer key"),
                    SpecField(
                        name="client_secret",
                        label="Consumer secret",
                        type=FieldType.PASSWORD,
                        secret=True,
                    ),
                ],
            ),
            AuthMethod(
                id="jwt_bearer",
                label="JWT bearer",
                description="Signed assertion with a certificate instead of a shared secret. "
                "Stronger, and nothing reusable is stored.",
                fields=[
                    SpecField(name="client_id", label="Consumer key"),
                    SpecField(
                        name="username",
                        label="Run-as username",
                        placeholder="integration@contoso.com",
                    ),
                    SpecField(
                        name="private_key",
                        label="Private key (PEM)",
                        type=FieldType.TEXTAREA,
                        secret=True,
                        help="Matches the certificate uploaded to the app's Use digital "
                        "signatures setting.",
                    ),
                ],
            ),
        ],
    )

    _token: str | None = None
    _instance: str | None = None

    # -- auth --------------------------------------------------------------------------

    @property
    def api_version(self) -> str:
        return self.config.get("api_version") or "v62.0"

    def _login_host(self) -> str:
        url = self.config["instance_url"].rstrip("/")
        return url if url.startswith("http") else f"https://{url}"

    def _sign_jwt(self) -> str:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        def b64(raw: bytes) -> bytes:
            return base64.urlsafe_b64encode(raw).rstrip(b"=")

        # `aud` is always the login host, even for a sandbox reached by My Domain.
        audience = (
            "https://test.salesforce.com"
            if ".sandbox." in self._login_host()
            else "https://login.salesforce.com"
        )
        header = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = b64(
            json.dumps(
                {
                    "iss": self.config["client_id"],
                    "sub": self.config["username"],
                    "aud": audience,
                    "exp": int(time.time()) + 180,
                }
            ).encode()
        )
        signing_input = header + b"." + claims
        key = serialization.load_pem_private_key(
            self.secrets["private_key"].strip().encode(), password=None
        )
        signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return (signing_input + b"." + b64(signature)).decode()

    def _authenticate(self) -> str:
        if self._token:
            return self._token
        token_url = f"{self._login_host()}/services/oauth2/token"
        if self.auth_method == "jwt_bearer":
            data = {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": self._sign_jwt(),
            }
        else:
            data = {
                "grant_type": "client_credentials",
                "client_id": self.config["client_id"],
                "client_secret": self.secrets["client_secret"],
            }
        response = httpx.post(token_url, data=data, timeout=45)
        payload = response.json() if response.content else {}
        if response.status_code >= 400 or "access_token" not in payload:
            raise ConnectionFailedError(
                payload.get("error_description")
                or payload.get("error")
                or f"Salesforce returned HTTP {response.status_code}."
            )
        self._token = payload["access_token"]
        self._instance = payload.get("instance_url") or self._login_host()
        return self._token

    def _get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        token = self._authenticate()
        response = httpx.get(
            f"{self._instance}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=120,
        )
        if response.status_code >= 400:
            body = response.json() if response.content else []
            message = body[0].get("message") if isinstance(body, list) and body else response.text
            raise ConnectionFailedError(str(message)[:300])
        return response.json()

    # -- Connector contract ------------------------------------------------------------

    def probe(self) -> dict[str, Any]:
        limits = self._get(f"/services/data/{self.api_version}/limits")
        daily = limits.get("DailyApiRequests", {})
        return {
            "server_version": f"Salesforce {self.api_version}",
            "instance": self._instance or "",
            "api_calls_remaining": str(daily.get("Remaining", "unknown")),
        }

    def browse(self, path: list[str]) -> list[BrowseNode]:
        if path:
            return []
        custom_only = bool(self.config.get("include_custom_only"))
        objects = self._get(f"/services/data/{self.api_version}/sobjects")["sobjects"]
        nodes = []
        for obj in objects:
            name = obj["name"]
            if not obj.get("queryable") or name.endswith(NOISE_SUFFIXES):
                continue
            if custom_only and not obj.get("custom"):
                continue
            nodes.append(
                BrowseNode(
                    id=name,
                    name=obj.get("label") or name,
                    kind="dataset",
                    path=[name],
                    meta={
                        "api_name": name,
                        "object_type": "Custom object" if obj.get("custom") else "Standard object",
                    },
                )
            )
        nodes.sort(key=lambda n: n.name.lower())
        return nodes

    def _describe_raw(self, obj: str) -> dict[str, Any]:
        return self._get(f"/services/data/{self.api_version}/sobjects/{obj}/describe")

    def describe(self, path: list[str]) -> DatasetSchema:
        raw = self._describe_raw(path[0])
        columns = [
            ColumnSchema(
                name=f["name"],
                native_type=f["type"],
                logical_type=SF_TYPE_MAP.get(f["type"], "unknown"),
                nullable=f.get("nillable", True),
                position=i,
                primary_key=f["type"] == "id",
                comment=f.get("label"),
            )
            for i, f in enumerate(raw["fields"])
            # Compound fields have no SOQL-selectable value of their own.
            if f["type"] not in {"address", "location"}
        ]
        return DatasetSchema(
            path=path, name=raw.get("label") or path[0], columns=columns, comment=raw.get("label")
        )

    def sample(self, path: list[str], limit: int = 100) -> SampleResult:
        schema = self.describe(path)
        # SOQL has no SELECT *; it also caps query length, so cap the projection.
        fields = [c.name for c in schema.columns][:200]
        soql = f"SELECT {', '.join(fields)} FROM {path[0]} LIMIT {int(limit)}"
        payload = self._get(f"/services/data/{self.api_version}/query", {"q": soql})
        rows = [[record.get(f) for f in fields] for record in payload.get("records", [])]
        return SampleResult(columns=fields, rows=rows, truncated=len(rows) >= limit)

    def relationships(self, obj: str) -> list[dict[str, str]]:
        """Declared lookups and master-detail links, used to seed the ontology graph."""
        raw = self._describe_raw(obj)
        links = []
        for field in raw["fields"]:
            for target in field.get("referenceTo") or []:
                links.append(
                    {
                        "from_table": obj,
                        "from_column": field["name"],
                        "to_table": target,
                        "to_column": "Id",
                    }
                )
        return links

    def read_batches(self, path: list[str], batch_size: int = 2000):
        """
        Page through an object with SOQL.

        Salesforce returns at most 2,000 records per call and hands back a cursor URL
        for the next page, so the whole object arrives in pages rather than at once.
        Compound fields are already excluded by `describe`, since SOQL cannot select
        them.
        """
        from app.engine.arrow import arrow_schema, rows_to_batch

        schema = self.describe(path)
        # SOQL has no SELECT * and caps query length, so the projection is capped too.
        columns = schema.columns[:200]
        fields = [c.name for c in columns]
        target = arrow_schema(columns)

        soql = f"SELECT {', '.join(fields)} FROM {path[0]}"
        payload = self._get(
            f"/services/data/{self.api_version}/query", {"q": soql}
        )

        while True:
            records = payload.get("records", [])
            if records:
                rows = [[record.get(field) for field in fields] for record in records]
                yield rows_to_batch(rows, columns, target)
            if payload.get("done", True):
                break
            next_url = payload.get("nextRecordsUrl")
            if not next_url:
                break
            payload = self._get(next_url)
