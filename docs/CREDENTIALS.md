# Credentials — what to get, and where it goes

Two kinds of credential exist in Assarium, and they are entered in different places.

| Kind | Where it goes | Stored how |
|---|---|---|
| **Platform settings** — engine, encryption key, Azure OpenAI | `apps/api/.env` on the server | plain text file, never in git |
| **Source credentials** — Postgres password, Snowflake key, SAS token… | The **Connect dialog in the browser** | Fernet-encrypted in the metadata DB; never returned to the browser |

You never put a source password in `.env`. You paste it into the dialog once, and it is
encrypted before it touches disk. Verified: the API response, the list endpoint and the
database column all contain no plaintext.

Portal menu names drift between releases. Where a path is given, treat it as the route
as of writing rather than a guarantee.

---

## Part 1 — Platform settings (`apps/api/.env`)

Create `apps/api/.env`. Nothing here is required to run locally with files and DuckDB.

```bash
# ---- Encryption -------------------------------------------------------------------
# Encrypts every stored source credential. Generate once and keep it safe: rotate it
# and every saved connection becomes undecryptable and must be re-entered.
# Generate:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
ASSARIUM_SECRET_KEY=

# Local development auto-generates this into ~/.assarium/secret.key. Outside local the
# app refuses to start without it rather than silently using an ephemeral key.
ASSARIUM_ENVIRONMENT=local

# ---- Metadata store ---------------------------------------------------------------
# Defaults to SQLite at ~/.assarium/assarium.db. Point at Postgres for anything shared.
# ASSARIUM_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/assarium

# ---- Compute engine ---------------------------------------------------------------
# duckdb (default, local files) or databricks
ASSARIUM_ENGINE=duckdb

# Only when ASSARIUM_ENGINE=databricks
# ASSARIUM_DATABRICKS_HOST=adb-1234567890123456.7.azuredatabricks.net
# ASSARIUM_DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/abc123def456
# ASSARIUM_DATABRICKS_TOKEN=dapi...
# ASSARIUM_DATABRICKS_CATALOG_BRONZE=assarium_bronze
# ASSARIUM_DATABRICKS_CATALOG_SILVER=assarium_silver
# ASSARIUM_DATABRICKS_CATALOG_GOLD=assarium_gold

# ---- Azure OpenAI (powers the sparkle explanations and the Ask tab) ---------------
ASSARIUM_AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com
ASSARIUM_AZURE_OPENAI_API_KEY=
ASSARIUM_AZURE_OPENAI_DEPLOYMENT=gpt-4o
# Use a managed identity instead of a key (leave API_KEY blank if you do):
# ASSARIUM_AZURE_OPENAI_USE_ENTRA_ID=true

# ---- Safety rails ------------------------------------------------------------------
ASSARIUM_QUERY_ROW_LIMIT=50000
ASSARIUM_PROFILE_SAMPLE_ROWS=50000
```

### Getting the Azure OpenAI values

1. Azure Portal → **Create a resource** → *Azure OpenAI* → create it (needs an approved
   subscription).
2. Open the resource → **Model deployments** → *Manage deployments* → **Deploy model**.
   Deploy e.g. `gpt-4o`. The **deployment name** you choose is
   `ASSARIUM_AZURE_OPENAI_DEPLOYMENT` — it is not necessarily the model name.
3. Resource → **Keys and Endpoint** → copy *Endpoint* and *KEY 1*.

Without these, everything except the sparkle and the Ask tab works, and both say so
plainly rather than erroring oddly.

---

## Part 2 — Source credentials (the Connect dialog)

**Sources → Add a source → pick the system.** The dialog only asks for what that system
actually needs, and marks the recommended auth method.

Grant **read-only** everywhere. Assarium never writes to your sources.

---

### PostgreSQL

**Fields:** Host · Port `5432` · Database · SSL mode · Username · Password

Ask your DBA, or Azure Portal → *Azure Database for PostgreSQL* → **Connection strings**.

Create a read-only role first:

```sql
CREATE ROLE assarium_reader LOGIN PASSWORD 'choose-a-strong-one';
GRANT CONNECT ON DATABASE yourdb TO assarium_reader;
GRANT USAGE ON SCHEMA public TO assarium_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO assarium_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO assarium_reader;
```

For Azure: add your IP under **Networking → Firewall rules**. The *Microsoft Entra ID*
auth option needs no stored password — the principal must already be mapped as a role
on the server.

---

### MySQL / MariaDB

**Fields:** Host · Port `3306` · Default database (optional) · Require SSL · Username · Password

```sql
CREATE USER 'assarium_reader'@'%' IDENTIFIED BY 'choose-a-strong-one';
GRANT SELECT ON yourdb.* TO 'assarium_reader'@'%';
FLUSH PRIVILEGES;
```

---

### SQL Server / Azure SQL

**Fields:** Server (`myserver.database.windows.net`) · Port `1433` · Database · Login · Password

Azure Portal → *SQL databases* → your database → **Connection strings**, and
**Set server firewall** to allow your IP.

```sql
CREATE LOGIN assarium_reader WITH PASSWORD = 'choose-a-strong-one';
CREATE USER assarium_reader FOR LOGIN assarium_reader;
ALTER ROLE db_datareader ADD MEMBER assarium_reader;
```

---

### Snowflake — key-pair is the one to use

**Fields:** Account identifier · User · Warehouse · Role · Database (optional) ·
Private key (PKCS#8 PEM) · Key passphrase (optional)

Snowflake is blocking single-factor password sign-in for service connections through
2026. The dialog offers password but flags it deprecated. Use key-pair.

```bash
# 1. Generate the key pair
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out assarium_key.p8 -nocrypt
openssl rsa -in assarium_key.p8 -pubout -out assarium_key.pub
```

```sql
-- 2. Register the public key (paste the body, without the BEGIN/END lines)
ALTER USER ASSARIUM_SVC SET RSA_PUBLIC_KEY='MIIBIjANBgkq...';

-- 3. A read-only role and its own warehouse, so this workload's cost is visible
CREATE ROLE ASSARIUM_READER;
CREATE WAREHOUSE ASSARIUM_WH WITH WAREHOUSE_SIZE = 'XSMALL' AUTO_SUSPEND = 60;
GRANT USAGE ON WAREHOUSE ASSARIUM_WH TO ROLE ASSARIUM_READER;
GRANT USAGE ON DATABASE YOURDB TO ROLE ASSARIUM_READER;
GRANT USAGE ON ALL SCHEMAS IN DATABASE YOURDB TO ROLE ASSARIUM_READER;
GRANT SELECT ON ALL TABLES IN DATABASE YOURDB TO ROLE ASSARIUM_READER;
GRANT ROLE ASSARIUM_READER TO USER ASSARIUM_SVC;
```

Paste the **whole contents of `assarium_key.p8`**, including the BEGIN/END lines, into
*Private key*. Account identifier is the `orgname-account_name` part of your Snowflake
URL, without `.snowflakecomputing.com`.

---

### Databricks

**Fields:** Workspace host · HTTP path · Default catalog · then either
OAuth (Client ID + Client secret) or a Personal access token

- Host and HTTP path: workspace → **SQL Warehouses** → your warehouse →
  **Connection details**. Host has no `https://`; HTTP path looks like
  `/sql/1.0/warehouses/abc123def456`.
- **OAuth M2M (recommended):** workspace **Settings → Identity and access →
  Service principals** → add one → **Secrets** → generate. Grant it `USE CATALOG`,
  `USE SCHEMA` and `SELECT` in Unity Catalog.
- **PAT:** **Settings → Developer → Access tokens → Generate new token.**

Same values go in `.env` if you also want Databricks as the *engine*.

---

### BigQuery

**Fields:** Project ID · Location · Service account JSON

1. Google Cloud Console → **IAM & Admin → Service Accounts → Create**.
2. Grant **BigQuery Data Viewer** and **BigQuery Job User**.
3. Open it → **Keys → Add key → Create new key → JSON** → download.
4. Paste the **entire file contents** into *Service account JSON*.

*Application default credentials* is offered if Assarium runs on GCP — nothing stored.

---

### Azure Data Lake Storage Gen2 / Blob

**Fields:** Storage account · Container (optional) · then one of four methods

**Service principal (recommended):**
1. Entra ID → **App registrations → New registration**. Copy *Directory (tenant) ID*
   and *Application (client) ID*.
2. **Certificates & secrets → New client secret** → copy the **Value** immediately;
   it is shown once.
3. Storage account → **Access Control (IAM) → Add role assignment** →
   **Storage Blob Data Reader** → assign to the app.

**SAS token:** Storage account → **Shared access signature** → Read + List, set an
expiry → *Generate SAS*. Copy the token, not the full URL.

**Account key:** Storage account → **Access keys**. Grants full account access — prefer
the other two.

**Managed identity:** only when Assarium runs on Azure. Nothing stored.

---

### Amazon S3

**Fields:** Region · Bucket (optional) · Custom endpoint (for MinIO/R2) ·
Access key ID · Secret access key · Session token (optional)

AWS Console → **IAM → Users → Create user** → attach a policy like:

```json
{ "Version": "2012-10-17", "Statement": [
  { "Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "arn:aws:s3:::your-bucket/*" },
  { "Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": "arn:aws:s3:::your-bucket" }
]}
```

Then **Security credentials → Create access key**. Copy both halves.

---

### SharePoint — certificate, not a secret

**Fields:** Tenant ID · Client ID · Site URL · Certificate private key (PEM) ·
Certificate thumbprint

A client secret works for Graph drive access but **not** for the SharePoint API surface,
so certificate auth is the path that works everywhere.

```bash
openssl req -x509 -newkey rsa:2048 -keyout assarium.key -out assarium.crt -days 730 -nodes \
  -subj "/CN=assarium"
openssl pkcs12 -export -out assarium.pfx -inkey assarium.key -in assarium.crt   # upload this
openssl x509 -in assarium.crt -noout -fingerprint -sha1                    # the thumbprint
```

1. Entra ID → **App registrations → New registration**. Copy tenant and client IDs.
2. **Certificates & secrets → Certificates → Upload certificate** → `assarium.pfx`.
3. **API permissions → Add → Microsoft Graph → Application permissions →
   `Sites.Selected`** → **Grant admin consent**.
4. `Sites.Selected` grants nothing until a site is assigned. Grant read on one site:

```http
POST https://graph.microsoft.com/v1.0/sites/{site-id}/permissions
{ "roles": ["read"],
  "grantedToIdentities": [{ "application": { "id": "<client-id>", "displayName": "Assarium" }}] }
```

Paste the contents of `assarium.key` into *Certificate private key*, and the fingerprint
(colons removed) into *Thumbprint*.

---

### Salesforce

**Fields:** Instance URL · API version · Consumer key · Consumer secret
(or, for JWT: run-as username + private key)

Connected Apps are restricted for new creation from Spring '26 — use an
**External Client App**.

1. Setup → **App Manager → New External Client App**.
2. Enable OAuth. Scopes: `Manage user data via APIs (api)` and `refresh_token`.
3. Under **Flow Enablement**, tick **Enable Client Credentials Flow** and set the
   **Run As** user — an integration user with read-only permissions.
4. **Consumer Key** and **Consumer Secret** are on the app's detail page.

Instance URL is your My Domain (`https://myorg.my.salesforce.com`); use the sandbox
domain for a sandbox. For JWT instead, upload a certificate and tick *Use digital
signatures*.

---

### File upload

No credentials. Files are stored on the platform host under
`~/.assarium/uploads/<upload-id>/` and go nowhere else. This is the fastest way to demo.

---

## Part 3 — What each credential can do

| Source | Browse | Profile | Full ingest | Notes |
|---|:--:|:--:|:--:|---|
| PostgreSQL | ✓ | ✓ | ✓ | Exact row/distinct counts pushed down |
| MySQL | ✓ | ✓ | ✓ | |
| SQL Server | ✓ | ✓ | ✓ | |
| Snowflake | ✓ | ✓ | ✓ | Key-pair; warehouse must be resumable |
| Databricks | ✓ | ✓ | ✓ | Unity Catalog |
| BigQuery | ✓ | ✓ | ✓ | Reads storage directly — ingest costs no query bytes |
| ADLS Gen2 | ✓ | ✓ | ✓ | Preview capped at 256 MB per object |
| Amazon S3 | ✓ | ✓ | ✓ | Same cap |
| SharePoint | ✓ | ✓ | ✓ | Same cap |
| Salesforce | ✓ | ✓ | ✓ | SOQL paging, 2,000 records per call |
| File upload | ✓ | ✓ | ✓ | |

## Part 4 — Installing the driver for a source

Drivers are optional so a laptop install never breaks on a native build. A source whose
driver is missing shows **"driver needed"** on its card and the exact command in its
dialog.

```bash
cd apps/api
./.venv/bin/pip install \
  "psycopg[binary]" pymysql pymssql snowflake-connector-python \
  google-cloud-bigquery databricks-sql-connector \
  azure-storage-file-datalake boto3 msal simple-salesforce openpyxl
```

Or one at a time — the connection dialog shows the exact command for whichever driver is
missing. Restart the API afterwards.
