from __future__ import annotations

import re

LogicalType = str

# Ordered longest-prefix-wins so "timestamp_ntz" beats "time".
_PATTERNS: list[tuple[str, LogicalType]] = [
    (r"^(bool|bit)", "boolean"),
    (r"^(timestamp|datetime|datetimeoffset|smalldatetime)", "timestamp"),
    (r"^date$", "date"),
    (r"^time", "string"),
    (r"^(numeric|decimal|money|smallmoney|number)", "decimal"),
    (r"^(float|double|real|binary_float|binary_double)", "float"),
    (r"^(tinyint|smallint|mediumint|integer|int|bigint|serial|bigserial|long)", "integer"),
    (r"^(json|jsonb|variant|object|struct|map|array|super)", "json"),
    (r"^(bytea|blob|varbinary|binary|image)", "binary"),
    (r"^(char|varchar|nchar|nvarchar|text|string|clob|uuid|uniqueidentifier|enum|xml)", "string"),
]


def to_logical_type(native: str | None) -> LogicalType:
    """Collapse a dialect-specific type name to the small set the platform reasons about."""
    if not native:
        return "unknown"
    name = native.strip().lower()
    # Drop precision/scale and any array suffix before matching.
    name = re.sub(r"\(.*\)$", "", name).strip()
    name = re.sub(r"\s+(unsigned|signed|zerofill)$", "", name).strip()
    if name.endswith("[]"):
        return "json"
    for pattern, logical in _PATTERNS:
        if re.match(pattern, name):
            return logical
    return "unknown"


NUMERIC_TYPES = {"integer", "float", "decimal"}
TEMPORAL_TYPES = {"date", "timestamp"}
