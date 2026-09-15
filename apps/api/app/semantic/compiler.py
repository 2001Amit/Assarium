from __future__ import annotations

from collections import deque
from typing import Any

from app.core.errors import AssariumError
from app.engine.base import Engine
from app.semantic.masking import VIEW_PII, explain, mask_expression
from app.semantic.types import (
    Attribute,
    CompiledQuery,
    Entity,
    Filter,
    Join,
    Measure,
    MetricQuery,
    SemanticModel,
)


class SemanticError(AssariumError):
    """
    The question cannot be answered from the model as it stands.

    This is the class the whole semantic layer exists to produce. A text-to-SQL system
    fails by returning a plausible number that is wrong; this fails by saying what is
    missing, which is the difference between a wrong board deck and a fixable one.
    """

    status_code = 422
    code = "semantic_error"


# Operators, and how many values each needs. Anything not listed cannot be compiled.
OPERATORS: dict[str, tuple[str, int | None]] = {
    "eq": ("=", 1),
    "ne": ("<>", 1),
    "gt": (">", 1),
    "gte": (">=", 1),
    "lt": ("<", 1),
    "lte": ("<=", 1),
    "in": ("IN", None),
    "not_in": ("NOT IN", None),
    "contains": ("LIKE", 1),
    "starts_with": ("LIKE", 1),
    "between": ("BETWEEN", 2),
    "is_null": ("IS NULL", 0),
    "is_not_null": ("IS NOT NULL", 0),
}

AGGREGATIONS = {
    "sum": "SUM({expr})",
    "avg": "AVG({expr})",
    "min": "MIN({expr})",
    "max": "MAX({expr})",
    "count": "COUNT({expr})",
    "count_distinct": "COUNT(DISTINCT {expr})",
    "median": "MEDIAN({expr})",
}

MAX_LIMIT = 50_000


class QueryCompiler:
    """Turns a MetricQuery into SQL, or refuses with a reason."""

    def __init__(
        self,
        model: SemanticModel,
        engine: Engine,
        permissions: frozenset[str] | set[str] | None = None,
    ):
        self.model = model
        self.engine = engine
        self._aliases: dict[str, str] = {}
        # What the caller may see. `None` means "not supplied", which is treated as the
        # least privilege rather than the most: a call site that forgot to pass
        # permissions gets masked data, not unmasked. Failing closed is the only safe
        # default for a parameter whose absence is invisible.
        self.permissions: frozenset[str] = frozenset(permissions or ())

    @property
    def may_see_pii(self) -> bool:
        return VIEW_PII in self.permissions

    # -- entry point ------------------------------------------------------------------

    def compile(self, query: MetricQuery) -> CompiledQuery:
        if not query.measures:
            raise SemanticError("Ask for at least one measure.")

        measures = [self._measure(measure_id) for measure_id in query.measures]
        base = self._base_entity(measures)
        # Claim the first alias for the base table so the generated SQL reads in the
        # order it executes. Users are shown this SQL; it should not look scrambled.
        self._alias(base)

        dimension_refs = [self.resolve_field(ref) for ref in query.dimensions]
        time_ref = self.resolve_field(query.time_dimension) if query.time_dimension else None

        needed = {base.id}
        needed.update(m.entity_id for m in measures)
        needed.update(entity.id for entity, _ in dimension_refs)
        if time_ref:
            needed.add(time_ref[0].id)
        for filter_ in query.filters:
            entity, _ = self.resolve_field(filter_.field)
            needed.add(entity.id)

        joins = self._join_path(base, needed - {base.id})

        params: list[Any] = []
        select: list[str] = []
        group_by: list[str] = []
        dimension_columns: list[str] = []
        measure_columns: list[str] = []
        notes: list[str] = []

        if time_ref:
            entity, attribute = time_ref
            grain = query.time_grain or "month"
            expression = self.engine.date_trunc(grain, self._column(entity, attribute))
            alias = f"{grain}_of_{attribute.name}"
            select.append(f"{expression} AS {self.engine.quote(alias)}")
            group_by.append(expression)
            dimension_columns.append(alias)

        for entity, attribute in dimension_refs:
            expression = self._column(entity, attribute)
            alias = self._dimension_alias(entity, attribute)
            select.append(f"{expression} AS {self.engine.quote(alias)}")
            group_by.append(expression)
            dimension_columns.append(alias)
            if attribute.contains_pii and not self.may_see_pii:
                notes.append(f"{attribute.label}: {explain(attribute.pii_kind)}")

        for measure in measures:
            select.append(f"{self._aggregate(measure)} AS {self.engine.quote(measure.name)}")
            measure_columns.append(measure.name)

        where, where_params = self._where(query.filters)
        params.extend(where_params)

        sql = f"SELECT {', '.join(select)}\nFROM {self._table(base)} AS {self._alias(base)}"
        for join in joins:
            sql += "\n" + self._join_clause(join)
        if where:
            sql += f"\nWHERE {where}"
        if group_by:
            sql += f"\nGROUP BY {', '.join(str(i + 1) for i in range(len(group_by)))}"

        order = self._order_by(query, dimension_columns, measure_columns)
        if order:
            sql += f"\nORDER BY {order}"

        limit = max(1, min(query.limit, MAX_LIMIT))
        sql += f"\nLIMIT {limit}"

        return CompiledQuery(
            sql=sql,
            params=params,
            dimension_columns=dimension_columns,
            measure_columns=measure_columns,
            base_entity=base.id,
            joined_entities=[j.to_entity for j in joins],
            notes=notes,
        )

    # -- resolution -------------------------------------------------------------------

    def _measure(self, measure_id: str) -> Measure:
        measure = self.model.measure(measure_id)
        if measure is None:
            available = ", ".join(sorted(m.id for m in self.model.measures)[:12]) or "none"
            raise SemanticError(
                f"There is no measure called '{measure_id}'. Defined measures: {available}.",
                details={"measure": measure_id},
            )
        if measure.aggregation == "ratio":
            raise SemanticError(
                f"'{measure.label}' is a ratio measure, which is not supported yet.",
                details={"measure": measure_id},
            )
        return measure

    def resolve_field(self, reference: str) -> tuple[Entity, Attribute]:
        """Resolve an "entity.attribute" reference, or explain why it cannot be."""
        if "." not in reference:
            raise SemanticError(
                f"'{reference}' is not a valid field. Use the form entity.attribute."
            )
        entity_id, _, attribute_name = reference.partition(".")
        entity = self.model.entity(entity_id)
        if entity is None:
            available = ", ".join(sorted(e.id for e in self.model.entities)[:12])
            raise SemanticError(
                f"There is no entity called '{entity_id}'. Known entities: {available}.",
                details={"entity": entity_id},
            )
        attribute = entity.attribute(attribute_name)
        if attribute is None:
            available = ", ".join(a.name for a in entity.attributes if not a.hidden)[:400]
            raise SemanticError(
                f"'{entity.label}' has no attribute '{attribute_name}'. Available: {available}.",
                details={"entity": entity_id, "attribute": attribute_name},
            )
        return entity, attribute

    def _base_entity(self, measures: list[Measure]) -> Entity:
        entity_ids = {m.entity_id for m in measures}
        if len(entity_ids) > 1:
            labels = ", ".join(sorted(entity_ids))
            raise SemanticError(
                "Those measures live on different tables and cannot be combined in one "
                f"query without double-counting ({labels}). Ask for them separately.",
                details={"entities": sorted(entity_ids)},
            )
        entity = self.model.entity(next(iter(entity_ids)))
        if entity is None:  # pragma: no cover - measures always carry a real entity
            raise SemanticError("The measure refers to an entity that no longer exists.")
        return entity

    # -- joins ------------------------------------------------------------------------

    def _join_path(self, base: Entity, targets: set[str]) -> list[Join]:
        """
        Find a safe path from the base entity to everything else the query touches.

        Every hop must go from the many side to the one side. Travelling the other way
        multiplies the fact rows, so the measures would silently over-count - the exact
        failure a semantic layer exists to prevent.
        """
        if not targets:
            return []

        # Directed adjacency: from a child entity to its parent is always safe.
        outgoing: dict[str, list[Join]] = {}
        for join in self.model.joins:
            outgoing.setdefault(join.from_entity, []).append(join)

        found: dict[str, list[Join]] = {base.id: []}
        queue: deque[str] = deque([base.id])
        while queue:
            current = queue.popleft()
            for join in outgoing.get(current, []):
                if join.to_entity in found:
                    continue
                if join.cardinality == "one_to_many":
                    continue
                found[join.to_entity] = [*found[current], join]
                queue.append(join.to_entity)

        unreachable = targets - found.keys()
        if unreachable:
            names = ", ".join(
                (self.model.entity(e).label if self.model.entity(e) else e)
                for e in sorted(unreachable)
            )
            raise SemanticError(
                f"{base.label} has no safe join path to {names}. Joining them would repeat "
                f"{base.label} rows and inflate the totals. Define the relationship, or "
                "query those entities separately.",
                details={"base": base.id, "unreachable": sorted(unreachable)},
            )

        # De-duplicate while keeping traversal order stable.
        ordered: list[Join] = []
        seen: set[str] = set()
        for target in sorted(targets):
            for join in found[target]:
                if join.id not in seen:
                    seen.add(join.id)
                    ordered.append(join)
        return ordered

    def _join_clause(self, join: Join) -> str:
        left = self.model.entity(join.from_entity)
        right = self.model.entity(join.to_entity)
        if left is None or right is None:  # pragma: no cover
            raise SemanticError("This model has a join pointing at a missing entity.")
        return (
            f"LEFT JOIN {self._table(right)} AS {self._alias(right)} "
            f"ON {self._alias(left)}.{self.engine.quote(join.from_column)} "
            f"= {self._alias(right)}.{self.engine.quote(join.to_column)}"
        )

    # -- fragments --------------------------------------------------------------------

    def _table(self, entity: Entity) -> str:
        return self.engine.qualified(entity.layer, entity.table)

    def _alias(self, entity: Entity) -> str:
        if entity.id not in self._aliases:
            self._aliases[entity.id] = f"e{len(self._aliases)}"
        return self._aliases[entity.id]

    def _column(self, entity: Entity, attribute: Attribute) -> str:
        """
        The SQL expression for one attribute, masked when the caller may not see it.

        Every path that names a column goes through here - select list, group by, filters,
        order by - which is the point. A mask applied in only some of them is not a mask:
        grouping on the real value and displaying the masked one still yields one row per
        person, and the ordering says which of them is largest.
        """
        raw = f"{self._alias(entity)}.{self.engine.quote(attribute.column)}"
        if attribute.contains_pii and not self.may_see_pii:
            return mask_expression(self.engine, raw, attribute.pii_kind)
        return raw

    def _dimension_alias(self, entity: Entity, attribute: Attribute) -> str:
        # Prefix with the entity when the same attribute name appears on more than one,
        # so two joined tables with a `name` column do not collide in the result.
        clashes = sum(
            1 for e in self.model.entities if e.attribute(attribute.name) is not None
        )
        return f"{entity.name}_{attribute.name}" if clashes > 1 else attribute.name

    def _aggregate(self, measure: Measure) -> str:
        entity = self.model.entity(measure.entity_id)
        if entity is None:  # pragma: no cover
            raise SemanticError(f"'{measure.label}' points at an entity that no longer exists.")

        template = AGGREGATIONS.get(measure.aggregation)
        if template is None:
            raise SemanticError(f"'{measure.aggregation}' is not a supported aggregation.")

        if measure.aggregation == "count" and not measure.column:
            expression = "*"
        else:
            attribute = entity.attribute(measure.column or "")
            if attribute is None:
                raise SemanticError(
                    f"'{measure.label}' is defined over column '{measure.column}', which is "
                    f"not part of {entity.label} any more."
                )
            expression = self._column(entity, attribute)

        aggregate = template.format(expr=expression)
        if measure.constraint:
            # The constraint is authored in the model, never supplied by a caller.
            aggregate = template.format(
                expr=f"CASE WHEN {measure.constraint} THEN {expression} END"
            )
        return aggregate

    def _where(self, filters: list[Filter]) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []

        for filter_ in filters:
            entity, attribute = self.resolve_field(filter_.field)
            if attribute.contains_pii and not self.may_see_pii:
                # Refused rather than masked. Equality on a hidden column is an oracle:
                # `WHERE email = 'someone@example.com'` returns rows or it does not, and
                # either answer identifies the person without ever displaying them.
                raise SemanticError(
                    f"{attribute.label} holds personal data, so it cannot be filtered on. "
                    "Filtering would let somebody confirm whether a specific person is in "
                    "the data without ever seeing the column."
                )
            column = self._column(entity, attribute)
            operator = OPERATORS.get(filter_.operator)
            if operator is None:
                raise SemanticError(f"'{filter_.operator}' is not a supported filter.")
            symbol, arity = operator

            if arity == 0:
                clauses.append(f"{column} {symbol}")
                continue

            if arity is not None and len(filter_.values) != arity:
                raise SemanticError(
                    f"The '{filter_.operator}' filter on {attribute.label} needs "
                    f"{arity} value(s), got {len(filter_.values)}."
                )

            if filter_.operator in {"in", "not_in"}:
                if not filter_.values:
                    raise SemanticError(f"The filter on {attribute.label} lists no values.")
                placeholders = ", ".join(["?"] * len(filter_.values))
                clauses.append(f"{column} {symbol} ({placeholders})")
                params.extend(filter_.values)
            elif filter_.operator == "between":
                clauses.append(f"{column} BETWEEN ? AND ?")
                params.extend(filter_.values)
            elif filter_.operator == "contains":
                clauses.append(f"{column} LIKE ?")
                params.append(f"%{filter_.values[0]}%")
            elif filter_.operator == "starts_with":
                clauses.append(f"{column} LIKE ?")
                params.append(f"{filter_.values[0]}%")
            else:
                clauses.append(f"{column} {symbol} ?")
                params.append(filter_.values[0])

        return " AND ".join(clauses), params

    def _order_by(
        self, query: MetricQuery, dimensions: list[str], measures: list[str]
    ) -> str:
        known = {*dimensions, *measures}
        clauses = []
        for order in query.order_by:
            field = order.field
            if field not in known:
                # Accept a measure id as well as its output column name.
                measure = self.model.measure(field)
                field = measure.name if measure and measure.name in known else field
            if field not in known:
                raise SemanticError(
                    f"Cannot order by '{order.field}': it is not one of the columns this "
                    f"query returns ({', '.join(sorted(known))})."
                )
            clauses.append(f"{self.engine.quote(field)} {order.direction.upper()}")

        if not clauses and measures:
            # A sensible default: biggest first, which is what someone almost always means.
            clauses.append(f"{self.engine.quote(measures[0])} DESC")
        return ", ".join(clauses)
