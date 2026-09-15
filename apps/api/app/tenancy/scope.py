"""
The one way to read and write tenant-owned rows.

Before this existed, every router looked rows up with `db.get(Model, id)` - an id and
nothing else - so anybody holding an id could reach any customer's row. The fix is not to
sprinkle `.where(tenant_id == ...)` across forty call sites and hope none is forgotten. It
is to make the unscoped call impossible to write by accident, and to have one place where
the rule is stated.

Two properties matter here and are tested:

1. **A row belonging to another tenant is `404`, never `403`.** A 403 confirms the row
   exists, which turns any id field into an oracle for enumerating other customers'
   objects. 404 says the same thing as a genuinely absent row, which is exactly right.

2. **`create()` fills in `tenant_id` itself.** A caller cannot forget it, and cannot
   override it - passing one is an error rather than a silent reassignment.
"""

from __future__ import annotations

from typing import Any, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.entities import TenantOwned
from app.tenancy.context import TenantContext

T = TypeVar("T", bound=TenantOwned)

#: What a missing row and another tenant's row both say. Identical on purpose.
NOT_FOUND = "That {label} does not exist."


def _label(model: type) -> str:
    """`DatasetRelationship` -> "dataset relationship", for a message a person can read."""
    name = model.__name__.removesuffix("Record")
    out: list[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index:
            out.append(" ")
        out.append(char.lower())
    return "".join(out)


class TenantScope:
    """
    A database session bound to one tenant.

    Every method is scoped. There is no escape hatch, because an escape hatch is what
    gets used at 6pm on a Friday.
    """

    def __init__(self, db: Session, context: TenantContext):
        if isinstance(db, TenantScope):
            # Passing a scope where a session belongs is harmless. The reverse is not,
            # and is caught below.
            db = db.db
        self.db = db
        self.context = context

    @property
    def tenant_id(self) -> str:
        return self.context.tenant_id

    # -- reading -----------------------------------------------------------------------

    def select(self, model: type[T]) -> Select:
        """A SELECT already filtered to this tenant. Build further conditions onto it."""
        return select(model).where(model.tenant_id == self.tenant_id)

    @staticmethod
    def assert_is_scope(candidate: object, where: str) -> None:
        """
        Refuse a Session standing in for a scope.

        `Session.get(Model, id)` and `TenantScope.get(Model, id)` have the same
        signature, so passing the session by mistake runs happily and looks up the row
        without a tenant filter. It has happened three times during this refactor and
        was invisible every time, because the code reads correctly. A type check at the
        boundary is cheaper than a fourth cross-tenant read.
        """
        if not isinstance(candidate, TenantScope):
            raise TypeError(
                f"{where} needs a TenantScope, got {type(candidate).__name__}. A Session "
                "has the same `.get(Model, id)` signature, so it would silently do an "
                "unscoped lookup."
            )

    def get(self, model: type[T], row_id: str | None) -> T:
        """
        One row by id, or `404`.

        The tenant condition is part of the query rather than a check afterwards. Fetching
        first and comparing second means the row is in memory before the decision, which
        is how a stray log line or an early `return` leaks it.
        """
        row = None
        if row_id:
            row = self.db.execute(
                self.select(model).where(model.id == row_id)
            ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(NOT_FOUND.format(label=_label(model)))
        return row

    def find(self, model: type[T], row_id: str | None) -> T | None:
        """Like `get`, but absence is an expected answer rather than an error."""
        if not row_id:
            return None
        return self.db.execute(
            self.select(model).where(model.id == row_id)
        ).scalar_one_or_none()

    def all(self, model: type[T], *conditions: Any) -> list[T]:
        query = self.select(model)
        for condition in conditions:
            query = query.where(condition)
        return list(self.db.execute(query).scalars().all())

    def one_where(self, model: type[T], *conditions: Any) -> T | None:
        query = self.select(model)
        for condition in conditions:
            query = query.where(condition)
        return self.db.execute(query).scalars().first()

    def count(self, model: type[T], *conditions: Any) -> int:
        from sqlalchemy import func

        query = select(func.count()).select_from(model).where(
            model.tenant_id == self.tenant_id
        )
        for condition in conditions:
            query = query.where(condition)
        return int(self.db.execute(query).scalar_one())

    # -- writing -----------------------------------------------------------------------

    def create(self, model: type[T], **values: Any) -> T:
        """
        Build a row already owned by this tenant.

        Passing `tenant_id` is refused rather than ignored. A caller that thinks it knows
        the tenant has misunderstood where tenancy comes from, and quietly overwriting
        their value would hide that.
        """
        if "tenant_id" in values:
            raise ValueError(
                "tenant_id is set by TenantScope, not by the caller. It comes from the "
                "validated session, never from anything the request supplied."
            )
        row = model(tenant_id=self.tenant_id, **values)
        self.db.add(row)
        return row

    def delete(self, row: TenantOwned) -> None:
        """
        Remove a row, refusing anything that is not ours.

        Rows normally arrive here from `get()` and are already scoped. The check costs
        nothing and closes the case where one was fetched some other way.
        """
        if row.tenant_id != self.tenant_id:
            raise NotFoundError(NOT_FOUND.format(label=_label(type(row))))
        self.db.delete(row)

    # -- convenience -------------------------------------------------------------------

    def qualified(self, layer: str, table: str) -> str:
        return self.context.qualified(layer, table)

    def require(self, permission: str) -> None:
        self.context.require(permission)
