import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit import AuditEntry

logger = logging.getLogger("assarium.audit")


def log_audit_event(
    db: Session,
    tenant_id: str,
    actor_id: str,
    action: str,
    target_type: str,
    target_id: str,
    details: dict[str, Any] | None = None,
) -> None:
    """
    Record an immutable audit event for compliance and security tracking.
    """
    entry = AuditEntry(
        tenant_id=tenant_id,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details or {},
    )
    db.add(entry)
    db.flush()
    logger.info(
        "Audit [%s]: %s on %s %s by %s",
        tenant_id, action, target_type, target_id, actor_id,
    )


def get_audit_events(db: Session, tenant_id: str, limit: int = 100) -> list[AuditEntry]:
    """Retrieve the most recent audit events for a tenant."""
    return (
        db.query(AuditEntry)
        .filter(AuditEntry.tenant_id == tenant_id)
        .order_by(AuditEntry.timestamp.desc())
        .limit(limit)
        .all()
    )
