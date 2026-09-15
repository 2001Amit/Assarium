# Scale — measured, not estimated

Every number below was produced by `apps/api/tests/scale_test.py` on a MacBook
(Apple silicon, 16 GB), DuckDB engine, single API process. Reproduce with:

```bash
cd apps/api && poetry run python tests/scale_test.py 100000 1000000 10000000
```

## What one table costs

| Rows | CSV | Upload | Profile | Pipeline (bronze→silver→gold) | Dashboard, 8 tiles | Warehouse on disk | Server RSS |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 K | 10 MB | 0.0 s | 1.3 s | **1.0 s** | 0.05 s | 19 MB | 262 MB |
| 1 M | 97 MB | 0.3 s | 1.2 s | **4.1 s** | 0.17 s | 54 MB | 503 MB |
| 10 M | 975 MB | 5.6 s | 1.3 s | **38.1 s** | 2.07 s | 411 MB | 707 MB |

Pipeline breakdown at 10 M rows: ingest 27.9 s, silver refine 9.3 s, gold build 0.7 s.

## How to read that

**Ingest dominates, and it scales linearly.** ~360 K rows/second, because rows are
streamed through Arrow rather than materialised in Python. Memory stays flat as row
count grows — 10× the data cost 1.4× the RSS.

**Profiling is constant-time.** It samples 50 K rows regardless of table size, so a
10 M-row table profiles as fast as a 100 K-row one. Where the source can answer cheaply,
exact null and distinct counts are pushed down as a single aggregate query instead.

**Gold stays small.** Aggregation collapses 10 M rows to ~2,000, which is why dashboards
stay quick — tiles read the summary, not the fact table.

**Storage compresses ~2.4×** versus source CSV (975 MB → 411 MB across all three layers,
holding the data three times over).

## Where it stops

| Limit | Value | Why, and what to do |
|---|---|---|
| Comfortable ceiling, DuckDB | **~50 M rows / ~5 GB per table** | Single-node. Beyond this, switch `ASSARIUM_ENGINE=databricks`. |
| Object-store preview | **256 MB per object** | ADLS/S3/SharePoint files are fetched whole for preview. Larger objects still *ingest* fine — the cap is on browsing. |
| Profiling sample | **50 K rows** | `ASSARIUM_PROFILE_SAMPLE_ROWS`. Stats above this are estimates, and the UI labels them as such. |
| Exact-count pushdown | **20 M rows, 80 columns** | Above this, profiling stays on the sample rather than issuing a full scan against your production warehouse unasked. |
| Query result | **50 K rows** | `ASSARIUM_QUERY_ROW_LIMIT`. |
| Salesforce ingest | **2,000 records/call** | Salesforce's own paging limit; large objects take proportionally longer. |
| Concurrent users | **~10–15 comfortable** | See below. |

## Concurrency

Measured on the 10 M-row dashboard:

| Concurrent dashboard loads | Result | Wall time |
|---:|---|---:|
| 4 | 4/4 clean | 7.3 s |
| 12 | 12/12 clean | 21.6 s |

This is *correct* but not *fast* under load, because 12 simultaneous scans of a 10 M-row
table compete for the same cores. In practice dashboards read gold tables (~2,000 rows)
and are near-instant; the numbers above are the worst case.

**A bug was found and fixed here.** A single DuckDB connection was shared across
FastAPI's threadpool, and concurrent requests interleaved results — at 4 users only 1
request came back clean. Each call now takes its own cursor over the same database
instance. Re-verified at 4/4 and 12/12.

## Choosing an engine

**DuckDB** (default) suits a laptop demo, a single team, tables under ~50 M rows, and
anything where you want zero cloud dependency. It holds a single-writer lock per file,
so run **one API process**.

**Databricks** suits multi-user deployments, tables past 50 M rows, data already in Unity
Catalog, and workloads needing separate compute scaling. Set `ASSARIUM_ENGINE=databricks`
plus the host, HTTP path and token — no other code or configuration changes.

One caveat on the Databricks engine: writes go through multi-row `INSERT`, which is
portable and needs no extra grants but is slow for bulk loads. Staging Parquet in a
Unity Catalog volume and running `COPY INTO` would be substantially faster, and needs
volume privileges the engine does not currently assume it has.

## Making it faster

1. **Pre-aggregate into gold** — already automatic, and the main reason dashboards are quick.
2. **Filter early** — dashboard filters compile into `WHERE`, so they cut scan volume.
3. **Raise `ASSARIUM_PROFILE_SAMPLE_ROWS`** only if you need tighter estimates; it is the
   one setting that trades time for accuracy.
4. **Move the metadata store to Postgres** (`ASSARIUM_DATABASE_URL`) before adding users —
   SQLite is the other single-writer component.
