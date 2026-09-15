"""
Column masking for personal data.

Profiling has always detected PII. Nothing enforced it: a viewer could group by a
customer's name and read the names, and the platform's only response was a note on the
result saying it had noticed. Detected-but-unenforced is the worst position to be in for a
regulator - the finding proves you knew.

Two rules shape everything here.

**Mask before grouping, not after.** If the real column is grouped and only the label is
masked, the result still has one row per person: the row count is a headcount, the
ordering leaks who is largest, and a filter narrows it to an individual. Masking the
expression that is *both* selected and grouped means two people who mask alike collapse
into one row, which is what a mask is supposed to mean.

**A filter on a masked column is refused, not masked.** Equality on a hidden column is an
oracle - `WHERE email = 'someone@example.com'` returns rows or does not, and either answer
identifies the person without ever displaying them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.engine.base import Engine

#: The permission that lifts masking. Held by roles that are allowed to see people, and
#: by nobody else - see `app.tenancy.models.ROLE_PERMISSIONS`.
VIEW_PII = "pii:view"

#: How much of each kind survives masking.
#:
#: These are not uniform on purpose. An email keeps its domain because "how many of our
#: customers are at acme.com" is a legitimate question that needs no names. A national id
#: or a card number keeps nothing, because no analysis needs part of one and every partial
#: is a step towards the whole.
STRATEGIES: dict[str, str] = {
    "email": "domain",
    "phone": "last4",
    "national_id": "redact",
    "credit_card": "redact",
    "iban": "redact",
    "ip_address": "prefix",
    "postal_code": "prefix",
    "date_of_birth": "year",
    "person_name": "initials",
    "street_address": "redact",
    "coordinates": "redact",
}

DEFAULT_STRATEGY = "redact"
REDACTED = "'••• masked'"


def strategy_for(kind: str | None) -> str:
    return STRATEGIES.get(kind or "", DEFAULT_STRATEGY)


def mask_expression(engine: Engine, column_sql: str, kind: str | None) -> str:
    """
    Wrap a column expression so it cannot identify anybody.

    Built from SQL the engines share rather than a vendor function, because the same
    model has to compile identically against DuckDB in a test and Databricks in
    production - a mask that only exists on one of them is a mask that is missing exactly
    where it matters.

    NULL stays NULL throughout. Turning an absent value into a mask string would invent
    data, and "how many are missing" is a question people legitimately ask of a masked
    column.
    """
    text = engine.cast_text(column_sql)
    strategy = strategy_for(kind)

    if strategy == "redact":
        return f"CASE WHEN {column_sql} IS NULL THEN NULL ELSE {REDACTED} END"

    if strategy == "domain":
        # Everything from the @ onwards. A value with no @ is not the email it claimed to
        # be, so it is redacted rather than passed through unmasked.
        return (
            f"CASE WHEN {column_sql} IS NULL THEN NULL "
            f"WHEN POSITION('@' IN {text}) > 0 "
            f"THEN CONCAT('•••@', SUBSTRING({text} FROM POSITION('@' IN {text}) + 1)) "
            f"ELSE {REDACTED} END"
        )

    if strategy == "last4":
        return (
            f"CASE WHEN {column_sql} IS NULL THEN NULL "
            f"WHEN LENGTH({text}) >= 4 "
            f"THEN CONCAT('•••', SUBSTRING({text} FROM LENGTH({text}) - 3)) "
            f"ELSE {REDACTED} END"
        )

    if strategy == "initials":
        # First letter only. Enough to keep a sorted list readable, not enough to name
        # anybody in it.
        return (
            f"CASE WHEN {column_sql} IS NULL THEN NULL "
            f"WHEN LENGTH({text}) >= 1 "
            f"THEN CONCAT(SUBSTRING({text} FROM 1 FOR 1), '•••') "
            f"ELSE {REDACTED} END"
        )

    if strategy == "prefix":
        # Coarse enough to group by region without locating a household.
        return (
            f"CASE WHEN {column_sql} IS NULL THEN NULL "
            f"WHEN LENGTH({text}) >= 3 "
            f"THEN CONCAT(SUBSTRING({text} FROM 1 FOR 3), '•••') "
            f"ELSE {REDACTED} END"
        )

    if strategy == "year":
        # A birth year supports age cohorts; a birth date is an identifier.
        return (
            f"CASE WHEN {column_sql} IS NULL THEN NULL "
            f"ELSE CONCAT(SUBSTRING({text} FROM 1 FOR 4), '•••') END"
        )

    return f"CASE WHEN {column_sql} IS NULL THEN NULL ELSE {REDACTED} END"


def explain(kind: str | None) -> str:
    """What the reader is told, so a masked column is never mistaken for a broken one."""
    strategy = strategy_for(kind)
    described = {
        "domain": "only the email domain is shown",
        "last4": "only the last four characters are shown",
        "initials": "only the first letter is shown",
        "prefix": "only the leading characters are shown",
        "year": "only the year is shown",
        "redact": "the values are hidden entirely",
    }[strategy]
    return (
        f"This column holds personal data, so {described}. Ask an owner for the "
        "permission to see personal data if your work needs the full values."
    )
