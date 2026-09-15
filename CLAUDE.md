# Assarium — working notes for coding agents

A self-serve data platform. You connect a source, it profiles the tables, decides whether
each one is raw, cleaned or report-ready, refines it, and serves governed metrics to
dashboards and a chat interface.

---

## Commands

Everything runs from the repo root unless stated.

```bash
./start-api.sh              # API on :8000  (--restart to reclaim the port)
./start-web.sh              # UI on :3000
./verify.sh                 # 55 end-to-end checks against a running API
```

Tests and checks — note these use the venv directly, not Poetry:

```bash
cd apps/api && ./.venv/bin/python -m pytest -q
cd apps/api && ./.venv/bin/python -m ruff check app tests
cd apps/web && npx tsc --noEmit
```

**Poetry is broken on this machine and is not the entry point.** It breaks itself
whenever the Python it was installed against moves. Use `.venv/bin/python` directly.

---

## The rules that are not negotiable

These are not style preferences. Each one is here because breaking it caused a real bug.

**Tenancy comes from the session, never from the request.** Every tenant-owned row is read
through `TenantScope` (`app/tenancy/scope.py`). There is no `db.get(Model, id)` in a
router and there must never be one again — that was 45 endpoints serving any customer's
data to any caller. A row belonging to another tenant is `404`, never `403`: a 403
confirms the row exists, which turns every id field into an enumeration oracle.

**A `Session` is not a `TenantScope`.** Both have `.get(Model, id)` with the same
signature, so passing the session by mistake type-checks, reads correctly, and silently
skips the tenant filter. This has happened three times. `TenantScope.assert_is_scope`
exists because of it.

**Quality rules evaluate raw bronze, before any cast.** `TRY_CAST` turns a bad value into
NULL, so a rule that runs after the cast inspects a clean column and quarantines nothing —
the exact silent loss quarantine exists to prevent.

**Refuse rather than approximate.** A query whose joins would fan out is declined with a
reason. A wrong number that looks right is worse than no number.

**Never let a name decide anything.** A table called `gold_sales` holding text dates is
bronze. Classification reads evidence; naming is a hint at most.

**Fail closed.** A missing permission means masked data, not unmasked. An unresolved
tenant raises rather than returning `None`. The absence of an argument is invisible, so it
must never be the permissive case.

---

## Conventions

Look at `app/orchestration/alerting.py` before writing a new module — it is the house
style.

- `from __future__ import annotations` at the top of every module.
- Module docstring says **why the module exists and what it refuses to do**, not what its
  functions are called. Name the decision and defend it.
- `@dataclass(frozen=True, slots=True)` for value objects. `StrEnum` for closed sets.
  `Protocol` for interfaces.
- Comments explain reasoning, never mechanics. `# increment the counter` is noise;
  `# counted from bytes written, not a declared length — the length is the client's claim`
  is the kind that earns its line.
- British spelling in prose (`behaviour`, `normalise`). Identifiers stay as they are.
- Line length 100. Ruff is the authority; run it rather than guessing.

---

## Testing

**A test name is a sentence about behaviour**, not a label:
`test_a_null_moving_between_columns_is_detected`, not `test_nulls`.

**Test the incident, not the happy path.** The happy path is table stakes. What is worth a
test is the thing that would cost something: a login form that tells a stranger which
emails belong to customers, an organisation left with no owner, a stolen token that still
works.

**Prove the test can fail.** A test written after the fix, which passes immediately, has
demonstrated nothing. Write it first and watch it go red.

**Isolation tests need a happy-path twin.** Every cross-tenant test asserts a 404 — and a
handler that crashes before reaching its own logic returns 404 for everyone, passing all
of them. `TestOwnTenantStillWorks` exists because exactly that happened.

---

## Layout

```
apps/api/app/
    tenancy/      TenantScope — the one way tenant-owned rows are read
    auth/         token validation, the get_scope dependency
    api/          routers; guard.py refuses to boot if a route is unguarded
    connectors/   one driver per source system
    profiling/    column profiling, PII detection, relationship inference
    medallion/    classification and the bronze/silver/gold pipeline
    quality/      declarative rules, quarantine routing
    semantic/     governed model, query compiler, PII masking
    lineage/      column-level lineage parsed from the SQL that ran
    dashboards/   tile types, generation, execution
    orchestration/ schedules, locking, the runner, alerting
    engine/       DuckDB (tests) and Databricks (production) behind one interface
```

---

## Things that will bite you

- **DuckDB is a test fixture, not a deployment target.** The production engine is
  Databricks. Anything written for one must compile on both — `engine.cast_text()` exists
  because DuckDB says `VARCHAR` and Spark says `STRING`.
- **`create_all` never alters an existing table.** A column added to a model after first
  run is simply absent. `assert_schema_matches` turns that into a startup failure instead
  of a confusing 500. There is no migration tool yet.
- **String replacements that silently miss.** Ruff reformats on save, so an edit anchored
  on pre-formatted text matches nothing and reports success. Assert on every replacement.
- **`synchronize_session` on a bulk UPDATE did not work here.** Revocation goes through
  ORM objects instead. Row counts are tiny; there was nothing to optimise.
