from __future__ import annotations

from pathlib import Path
from typing import Any

from app.connectors.file_base import PREVIEW_BYTE_CAP, FileConnector
from app.connectors.types import (
    BrowseLevel,
    BrowseNode,
    Capabilities,
    CredentialSpec,
    SourceCategory,
    SpecField,
)
from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.ingestion.types import FileChange


class LocalFileConnector(FileConnector):
    """Files uploaded through the browser. No credentials; scoped to one upload folder."""

    spec = CredentialSpec(
        source_id="files",
        name="File upload",
        category=SourceCategory.FILE,
        summary="CSV, TSV, Parquet, JSON or Excel files uploaded directly.",
        icon="file-spreadsheet",
        capabilities=Capabilities(
            levels=[BrowseLevel(key="file", label="File", plural="Files")]
        ),
        fields=[
            SpecField(
                name="label",
                label="Name this upload set",
                placeholder="Q3 finance extracts",
                help="Files are stored on the platform host and are not sent anywhere else.",
            )
        ],
        auth_methods=[],
    )

    @property
    def root(self) -> Path:
        upload_id = self.config.get("upload_id")
        if not upload_id:
            raise NotFoundError("This upload set has no storage folder yet.")
        path = get_settings().data_dir / "uploads" / str(upload_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def probe(self) -> dict[str, Any]:
        files = [f for f in self.root.iterdir() if f.is_file() and self.is_tabular(f.name)]
        return {"server_version": "Local storage", "files": str(len(files))}

    def browse(self, path: list[str]) -> list[BrowseNode]:
        base = self.root.joinpath(*path)
        if not base.is_dir():
            return []
        nodes: list[BrowseNode] = []
        for entry in sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if entry.is_dir():
                nodes.append(
                    BrowseNode(
                        id="/".join([*path, entry.name]),
                        name=entry.name,
                        kind="folder",
                        path=[*path, entry.name],
                        has_children=True,
                    )
                )
            elif self.is_tabular(entry.name):
                nodes.append(
                    BrowseNode(
                        id="/".join([*path, entry.name]),
                        name=entry.name,
                        kind="dataset",
                        path=[*path, entry.name],
                        size_bytes=entry.stat().st_size,
                        meta={"object_type": entry.suffix.lstrip(".").upper()},
                    )
                )
        return nodes

    def _download(self, path: list[str], byte_cap: int = PREVIEW_BYTE_CAP) -> Path:
        # Resolve inside the upload root so a crafted path cannot escape it.
        root = self.root.resolve()
        target = root.joinpath(*path).resolve()
        if not target.is_relative_to(root):
            raise NotFoundError("That file is outside this upload set.")
        if not target.is_file():
            raise NotFoundError(f"{'/'.join(path)} was not found in this upload set.")
        return target

    def stat(self, path: list[str]) -> FileChange:
        """Local files have no etag, so size and modification time stand in for one."""
        target = self._download(path)
        info = target.stat()
        return FileChange(
            path=path,
            name=target.name,
            etag=f"{info.st_size}-{int(info.st_mtime)}",
            size_bytes=info.st_size,
            modified_at=str(int(info.st_mtime)),
        )
