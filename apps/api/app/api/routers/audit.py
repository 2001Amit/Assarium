from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.audit.service import get_audit_events
from app.auth.deps import get_scope, get_tenant_context
from app.tenancy.context import current
from app.tenancy.scope import TenantScope

# Every route below resolves a tenant before the handler runs. Declared once here
# rather than per endpoint, so a new endpoint is guarded by default instead of by
# whoever remembers. `assert_routes_are_guarded` at startup proves none slipped past.
router = APIRouter(
    prefix="/api/audit", tags=["audit"],
    dependencies=[Depends(get_tenant_context)],
)


class AuditEventResponse(BaseModel):
    id: str
    tenant_id: str
    actor_id: str
    action: str
    target_type: str
    target_id: str
    details: dict[str, Any]
    timestamp: datetime


@router.get("", response_model=list[AuditEventResponse])
def list_audit_events(
    limit: int = 100,
    scope: TenantScope = Depends(get_scope),
) -> list[AuditEventResponse]:
    """Retrieve the recent audit events for the active tenant."""
    tenant = current()
    return get_audit_events(scope.db, tenant_id=tenant.tenant_id, limit=limit)
