from __future__ import annotations

from dataclasses import dataclass

from app.engine.base import Engine
from app.quality.rules import QualityPolicy, Rule

# Columns the quality step adds to a quarantined row so it can be diagnosed and replayed.
ERROR_COLUMN = "_dq_error"
RULE_COLUMN = "_dq_rule"
RUN_COLUMN = "_dq_run_id"
QUARANTINED_AT = "_dq_quarantined_at"


@dataclass
class QualitySql:
    """The SQL that evaluates a policy and splits clean rows from failures."""

    evaluated: str
    clean: str
    quarantine: str
    #: Counts per warn rule. None when the policy has no warn rules.
    warning_summary: str | None
    rejecting_rules: list[Rule]
    warning_rules: list[Rule]


def _literal(value: object) -> str:
    """
    Render a rule parameter as a SQL literal.

    Rule values come from profiling the source's own data, so they can contain quotes.
    Everything is escaped here; nothing is interpolated raw.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _predicate(engine: Engine, rule: Rule) -> str | None:
    """A boolean expression that is TRUE when the row *fails* this rule."""
    if rule.column is None:
        return None
    column = engine.quote(rule.column)

    if rule.kind == "not_null":
        return f"{column} IS NULL"

    if rule.kind == "type":
        target = rule.params.get("to", "string")
        # The rule must be no stricter than the transform that follows it. Silver strips
        # currency symbols and thousands separators before casting, so "1,850.00" is a
        # value the pipeline can handle - quarantining it would be a false positive, and
        # a quarantine full of rows that were actually fine is one nobody reads.
        candidate = (
            engine.strip_non_numeric(column)
            if target in {"integer", "float", "decimal"}
            else column
        )
        # A value that is present but will not convert is the failure. A null is a
        # separate concern handled by not_null, so it must not be caught twice.
        return (
            f"({column} IS NOT NULL AND TRIM(CAST({column} AS VARCHAR)) <> '' "
            f"AND {engine.try_cast(candidate, target)} IS NULL)"
        )

    if rule.kind == "allowed_values":
        values = rule.params.get("values") or []
        if not values:
            return None
        rendered = ", ".join(_literal(v) for v in values)
        return f"({column} IS NOT NULL AND {column} NOT IN ({rendered}))"

    if rule.kind == "non_negative":
        return f"({column} IS NOT NULL AND {column} < 0)"

    if rule.kind == "range":
        low, high = rule.params.get("min"), rule.params.get("max")
        bounds = []
        if low is not None:
            bounds.append(f"{column} < {_literal(low)}")
        if high is not None:
            bounds.append(f"{column} > {_literal(high)}")
        return f"({column} IS NOT NULL AND ({' OR '.join(bounds)}))" if bounds else None

    # `unique` cannot be expressed per row; it is checked by the merge key instead.
    return None


def build(
    engine: Engine,
    source_sql: str,
    policy: QualityPolicy,
    run_id: str,
    projection: str = "*",
) -> QualitySql:
    """
    Wrap a silver projection in rule evaluation and split it in two.

    Rows are labelled with *every* rule they broke, not just the first. A row that fails
    three checks is usually one underlying problem, and seeing all three is how you find
    it.
    """
    rejecting = policy.rejecting()
    warnings = [r for r in policy.rules if r.severity == "warn"]

    reject_cases: list[str] = []
    for rule in rejecting:
        predicate = _predicate(engine, rule)
        if predicate:
            reject_cases.append(
                f"CASE WHEN {predicate} THEN {_literal(rule.label())} END"
            )

    warn_cases: list[str] = []
    for rule in warnings:
        predicate = _predicate(engine, rule)
        if predicate:
            warn_cases.append(f"CASE WHEN {predicate} THEN {_literal(rule.label())} END")

    reject_expr = (
        f"CONCAT_WS('; ', {', '.join(reject_cases)})" if reject_cases else "''"
    )
    warn_expr = f"CONCAT_WS('; ', {', '.join(warn_cases)})" if warn_cases else "''"

    evaluated = (
        f"SELECT {projection},\n"
        f"       {reject_expr} AS {engine.quote(RULE_COLUMN)},\n"
        f"       {warn_expr} AS _dq_warning\n"
        f"FROM ({source_sql}) AS _dq_source"
    )

    rule_col = engine.quote(RULE_COLUMN)
    clean = (
        f"SELECT * EXCLUDE ({rule_col}, _dq_warning)\n"
        f"FROM ({evaluated}) AS _dq_evaluated\n"
        f"WHERE {rule_col} = ''"
    )
    quarantine = (
        f"SELECT *,\n"
        f"       {_literal(run_id)} AS {engine.quote(RUN_COLUMN)},\n"
        f"       {engine.current_timestamp()} AS {engine.quote(QUARANTINED_AT)}\n"
        f"FROM ({evaluated}) AS _dq_evaluated\n"
        f"WHERE {rule_col} <> ''"
    )

    # A warn rule that is evaluated and then thrown away is no rule at all. The clean
    # table stays uncluttered, but the counts are reported on the run so somebody sees
    # them.
    warning_summary = (
        f"SELECT _dq_warning AS rule, count(*) AS rows\n"
        f"FROM ({evaluated}) AS _dq_evaluated\n"
        f"WHERE _dq_warning <> ''\n"
        f"GROUP BY 1 ORDER BY 2 DESC"
    ) if warn_cases else None

    return QualitySql(
        evaluated=evaluated,
        clean=clean,
        quarantine=quarantine,
        warning_summary=warning_summary,
        rejecting_rules=rejecting,
        warning_rules=warnings,
    )
