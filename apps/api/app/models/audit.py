from datetime import datetime
from typing import Any

from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models.base import Base, new_id, utcnow


class AuditEntry(Base):
    """
    Append-only audit log for sensitive operations (schema changes, role grants, etc).
    
    In a real production system with compliance requirements (SOC2, HIPAA), this table
    must never be updated or deleted from, which is enforced via database-level triggers
    or by shipping the logs to a WORM (Write Once Read Many) storage system.
    """
    
    __tablename__ = "audit_entries"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(index=True)
    actor_id: Mapped[str] = mapped_column(index=True)
    action: Mapped[str] = mapped_column(index=True)
    target_type: Mapped[str]
    target_id: Mapped[str]
    details: Mapped[dict[str, Any]] = mapped_column(JSON)
    timestamp: Mapped[datetime] = mapped_column(default=utcnow, index=True)
