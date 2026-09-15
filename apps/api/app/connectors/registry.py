from __future__ import annotations

from typing import Any

from app.connectors.base import Connector
from app.connectors.drivers.adls import ADLSConnector
from app.connectors.drivers.bigquery import BigQueryConnector
from app.connectors.drivers.databricks import DatabricksConnector
from app.connectors.drivers.local_files import LocalFileConnector
from app.connectors.drivers.mssql import SQLServerConnector
from app.connectors.drivers.mysql import MySQLConnector
from app.connectors.drivers.postgres import PostgresConnector
from app.connectors.drivers.s3 import S3Connector
from app.connectors.drivers.salesforce import SalesforceConnector
from app.connectors.drivers.sharepoint import SharePointConnector
from app.connectors.drivers.snowflake import SnowflakeConnector
from app.connectors.types import CredentialSpec, FieldType
from app.core.errors import NotFoundError, ValidationError

# Display order in the source picker: the sources people reach for first, first.
DRIVERS: list[type[Connector]] = [
    PostgresConnector,
    SnowflakeConnector,
    DatabricksConnector,
    SQLServerConnector,
    MySQLConnector,
    BigQueryConnector,
    ADLSConnector,
    S3Connector,
    SharePointConnector,
    SalesforceConnector,
    LocalFileConnector,
]

_BY_ID: dict[str, type[Connector]] = {d.spec.source_id: d for d in DRIVERS}


def list_specs() -> list[dict[str, Any]]:
    """Every source the platform offers, with a note on driver availability."""
    out = []
    for driver in DRIVERS:
        spec = driver.spec.model_dump(mode="json")
        spec["available"] = driver.is_available()
        spec["install_hint"] = (
            None
            if driver.is_available()
            else f"./.venv/bin/pip install {driver.requires[1]}"  # type: ignore[index]
        )
        out.append(spec)
    return out


def get_spec(source_id: str) -> CredentialSpec:
    return get_driver(source_id).spec


def get_driver(source_id: str) -> type[Connector]:
    driver = _BY_ID.get(source_id)
    if driver is None:
        raise NotFoundError(f"Unknown data source '{source_id}'.")
    return driver


def split_payload(source_id: str, auth_method: str, values: dict[str, Any]) -> tuple[dict, dict]:
    """
    Validate a submitted connection form against its spec and split it into the
    non-secret config that is stored in the clear and the secrets that are encrypted.
    """
    spec = get_spec(source_id)
    method = next((m for m in spec.auth_methods if m.id == auth_method), None)
    if spec.auth_methods and method is None:
        raise ValidationError(
            f"'{auth_method}' is not a valid authentication method for {spec.name}."
        )

    applicable = list(spec.fields) + list(method.fields if method else [])
    config: dict[str, Any] = {"auth_method": auth_method} if spec.auth_methods else {}
    secrets: dict[str, Any] = {}
    missing: list[str] = []

    for field in applicable:
        if not _visible(field, values):
            continue
        raw = values.get(field.name, field.default)
        if raw in (None, ""):
            if field.required:
                missing.append(field.label)
            continue
        value = _coerce(field.type, raw)
        (secrets if field.secret else config)[field.name] = value

    if missing:
        label = "fields: " if len(missing) > 1 else "field: "
        raise ValidationError(
            "Missing required " + label + ", ".join(missing),
            details={"fields": missing},
        )
    return config, secrets


def _visible(field: Any, values: dict[str, Any]) -> bool:
    if not field.show_if:
        return True
    return all(str(values.get(key)) in allowed for key, allowed in field.show_if.items())


def _coerce(field_type: FieldType, raw: Any) -> Any:
    if field_type == FieldType.NUMBER:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return float(raw)
    if field_type == FieldType.BOOLEAN:
        return raw if isinstance(raw, bool) else str(raw).lower() in {"true", "1", "yes", "on"}
    return raw.strip() if isinstance(raw, str) else raw


def build(source_id: str, config: dict[str, Any], secrets: dict[str, Any]) -> Connector:
    return get_driver(source_id)(config, secrets)
