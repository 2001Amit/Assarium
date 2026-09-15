from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field

from app.core.errors import AssariumError


class TenantResolutionError(AssariumError):
    """The caller could not be placed in a tenant, so no data may be reached."""

    status_code = 403
    code = "tenant_not_resolved"


class PermissionDenied(AssariumError):
    status_code = 403
    code = "permission_denied"


@dataclass(frozen=True)
class TenantContext:
    """
    Who is asking, and which tenant they are asking as.

    Built once per request from a validated token and never from request input. Frozen
    so no downstream code can widen its own access by mutating it.
    """

    tenant_id: str
    slug: str
    name: str
    catalog: str
    storage_container: str

    subject: str
    email: str | None
    role: str
    permissions: frozenset[str] = field(default_factory=frozenset)

    #: True when the caller is a service principal running a scheduled job rather than
    #: a person. Used by the audit log, and to allow job-only operations.
    is_service: bool = False

    def require(self, permission: str) -> None:
        if permission not in self.permissions:
            raise PermissionDenied(
                f"Your role ({self.role}) cannot {permission.replace(':', ' ')}."
            )

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def qualified(self, layer: str, table: str) -> str:
        """Three-part name inside this tenant's own catalog. The only way to name a table."""
        return f"{self.catalog}.{layer}.{table}"


# Request-scoped, so background code reached from a request inherits the same tenant
# rather than having to thread it through every call signature.
_current: ContextVar[TenantContext | None] = ContextVar("assarium_tenant", default=None)


def set_current(context: TenantContext | None):
    return _current.set(context)


def reset_current(token) -> None:
    _current.reset(token)


def current() -> TenantContext:
    """
    The tenant for this request.

    Raises rather than returning None: a caller that forgot to resolve a tenant must
    fail closed, never fall through to unscoped data.
    """
    context = _current.get()
    if context is None:
        raise TenantResolutionError(
            "No tenant is in scope for this request. This is a bug: every data path "
            "must run inside a resolved tenant context."
        )
    return context


def current_or_none() -> TenantContext | None:
    """For logging and metrics, where absence is legitimate."""
    return _current.get()
