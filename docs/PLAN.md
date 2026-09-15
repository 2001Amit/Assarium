# Assarium — Self-Serve Data Platform

**Goal:** remove the organisation's dependency on a data analyst and on Power BI. A user connects a
source, picks what matters, and the platform does discovery → quality profiling → medallion
refinement → semantic modelling → dashboards → natural-language analysis, with governed exports.

## Locked decisions

| Area | Decision | Rationale |
|---|---|---|
| Compute | Pluggable `Engine` interface. `DuckDBEngine` (local, default) and `DatabricksEngine` (Unity Catalog + Delta) behind one contract. | Full pipeline runs on a laptop today; production swaps by config, no call-site changes. |
| Frontend | Next.js 15 App Router, TypeScript, Tailwind v4, custom design system | Server components for heavy metadata screens; no component-kit look. |
| Backend | FastAPI, Python 3.11, Poetry | Every connector SDK and profiling library is Python-native. |
| LLM | Azure OpenAI behind an `LLMProvider` interface | Stays in tenant. v1 GA API (`{endpoint}/openai/v1`, no `api-version`). |
| NL analysis | Semantic-layer compilation, **not** freehand text-to-SQL | Text-to-SQL fails as plausible-but-wrong; a semantic layer fails as an error. |
| Sequencing | Connector breadth first | User's call. |

## Design language

Enterprise, restrained, information-dense. No emojis anywhere — Lucide icons only.
Neutral zinc surface scale, single desaturated indigo accent, muted layer-identity hues
(bronze/silver/gold are *never* literal orange/grey/yellow). Tabular numerals everywhere,
4px spacing grid, 1px hairline borders instead of shadows, motion under 150ms.

## Phases

- **P0 Foundations** — done. Monorepo, config, Fernet secret store, metadata DB, design tokens.
- **P1 Connectors** — done. `Connector` ABC, declarative `CredentialSpec` driving the whole connection UI, and 11
  drivers: Postgres, MySQL, SQL Server, Snowflake, BigQuery, Databricks SQL, ADLS Gen2, S3, SharePoint,
  Salesforce, file upload.
- **P2 Discovery** — done. Column browser, multi-select, column profiling with exact-count pushdown,
  shadow-type detection, PII detection, key ranking, declared + inferred relationships.
- **P3 Medallion** — done. `Engine` abstraction (DuckDB with one database file per layer; Databricks over
  Unity Catalog), Arrow streaming ingestion, profile-driven silver refinement, default gold aggregates,
  per-step run lineage with the exact SQL.
- **P4 Semantic + Ontology** — done. Entities/attributes/measures derived from silver, governed metric
  compiler with fan-out protection and bound parameters, force-directed ontology graph, live query builder.
- **P5 Dashboards** — done. Auto-composed from the model (stat row, trend, breakdowns, detail),
  validated chart palette, cross-filter, drill-down, per-tile table/SQL/CSV, XLSX workbook with definitions.
- **P6 AI** — done. Azure OpenAI v1 GA surface behind a provider interface (API key or Entra ID),
  per-tile explanations, and a chat that compiles to metric queries with one compiler-guided repair round.
- **P7 Hardening** — auth, RBAC, audit log, tests, containerisation, docs.

## Design rules the code enforces

1. **Naming never decides a layer.** It only reinforces a verdict the data already supports; locked by
   adversarial tests in `tests/test_classifier.py`.
2. **The layers are a ladder, not three buckets.** Cleanliness evidence establishes "left bronze";
   aggregation evidence separates silver from gold. Gold data is also clean, so those signals must not compete.
3. **Everything lands in bronze first,** even when it already looks conformed. That one extra write buys
   reproducibility: refinement can be corrected and re-run without touching the source again.
4. **Money is decimal, never float.** `DECIMAL(38, 9)` end to end.
5. **A rate is averaged, never summed.** Gold aggregates detect ratio-shaped measures and use AVG.
6. **Every generated step records its SQL,** so any number can be traced back to its derivation.
7. **A metric exists in the model or it does not exist.** There is no free-text SQL path. An unknown
   measure, an unknown attribute, measures from two unjoinable tables, or a join that would fan out and
   over-count are all refused with a reason — never answered approximately.
8. **Filter values are bound, never interpolated.** Identifiers come only from the model; values only
   ever travel as driver parameters.
9. **The semantic model is built over silver, not gold.** Silver is row-level and joinable, so a measure
   defined there answers at any grain. Defining the same measure over a pre-aggregated gold table too
   would give the organisation two ways to compute revenue.
10. **The chart palette is validated, not chosen.** Eight slots, run through the dataviz validator against
    both surfaces: lightness band, chroma floor, adjacent-pair CVD separation, normal-vision floor and
    contrast. The slot ORDER is the colourblind-safety mechanism — reordering invalidates the result.
    Three light slots sit under 3:1 contrast, so every chart tile ships a table view as relief.
11. **A rate is averaged, a total is summed, and an axis carries neither's decimals.** Formatting comes
    from the measure definition, so one figure reads the same in a table, a tile and a chat answer.
12. **The chat never sees SQL.** It emits a metric query built from ids the prompt lists verbatim; the
    compiler is the authority, and its refusal is fed back once so the model can correct itself.

## Known constraints

- **There is no authentication.** Anyone who can reach the API has full access to every connection and
  every dataset. Fine on localhost; not fine anywhere shared. This is P7 and it is the single biggest gap.
- **DuckDB holds a single-writer lock** per database file, so the local engine assumes one API process.
  SQLite, the default metadata store, is the same. Databricks and Postgres remove both limits.
- **Gold tables are derived independently of the semantic model.** The better architecture is for gold to
  be *materialisations of semantic metric queries*, so a pre-aggregated table can never drift from the
  definition it is meant to speed up. Worth doing before gold is relied on for reporting.
- **Loads are full replace, not incremental.** Every run re-reads the source in full. Fine at the scales
  measured; needs watermark/CDC handling before large recurring loads.
- **No scheduling.** Runs are triggered by hand.
- **Databricks engine writes via multi-row INSERT.** Portable and grant-free, but slow for bulk. Staging
  Parquet in a UC volume plus `COPY INTO` is the faster path when volume privileges exist.

## Verified

`apps/api/tests/e2e_verify.py` runs the whole product against a wiped `~/.assarium` and asserts 54 checks:
source catalogue, connect, upload, browse, select, profile, three-way layer classification, PII
detection, relationship inference, pipeline, dedup, metadata drop, decimal conversion, semantic build,
governed queries, four refusals, SQL injection through a filter, ontology, dashboard generation,
cross-filter, XLSX and CSV export, and AI degradation. Plus 49 unit tests. See `docs/SCALE.md` for
measured throughput and limits.

## Layer classification — evidence, not vibes

The classifier scores each table on measurable signals and reports *why*, with a confidence value the
user can override. Never a silent guess.

| Signal | Bronze | Silver | Gold |
|---|---|---|---|
| Type purity (numerics/dates held as text) | low | high | high |
| Null density in key columns | high | low | low |
| Exact duplicate row ratio | high | ~0 | ~0 |
| Declared/inferable primary key | often absent | present | present |
| Referential integrity to other selections | weak | strong | pre-joined |
| Denormalisation (width vs. cardinality) | source-shaped | normalised | wide/aggregated |
| Pre-aggregated measures + grain columns | absent | rare | present |
| Naming (`raw_`, `stg_`, `dim_`, `fact_`, `agg_`) | corroborating only | | |

Naming alone never decides a layer; it only adjusts a score already supported by data evidence.

## Connector credential requirements (researched)

Each driver ships a `CredentialSpec` that the UI renders. Notable current constraints:

- **Snowflake** — key-pair (PKCS#8 PEM + optional passphrase) is the default and only future-proof
  service auth; single-factor password sign-in is being blocked through Aug–Oct 2026. Password mode
  is offered but flagged as deprecated in the UI.
- **Salesforce** — External Client App (Connected App creation is restricted from Spring '26);
  client-credentials or JWT-bearer, plus instance URL and API version.
- **SharePoint / OneDrive** — Microsoft Graph app-only with `Sites.Selected`; certificate credential
  (client secrets are not supported against the SharePoint API surface).
- **BigQuery** — service-account JSON key or workload identity; project + location.
- **ADLS Gen2 / S3** — service principal / managed identity / SAS, and IAM role / access key respectively.
- **Databricks SQL** — host, HTTP path, PAT or OAuth M2M (service principal client id + secret).
