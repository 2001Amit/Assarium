from __future__ import annotations

from pathlib import Path
from typing import Any

from app.connectors.file_base import PREVIEW_BYTE_CAP, FileConnector
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
from app.ingestion.types import FileChange


class ADLSConnector(FileConnector):
    """Azure Data Lake Storage Gen2 / Blob Storage with hierarchical namespace."""

    requires = ("azure.storage.filedatalake", "azure-storage-file-datalake", "adls")

    spec = CredentialSpec(
        source_id="adls",
        name="Azure Data Lake Storage",
        category=SourceCategory.OBJECT_STORE,
        summary="ADLS Gen2 and Blob Storage containers holding CSV, Parquet, JSON or Excel.",
        icon="cloud",
        docs_url="https://learn.microsoft.com/azure/storage/blobs/data-lake-storage-introduction",
        capabilities=Capabilities(
            levels=[
                BrowseLevel(key="container", label="Container", plural="Containers"),
                BrowseLevel(key="path", label="Folder", plural="Folders"),
                BrowseLevel(key="file", label="File", plural="Files"),
            ]
        ),
        fields=[
            SpecField(
                name="account_name",
                label="Storage account",
                placeholder="mystorageaccount",
                help="The account name only, without .dfs.core.windows.net.",
            ),
            SpecField(
                name="container",
                label="Container",
                required=False,
                help="Optional. Leave blank to browse every container the credential can see.",
            ),
        ],
        auth_methods=[
            AuthMethod(
                id="service_principal",
                label="Microsoft Entra service principal",
                description="An app registration granted Storage Blob Data Reader on the "
                "account or container.",
                recommended=True,
                fields=[
                    SpecField(name="tenant_id", label="Tenant ID"),
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
                id="sas",
                label="SAS token",
                description="A shared access signature scoped to the container. Expires on the "
                "date you set when issuing it.",
                fields=[
                    SpecField(
                        name="sas_token",
                        label="SAS token",
                        type=FieldType.PASSWORD,
                        secret=True,
                        placeholder="sv=2023-01-03&ss=b&srt=co&sp=rl&se=...",
                    )
                ],
            ),
            AuthMethod(
                id="account_key",
                label="Account key",
                description="The storage account's shared key. Grants full account access, so "
                "prefer a service principal or SAS where possible.",
                fields=[
                    SpecField(
                        name="account_key",
                        label="Account key",
                        type=FieldType.PASSWORD,
                        secret=True,
                    )
                ],
            ),
            AuthMethod(
                id="managed_identity",
                label="Managed identity",
                description="Use the identity assigned to the host running Assarium. Nothing is "
                "stored.",
                fields=[],
            ),
        ],
    )

    _service: Any = None

    @property
    def service(self) -> Any:
        if self._service is None:
            self.ensure_driver()
            from azure.storage.filedatalake import DataLakeServiceClient

            account = self.config["account_name"]
            url = f"https://{account}.dfs.core.windows.net"

            if self.auth_method == "service_principal":
                from azure.identity import ClientSecretCredential

                credential: Any = ClientSecretCredential(
                    tenant_id=self.config["tenant_id"],
                    client_id=self.config["client_id"],
                    client_secret=self.secrets["client_secret"],
                )
            elif self.auth_method == "managed_identity":
                from azure.identity import DefaultAzureCredential

                credential = DefaultAzureCredential()
            elif self.auth_method == "sas":
                credential = self.secrets["sas_token"]
            else:
                credential = self.secrets["account_key"]

            self._service = DataLakeServiceClient(
                account_url=url, credential=credential
            )
        return self._service

    def close(self) -> None:
        if self._service is not None:
            self._service.close()
            self._service = None

    def probe(self) -> dict[str, Any]:
        containers = [c.name for c in self.service.list_file_systems(max_results=5)]
        if self.config.get("container") and self.config["container"] not in containers:
            # A container-scoped SAS cannot enumerate, so verify by touching it directly.
            self.service.get_file_system_client(self.config["container"]).get_directory_client(
                "/"
            ).get_directory_properties()
        return {
            "server_version": "ADLS Gen2",
            "account": self.config["account_name"],
            "containers_visible": str(len(containers)),
        }

    def browse(self, path: list[str]) -> list[BrowseNode]:
        configured = self.config.get("container")
        if not path:
            if configured:
                return [
                    BrowseNode(
                        id=configured,
                        name=configured,
                        kind="container",
                        path=[configured],
                        has_children=True,
                    )
                ]
            return [
                BrowseNode(
                    id=c.name, name=c.name, kind="container", path=[c.name], has_children=True
                )
                for c in self.service.list_file_systems()
            ]

        container, *rest = path
        prefix = "/".join(rest)
        fs = self.service.get_file_system_client(container)
        nodes: list[BrowseNode] = []
        for entry in fs.get_paths(path=prefix or None, recursive=False):
            name = entry.name.rsplit("/", 1)[-1]
            node_path = [container, *entry.name.split("/")]
            if entry.is_directory:
                nodes.append(
                    BrowseNode(
                        id=entry.name,
                        name=name,
                        kind="folder",
                        path=node_path,
                        has_children=True,
                    )
                )
            elif self.is_tabular(name):
                nodes.append(
                    BrowseNode(
                        id=entry.name,
                        name=name,
                        kind="dataset",
                        path=node_path,
                        size_bytes=entry.content_length,
                        meta={"object_type": Path(name).suffix.lstrip(".").upper()},
                    )
                )
        nodes.sort(key=lambda n: (n.kind != "folder", n.name.lower()))
        return nodes

    def _download(self, path: list[str], byte_cap: int = PREVIEW_BYTE_CAP) -> Path:
        container, *rest = path
        key = "/".join(rest)
        client = self.service.get_file_system_client(container).get_file_client(key)
        props = client.get_file_properties()
        if props.size > byte_cap:
            raise ConnectionFailedError(
                f"{key} is {props.size / 1e9:.1f} GB, above the {byte_cap / 1e9:.1f} GB preview "
                "limit. Ingest it into a layer first, then explore the result."
            )
        target = self._temp_path(key)
        with open(target, "wb") as handle:
            client.download_file().readinto(handle)
        return target

    def stat(self, path: list[str]) -> FileChange:
        container, *rest = path
        key = "/".join(rest)
        client = self.service.get_file_system_client(container).get_file_client(key)
        props = client.get_file_properties()
        return FileChange(
            path=path,
            name=key.rsplit("/", 1)[-1],
            etag=str(props.etag or "").strip('"'),
            size_bytes=props.size,
            modified_at=str(props.last_modified or ""),
        )
