from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.errors import ConfigurationError


class Settings(BaseSettings):
    """Runtime configuration. Everything is overridable by environment variable."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../../.env"), env_prefix="ASSARIUM_", extra="ignore"
    )

    app_name: str = "Assarium"
    environment: str = "local"
    debug: bool = True

    # Where the platform keeps its own state: metadata DB, medallion warehouse, artefacts.
    data_dir: Path = Field(default=Path.home() / ".assarium")

    # Metadata store. SQLite locally; point at Postgres in a deployed environment.
    database_url: str | None = None

    # 32-byte urlsafe-base64 Fernet key. Auto-generated into data_dir on first run when unset,
    # which is fine for local development and never acceptable in production.
    secret_key: str | None = None

    cors_origins: list[str] = ["http://localhost:3000"]

    # ---- Authentication and tenancy --------------------------------------------------
    # "entra"  validate Microsoft Entra ID bearer tokens (the only production mode)
    # "local"  fixed development identity, no directory required
    auth_mode: str = "local"

    entra_audience: str | None = None
    #: Directories allowed to authenticate. Empty means any registered tenant's
    #: directory, which is still gated by the tenant registry.
    entra_allowed_tenant_ids: list[str] = Field(default_factory=list)

    local_dev_subject: str = "00000000-0000-0000-0000-000000000001"
    local_dev_email: str = "developer@localhost"
    local_dev_directory: str = "local-development"

    # ---- Secrets ----------------------------------------------------------------------
    # "keyvault" resolve source credentials from Azure Key Vault by reference
    # "local"    encrypted at rest in the metadata database (development only)
    secrets_backend: str = "local"
    key_vault_url: str | None = None

    # Compute engine. "databricks" is the only supported production value; "duckdb"
    # exists for the test suite and local development and is refused outside local.
    engine: str = "duckdb"

    databricks_client_id: str | None = None
    databricks_client_secret: str | None = None
    #: Catalog naming: assarium_{env}_{tenant_slug}, plus a shared control catalog.
    catalog_prefix: str = "assarium"
    control_catalog: str | None = None

    databricks_host: str | None = None
    databricks_http_path: str | None = None

    # Azure OpenAI (v1 GA surface: base_url = {endpoint}/openai/v1, no api-version).
    azure_openai_endpoint: str | None = None
    azure_openai_api_key: str | None = None
    azure_openai_deployment: str = "gpt-4o"
    azure_openai_use_entra_id: bool = False

    # Safety rails for anything the analysis layer executes.
    query_row_limit: int = 50_000
    query_timeout_seconds: int = 120
    profile_sample_rows: int = 50_000

    # ---- Orchestration ----------------------------------------------------------------
    scheduler_enabled: bool = True
    scheduler_poll_seconds: int = 30

    # ---- Alerting ---------------------------------------------------------------------
    # When set, a JSON payload is POSTed to this URL on every pipeline event.
    # Accepts Slack, Teams, PagerDuty, or any webhook-compatible endpoint.
    alert_webhook_url: str | None = None

    @property
    def warehouse_dir(self) -> Path:
        return self.data_dir / "warehouse"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.data_dir / 'assarium.db'}"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.warehouse_dir, self.data_dir / "uploads"):
            path.mkdir(parents=True, exist_ok=True)


    def enforce_production_guards(self) -> None:
        """
        Refuse to start in an unsafe combination.

        These are the settings where a wrong value is not a misconfiguration but a
        security hole, so the process fails loudly at boot rather than serving traffic
        with authentication disabled or every tenant sharing one engine.
        """
        if self.environment == "local":
            return

        problems: list[str] = []
        if self.auth_mode != "entra":
            problems.append(
                "ASSARIUM_AUTH_MODE must be 'entra' outside local development; "
                f"it is '{self.auth_mode}', which would accept a fixed identity for "
                "every caller."
            )
        if self.auth_mode == "entra" and not self.entra_audience:
            problems.append("ASSARIUM_ENTRA_AUDIENCE must be set when auth_mode is 'entra'.")
        if self.engine != "databricks":
            problems.append(
                f"ASSARIUM_ENGINE must be 'databricks' outside local development; "
                f"it is '{self.engine}'. DuckDB is a single-node test fixture and "
                "provides no tenant isolation."
            )
        if self.secrets_backend != "keyvault":
            problems.append(
                "ASSARIUM_SECRETS_BACKEND must be 'keyvault' outside local development."
            )
        if self.secrets_backend == "keyvault" and not self.key_vault_url:
            problems.append("ASSARIUM_KEY_VAULT_URL must be set when using Key Vault.")
        if not self.secret_key:
            problems.append("ASSARIUM_SECRET_KEY must be set outside local development.")

        if problems:
            raise ConfigurationError(
                "Refusing to start with an unsafe configuration:\n  - "
                + "\n  - ".join(problems)
            )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    settings.enforce_production_guards()
    return settings
