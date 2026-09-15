from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------------------
# Credential specification
#
# Every driver declares, declaratively, what it needs from a user. The frontend renders
# the connection dialog from this spec alone, so adding a source never means touching UI.
# --------------------------------------------------------------------------------------


class FieldType(StrEnum):
    TEXT = "text"
    PASSWORD = "password"
    TEXTAREA = "textarea"
    NUMBER = "number"
    SELECT = "select"
    BOOLEAN = "boolean"
    FILE = "file"


class FieldOption(BaseModel):
    value: str
    label: str


class SpecField(BaseModel):
    name: str
    label: str
    type: FieldType = FieldType.TEXT
    required: bool = True
    secret: bool = False
    placeholder: str | None = None
    help: str | None = None
    default: Any | None = None
    options: list[FieldOption] | None = None
    # Render only when another field in the same form holds one of these values.
    show_if: dict[str, list[str]] | None = None
    # Visual grouping inside the dialog.
    group: str = "Connection"


class AuthMethod(BaseModel):
    id: str
    label: str
    description: str
    fields: list[SpecField] = Field(default_factory=list)
    recommended: bool = False
    deprecated: bool = False
    notice: str | None = None


class SourceCategory(StrEnum):
    WAREHOUSE = "warehouse"
    DATABASE = "database"
    LAKEHOUSE = "lakehouse"
    OBJECT_STORE = "object_store"
    SAAS = "saas"
    FILE = "file"


class BrowseLevel(BaseModel):
    """Names one rung of a source's hierarchy so the browser UI can label itself."""

    key: str
    label: str
    plural: str


class Capabilities(BaseModel):
    sql: bool = False
    incremental: bool = False
    row_count_estimate: bool = False
    levels: list[BrowseLevel] = Field(default_factory=list)


class CredentialSpec(BaseModel):
    source_id: str
    name: str
    category: SourceCategory
    summary: str
    icon: str
    docs_url: str | None = None
    capabilities: Capabilities = Field(default_factory=Capabilities)
    auth_methods: list[AuthMethod] = Field(default_factory=list)
    # Fields required regardless of the chosen auth method.
    fields: list[SpecField] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# Browsing and schema
# --------------------------------------------------------------------------------------

NodeKind = Literal["container", "namespace", "dataset", "folder"]


class BrowseNode(BaseModel):
    """One entry in a source's hierarchy: a catalog, schema, table, folder or file."""

    id: str
    name: str
    kind: NodeKind
    # Fully-qualified path from the source root, e.g. ["sales_db", "public", "orders"].
    path: list[str]
    has_children: bool = False
    row_estimate: int | None = None
    size_bytes: int | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class ColumnSchema(BaseModel):
    name: str
    native_type: str
    # Normalised across sources so downstream logic never branches on dialect.
    logical_type: Literal[
        "string", "integer", "float", "decimal", "boolean", "date", "timestamp", "json", "binary",
        "unknown",
    ] = "unknown"
    nullable: bool = True
    position: int = 0
    primary_key: bool = False
    comment: str | None = None


class DatasetSchema(BaseModel):
    path: list[str]
    name: str
    columns: list[ColumnSchema]
    row_estimate: int | None = None
    comment: str | None = None


class SampleResult(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool = False


class ConnectionTestResult(BaseModel):
    ok: bool
    message: str
    latency_ms: int | None = None
    server_version: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
