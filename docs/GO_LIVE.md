# Credentials, configuration, and how far this is from running

Three questions answered here, in order:

1. **Which credentials do I need, and exactly where does each one go?**
2. **What does my `.env` look like?**
3. **How many steps away is a full run?**

The short version of (3): **running it locally with real sources is zero steps away — that
works today.** Running it as a multi-tenant product for paying customers is blocked on five
pieces of work, all listed in `AUDIT.md`, plus the infrastructure below.

---

# Part 1 — The three ways to run this

Which credentials you need depends entirely on which of these you are doing. Most people
skip straight to the third and get stuck on credentials they do not yet need.

| | Credentials needed | Isolation | Use it for |
|---|---|---|---|
| **A. Local demo** | none | single user | showing the product |
| **B. Local + real sources** | per-source only, entered in the browser | single user | proving it works on real data |
| **C. Multi-tenant production** | the full list below | per-tenant catalog | actual customers |

**A and B work today.** C does not, and the audit says why.

---

# Part 2 — Credentials, A to Z

## The rule that decides where anything goes

> **Platform credentials go in `.env`. Source credentials never do.**

A source credential — a Postgres password, a Snowflake key, a SharePoint client secret —
is entered in the Connect dialog in the browser, goes straight to Key Vault, and only a
reference (`source-{id}-{field}`) is stored in our database. If you find yourself putting a
customer's database password in `.env`, something has gone wrong.

---

## Group A — Azure platform

Your infra engineer creates these. Everything here goes in `.env` or into the app's
environment at deploy.

### A1. Entra app registration (for the API)

**What:** an app registration representing the Assarium API, so it can validate the tokens
your users present.

**How to get it:** Entra ID → App registrations → New registration. Then *Expose an API* →
set an Application ID URI.

**Where it goes:** `ASSARIUM_ENTRA_AUDIENCE` — the Application ID URI (`api://…`) or the
client id.

**Note:** this is *not* a secret. The API only validates tokens; it does not need a client
secret to do that.

### A2. Managed Identity for the API's compute

**What:** the identity your App Service or Container App runs as.

**How to get it:** enable System-assigned identity on the resource. There is no value to
copy — that is the point.

**Where it goes:** nowhere. `DefaultAzureCredential` picks it up automatically.

**What it needs granted:**
- **Key Vault Secrets Officer** on the vault. Note: *Officer*, not *User* — the app writes
  source credentials as well as reading them.
- Nothing on storage. Databricks reaches storage through its own Access Connector.

### A3. Azure Key Vault

**What:** where every customer's source credentials live.

**How to get it:** Azure portal -> Key Vault -> Create. One vault per environment.

**Where it goes:** `ASSARIUM_KEY_VAULT_URL=https://<vault-name>.vault.azure.net/`

**Also set:** `ASSARIUM_SECRETS_BACKEND=keyvault`

### A4. Metadata database

**What:** Postgres holding tenants, connections, datasets, runs, dashboards. No secret
values — only Key Vault references.

**How to get it:** Azure Database for PostgreSQL Flexible Server. Use Entra authentication
if you can; the managed identity then needs to be a database user.

**Where it goes:**
```
ASSARIUM_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/assarium
```

**Do not** leave this on the SQLite default outside local. SQLite is one file on one
machine — two API replicas would each have their own.

### A5. Encryption key

**What:** a Fernet key. Used for the local secret store and, derived, for signing sessions.

**How to get it:**
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**Where it goes:** `ASSARIUM_SECRET_KEY` — and the real value belongs in Key Vault, injected
as an environment variable at deploy. Not in a file on disk.

---

## Group B — Databricks

### B1. Service principal, OAuth M2M

**What:** how Assarium authenticates to Databricks. **Not** a personal access token.

**How to get it:** Databricks account console → User management → Service principals → new
principal → Secrets → Generate secret. You get a client id and a client secret.

**Where it goes:**
```
ASSARIUM_DATABRICKS_CLIENT_ID=<uuid>
ASSARIUM_DATABRICKS_CLIENT_SECRET=<from Key Vault at deploy, never in a file>
```

**What it needs granted in Unity Catalog:**
- `CREATE CATALOG` on the metastore — so a new tenant gets its own catalog at signup
- `USE CATALOG`, `USE SCHEMA`, `SELECT`, `MODIFY`, `CREATE TABLE` on each tenant catalog
- `READ FILES` / `WRITE FILES` on the tenant's external location only

Note what it must **not** have: Account Admin. That role can grant itself anything in any
workspace, which makes its secret the most valuable credential in the estate.

### B2. Workspace host

**Where it goes:**
```
ASSARIUM_DATABRICKS_HOST=adb-1234567890123456.7.azuredatabricks.net
```
Hostname only — no `https://`, no trailing slash.

### B3. SQL warehouse HTTP path

**How to get it:** SQL Warehouses → your warehouse → Connection details.

**Where it goes:**
```
ASSARIUM_DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/abc123def456
```

Use a **serverless** warehouse if available: a classic warehouse takes minutes to start, and
the first dashboard load of the day will look broken.

### B4. Engine selection

```
ASSARIUM_ENGINE=databricks
```

The app refuses to start with `duckdb` outside a local environment.

---

## Group C — Azure OpenAI (optional)

Needed only for the AI explanations and the Ask tab. Everything else works without it.

**How to get it:** Azure OpenAI resource → Deployments → deploy `gpt-4o`.

**Where it goes:**
```
ASSARIUM_AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
ASSARIUM_AZURE_OPENAI_DEPLOYMENT=gpt-4o
ASSARIUM_AZURE_OPENAI_USE_ENTRA_ID=true
```

**Prefer Entra over an API key.** With `USE_ENTRA_ID=true` the managed identity needs the
**Cognitive Services OpenAI User** role and there is no key to rotate or leak. Only fall back
to `ASSARIUM_AZURE_OPENAI_API_KEY` if you cannot grant that role.

---

## Group D — Source credentials (these do NOT go in `.env`)

Entered in the Connect dialog, stored in Key Vault. Listed so you know what to have ready
before a demo.

| Source | What to have ready | Notes |
|---|---|---|
| **SharePoint** | Directory (tenant) id, client id, client secret or certificate, site URL | Graph `Sites.Selected` is the right permission — it scopes to named sites instead of the whole tenant. Prefer **application** permissions so the connection is not tied to an employee |
| **Salesforce** | Connected app consumer key + secret, then OAuth in the browser | Watch the five-token limit: a sixth authorization silently revokes the oldest. Prefer the JWT bearer flow for a connection that must outlive any one person |
| **Postgres / MySQL / SQL Server** | host, port, database, user, password | Give it a **read-only** role. Assarium never writes back to a source |
| **Snowflake** | account identifier, user, warehouse, role, key pair | Use key-pair, not a password — Snowflake is pushing MFA on password auth and a password-authenticated integration will break |
| **BigQuery** | service account JSON, project id | Grant `BigQuery Data Viewer` + `Job User`, nothing more |
| **ADLS Gen2 / S3** | container/bucket and path | Prefer managed identity or a role, not an access key |
| **File upload** | nothing | The zero-credential demo path |

---

# Part 3 — Your `.env`

`.env.example` in the repo root is the authoritative template and now documents every
setting. Copy it:

```bash
cp .env.example apps/api/.env
```

## For local work — this is the whole file

```
ASSARIUM_ENVIRONMENT=local
ASSARIUM_ENGINE=duckdb
```

That is genuinely it. Everything else has a working default, the encryption key is generated
into `~/.assarium/secret.key` on first run, and source credentials are entered in the
browser.

## For production

```
ASSARIUM_ENVIRONMENT=production
ASSARIUM_DEBUG=false

# Identity
ASSARIUM_AUTH_MODE=entra
ASSARIUM_ENTRA_AUDIENCE=api://assarium-api
ASSARIUM_ENTRA_ALLOWED_TENANT_IDS=["<directory-id>"]

# Secrets
ASSARIUM_SECRETS_BACKEND=keyvault
ASSARIUM_KEY_VAULT_URL=https://kv-assarium-prod-001.vault.azure.net/
ASSARIUM_SECRET_KEY=<injected from Key Vault at deploy>

# Metadata
ASSARIUM_DATABASE_URL=<injected from Key Vault at deploy>

# Compute
ASSARIUM_ENGINE=databricks
ASSARIUM_DATABRICKS_HOST=adb-xxxx.7.azuredatabricks.net
ASSARIUM_DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/xxxx
ASSARIUM_DATABRICKS_CLIENT_ID=<uuid>
ASSARIUM_DATABRICKS_CLIENT_SECRET=<injected from Key Vault at deploy>
ASSARIUM_CATALOG_PREFIX=assarium

# AI
ASSARIUM_AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
ASSARIUM_AZURE_OPENAI_DEPLOYMENT=gpt-4o
ASSARIUM_AZURE_OPENAI_USE_ENTRA_ID=true

# Frontend origin - exact, never a wildcard
ASSARIUM_CORS_ORIGINS=["https://app.assarium.com"]
```

**The four marked "injected from Key Vault at deploy" must never be written into a file.**
They come from the deployment platform's own secret injection — App Service settings sourced
from Key Vault references, or the container platform's secret mount. A `.env` file
containing them is how credentials end up in git history, where rotating them is no
longer enough.

---

# Part 4 — How far is a full run

## Local demo: ready now

```bash
./start-api.sh
./start-web.sh
```

Open `http://localhost:3000`, upload a CSV, run the pipeline, look at a dashboard. Zero
credentials.

## Local with a real source: ready now

Same, plus the source's own credentials typed into the Connect dialog. Postgres, Snowflake,
SharePoint, Salesforce, BigQuery all work. Single user — no tenant isolation, which is fine
because there is only one of you.

## Multi-tenant production: five pieces of work, plus infra

Being straight about this, because the design documents describe the destination and it
would be easy to read them as describing the present.

### Blocking on the codebase — done

All five are closed. `AUDIT.md` records what was built and, more usefully, the six further
defects the work uncovered — including a route guard that passed by inspecting nothing, and
a `Session` standing in for a `TenantScope` that let one tenant create a schedule on
another's connection.

**389 unit tests, 55 end-to-end checks, lint and typecheck clean.**

### Blocking on infrastructure

| # | Work | Owner |
|---|---|---|
| 1 | Databricks service principal with the grants in B1 — and *not* Account Admin | infra |
| 2 | Key Vault RBAC for the API's managed identity (Secrets Officer) | infra |
| 3 | Postgres for metadata | infra |
| 4 | One external location per tenant container | infra |

**Item 0 comes first and nothing else should start before it.** Rotation alone is not enough
if that secret was ever pushed — the old value stays reachable in history, and in every fork
and clone.

### Then, before customers

Rate limiting, upload caps, query timeout, security headers, and wiring the audit recorder —
`AUDIT.md` items 6 and 7. None block a pilot with one friendly customer; all block a
security questionnaire.

## Honest summary

- **Demo tomorrow:** ready.
- **Pilot with one real customer on real data:** ready, once your infra engineer finishes
  item 0 and the Databricks service principal exists.
- **Two customers on the same deployment:** the code is ready and the test that proves it
  exists — 59 cross-tenant tests assert that A asking for B's objects gets a 404, across
  every router. What remains before real customers is infrastructure, not application code:
  the rotated credential, the service principal, and Alembic migrations before there is
  data worth keeping.

The data engineering underneath — layer classification, quarantine, watermarks, SCD2,
exactly-once scheduling — is done and covered by 273 passing tests. What is missing is the
access control wrapped around it, and that is a known, bounded piece of work rather than an
open question.
