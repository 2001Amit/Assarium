from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from app.core.config import Settings
from app.core.errors import AssariumError
from app.engine.catalog_manager import CatalogManager, sanitize_tenant_slug


def test_sanitize_tenant_slug():
    assert sanitize_tenant_slug("foo_bar") == "foo_bar"
    assert sanitize_tenant_slug("Tenant-123!") == "tenant_123_"
    # Databricks catalog names cannot start with a number
    assert sanitize_tenant_slug("123tenant") == "t_123tenant"


@pytest.fixture
def mock_dbsql():
    with patch("app.engine.catalog_manager.get_settings") as mock_settings:
        mock_settings.return_value = Settings(
            environment="dev",
            databricks_host="test-host",
            databricks_http_path="/test",
            databricks_token="token",
        )
        
        mock_connection = MagicMock()
        mock_cursor = MagicMock()
        mock_connection.cursor.return_value.__enter__.return_value = mock_cursor
        
        yield mock_connection, mock_cursor


def test_provision_tenant(mock_dbsql):
    mock_connection, mock_cursor = mock_dbsql
    
    manager = CatalogManager(connection=mock_connection)
    catalog = manager.provision_tenant("Acme Corp")
    
    assert catalog == "assarium_dev_acme_corp"
    
    expected_calls = [
        call("CREATE CATALOG IF NOT EXISTS `assarium_dev_acme_corp`"),
        call("CREATE SCHEMA IF NOT EXISTS `assarium_dev_acme_corp`.`bronze`"),
        call("CREATE SCHEMA IF NOT EXISTS `assarium_dev_acme_corp`.`silver`"),
        call("CREATE SCHEMA IF NOT EXISTS `assarium_dev_acme_corp`.`gold`"),
        call("CREATE SCHEMA IF NOT EXISTS `assarium_dev_acme_corp`.`assarium`"),
    ]
    mock_cursor.execute.assert_has_calls(expected_calls, any_order=False)


def test_grant_access_api(mock_dbsql):
    mock_connection, mock_cursor = mock_dbsql
    
    manager = CatalogManager(connection=mock_connection)
    manager.grant_access("Acme Corp", "api-sp", role="api")
    
    expected_calls = [
        call("GRANT USE CATALOG ON CATALOG `assarium_dev_acme_corp` TO `api-sp`"),
        call("GRANT USE SCHEMA ON CATALOG `assarium_dev_acme_corp` TO `api-sp`"),
        call("GRANT SELECT ON CATALOG `assarium_dev_acme_corp` TO `api-sp`"),
    ]
    mock_cursor.execute.assert_has_calls(expected_calls, any_order=False)


def test_grant_access_jobs(mock_dbsql):
    mock_connection, mock_cursor = mock_dbsql
    
    manager = CatalogManager(connection=mock_connection)
    manager.grant_access("Acme Corp", "jobs-sp", role="jobs")
    
    expected_calls = [
        call("GRANT USE CATALOG ON CATALOG `assarium_dev_acme_corp` TO `jobs-sp`"),
        call("GRANT USE SCHEMA ON CATALOG `assarium_dev_acme_corp` TO `jobs-sp`"),
        call("GRANT SELECT, MODIFY, CREATE TABLE ON CATALOG `assarium_dev_acme_corp` TO `jobs-sp`"),
    ]
    mock_cursor.execute.assert_has_calls(expected_calls, any_order=False)


def test_provision_failure_raises_error(mock_dbsql):
    mock_connection, mock_cursor = mock_dbsql
    mock_cursor.execute.side_effect = Exception("Databricks down")
    
    manager = CatalogManager(connection=mock_connection)
    with pytest.raises(AssariumError, match="Failed to execute provisioning query"):
        manager.provision_tenant("Acme Corp")
