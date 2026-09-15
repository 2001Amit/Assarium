from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

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

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = ["https://graph.microsoft.com/.default"]


class SharePointConnector(FileConnector):
    """
    SharePoint Online document libraries through Microsoft Graph, app-only.

    Certificate credentials are the recommended path: `Sites.Selected` grants site-scoped
    access, and the SharePoint API surface does not accept client secrets.
    """

    requires = ("msal", "msal", "sharepoint")

    spec = CredentialSpec(
        source_id="sharepoint",
        name="SharePoint",
        category=SourceCategory.SAAS,
        summary="Document libraries in SharePoint Online, read through Microsoft Graph.",
        icon="folder",
        docs_url="https://learn.microsoft.com/graph/permissions-reference",
        capabilities=Capabilities(
            levels=[
                BrowseLevel(key="site", label="Site", plural="Sites"),
                BrowseLevel(key="library", label="Library", plural="Libraries"),
                BrowseLevel(key="folder", label="Folder", plural="Folders"),
                BrowseLevel(key="file", label="File", plural="Files"),
            ]
        ),
        fields=[
            SpecField(name="tenant_id", label="Directory (tenant) ID"),
            SpecField(name="client_id", label="Application (client) ID"),
            SpecField(
                name="site_url",
                label="Site URL",
                required=False,
                placeholder="https://contoso.sharepoint.com/sites/Finance",
                help="Required when the app holds Sites.Selected, which cannot list sites.",
            ),
        ],
        auth_methods=[
            AuthMethod(
                id="certificate",
                label="Certificate",
                description="App-only access with an uploaded certificate. The only credential "
                "type supported across the whole SharePoint surface.",
                recommended=True,
                notice="Grant the Graph application permission Sites.Selected, consent it, then "
                "assign the app read on each site you want reachable.",
                fields=[
                    SpecField(
                        name="certificate_pem",
                        label="Certificate private key (PEM)",
                        type=FieldType.TEXTAREA,
                        secret=True,
                        placeholder="-----BEGIN PRIVATE KEY-----",
                    ),
                    SpecField(
                        name="certificate_thumbprint",
                        label="Certificate thumbprint",
                        placeholder="A1B2C3...",
                        help="Shown in the app registration under Certificates & secrets.",
                    ),
                ],
            ),
            AuthMethod(
                id="client_secret",
                label="Client secret",
                description="Simpler to set up and sufficient for Graph drive access, but it "
                "cannot reach SharePoint's own REST or CSOM endpoints.",
                fields=[
                    SpecField(
                        name="client_secret",
                        label="Client secret",
                        type=FieldType.PASSWORD,
                        secret=True,
                    )
                ],
            ),
        ],
    )

    _token: str | None = None

    def _acquire_token(self) -> str:
        if self._token:
            return self._token
        msal = self.ensure_driver()
        authority = f"https://login.microsoftonline.com/{self.config['tenant_id']}"

        if self.auth_method == "certificate":
            credential: Any = {
                "private_key": self.secrets["certificate_pem"],
                "thumbprint": self.config["certificate_thumbprint"].replace(":", "").strip(),
            }
        else:
            credential = self.secrets["client_secret"]

        app = msal.ConfidentialClientApplication(
            client_id=self.config["client_id"],
            authority=authority,
            client_credential=credential,
        )
        result = app.acquire_token_for_client(scopes=SCOPE)
        if "access_token" not in result:
            raise ConnectionFailedError(
                result.get("error_description", "Microsoft Entra ID rejected the credential.")
                .splitlines()[0]
            )
        self._token = result["access_token"]
        return self._token

    def _get(self, url: str, **kwargs: Any) -> dict[str, Any]:
        if not url.startswith("http"):
            url = f"{GRAPH}{url}"
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {self._acquire_token()}"},
            timeout=60,
            follow_redirects=True,
            **kwargs,
        )
        if response.status_code >= 400:
            detail = response.json().get("error", {}).get("message", response.text)
            raise ConnectionFailedError(str(detail).splitlines()[0][:300])
        return response.json()

    def _site_id(self) -> str:
        parsed = urlparse(self.config["site_url"])
        site_path = parsed.path.strip("/")
        data = self._get(f"/sites/{parsed.netloc}:/{site_path}")
        return data["id"]

    def probe(self) -> dict[str, Any]:
        if self.config.get("site_url"):
            site = self._get(f"/sites/{self._site_id()}")
            return {"server_version": "Microsoft Graph v1.0", "site": site.get("displayName")}
        sites = self._get("/sites?search=*").get("value", [])
        return {"server_version": "Microsoft Graph v1.0", "sites_visible": str(len(sites))}

    def browse(self, path: list[str]) -> list[BrowseNode]:
        if not path:
            if self.config.get("site_url"):
                site = self._get(f"/sites/{self._site_id()}")
                return [
                    BrowseNode(
                        id=site["id"],
                        name=site.get("displayName") or site["name"],
                        kind="container",
                        path=[site["id"]],
                        has_children=True,
                        meta={"web_url": site.get("webUrl")},
                    )
                ]
            return [
                BrowseNode(
                    id=s["id"],
                    name=s.get("displayName") or s.get("name") or s["id"],
                    kind="container",
                    path=[s["id"]],
                    has_children=True,
                    meta={"web_url": s.get("webUrl")},
                )
                for s in self._get("/sites?search=*").get("value", [])
            ]

        site_id, *rest = path
        if not rest:
            return [
                BrowseNode(
                    id=d["id"],
                    name=d["name"],
                    kind="namespace",
                    path=[site_id, d["id"]],
                    has_children=True,
                    meta={"drive_type": d.get("driveType")},
                )
                for d in self._get(f"/sites/{site_id}/drives").get("value", [])
            ]

        drive_id, *segments = rest
        if segments:
            folder = "/".join(segments)
            url = f"/drives/{drive_id}/root:/{folder}:/children"
        else:
            url = f"/drives/{drive_id}/root/children"

        nodes: list[BrowseNode] = []
        for item in self._get(url).get("value", []):
            child_path = [site_id, drive_id, *segments, item["name"]]
            if "folder" in item:
                nodes.append(
                    BrowseNode(
                        id=item["id"],
                        name=item["name"],
                        kind="folder",
                        path=child_path,
                        has_children=item["folder"].get("childCount", 0) > 0,
                    )
                )
            elif self.is_tabular(item["name"]):
                nodes.append(
                    BrowseNode(
                        id=item["id"],
                        name=item["name"],
                        kind="dataset",
                        path=child_path,
                        size_bytes=item.get("size"),
                        meta={
                            "object_type": Path(item["name"]).suffix.lstrip(".").upper(),
                            "modified": item.get("lastModifiedDateTime"),
                        },
                    )
                )
        nodes.sort(key=lambda n: (n.kind != "folder", n.name.lower()))
        return nodes

    def _download(self, path: list[str], byte_cap: int = PREVIEW_BYTE_CAP) -> Path:
        _site_id, drive_id, *segments = path
        item_path = "/".join(segments)
        meta = self._get(f"/drives/{drive_id}/root:/{item_path}")
        if (meta.get("size") or 0) > byte_cap:
            raise ConnectionFailedError(
                f"{item_path} is above the preview size limit. Ingest it into a layer first."
            )
        target = self._temp_path(segments[-1])
        with httpx.stream(
            "GET",
            f"{GRAPH}/drives/{drive_id}/root:/{item_path}:/content",
            headers={"Authorization": f"Bearer {self._acquire_token()}"},
            follow_redirects=True,
            timeout=300,
        ) as response:
            response.raise_for_status()
            with open(target, "wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
        return target

    def stat(self, path: list[str]) -> FileChange:
        """
        Graph reports two version markers. cTag changes only when the *content* changes;
        eTag also changes on a metadata edit, which would cause needless re-downloads.
        """
        _site_id, drive_id, *segments = path
        item_path = "/".join(segments)
        meta = self._get(f"/drives/{drive_id}/root:/{item_path}")
        return FileChange(
            path=path,
            name=meta.get("name", segments[-1]),
            etag=str(meta.get("cTag") or meta.get("eTag") or ""),
            size_bytes=meta.get("size"),
            modified_at=meta.get("lastModifiedDateTime"),
        )
