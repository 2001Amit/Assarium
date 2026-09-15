"""
Reading and writing the ingestion state: watermarks and landed files.

Kept apart from the pipeline so the rule that matters - a watermark only advances after
a successful write - lives in one place and can be tested on its own.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingestion.models import DatasetWatermark, IngestedFile
from app.ingestion.types import IngestionPlan, WatermarkState
from app.models.base import utcnow

logger = logging.getLogger("assarium.ingestion")


def load_state(db: Session, dataset_id: str) -> WatermarkState:
    """Where the last successful load got to. A dataset with no history starts at zero."""
    row = db.execute(
        select(DatasetWatermark).where(DatasetWatermark.dataset_id == dataset_id)
    ).scalar_one_or_none()
    if row is None:
        return WatermarkState()
    return WatermarkState(
        strategy=row.strategy,  # type: ignore[arg-type]
        column=row.column_name,
        value=row.value,
        token=row.token,
        last_success_at=row.last_success_at,
        rows_total=row.rows_total or 0,
    )


def commit_state(
    db: Session,
    *,
    tenant_id: str,
    connection_id: str,
    dataset_id: str,
    plan: IngestionPlan,
    rows_read: int,
    run_id: str,
) -> WatermarkState:
    """
    Advance the watermark. Call this only after the data has been written.

    Advancing before the write would mean a crash between the two loses that window
    permanently, because the next run would start after it.
    """
    previous = load_state(db, dataset_id)
    state = plan.next_state(rows_read, previous)

    row = db.execute(
        select(DatasetWatermark).where(DatasetWatermark.dataset_id == dataset_id)
    ).scalar_one_or_none()
    if row is None:
        row = DatasetWatermark(
            tenant_id=tenant_id, connection_id=connection_id, dataset_id=dataset_id
        )
        db.add(row)

    row.strategy = state.strategy
    row.column_name = state.column
    row.value = state.value
    row.token = state.token
    row.rows_total = state.rows_total
    row.last_run_id = run_id
    row.last_success_at = utcnow()
    db.flush()

    logger.info(
        "Watermark for dataset %s advanced to %s (%s rows this run)",
        dataset_id, state.value or state.token or "n/a", rows_read,
    )
    return state


def known_file_etags(db: Session, dataset_id: str) -> dict[str, str]:
    """
    File key -> etag for versions already landed successfully.

    A file whose last attempt FAILED is deliberately absent, so the next run retries it
    instead of treating it as done.
    """
    rows = db.execute(
        select(IngestedFile).where(
            IngestedFile.dataset_id == dataset_id,
            IngestedFile.status != "FAILED",
        )
    ).scalars()
    return {row.file_key: row.etag for row in rows}


def record_file(
    db: Session,
    *,
    tenant_id: str,
    connection_id: str,
    dataset_id: str,
    change,
    run_id: str,
    rows: int | None = None,
    status: str = "LANDED",
    error: str | None = None,
) -> None:
    """Record one file version. Re-recording the same version is a no-op."""
    file_key = "/".join(change.path)
    existing = db.execute(
        select(IngestedFile).where(
            IngestedFile.dataset_id == dataset_id,
            IngestedFile.file_key == file_key,
            IngestedFile.etag == (change.etag or ""),
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.status = status
        existing.rows = rows
        existing.run_id = run_id
        existing.error = error
        db.flush()
        return

    db.add(IngestedFile(
        tenant_id=tenant_id,
        connection_id=connection_id,
        dataset_id=dataset_id,
        file_key=file_key,
        file_name=change.name,
        etag=change.etag or "",
        size_bytes=change.size_bytes,
        source_modified_at=change.modified_at,
        status=status,
        rows=rows,
        run_id=run_id,
        error=error,
    ))
    db.flush()
