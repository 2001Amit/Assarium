from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Query, UploadFile

from app.api.schemas import (
    ConnectionCreate,
    ConnectionRead,
    ConnectionTestRequest,
)
from app.auth.deps import get_scope, get_tenant_context
from app.connectors.registry import build, get_spec, split_payload
from app.connectors.types import BrowseNode, ConnectionTestResult, DatasetSchema, SampleResult
from app.core.config import get_settings
from app.core.errors import ValidationError
from app.models.base import new_id, utcnow
from app.models.entities import Connection, Dataset
from app.secrets.connections import (
    forget_secrets,
    load_secrets,
    merge_secrets,
    store_secrets,
)
from app.tenancy.scope import TenantScope

# Every route below resolves a tenant before the handler runs. Declared once here
# rather than per endpoint, so a new endpoint is guarded by default instead of by
# whoever remembers. `assert_routes_are_guarded` at startup proves none slipped past.
router = APIRouter(
    prefix="/api/connections", tags=["connections"],
    dependencies=[Depends(get_tenant_context)],
)


#: Caps on an upload. Without them a single request can fill the disk, which takes the
#: platform down for every tenant rather than just the one doing it.
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
MAX_UPLOAD_FILES = 50


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _open(connection: Connection):
    secrets = load_secrets(connection.secret_refs or {})
    return build(connection.source_id, dict(connection.config), secrets)


def _merge_stored_secrets(
    scope: TenantScope, request: ConnectionTestRequest | ConnectionCreate,
    connection_id: str | None,
) -> dict[str, Any]:
    """
    Let an edit omit secrets the user did not retype.

    Scoped, like every other lookup. Unscoped, this would let a caller name another
    tenant's connection id and have that tenant's stored credentials merged into a
    connection test they control - which is a way to read a secret without ever seeing it.
    """
    values = dict(request.values)
    if not connection_id:
        return values
    existing = scope.find(Connection, connection_id)
    if existing is None or not existing.secret_refs:
        return values
    return merge_secrets(connection_id, existing.secret_refs, values)


def _to_read(scope: TenantScope, connection: Connection) -> ConnectionRead:
    TenantScope.assert_is_scope(scope, "_to_read")
    dataset_count = scope.count(Dataset, Dataset.connection_id == connection.id)
    return ConnectionRead(
        id=connection.id,
        name=connection.name,
        source_id=connection.source_id,
        source_name=get_spec(connection.source_id).name,
        auth_method=connection.auth_method,
        config=connection.config,
        status=connection.status,
        status_message=connection.status_message,
        last_tested_at=connection.last_tested_at,
        created_at=connection.created_at,
        dataset_count=dataset_count,
    )


# --------------------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------------------


@router.post("/test", response_model=ConnectionTestResult)
def test_connection(
    request: ConnectionTestRequest, scope: TenantScope = Depends(get_scope)
) -> ConnectionTestResult:
    """Verify credentials without saving anything."""
    scope.require("connection:write")
    values = _merge_stored_secrets(scope, request, request.connection_id)
    config, secrets = split_payload(request.source_id, request.auth_method, values)
    with build(request.source_id, config, secrets) as connector:
        return connector.test()


@router.post("", response_model=ConnectionRead, status_code=201)
def create_connection(
    request: ConnectionCreate, scope: TenantScope = Depends(get_scope)
) -> ConnectionRead:
    scope.require("connection:write")
    if scope.one_where(Connection, Connection.name == request.name):
        raise ValidationError(f"A connection named '{request.name}' already exists.")

    config, secrets = split_payload(request.source_id, request.auth_method, request.values)
    if request.source_id == "files":
        # Each upload set gets an isolated folder; nothing is shared between connections.
        config["upload_id"] = new_id()
    connection = scope.create(
        Connection,
        name=request.name,
        source_id=request.source_id,
        auth_method=request.auth_method,
        config=config,
    )
    scope.db.flush()  # assigns the id the secret references are built from

    # Credentials go to the secret backend, never into this row.
    connection.secret_refs = store_secrets(connection.id, secrets)

    # Record the outcome rather than refusing to save: a source can be briefly unreachable
    # and the user should not lose the form they just filled in.
    with build(request.source_id, config, secrets) as connector:
        result = connector.test()
    connection.status = "ok" if result.ok else "failed"
    connection.status_message = result.message
    connection.last_tested_at = utcnow()

    scope.db.commit()
    return _to_read(scope, connection)


@router.get("", response_model=list[ConnectionRead])
def list_connections(scope: TenantScope = Depends(get_scope)) -> list[ConnectionRead]:
    rows = scope.db.execute(
        scope.select(Connection).order_by(Connection.created_at.desc())
    ).scalars().all()
    return [_to_read(scope, c) for c in rows]


@router.get("/{connection_id}", response_model=ConnectionRead)
def get_connection(
    connection_id: str, scope: TenantScope = Depends(get_scope)
) -> ConnectionRead:
    return _to_read(scope, scope.get(Connection, connection_id))


@router.delete("/{connection_id}", status_code=204)
def delete_connection(connection_id: str, scope: TenantScope = Depends(get_scope)) -> None:
    scope.require("connection:write")
    connection = scope.get(Connection, connection_id)
    # Remove the credentials too: a deleted connection must not leave a live secret
    # sitting in the vault with nothing pointing at it.
    forget_secrets(connection.secret_refs or {})
    scope.delete(connection)
    scope.db.commit()


@router.post("/{connection_id}/test", response_model=ConnectionTestResult)
def retest_connection(
    connection_id: str, scope: TenantScope = Depends(get_scope)
) -> ConnectionTestResult:
    connection = scope.get(Connection, connection_id)
    with _open(connection) as connector:
        result = connector.test()
    connection.status = "ok" if result.ok else "failed"
    connection.status_message = result.message
    connection.last_tested_at = utcnow()
    scope.db.commit()
    return result


@router.get("/{connection_id}/browse", response_model=list[BrowseNode])
def browse(
    connection_id: str,
    path: list[str] = Query(default=[]),
    scope: TenantScope = Depends(get_scope),
) -> list[BrowseNode]:
    """List the children of `path`. An empty path returns the source's top level."""
    with _open(scope.get(Connection, connection_id)) as connector:
        return connector.browse(list(path))


@router.get("/{connection_id}/schema", response_model=DatasetSchema)
def dataset_schema(
    connection_id: str,
    path: list[str] = Query(...),
    scope: TenantScope = Depends(get_scope),
) -> DatasetSchema:
    with _open(scope.get(Connection, connection_id)) as connector:
        return connector.describe(list(path))


@router.get("/{connection_id}/sample", response_model=SampleResult)
def dataset_sample(
    connection_id: str,
    path: list[str] = Query(...),
    limit: int = Query(default=100, ge=1, le=1000),
    scope: TenantScope = Depends(get_scope),
) -> SampleResult:
    with _open(scope.get(Connection, connection_id)) as connector:
        return connector.sample(list(path), limit=limit)


@router.post("/{connection_id}/upload")
def upload_files(
    connection_id: str,
    files: list[UploadFile] = File(...),
    scope: TenantScope = Depends(get_scope),
) -> dict[str, Any]:
    """Store uploaded files in this connection's own folder."""
    scope.require("connection:write")
    connection = scope.get(Connection, connection_id)
    if connection.source_id != "files":
        raise ValidationError("Only a file-upload connection accepts uploads.")

    folder = get_settings().data_dir / "uploads" / str(connection.config["upload_id"])
    folder.mkdir(parents=True, exist_ok=True)

    if len(files) > MAX_UPLOAD_FILES:
        raise ValidationError(
            f"That is {len(files)} files. Upload at most {MAX_UPLOAD_FILES} at a time."
        )

    stored: list[str] = []
    written = 0
    for upload in files:
        # Keep the basename only, so a crafted filename cannot escape the folder.
        name = Path(upload.filename or "upload").name
        if not name or name.startswith("."):
            raise ValidationError(f"'{upload.filename}' is not a usable file name.")
        target = folder / name
        # Counted as it streams, not from a declared Content-Length: the length is the
        # client's claim, and the bytes are what actually fill the disk.
        with open(target, "wb") as handle:
            while chunk := upload.file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    handle.close()
                    target.unlink(missing_ok=True)
                    raise ValidationError(
                        f"That upload exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB "
                        "limit for a single batch. Split it, or connect the source "
                        "directly instead of uploading extracts."
                    )
                handle.write(chunk)
        stored.append(name)

    connection.status = "ok"
    connection.status_message = f"{len(stored)} file(s) available."
    connection.last_tested_at = utcnow()
    scope.db.commit()
    return {"stored": stored}
