# Assarium — production architecture

The target: a multi-tenant SaaS data platform running on Azure Databricks + Unity Catalog,
ingesting from real source systems on a schedule, with no dummy data anywhere.

This supersedes the local-first design in `PLAN.md`. That design got the product surface
right — connectors, classification, semantic layer, dashboards — and the data plumbing
underneath it wrong for production. This document fixes the plumbing.

---

## 1. The decisions, and why

### 1.1 DuckDB is demoted, not deleted

Production runs on **Databricks + Unity Catalog only**. DuckDB is removed as a
configurable deployment engine — two production paths means every feature gets built
twice and the second one is never the one that matters.

But DuckDB stays as the **test fixture**, and that is a deliberate, defended choice:

- The suite runs in about a second, with no cloud credentials and no cluster start.
- A CI pipeline that needs a live Databricks workspace is slow (~5 min cluster spin-up),
  costs money per run, is flaky, and needs production secrets in CI.
- Dialect differences are already isolated behind the `Engine` interface.

The risk this creates is real and must be named: **a test can pass on DuckDB and fail on
Databricks.** The mitigation is a second, smaller integration suite that runs against a
real Databricks workspace before release, covering exactly the things DuckDB cannot
prove — MERGE semantics, Unity Catalog grants, Auto Loader, schema evolution.

So: `ASSARIUM_ENGINE` stops being a deployment choice. Databricks is the engine. DuckDB
is a fixture.

### 1.2 Isolation model: catalog per tenant

Databricks' own guidance is that **the catalog is the primary unit of data isolation**,
and that each catalog should get its own managed storage container. Row filters on a
shared table are a weaker fallback, not the first choice.

```
assarium_{env}_{tenant_slug}          one catalog per tenant
    bronze / silver / gold            medallion schemas
    (own ADLS container or path)      storage isolation, not just logical

assarium_{env}_platform               shared control plane, no tenant data
    control.tenants                   tenant registry
    control.connections               source configuration (no secrets)
    control.ingestion_file_tracker    per-file watermark and lifecycle
    control.pipeline_runs             run history
    control.load_audit                per-table outcome per run
    control.access_audit              who read what, when
```

Row-level security is still applied **inside** each tenant catalog for sub-tenant roles
(e.g. a property manager who may only see their own portfolio). It is defence in depth,
not the isolation boundary.

**Why this over row filters on a shared table:** a bug in a filter predicate leaks every
tenant's data at once. A bug in catalog routing fails closed — the query errors rather
than returning someone else's rows. Storage-level separation also means an export, a
backup or a deletion request is scoped to one container.

### 1.3 Tenant identity comes from the token, never the request

The `tenant_id` is resolved **server-side from the validated JWT** and nothing else.

- Entra ID issues the token; the API validates signature, issuer, audience and expiry
  against the published JWKS.
- The `tid` claim is matched against the tenant registry. An unknown `tid` is rejected.
- A `tenant_id` in a request body, query string or header is **ignored**, and its presence
  is logged as a suspicious event.
- Every query the platform compiles is scoped to the resolved tenant's catalog. There is
  no code path that takes a tenant from user input.

This is the single most important rule in the system. Everything else is recoverable.

### 1.4 Secrets never live in the application

| Secret | Where it lives | How it is read |
|---|---|---|
| Source credentials (SharePoint, Postgres, Snowflake…) | **Azure Key Vault** | Fetched at run time via Managed Identity |
| Databricks access | **OAuth M2M** (service principal) | Token minted per session, auto-refreshed |
| Assarium's own DB, JWT signing | Key Vault, injected as env at deploy | Never in the image or repo |

The application database stores a **Key Vault secret reference** (a URI), not a value. The
current design's encrypted-blob-in-Postgres is replaced. Rationale: the blast radius of a
database compromise should not include every customer's source credentials.

Personal access tokens are not used anywhere. `.gitignore` is not a security control — it
is a convenience; the real control is that no secret value ever enters the repo or the
image.

### 1.5 Ingestion is incremental and idempotent

Full-replace loading is removed. Every source gets a watermark strategy:

| Source kind | Strategy |
|---|---|
| Files (SharePoint, ADLS, S3) | Graph `delta` / etag / `lastModified` per file, recorded in the tracker |
| Databases (Postgres, MySQL, SQL Server, Snowflake, BigQuery) | High-water mark on a monotonic column, or CDC where available |
| Salesforce | `SystemModstamp` watermark |

Landing into bronze uses **Auto Loader** (`cloudFiles`) with `schemaEvolutionMode=addNewColumns`
and `_rescued_data`, which gives exactly-once via checkpoints. Bronze → silver and
silver → gold use **MERGE**, not overwrite.

Because MERGE accumulates small files, `OPTIMIZE` is scheduled on merged tables, or
Predictive Optimization is enabled on Unity Catalog managed tables.

### 1.6 Bad rows are quarantined, never silently dropped

The current silver step uses `TRY_CAST(...)`, which turns an unparseable value into NULL
and loses the row's problem without telling anyone. That contradicts the product's own
premise — that a wrong answer is worse than a refusal.

Replaced with an explicit split:

```
silver.{entity}              rows that passed every rule
silver.{entity}_quarantine   rows that failed, with _dq_rule, _dq_error, _dq_run_id
```

Quarantine counts surface on the pipeline run and on the dataset, so nobody has to go
looking. A load that quarantines more than a configurable share of its rows fails the run
rather than publishing a partial table as if it were complete.

### 1.7 History is preserved (SCD2)

Dimensions are loaded as **slowly-changing type 2**: `valid_from`, `valid_to`,
`is_current`, and a `change_hash` so unchanged rows are not rewritten. Facts are appended
with a stable surrogate key.

Without this, "what was revenue last quarter" is unanswerable after the source updates a
row — which for a REIT platform tracking rent changes is not an edge case.

---

## 2. Layout

```
apps/api/app/
    tenancy/          tenant registry, resolution from JWT, per-request context
    auth/             Entra ID JWT validation, JWKS cache, role mapping
    secrets/          Key Vault provider, Managed Identity, reference resolution
    connectors/       unchanged in shape; gains watermark support per driver
    ingestion/        incremental readers, file tracker, Auto Loader landing
    medallion/        bronze/silver/gold with MERGE, quarantine, SCD2
    quality/          declarative DQ rules, quarantine routing, thresholds
    engine/           Databricks engine (production), DuckDB (test fixture only)
    semantic/         unchanged; every query scoped to the caller's catalog
    dashboards/       unchanged
    ai/               unchanged
    orchestration/    schedules, run locking, retries, backfill
    audit/            append-only access and change log

infra/
    terraform/        Azure estate: RG, KV, ADLS, Databricks, UC, per-tenant catalogs
    bundles/          Databricks Asset Bundle: jobs, clusters, per-environment targets
```

Terraform provisions **infrastructure**; Asset Bundles deploy **jobs and code**. They are
not interchangeable — updating a pipeline should not require `terraform apply`.

---

## 3. Build order

Each phase leaves the system working and tested.

| Phase | What | Why this order |
|---|---|---|
| **P1** ✅ | Tenancy foundation: registry, JWT auth, tenant context, catalog routing | Everything else has to be tenant-aware from birth. Retrofitting is a rewrite. |
| **P2** ✅ | Secrets: Key Vault provider, Managed Identity, migrate off encrypted blobs | Before any real credential is entered anywhere. |
| **P3** ✅ | Incremental ingestion: watermark store, file tracker, per-driver strategies | The biggest functional gap; unblocks real sources. |
| **P4** ✅ | Quality: DQ rule framework, quarantine tables, thresholds, run failure | Data correctness before more volume. |
| **P5** ✅ | Medallion rework: MERGE, SCD2, schema evolution, OPTIMIZE | Turns the pipeline production-grade. |
| **P6** ◐ | Orchestration: schedules, locking, retries, backfill, alerting | Removes the human from the loop. |
| **P7** | Databricks hardening: OAuth M2M, UC grants, per-tenant catalogs, RLS | Real infra wiring. |
| **P8** | IaC: Terraform for the estate, Asset Bundles for jobs, CI/CD | Reproducible, reviewable infrastructure. |
| **P9** | Audit and observability: access log, lineage, metrics, cost per tenant | What an enterprise buyer asks for on day one. |

---

## 4. Patterns considered and deliberately not adopted

- **Single shared catalog with row filters** — replaced by catalog-per-tenant.
- **Personal access tokens** — replaced by OAuth M2M.
- **Credentials in the repo** — replaced by Key Vault references.
- **Hand-run Python scripts** — replaced by scheduled, tracked, restartable jobs.
- **Fabric as a required hop** — Assarium's own connectors land directly into ADLS Gen2,
  which is where Unity Catalog's external locations already point. Fabric capacity stays
  available for Power BI Direct Lake, but is not in the ingestion path. One fewer system
  in the critical path is one fewer thing to debug at 2am.

What *is* carried over, because it was right: the file tracker concept, the quarantine
pattern, the medallion layout on ADLS containers, the Access Connector + Managed Identity
storage credential pattern, and the control-table-driven column mapping idea.


---

## 5. Progress

| Phase | State | Evidence |
|---|---|---|
| P1 Tenancy | **Done** | 29 isolation tests. Tenant resolves from the token's `tid` only; a header naming another tenant is ignored and logged. Boot guards refuse local auth, DuckDB or local secrets outside a local environment. |
| P2 Secrets | **Done** | Credentials moved out of the database into a `SecretProvider`. The row holds `source-{id}-{field}`; a canary value planted through the API appears nowhere in the database file, the encrypted store, or any API response. |
| P3 Ingestion | **Done** | Watermark selection with ceiling snapshotting, per-file etag comparison, MERGE-based idempotent writes. Verified: a second run over an unchanged file is skipped, and replaying a window does not duplicate rows. |
| P4 Quality | **Done** | Rules derived from profiling; failing rows land in `{table}_quarantine` with their original value and the rule that caught them. A load exceeding the reject threshold fails rather than publishing a partial table. |

### Bugs found and fixed while building these

- **Quality ran after the cast.** `TRY_CAST` had already turned the bad value into NULL, so
  the type rule inspected a clean column and quarantined nothing — the exact silent loss
  quarantine exists to replace. Rules now evaluate raw bronze, before any cast, which also
  means a quarantined row keeps its original value. Four regression tests pin the ordering.
- **`derive_policy` skipped rules on key columns.** A `continue` after the key rule meant a
  column that was both a key and mistyped never got its type rule.
- **A text column of numbers was chosen as a key.** `monthly_rent` held as text is unique by
  accident, not because it identifies anything. Columns with a numeric shadow type are no
  longer key-eligible, and property vocabulary (rent, NOI, sqft, occupancy) was added to the
  measure names.
- **The type rule was stricter than the transform.** It rejected `1,850.00`, which silver
  strips and converts fine. A quarantine full of rows that were actually fine is one nobody
  reads, so the rule now applies the same cleaning.
- **`REGEXP_REPLACE` failed to bind on a typed column.** Now casts to text first.

| P5 SCD2 | **Done** | Dimensions keep history: a changed row closes off its predecessor rather than overwriting it, unchanged rows are untouched, and replaying a load invents nothing. |
| P6 Orchestration | **Scheduler core done** | Cron schedules with conditional-update locking (verified: 8 concurrent workers, exactly one wins), exponential backoff, and stale-lock reclaim. Remaining: the runner loop and alerting. |

### More bugs found and fixed

- **Changing the SCD2 tracked column set silently re-versioned every row.** Hashes computed
  over different column sets can never match, so a routine schema change would have doubled
  a dimension with nobody noticing. The tracked set is now recorded and a change is reported
  explicitly; column order no longer affects the hash.
- **Internal tables leaked into user-facing listings.** Staging and SCD metadata tables live
  in the same schema as real data and were appearing in the warehouse browser and the
  semantic model builder. Anything prefixed `_` is now filtered out.
- **Timezone comparisons raised `TypeError`.** SQLite returns naive datetimes; comparing one
  against an aware `utcnow()` throws. Centralised in `ensure_utc()` rather than patched at
  each call site.

159 unit tests, 55 end-to-end, lint clean.
