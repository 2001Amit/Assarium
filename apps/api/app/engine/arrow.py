from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any

import pyarrow as pa

from app.connectors.types import ColumnSchema
from app.core.errors import QueryError
from app.engine.base import safe_identifier

# Money is the reason this maps to decimal rather than double. A float64 cannot hold
# 0.1 exactly, and a platform whose revenue totals drift in the ninth decimal place is
# not one anybody should report from. 38 digits with 9 decimal places covers currency,
# rates and quantities without needing per-source precision metadata.
DECIMAL_PRECISION = 38
DECIMAL_SCALE = 9

LOGICAL_TO_ARROW: dict[str, pa.DataType] = {
    "string": pa.string(),
    "integer": pa.int64(),
    "float": pa.float64(),
    "decimal": pa.decimal128(DECIMAL_PRECISION, DECIMAL_SCALE),
    "boolean": pa.bool_(),
    "date": pa.date32(),
    "timestamp": pa.timestamp("us"),
    "json": pa.string(),
    "binary": pa.binary(),
    "unknown": pa.string(),
}

_QUANTUM = Decimal(1).scaleb(-DECIMAL_SCALE)
_TRUE = {"true", "t", "yes", "y", "1"}
_FALSE = {"false", "f", "no", "n", "0"}


def arrow_schema(columns: list[ColumnSchema]) -> pa.Schema:
    """Canonical Arrow schema for a dataset, keyed off its normalised logical types."""
    return pa.schema(
        [
            pa.field(
                safe_identifier(column.name),
                LOGICAL_TO_ARROW.get(column.logical_type, pa.string()),
            )
            for column in columns
        ]
    )


def rows_to_batch(
    rows: list[list[Any]], columns: list[ColumnSchema], schema: pa.Schema
) -> pa.RecordBatch:
    """Convert driver rows into one Arrow batch, coercing each column to its declared type."""
    arrays = []
    for position, column in enumerate(columns):
        target = schema.field(position).type
        values = [row[position] if position < len(row) else None for row in rows]
        arrays.append(_to_array(values, target, column.name))
    return pa.RecordBatch.from_arrays(arrays, schema=schema)


def _to_array(values: list[Any], target: pa.DataType, column_name: str) -> pa.Array:
    coerce = _COERCERS.get(str(target), _coerce_string)
    try:
        return pa.array([coerce(value) for value in values], type=target)
    except (pa.ArrowInvalid, ValueError, TypeError) as exc:
        raise QueryError(
            f"Column '{column_name}' holds a value that does not fit its declared type "
            f"({target}): {str(exc).splitlines()[0][:160]}"
        ) from exc


# --------------------------------------------------------------------------------------
# per-type coercion
#
# Drivers are inconsistent about what Python type they hand back for the same SQL type,
# so every value is normalised here rather than trusted.
# --------------------------------------------------------------------------------------


def _coerce_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else str(value)


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(Decimal(str(value).strip().replace(",", "")))
    except (InvalidOperation, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        return float(str(value).strip().replace(",", "").lstrip("$£€₹¥"))
    except ValueError:
        return None


def _coerce_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        text = str(value).strip().replace(",", "").lstrip("$£€₹¥").rstrip("%")
        return Decimal(text).quantize(_QUANTUM)
    except (InvalidOperation, ValueError):
        return None


def _coerce_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def _coerce_date(value: Any) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _coerce_timestamp(value: Any) -> dt.datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        # Arrow's timestamp("us") is timezone-naive here; normalise to UTC then drop the
        # offset so mixed-offset sources stay comparable.
        return value.astimezone(dt.UTC).replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(dt.UTC).replace(tzinfo=None) if parsed.tzinfo else parsed


def _coerce_binary(value: Any) -> bytes | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return str(value).encode()


_COERCERS = {
    "string": _coerce_string,
    "int64": _coerce_int,
    "double": _coerce_float,
    f"decimal128({DECIMAL_PRECISION}, {DECIMAL_SCALE})": _coerce_decimal,
    "bool": _coerce_bool,
    "date32[day]": _coerce_date,
    "timestamp[us]": _coerce_timestamp,
    "binary": _coerce_binary,
}
