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


class S3Connector(FileConnector):
    requires = ("boto3", "boto3", "s3")

    spec = CredentialSpec(
        source_id="s3",
        name="Amazon S3",
        category=SourceCategory.OBJECT_STORE,
        summary="S3 buckets and S3-compatible stores such as MinIO or Cloudflare R2.",
        icon="cloud",
        capabilities=Capabilities(
            levels=[
                BrowseLevel(key="bucket", label="Bucket", plural="Buckets"),
                BrowseLevel(key="prefix", label="Prefix", plural="Prefixes"),
                BrowseLevel(key="object", label="Object", plural="Objects"),
            ]
        ),
        fields=[
            SpecField(name="region", label="Region", default="us-east-1"),
            SpecField(
                name="bucket",
                label="Bucket",
                required=False,
                help="Optional. Required when the credential cannot list buckets.",
            ),
            SpecField(
                name="endpoint_url",
                label="Custom endpoint",
                required=False,
                group="Network",
                placeholder="https://minio.internal:9000",
                help="For S3-compatible stores. Leave blank for AWS.",
            ),
        ],
        auth_methods=[
            AuthMethod(
                id="access_key",
                label="Access key",
                description="An IAM user or role access key with s3:GetObject and s3:ListBucket.",
                recommended=True,
                fields=[
                    SpecField(name="access_key_id", label="Access key ID"),
                    SpecField(
                        name="secret_access_key",
                        label="Secret access key",
                        type=FieldType.PASSWORD,
                        secret=True,
                    ),
                    SpecField(
                        name="session_token",
                        label="Session token",
                        type=FieldType.PASSWORD,
                        secret=True,
                        required=False,
                        help="Only for temporary STS credentials.",
                    ),
                ],
            ),
            AuthMethod(
                id="instance_role",
                label="Instance role",
                description="Use the IAM role attached to the host running Assarium. Nothing is "
                "stored.",
                fields=[],
            ),
        ],
    )

    _client: Any = None

    @property
    def client(self) -> Any:
        if self._client is None:
            boto3 = self.ensure_driver()
            kwargs: dict[str, Any] = {"region_name": self.config.get("region") or "us-east-1"}
            if self.config.get("endpoint_url"):
                kwargs["endpoint_url"] = self.config["endpoint_url"]
            if self.auth_method == "access_key":
                kwargs["aws_access_key_id"] = self.config["access_key_id"]
                kwargs["aws_secret_access_key"] = self.secrets["secret_access_key"]
                if self.secrets.get("session_token"):
                    kwargs["aws_session_token"] = self.secrets["session_token"]
            self._client = boto3.client("s3", **kwargs)
        return self._client

    def probe(self) -> dict[str, Any]:
        bucket = self.config.get("bucket")
        if bucket:
            self.client.head_bucket(Bucket=bucket)
            return {"server_version": "Amazon S3", "bucket": bucket}
        buckets = self.client.list_buckets().get("Buckets", [])
        return {"server_version": "Amazon S3", "buckets_visible": str(len(buckets))}

    def browse(self, path: list[str]) -> list[BrowseNode]:
        configured = self.config.get("bucket")
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
                    id=b["Name"],
                    name=b["Name"],
                    kind="container",
                    path=[b["Name"]],
                    has_children=True,
                )
                for b in self.client.list_buckets().get("Buckets", [])
            ]

        bucket, *rest = path
        prefix = "/".join(rest)
        if prefix:
            prefix += "/"
        paginator = self.client.get_paginator("list_objects_v2")
        nodes: list[BrowseNode] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
            for common in page.get("CommonPrefixes", []):
                key = common["Prefix"].rstrip("/")
                nodes.append(
                    BrowseNode(
                        id=key,
                        name=key.rsplit("/", 1)[-1],
                        kind="folder",
                        path=[bucket, *key.split("/")],
                        has_children=True,
                    )
                )
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                name = key.rsplit("/", 1)[-1]
                if not self.is_tabular(name):
                    continue
                nodes.append(
                    BrowseNode(
                        id=key,
                        name=name,
                        kind="dataset",
                        path=[bucket, *key.split("/")],
                        size_bytes=obj.get("Size"),
                        meta={"object_type": Path(name).suffix.lstrip(".").upper()},
                    )
                )
        nodes.sort(key=lambda n: (n.kind != "folder", n.name.lower()))
        return nodes

    def _download(self, path: list[str], byte_cap: int = PREVIEW_BYTE_CAP) -> Path:
        bucket, *rest = path
        key = "/".join(rest)
        head = self.client.head_object(Bucket=bucket, Key=key)
        if head["ContentLength"] > byte_cap:
            raise ConnectionFailedError(
                f"{key} is {head['ContentLength'] / 1e9:.1f} GB, above the preview limit. "
                "Ingest it into a layer first, then explore the result."
            )
        target = self._temp_path(key)
        self.client.download_file(bucket, key, str(target))
        return target

    def stat(self, path: list[str]) -> FileChange:
        bucket, *rest = path
        key = "/".join(rest)
        head = self.client.head_object(Bucket=bucket, Key=key)
        return FileChange(
            path=path,
            name=key.rsplit("/", 1)[-1],
            etag=(head.get("ETag") or "").strip('"'),
            size_bytes=head.get("ContentLength"),
            modified_at=str(head.get("LastModified") or ""),
        )
