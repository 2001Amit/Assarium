from __future__ import annotations

import re
from collections.abc import Sequence

INT_RE = re.compile(r"^[+-]?\d{1,18}$")
FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
# Only unambiguous, sortable date shapes. Ambiguous forms such as 03/09/2026 are
# deliberately not treated as dates, because guessing day-vs-month order silently
# corrupts every downstream time series.
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")
BOOL_VALUES = {"true", "false", "t", "f", "yes", "no", "y", "n"}
CURRENCY_RE = re.compile(r"^[+-]?[$£€₹¥]\s?[\d,]+(\.\d+)?$|^[+-]?[\d,]+(\.\d+)?\s?%$")


def infer_shadow_type(values: Sequence[object]) -> tuple[str | None, float]:
    """
    Detect a text column that is really a number, date or boolean.

    This is the single strongest indicator that a table has not been through a
    silver-layer type conversion, so it feeds directly into layer classification.
    """
    strings = [str(v).strip() for v in values if v is not None and str(v).strip() != ""]
    if len(strings) < 8:
        return None, 0.0

    total = len(strings)
    sample = strings[:5000]
    size = len(sample)

    lowered = [s.lower() for s in sample]
    if all(s in BOOL_VALUES for s in lowered) and len(set(lowered)) <= 2:
        return "boolean", 1.0

    # A code with leading zeros is an identifier, not a number: "007" != 7.
    padded = sum(1 for s in sample if len(s) > 1 and s[0] == "0" and s[1] != ".")
    if padded / size > 0.05:
        return None, 0.0

    int_ratio = sum(1 for s in sample if INT_RE.match(s)) / size
    if int_ratio >= 0.95:
        return "integer", int_ratio

    float_ratio = sum(1 for s in sample if FLOAT_RE.match(s.replace(",", ""))) / size
    if float_ratio >= 0.95:
        return "float", float_ratio

    currency_ratio = sum(1 for s in sample if CURRENCY_RE.match(s)) / size
    if currency_ratio >= 0.9:
        return "decimal", currency_ratio

    date_ratio = sum(1 for s in sample if DATE_RE.match(s)) / size
    if date_ratio >= 0.9:
        has_time = any("T" in s or " " in s.strip() for s in sample[:200] if DATE_RE.match(s))
        return ("timestamp" if has_time else "date"), date_ratio

    _ = total
    return None, 0.0
