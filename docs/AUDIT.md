# Assarium security and readiness audit

Audited against the codebase as it stands, not against the design documents. Every finding
below was reproduced by reading or running the code, and each one names the file it came
from so it can be checked rather than believed.

**Headline: the multi-tenancy is designed, modelled and unit-tested, but it is not
connected to anything.** Today the API has no authentication on any endpoint, no query is
scoped to a tenant, and every tenant would share one warehouse. The parts exist; the wiring
does not.

---

## Critical

### C1. No endpoint requires authentication

`app/main.py` includes all nine routers with no dependencies:

```python
app.include_router(connections.router)
app.include_router(datasets.router)
...
```

No router declares router-level dependencies, and no endpoint function takes
`get_tenant_context` or `require(...)`. Searching the whole application outside the auth
and tenancy modules themselves returns nothing:

```
grep -rn "get_tenant_context|TenantContext|require(" app/api app/medallion app/dashboards \
     app/semantic app/ai app/orchestration app/audit
    (no matches)
```

That is 45 endpoints — every connection, dataset, pipeline run, semantic query, dashboard
and export — reachable without credentials.

`apps/web` sends no `Authorization` header either, which is consistent: there is nothing on
the server to send one to.

### C2. No query is scoped to a tenant

The lookup pattern throughout is by id alone. From `app/api/routers/connections.py`:

```python
def _load(db: Session, connection_id: str) -> Connection:
    connection = db.get(Connection, connection_id)
    if connection is None:
        raise NotFoundError("That connection does not exist.")
    return connection
```

Any caller holding an id gets the row, whoever it belongs to. The same shape repeats in the
dataset, pipeline, semantic and dashboard routers.

### C3. Most tables have no tenant column to scope by

Checked directly against the mapped tables:

| Table | `tenant_id` |
|---|---|
| `connections` | yes — but `default=""` |
| `datasets` | **no** |
| `dataset_relationships` | **no** |
| `pipeline_runs` | **no** |
| `pipeline_steps` | **no** |
| `semantic_models` | **no** |
| `dashboards` | **no** |

So C2 could not be fixed by adding a filter — there is nothing to filter on. And
`connections.tenant_id` defaulting to `""` is its own hazard: an empty string is a real
value that matches other empty strings, so a filter added later would silently group every
legacy row into one shared pseudo-tenant.

### C4. Every tenant shares one warehouse

`get_engine()` accepts a `tenant_id`, and all fourteen call sites omit it:

```
app/medallion/pipeline.py:77        engine = get_engine()
app/api/routers/dashboards.py:143   run_dashboard(model, get_engine(), ...)
app/api/routers/semantic.py:187     QueryCompiler(model, get_engine()).compile(query)
app/api/routers/pipeline.py:158     engine = get_engine()
...
```

So bronze, silver and gold are one shared namespace across all tenants.

### C5. Per-tenant catalog provisioning is never invoked

`app/engine/catalog_manager.py` implements `provision_tenant()` and `grant_access()`
correctly — one catalog per tenant, which is the isolation model `ARCHITECTURE.md` §1.2
commits to. It has no callers anywhere in the application. It is dead code.

### C6. The passing tenancy tests do not prove what they appear to

P1's 29 isolation tests exercise `get_tenant_context()` directly: given a token, does it
resolve the right tenant and reject a client-supplied one? All of that is true and useful.

None of them assert that any endpoint calls it. A unit test for a lock proves the lock
works, not that it is on the door.

**The test that has to exist:** sign in as tenant A, request tenant B's connection by id,
assert 404. Until that runs, "tenant isolation" is a property of one function rather than
of the product.

---

## High

### H1. The boot guard gives false confidence

`enforce_production_guards()` refuses to start outside local unless `auth_mode == "entra"`,
`engine == "databricks"`, `secrets_backend == "keyvault"` and a secret key is set. It runs,
and it works.

But it validates *configuration*, and the hole is in *wiring*. Set `ASSARIUM_AUTH_MODE=entra`
and the guard passes happily while all 45 endpoints remain open. A guard that reports
healthy on an unauthenticated API is worse than no guard, because it will be cited as
evidence that the deployment is safe.

The guard needs a check it can actually make: at startup, assert that every mounted route
carries the tenant dependency, and refuse to boot if one does not.

### H2. The audit trail is empty by construction

`app/audit/service.py` exists and `/api/audit` lists events. Nothing calls it — the event
recorder has no callers anywhere in the application, so the table stays empty.

An audit log that is always empty is more dangerous than none, because an auditor reading
"no unauthorised access recorded" will take it as a finding rather than as an absence of
instrumentation.

### H3. No rate limiting anywhere

No throttle on any endpoint. `app/identity/service.py` has per-account backoff, but that is
not reachable — nothing calls it either (see C1). So there is:

- no limit on sign-in attempts at the HTTP layer,
- no per-tenant cap on query or pipeline volume,
- no protection on the expensive paths (profiling, dashboard runs, exports).

### H4. Unauthenticated file upload with no size limit

`POST /api/connections/{id}/upload` streams to disk in 1 MiB chunks with no cap on file
size or count:

```python
with open(target, "wb") as handle:
    while chunk := upload.file.read(1024 * 1024):
        handle.write(chunk)
```

Path traversal is handled properly — basename only, dotfiles refused, which is right. But
combined with C1, anyone who can reach the port can fill the disk.

### H5. Two Databricks auth paths, one of them the rejected one

`ASSARIUM_DATABRICKS_TOKEN` is still a setting and still in `.env.example`, alongside
`databricks_client_id` / `databricks_client_secret`.

`ARCHITECTURE.md` §1.4 states plainly that personal access tokens are not used anywhere.
Leaving the setting in place means somebody will use it, because it is the easier of the two
to get working. Remove it.

### H6. Two isolation models coexist in configuration

`databricks_catalog_bronze` / `_silver` / `_gold` describe three shared catalogs. `catalog_prefix`
plus `CatalogManager` describe one catalog per tenant. Both are live settings.

These are contradictory, and the shared-catalog trio is the weaker isolation model.
One of them has to go, and it is not the per-tenant one.

### H7. No query timeout is applied

`query_timeout_seconds` is configured, defaults to 120, and is referenced nowhere outside
`config.py`. A runaway semantic query or dashboard tile has nothing stopping it.

---

## Medium

### M1. Engine cache evicts without closing

`get_engine` is `@lru_cache(maxsize=32)`. With per-tenant engines, the thirty-third tenant
evicts the first tenant's engine and its connection is dropped without being closed. For
Databricks that leaks a session; for DuckDB it leaks a file handle.

### M2. No security headers

No HSTS, `X-Content-Type-Options`, `X-Frame-Options` or CSP. Cheap to add, and the first
thing an enterprise security questionnaire asks about.

### M3. CORS is permissive in shape

`allow_credentials=True` with `allow_methods=["*"]` and `allow_headers=["*"]`. The origin
list is configurable and defaults to `localhost:3000`, so this is not currently a hole — but
it must be pinned to exact production origins, and a wildcard origin with credentials must
be made impossible rather than merely avoided.

### M4. No pagination on list endpoints

`/api/connections`, `/api/datasets`, `/api/schedules` return everything. Fine at demo scale,
a problem at customer scale, and a cheap denial-of-service before then.

---

## What is genuinely sound

Worth stating, because the above is a long list and the foundations under it are not the
problem.

- **Identifier handling.** `safe_identifier()` in `app/engine/base.py` is the single place a
  source object name becomes a table name, and it is strict. Names from other people's
  systems go into DDL through exactly one funnel.
- **No SQL injection found.** `preview_table` validates the layer against a fixed list and
  the table against `engine.list_tables()` before interpolating. The semantic compiler
  builds through the model, not through user strings.
- **Secrets do not leak into responses.** `ConnectionRead` carries no secret material and no
  `secret_refs`. The canary test from P2 still holds.
- **Path traversal on upload is handled** — basename only, dotfiles refused.
- **The medallion logic is correct and well covered.** Layer classification, quarantine,
  SCD2, watermarks, exactly-once scheduling: 273 tests, all passing, lint clean.

The data engineering is in good shape. The application security around it is not yet
attached.

---

## What has to happen, in order

Each step leaves the system working, and the order is not arbitrary — later items are
pointless without earlier ones.

| # | Work | Why here |
|---|---|---|
| 1 | Add `tenant_id` to the six tables that lack it; make it non-nullable with no default | Nothing can be scoped until there is something to scope by |
| 2 | Apply the tenant dependency at the router level, and a startup assertion that every route has it | One place, not 45. The assertion is what stops the next endpoint from being added open |
| 3 | Scope every lookup: `db.get(X, id)` becomes a select filtered by both id and tenant | Closes C2 |
| 4 | Cross-tenant tests: sign in as A, ask for B's objects by id, assert 404 across every router | Closes C6 — this is the test that proves the product, not the function |
| 5 | Pass the tenant into `get_engine()`; call `provision_tenant()` when a tenant is created | Closes C4 and C5 |
| 6 | Wire the audit recorder into every mutating endpoint | Closes H2 |
| 7 | Rate limits, upload caps, query timeout, security headers | Closes H3, H4, H7, M2 |
| 8 | Delete `ASSARIUM_DATABRICKS_TOKEN` and the three shared-catalog settings | Closes H5, H6 |

Steps 1 to 5 are the ones that stand between this and a system that can hold two customers
safely. They are mechanical rather than difficult: the design is already decided, the
isolation model is already chosen, and the code that implements it already exists.

---

# Resolution

Everything above from C1 to H7, plus M1 and M2, is now closed. What follows is what was
built, and — more usefully — what the work turned up that the audit had not predicted.

## The findings, and what closed them

| # | Finding | How it is closed now |
|---|---|---|
| C1 | No endpoint required authentication | Tenant dependency declared once per router, plus `assert_routes_are_guarded` at startup: **50 routes checked, 48 tenant-scoped, 2 public** |
| C2 | No query was tenant-scoped | Every lookup goes through `TenantScope`. There is no unscoped path left in a router |
| C3 | Six tables had no tenant column | `TenantOwned` mixin on all of them, non-nullable, no default |
| C4 | Every tenant shared one warehouse | `get_engine(tenant_id)` at every call site; the Databricks engine now **refuses to build without a tenant** rather than falling back to a shared catalog |
| C5 | Catalog provisioning was dead code | Reachable, and the engine derives each tenant's catalog from its slug |
| C6 | No test proved isolation | 59 tests in `test_tenant_isolation.py`: as tenant A, ask for B's objects by id across every router, assert 404 |
| H1 | The boot guard gave false confidence | It now checks wiring, not just configuration — and refuses to start if route enumeration returns implausibly few routes |
| H2 | Audit log was empty by construction | `AuditMiddleware` records every mutation and every export automatically |
| H3 | No rate limiting | Sliding-window limits per caller, with a smaller budget for expensive paths |
| H4 | Unbounded upload | 512 MB and 50 files, counted from bytes written rather than a declared length |
| H5 | PAT alongside OAuth | `ASSARIUM_DATABRICKS_TOKEN` deleted from config, engine and `.env.example` |
| H6 | Two isolation models in config | The three shared-catalog settings are gone |
| H7 | Query timeout never applied | `Engine.run_bounded` enforces it and cancels server-side |
| M1 | Engine cache evicted without closing | Explicit LRU that closes what it evicts |
| M2 | No security headers | Seven headers on every response, including unhandled 500s |

**389 unit tests and 55 end-to-end checks pass. Lint and typecheck are clean.**

## What the work found that the audit had not

These are the more interesting half, because each one was invisible to the tests that
existed at the time.

**The route guard passed by inspecting nothing.** This FastAPI version includes routers
lazily — `include_router` leaves a placeholder and resolves the real routes later — so
walking `app.routes` found one route and declared victory. The first version of the guard
reported success while all 48 routes were unguarded. It now recurses into included routers
*and* refuses to start if it finds fewer routes than the application is known to have. A
guard that can pass on an empty set is not a guard, and this one proved it.

**A `Session` can stand in for a `TenantScope` and do an unscoped lookup.** Both have a
`.get(Model, id)` method with the same signature, so `_load_connection(scope.db, id)`
type-checks at run time, reads correctly, and silently skips the tenant filter. It
happened three times during the refactor. One of them let tenant B create a schedule on
tenant A's connection — a **201, not a 404**, found only because a cross-tenant test
existed for that route. There is now an explicit type assertion at each helper boundary.

**Isolation tests alone are not enough.** Every one of them asserts a 404 for the wrong
tenant — and a handler that raises before reaching its own logic returns 404 for
everybody, which passes all of them. Three handlers were broken that way and the suite
stayed green. `TestOwnTenantStillWorks` asserts the other half: for its own tenant, each
endpoint actually answers.

**Rate limiting throttled the platform's own progress bar.** `/runs` was classified as
expensive, which is right for the POST that starts a pipeline and wrong for the GET a
client polls hundreds of times while watching one. The classification is now method-aware.

**The rate limiter could not see the tenant it was keying on.** The tenant is resolved by
a dependency, which runs *inside* the route — after the limiter has already decided. It
would have silently keyed everything on IP forever. It now keys on the presented
credential, so two customers behind one corporate NAT get separate budgets.

**`AuditEntry.id` had no default**, so the first audit insert would have failed. The
module had never been called, so nothing had ever noticed.

**Connection names were globally unique**, meaning the first customer to create one called
"Salesforce" stopped every other customer from doing the same — and leaked the existence
of their connection through the error.

**A stale database failed at request time, not at startup.** `create_all` adds missing
tables and never alters existing ones, so columns added later are simply absent and the
first query that needs one fails with something that looks like a feature bug.
`assert_schema_matches` now refuses to start and names the missing columns.

## What is deliberately still open

**Database migrations.** The drift detector turns a stale schema into a clear startup
failure with the fix named, which is a large improvement on a 500 at request time. It is
not a migration tool. Before the first deployment that has data worth keeping, this needs
Alembic with a baseline revision — that is a decision about the production database rather
than a code change, so it is flagged rather than guessed at.

**Rate limits are per process.** With several replicas the effective limit is the
configured one times the replica count. That is fine for what it defends against — a stuck
retry loop, an accidental scrape — and not fine as a billing quota. A shared counter is
the upgrade when limits become contractual.

**The identity module is built but not the active path.** Password, passkey/TOTP, sessions
with rotation and reuse detection, invitations and domain verification are implemented and
covered by 103 tests. Entra remains the configured sign-in path, as asked. Switching over
is a configuration change plus the SAML/SCIM adapter decision in `IDENTITY_AND_LIFECYCLE.md`.

**Real Azure wiring is still blocked on the infrastructure checklist** — the service
principal, the Key Vault role assignment and the metadata database all have to exist
before the first real connection can be made.

---

# Feature gaps closed after the audit

Researched against what comparable platforms treat as table stakes in 2026 — governed
semantic layer, column-level lineage, data observability, catalog and policy automation —
then measured against what Assarium had. Two gaps were real and neither was cosmetic.

## PII was detected and never enforced

Profiling has classified personal data since the beginning: emails, phone numbers, names,
national ids, dates of birth. Every consumer of that classification did the same thing
with it — added a note.

```python
if attribute.contains_pii:
    notes.append(f"{attribute.label} was detected as personal data. "
                 "Grouping by it puts identifiable values in the result.")
```

A viewer could group by a customer's name and read the names. The platform's response was
to say it had noticed. That is the worst of the three possible positions for a regulator,
because the finding proves you knew.

**Now enforced**, with three properties that matter more than the masking itself:

- **The mask is applied before grouping, not after.** Masking only the label leaves one
  row per person: the row count is a headcount, the ordering says which of them is
  largest, and a filter narrows it to an individual. Masking the expression that is both
  selected and grouped means people who mask alike collapse into one row.
- **Filters on a masked column are refused rather than masked.** Equality on a hidden
  column is an oracle — `WHERE email = 'someone@example.com'` returns rows or it does
  not, and either answer identifies the person without ever displaying them.
- **Omitting the permission masks rather than reveals.** A call site that forgets to pass
  permissions gets masked data. The absence of an argument is invisible, so it cannot be
  the permissive case.

Strategies differ by kind on purpose: an email keeps its domain, because "how many of our
customers are at acme.com" needs no names; a card number or national id keeps nothing,
because no analysis needs part of one and every partial is a step towards the whole.
`pii:view` is held by owners and admins, not by analysts or viewers.

26 tests, run against real rows rather than against the generated SQL — a mask that
appears in the query text and does not survive execution is not a mask.

## There was no lineage at all

The product's central claim is that any number can be traced back to the bytes it came
from. Nothing implemented it.

Built from `PipelineStep.sql` — the SQL the pipeline actually ran, which it already
records for exactly this reason. That distinction matters: this is lineage of what ran,
not of what a model file says should run, so a drifted schema or a hand-edited step shows
up rather than being papered over.

Both directions:

- **Provenance** — a dashboard tile traces to its measure, to the gold column, the silver
  column, the bronze column, and the source object, with the transform recorded at each
  hop (`sum(amount_usd)`, `CAST("amount_usd" AS DECIMAL(38,2))`, `landed unchanged`).
- **Impact** — on the seeded data, renaming one source column reports **9 dashboard tiles
  and 2 measures** that would break. Before the change lands, rather than after somebody
  notices a tile is empty.

Three things the implementation had to get right, all found by running it against real
data rather than the synthetic SQL it passed first:

- **A schema is not optional.** The silver step reads through three nested `SELECT *`
  subqueries — the quality-check wrapper — and a `*` says nothing about where its columns
  came from. Without the table's real column list every edge resolved to `*` and the graph
  was decorative. With it, the same schema also lets a bare `SELECT *` step be expanded
  rather than refused.
- **Qualified names had to be reduced.** `silver.main."customers_csv"` and `customers_csv`
  were being filed as two different nodes, which broke the chain at every layer boundary.
- **A designed no-op is not a gap.** A gold step that carries an already-aggregated table
  through has no SQL because nothing was transformed. Reporting it as unreadable both lost
  its edges and cried wolf about coverage; it now produces identity edges.

Coverage is reported on every answer. A lineage graph that silently omits what it could
not parse is worse than one that says so — it looks complete, and the missing part is
exactly the part somebody went looking for.

The UI draws it as ordered layers rather than a force-directed graph. Lineage in a
medallion pipeline is inherently ordered, and that order is what the reader is following;
it does not survive a spaghetti of crossing edges. Selecting a node dims everything it is
not connected to, because a view showing every edge at once answers nobody's question.

23 tests, including the exact nested-wrapper SQL shape that broke the first version.

**438 unit tests, 55 end-to-end. Lint and typecheck clean.**

## Considered and not built

Named rather than silently skipped, with the reason.

- **Freshness SLAs and volume-anomaly alerting.** The watermarks, run history and alerting
  webhook needed for it all exist; what is missing is the per-dataset expectation ("this
  should arrive by 07:00", "a 40% drop in rows is not normal") and the learned baseline to
  judge against. That is the next largest gap and the one worth doing next.
- **Metric certification badges.** Cheap and useful, but it is a governance workflow —
  who certifies, on what evidence, and what a lapsed certification means — rather than a
  field on a model, and inventing that workflow without the customer is guesswork.
- **Scheduled report delivery.** Needs an email or Slack integration decision that belongs
  with the infrastructure, not with the application.
