from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete

from app.api.schemas import DatasetDetail, DatasetRead, DatasetSelection, LayerOverride
from app.auth.deps import get_scope, get_tenant_context
from app.connectors.registry import build
from app.connectors.types import DatasetSchema
from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationError
from app.medallion.classifier import classify_layer
from app.models.base import utcnow
from app.models.entities import Connection, Dataset, DatasetRelationship
from app.profiling.profiler import profile_dataset
from app.profiling.relationships import DatasetContext, infer_relationships
from app.profiling.types import DatasetProfile
from app.secrets.connections import load_secrets
from app.tenancy.scope import TenantScope

# Every route below resolves a tenant before the handler runs. Declared once here
# rather than per endpoint, so a new endpoint is guarded by default instead of by
# whoever remembers. `assert_routes_are_guarded` at startup proves none slipped past.
router = APIRouter(
    prefix="/api", tags=["datasets"],
    dependencies=[Depends(get_tenant_context)],
)
settings = get_settings()


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _open(connection: Connection):
    secrets = load_secrets(connection.secret_refs or {})
    return build(connection.source_id, dict(connection.config), secrets)


def _read(dataset: Dataset) -> DatasetRead:
    return DatasetRead(
        id=dataset.id,
        connection_id=dataset.connection_id,
        name=dataset.name,
        path=dataset.path,
        row_estimate=dataset.row_estimate,
        column_count=len(dataset.column_schema or []),
        detected_layer=dataset.detected_layer,
        layer_confidence=dataset.layer_confidence,
        layer_override=dataset.layer_override,
        effective_layer=dataset.effective_layer,
        status=dataset.status,
        profiled_at=dataset.profiled_at,
    )


def _detail(dataset: Dataset) -> DatasetDetail:
    return DatasetDetail(
        **_read(dataset).model_dump(),
        columns=dataset.column_schema or [],
        profile=dataset.profile,
        layer_evidence=dataset.layer_evidence,
    )


def _schema_of(dataset: Dataset) -> DatasetSchema:
    return DatasetSchema(
        path=dataset.path,
        name=dataset.name,
        columns=dataset.column_schema or [],
        row_estimate=dataset.row_estimate,
    )


# --------------------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------------------


@router.post("/connections/{connection_id}/datasets", response_model=list[DatasetRead])
def select_datasets(
    connection_id: str, request: DatasetSelection, scope: TenantScope = Depends(get_scope)
) -> list[DatasetRead]:
    """Add datasets to the selection, reading each one's schema as it is added."""
    connection = scope.get(Connection, connection_id)
    if not request.paths:
        raise ValidationError("Select at least one dataset.")

    existing = {
        row.path_key
        for row in scope.db.execute(
            scope.select(Dataset).where(Dataset.connection_id == connection_id)
        ).scalars()
    }

    created: list[Dataset] = []
    with _open(connection) as connector:
        for path in request.paths:
            path_key = "".join(path)
            if path_key in existing:
                continue
            schema = connector.describe(path)
            dataset = scope.create(
                Dataset,
                connection_id=connection_id,
                name=path[-1],
                path=path,
                path_key=path_key,
                column_schema=[c.model_dump(mode="json") for c in schema.columns],
                row_estimate=schema.row_estimate,
                status="selected",
            )
            created.append(dataset)

    scope.db.commit()
    return [_read(d) for d in created]


@router.get("/datasets", response_model=list[DatasetRead])
def list_datasets(
    connection_id: str | None = Query(default=None), scope: TenantScope = Depends(get_scope)
) -> list[DatasetRead]:
    statement = scope.select(Dataset).order_by(Dataset.created_at)
    if connection_id:
        statement = statement.where(Dataset.connection_id == connection_id)
    return [_read(d) for d in scope.db.execute(statement).scalars()]


@router.get("/datasets/{dataset_id}", response_model=DatasetDetail)
def get_dataset(dataset_id: str, scope: TenantScope = Depends(get_scope)) -> DatasetDetail:
    return _detail(scope.get(Dataset, dataset_id))


@router.delete("/datasets/{dataset_id}", status_code=204)
def remove_dataset(dataset_id: str, scope: TenantScope = Depends(get_scope)) -> None:
    scope.db.delete(scope.get(Dataset, dataset_id))
    scope.db.commit()


@router.patch("/datasets/{dataset_id}", response_model=DatasetRead)
def override_layer(
    dataset_id: str, request: LayerOverride, scope: TenantScope = Depends(get_scope)
) -> DatasetRead:
    """Let a data owner correct the classifier. Their answer always wins."""
    dataset = scope.get(Dataset, dataset_id)
    if request.layer is not None and request.layer not in {"bronze", "silver", "gold"}:
        raise ValidationError("A layer must be bronze, silver or gold.")
    dataset.layer_override = request.layer
    scope.db.commit()
    return _read(dataset)


# --------------------------------------------------------------------------------------
# profiling
# --------------------------------------------------------------------------------------


@router.post("/datasets/{dataset_id}/profile", response_model=DatasetDetail)
def run_profile(dataset_id: str, scope: TenantScope = Depends(get_scope)) -> DatasetDetail:
    """Profile one dataset and classify which medallion layer its data is already at."""
    dataset = scope.get(Dataset, dataset_id)
    connection = scope.get(Connection, dataset.connection_id)

    with _open(connection) as connector:
        schema = _schema_of(dataset)
        profile = profile_dataset(
            connector, dataset.path, schema, sample_rows=settings.profile_sample_rows
        )

    verdict = classify_layer(dataset.name, schema, profile)

    dataset.profile = profile.as_dict()
    dataset.row_estimate = profile.row_count or dataset.row_estimate
    dataset.detected_layer = verdict.layer
    dataset.layer_confidence = verdict.confidence
    dataset.layer_evidence = verdict.model_dump(mode="json")
    dataset.profiled_at = utcnow()
    dataset.status = "profiled"
    scope.db.commit()

    return _detail(dataset)


# --------------------------------------------------------------------------------------
# relationships
# --------------------------------------------------------------------------------------


@router.post("/connections/{connection_id}/relationships")
def detect_relationships(
    connection_id: str, scope: TenantScope = Depends(get_scope)
) -> dict[str, Any]:
    """Re-derive join paths across every profiled dataset in this connection."""
    connection = scope.get(Connection, connection_id)
    datasets = list(
        scope.db.execute(
            scope.select(Dataset).where(Dataset.connection_id == connection_id)
        ).scalars()
    )
    profiled = [d for d in datasets if d.profile]
    if len(profiled) < 2:
        raise ValidationError(
            "Profile at least two datasets before looking for relationships between them."
        )

    contexts = [
        DatasetContext(
            id=d.id,
            name=d.name,
            path=d.path,
            schema=_schema_of(d),
            profile=DatasetProfile.model_validate(d.profile),
        )
        for d in profiled
    ]

    with _open(connection) as connector:
        found = infer_relationships(connector, contexts)

    # Preserve rejections so a user's "no" is not undone by the next detection run.
    rejected = {
        (r.from_dataset_id, r.from_column, r.to_dataset_id, r.to_column)
        for r in scope.db.execute(
            scope.select(DatasetRelationship).where(
                DatasetRelationship.connection_id == connection_id,
                DatasetRelationship.accepted.is_(False),
            )
        ).scalars()
    }

    scope.db.execute(
        delete(DatasetRelationship).where(
            DatasetRelationship.connection_id == connection_id,
            DatasetRelationship.accepted.is_(True),
        )
    )

    kept = 0
    for relationship in found:
        key = (
            relationship.from_dataset,
            relationship.from_column,
            relationship.to_dataset,
            relationship.to_column,
        )
        if key in rejected:
            continue
        kept += 1
        scope.create(
            DatasetRelationship,
            connection_id=connection_id,
            from_dataset_id=relationship.from_dataset,
            from_column=relationship.from_column,
            to_dataset_id=relationship.to_dataset,
            to_column=relationship.to_column,
            kind=relationship.kind,
            confidence=relationship.confidence,
            overlap=relationship.overlap,
            cardinality=relationship.cardinality,
        )
    scope.db.commit()

    return {
        "found": kept,
        "declared": len([r for r in found if r.kind == "declared"]),
        "inferred": len([r for r in found if r.kind == "inferred"]),
    }


@router.get("/connections/{connection_id}/relationships")
def list_relationships(
    connection_id: str, scope: TenantScope = Depends(get_scope)
) -> list[dict[str, Any]]:
    rows = scope.db.execute(
        scope.select(DatasetRelationship)
        .where(DatasetRelationship.connection_id == connection_id)
        .order_by(DatasetRelationship.confidence.desc())
    ).scalars()
    names = {
        d.id: d.name
        for d in scope.db.execute(
            scope.select(Dataset).where(Dataset.connection_id == connection_id)
        ).scalars()
    }
    return [
        {
            "id": r.id,
            "from_dataset_id": r.from_dataset_id,
            "from_dataset": names.get(r.from_dataset_id, "?"),
            "from_column": r.from_column,
            "to_dataset_id": r.to_dataset_id,
            "to_dataset": names.get(r.to_dataset_id, "?"),
            "to_column": r.to_column,
            "kind": r.kind,
            "confidence": r.confidence,
            "overlap": r.overlap,
            "cardinality": r.cardinality,
            "accepted": r.accepted,
        }
        for r in rows
    ]


@router.patch("/relationships/{relationship_id}")
def set_relationship_accepted(
    relationship_id: str, accepted: bool = Query(...), scope: TenantScope = Depends(get_scope)
) -> dict[str, Any]:
    relationship = scope.get(DatasetRelationship, relationship_id)
    if relationship is None:
        raise NotFoundError("That relationship does not exist.")
    relationship.accepted = accepted
    scope.db.commit()
    return {"id": relationship.id, "accepted": relationship.accepted}
