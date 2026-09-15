"""
Automated per-tenant Unity Catalog provisioning.

The Assarium multi-tenant model uses one catalog per tenant:
  assarium_{env}_{tenant_slug}
    - bronze
    - silver
    - gold

This isolates data physically and logically. It prevents any possibility of
cross-tenant leakage in a SQL query, and ensures that dropping a tenant is
as simple as dropping their catalog.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.core.config import get_settings
from app.core.errors import AssariumError

logger = logging.getLogger("assarium.engine.catalog")


def sanitize_tenant_slug(tenant_id: str) -> str:
    """Make the tenant ID safe for use as a Databricks catalog name.
    
    Catalog names must contain only alphanumeric characters and underscores,
    and cannot start with a number.
    """
    slug = re.sub(r"[^a-zA-Z0-9_]", "_", tenant_id).lower()
    if slug and slug[0].isdigit():
        slug = f"t_{slug}"
    return slug


class CatalogManager:
    """Manages the creation and granting of tenant-specific catalogs."""

    def __init__(self, connection: Any = None) -> None:
        """
        Args:
            connection: A Databricks SQL connection. If not provided, one will
                be created from the environment.
        """
        self.settings = get_settings()
        self._owned_connection = False
        if connection is None:
            self._connection = self._create_connection()
            self._owned_connection = True
        else:
            self._connection = connection

    def _create_connection(self) -> Any:
        try:
            from databricks import sql as dbsql
        except ImportError as exc:
            raise AssariumError(
                "Cannot provision catalogs without databricks-sql-connector"
            ) from exc

        # Same OAuth M2M service principal as the engine. Provisioning a catalog is a
        # privileged act, so it must be attributable to a principal with grants that can
        # be reviewed - not to a token somebody pasted in.
        from databricks.sdk.core import Config

        config = Config(
            host=f"https://{self.settings.databricks_host.replace('https://', '').strip('/')}",
            client_id=self.settings.databricks_client_id,
            client_secret=self.settings.databricks_client_secret,
        )
        return dbsql.connect(
            server_hostname=self.settings.databricks_host.replace("https://", "").strip("/"),
            http_path=self.settings.databricks_http_path,
            credentials_provider=lambda: config.authenticate,
            _user_agent_entry="Assarium",
        )

    def close(self) -> None:
        if self._owned_connection and self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> CatalogManager:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def provision_tenant(self, tenant_id: str) -> str:
        """
        Create a catalog and medallion schemas for a new tenant.
        
        Returns:
            The name of the provisioned catalog.
        """
        slug = sanitize_tenant_slug(tenant_id)
        catalog_name = f"assarium_{self.settings.environment}_{slug}"
        logger.info("Provisioning catalog '%s' for tenant '%s'", catalog_name, tenant_id)

        queries = [
            f"CREATE CATALOG IF NOT EXISTS `{catalog_name}`",
            f"CREATE SCHEMA IF NOT EXISTS `{catalog_name}`.`bronze`",
            f"CREATE SCHEMA IF NOT EXISTS `{catalog_name}`.`silver`",
            f"CREATE SCHEMA IF NOT EXISTS `{catalog_name}`.`gold`",
            f"CREATE SCHEMA IF NOT EXISTS `{catalog_name}`.`assarium`",
        ]

        with self._connection.cursor() as cursor:
            for query in queries:
                try:
                    cursor.execute(query)
                except Exception as exc:
                    raise AssariumError(
                        f"Failed to execute provisioning query: {query}\nError: {exc}"
                    ) from exc

        logger.info("Provisioned Medallion schemas in '%s'", catalog_name)
        return catalog_name

    def grant_access(self, tenant_id: str, principal: str, role: str = "api") -> None:
        """
        Grant access to the tenant's catalog.
        
        Args:
            tenant_id: The tenant identifier.
            principal: The Databricks service principal ID or user email.
            role: 'api' (read-only for serving) or 'jobs' (read-write for pipelines).
        """
        slug = sanitize_tenant_slug(tenant_id)
        catalog = f"assarium_{self.settings.environment}_{slug}"
        
        grants = [
            f"GRANT USE CATALOG ON CATALOG `{catalog}` TO `{principal}`",
            f"GRANT USE SCHEMA ON CATALOG `{catalog}` TO `{principal}`",
        ]

        if role == "jobs":
            # Pipelines need full control to create/replace tables
            grants.append(
                f"GRANT SELECT, MODIFY, CREATE TABLE "
                f"ON CATALOG `{catalog}` TO `{principal}`"
            )
        else:
            # API needs only read access
            grants.append(f"GRANT SELECT ON CATALOG `{catalog}` TO `{principal}`")

        with self._connection.cursor() as cursor:
            for query in grants:
                try:
                    cursor.execute(query)
                except Exception as exc:
                    logger.warning(
                        "Failed to grant access on '%s' to '%s': %s", catalog, principal, exc
                    )
