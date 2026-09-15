from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.profiling.types import DatasetProfile

RuleKind = Literal["not_null", "type", "unique", "allowed_values", "range", "non_negative"]
Severity = Literal["reject", "warn"]


@dataclass
class Rule:
    """
    One check a row must pass.

    Every rule carries the reason it exists. A row lands in quarantine with the rule that
    caught it, so the person looking at the quarantine table can tell the difference
    between "the source sent rubbish" and "our rule is wrong" - which is the whole point
    of quarantining rather than dropping.
    """

    id: str
    kind: RuleKind
    column: str | None
    severity: Severity = "reject"
    params: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    #: True when the rule was proposed from profiling rather than authored by a person.
    generated: bool = True

    def label(self) -> str:
        return f"{self.kind}:{self.column}" if self.column else self.kind


@dataclass
class QualityPolicy:
    """The rules for one dataset, plus what to do when too many rows fail."""

    rules: list[Rule] = field(default_factory=list)

    #: Fail the whole load when more than this share of rows is rejected. A load that
    #: quietly publishes 40% of its rows is worse than one that stops and says so.
    max_reject_ratio: float = 0.05
    #: Below this many rows the ratio is meaningless, so only an absolute count applies.
    min_rows_for_ratio: int = 100

    def rejecting(self) -> list[Rule]:
        return [r for r in self.rules if r.severity == "reject"]

    def breached(self, total_rows: int, rejected_rows: int) -> str | None:
        """The reason the load should fail, or None if it is within tolerance."""
        if rejected_rows == 0:
            return None
        if total_rows < self.min_rows_for_ratio:
            return None
        ratio = rejected_rows / total_rows
        if ratio > self.max_reject_ratio:
            return (
                f"{rejected_rows:,} of {total_rows:,} rows ({ratio:.1%}) failed quality "
                f"rules, above the {self.max_reject_ratio:.0%} threshold. The load was "
                "stopped rather than publishing a partial table."
            )
        return None


def derive_policy(profile: DatasetProfile, key_columns: list[str] | None = None) -> QualityPolicy:
    """
    Propose rules from what profiling measured.

    Only rules the data itself supports are proposed. A column that is already 30% null
    does not get a not-null rule, because that would quarantine a third of the table on
    the first run and teach everyone to ignore quarantine.
    """
    rules: list[Rule] = []
    keys = set(key_columns or [])

    for column in profile.columns:
        name = column.name

        # A key must be present, or the merge downstream is meaningless. Note there is
        # no `continue` here: a key column can also be mistyped, and skipping its other
        # rules would let exactly that row through unchecked.
        if name in keys:
            rules.append(Rule(
                id=f"not_null_{name}", kind="not_null", column=name, severity="reject",
                reason=f"{name} identifies a row, so it cannot be empty.",
            ))

        # Effectively-complete columns: treat the rare gap as an error worth seeing.
        elif column.null_pct == 0 and profile.sampled_rows > 50:
            rules.append(Rule(
                id=f"not_null_{name}", kind="not_null", column=name, severity="warn",
                reason=(
                    f"{name} was complete in every sampled row, so a null is unexpected "
                    "and worth flagging - but not worth rejecting the row over."
                ),
            ))

        # A text column that holds numbers or dates gets a conversion rule. Failing the
        # conversion is exactly what TRY_CAST used to hide by writing NULL.
        if column.shadow_type:
            rules.append(Rule(
                id=f"type_{name}", kind="type", column=name, severity="reject",
                params={"to": column.shadow_type},
                reason=(
                    f"{name} is text that holds {column.shadow_type} values. A value that "
                    "will not convert is a data problem, not a missing value."
                ),
            ))

        # Small, stable domains: a new value is usually a source change worth noticing.
        if column.top_values and column.distinct_count and column.distinct_count <= 12:
            allowed = [t.value for t in column.top_values]
            if len(allowed) >= column.distinct_count:
                rules.append(Rule(
                    id=f"allowed_{name}", kind="allowed_values", column=name,
                    severity="warn", params={"values": allowed},
                    reason=(
                        f"{name} only ever held {len(allowed)} values. A new one is "
                        "probably a source change rather than a bad row."
                    ),
                ))

    return QualityPolicy(rules=rules)
