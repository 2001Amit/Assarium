# Running the demo

A 12-minute walkthrough that needs no cloud account. Everything runs locally on DuckDB
with three CSVs.

---

## Before the room

### 1. Start both processes

```bash
./assarium start
```

Open **http://localhost:3000**.

This works from any directory in the repo — root, `apps/`, `apps/api/` or `apps/web/`.
Check anytime with `./assarium status`. The web app proxies `/api` to port 8000, so the browser
only ever sees one origin.

These scripts use the project's own virtualenv at `apps/api/.venv` directly. They do not
go through Poetry, so a broken or missing Poetry install on the host cannot stop the app
from running — a real failure mode, since Poetry breaks itself whenever the Python it was
installed against moves. If the virtualenv is missing, `start-api.sh` creates it and
installs the dependencies on first run.

Running the commands by hand instead:

```bash
cd apps/api && ./.venv/bin/uvicorn app.main:app --port 8000
cd apps/web && npm run dev
```

### 2. Wipe any previous state

```bash
rm -rf ~/.assarium
```

This clears the metadata DB, uploads and the medallion warehouse. Restart the API after
wiping — it holds the database files open.

### 3. Make the demo data

```bash
./assarium verify    # proves the build is healthy — then cleans up after itself
./assarium demo      # seeds the same data and leaves it in place
```

Run `verify` before you present: 55 assertions in about ten seconds, and it tells you
immediately if anything is broken. Then run `demo` to put the data back, since `verify`
removes what it creates. Two uses:
it proves the build is healthy before you present, and it leaves a working connection
behind. **For a live demo, wipe again afterwards** so you can perform the steps yourself.

The three files it generates are deliberately different, and that is the whole story:

| File | What it is | What Assarium should say |
|---|---|---|
| `orders_raw.csv` | 540 rows, 40 duplicated, blank amounts, whitespace-only channels, `ingested_at` and `batch_id` still attached | **Bronze** |
| `customers.csv` | 120 clean rows, keyed on `customer_id`, has email and name | **Silver** |
| `sales_summary.csv` | 40 rows, revenue and margin already aggregated by country and month | **Gold** |

### 4. Optional — turn on the AI

Put `ASSARIUM_AZURE_OPENAI_ENDPOINT`, `ASSARIUM_AZURE_OPENAI_API_KEY` and
`ASSARIUM_AZURE_OPENAI_DEPLOYMENT` in `apps/api/.env` and restart. See
[CREDENTIALS.md](CREDENTIALS.md). Without them the sparkle and Ask tab display a clear
"not configured" message — which is a fine thing to show, but say it is deliberate.

---

## The walkthrough

### Step 1 — Sources (1 min)

Land on **Sources**. Eleven systems, grouped by kind.

> "Every system here declares what credentials it needs. The dialog is generated from
> that declaration — adding a twelfth source touches no frontend code."

Open **Snowflake**. Point at the three auth methods: key-pair marked *recommended*,
password marked *deprecated* with the reason.

> "Snowflake is blocking single-factor password sign-in for service accounts through
> 2026. The platform tells you that at the moment you'd otherwise pick the wrong one."

Close it. Open **SharePoint** — certificate is recommended, because a client secret
cannot reach the SharePoint API surface. Close.

> "That's researched per source, not a generic username-and-password form."

### Step 2 — Connect (1 min)

**File upload → name it "Ops extracts" → Save connection.**

Upload the three CSVs from step 3 above.

> "No credentials for local files. Everything else — a Postgres password, a Snowflake
> private key — is encrypted before it touches disk and is never sent back to the
> browser."

### Step 3 — Datasets: the part that matters (4 min)

Go to **Datasets**. Browse shows the three files. Tick all three → **Add to selection**.

Click **Profile all**. Then click into `orders_raw.csv`.

**This is the centrepiece. Read the evidence panel out loud.**

> "It says Bronze at 83% confidence, and here is why: 7.4% of rows are exact
> duplicates. No column identifies a row. Ingestion metadata is still attached.
> One column has values that are blank but not null."
>
> "Now look at this one." *(point at the naming signal)* "The name contains 'raw',
> which conventionally marks a bronze table — and underneath: *naming only reinforces
> the data evidence; it never decides the layer*."

That line is the argument. Make it explicitly:

> "A table called `gold_customer_mart` full of dirty row-level data is still bronze.
> A table called `raw_dump` that's clean and keyed is silver. There are tests that
> assert exactly that, because a classifier you can fool by renaming a table is a
> classifier nobody should trust."

Click `customers.csv` — Silver at 98%, and **email and customer name are flagged as
personal data** with a confidence and a basis.

Click `sales_summary.csv` — Gold, because it has pre-aggregated measures at a grain
with no row-level identifier.

Now **Detect relationships**. One join found:
`orders_raw.customer_id → customers.customer_id`, 100% value overlap, many-to-one.

> "It didn't guess from the column name. Name affinity proposed it; 100% value overlap
> confirmed it. A name alone is a guess, and value overlap alone matches every pair of
> integer columns in the warehouse."

### Step 4 — Pipeline (2 min)

Go to **Pipeline** → **Run refinement**.

Three layers fill in. Expand `orders_raw.csv` → **Refine to silver**:

- Converted 1 currency column from floating point to exact decimal
- Trimmed whitespace and turned blanks into NULL in 4 columns
- Removed 2 ingestion metadata columns: `ingested_at`, `batch_id`
- Removed exact duplicate rows (7.4% of the sample)

**540 rows in bronze → 500 in silver.**

Expand **Build gold** and show the SQL.

> "Every step records the exact statement it ran. Any number on any dashboard traces
> back to a query you can read."

Two things worth calling out:

> "The currency conversion is real. Summing 500 dollar amounts as float64 gives
> 52818.14999999999. As decimal it gives 52818.15. On a revenue dashboard that
> difference is the whole ballgame."

> "And `customers.csv` got no gold table — deliberately. It says: *this is a dimension
> or reference list; it joins to a fact rather than becoming one.*"

Click any table to preview the rows.

### Step 5 — Semantic model (2 min)

Go to **Semantic model**. Entities on the left, measures underneath each.

Point at `sales_summary` → `Average margin percent`.

> "There is no `total_margin_pct`. You cannot sum a percentage, so the platform never
> offers it. Same for average order value — average only."

Build a query: tick **Total amount USD**, **Order date**, **Status** → **Run**.
24 rows, formatted currency, and **Show SQL**.

Now break it on purpose. Tick **Customers count** together with **Total amount USD**:

> *"Those measures live on different tables and cannot be combined in one query without
> double-counting."*

> "This is the difference between this and a text-to-SQL tool. A text-to-SQL system
> would have written that join and returned an inflated number that looks completely
> reasonable on a slide. This refuses, and tells you why."

### Step 6 — Ontology (1 min)

**Ontology.** Three nodes, the join drawn between them.

Click **Orders raw** — unrelated entities dim; the panel shows rows, measures,
dimensions, what it can be trended on, and its joins.

Point at the banner:

> "*Nothing joins to Sales summary. Questions that combine it with another entity cannot
> be answered until a relationship is defined.* It tells you what it can't do."

### Step 7 — Dashboards (2 min)

**Dashboards → Generate.** Eight tiles, composed from what the model supports.

> "Nothing was configured. It picked orders as the fact — not the pre-aggregated summary
> table — because orders has a date, joins to customers, and 500 rows of grain."

**Click the "shipped" bar.** Every tile re-filters — 500 → 232, and a filter chip
appears. Click it again to clear.

**Use the drill pills** on a breakdown: `status` → `country`. Then back.

**Sparkle** on a tile — an explanation, or the "not configured" message.

**Table and SQL icons** on any chart:

> "Every chart has a table view. That's not decoration: three colours in the palette sit
> below 3:1 contrast on white, and the method I followed requires visible labels or a
> table view when that's true."

**XLSX** → open the download. First sheet is *About*: the filters applied and every
measure definition.

> "Numbers travelling without their definitions is how two teams end up arguing about
> what revenue means."

### Step 8 — Ask (1 min, only with AI configured)

**Ask** → *"What is total revenue by country this year?"*

> "It never writes SQL. It emits a metric query against the model, and the compiler
> validates it. If it invents a measure, the compiler refuses and hands the reason back
> so it can correct itself once — then it either answers or tells you what's missing."

---

## If you have 3 minutes, not 12

1. **Datasets** → `orders_raw.csv` → the evidence panel. *"It says why, and naming never decides."*
2. **Pipeline** → the silver step. *"540 → 500 rows, and here's the SQL."*
3. **Semantic model** → tick two measures from different tables. *"It refuses rather than double-counting."*
4. **Dashboards** → click a bar. *"Everything cross-filters."*

---

## Questions you will be asked

**"How big can it go?"**
10 million rows through the whole pipeline in 38 seconds on a laptop, dashboards at 2.1s.
Numbers and limits in [SCALE.md](SCALE.md).

**"Where does my data go?"**
Nowhere. Sources are read directly, refined data lands in DuckDB files under `~/.assarium`,
or in your own Databricks catalogs. The only outbound call is to Azure OpenAI, and only
if you configure it — and it receives the model's *structure* and figures, never raw rows.

**"How is it different from Power BI?"**
Three things. It classifies and repairs your data before modelling it, rather than
assuming it's clean. Measures are defined once and cannot be redefined per report. And
questions it can't answer correctly are refused rather than answered approximately.

**"Can I edit what it generated?"**
Yes — layer classifications, the semantic model, and dashboards are all editable. A
rebuild warns before overwriting hand-authored changes.

**"Is it secure?"**
Credentials are Fernet-encrypted at rest and never returned to the browser. Filter values
are bound as parameters, never string-interpolated. **There is no authentication yet** —
do not expose it beyond localhost or a trusted network until that lands.

---

## If something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| Sources page empty | API not running | Check port 8000; `curl localhost:8000/api/health` |
| `poetry: ModuleNotFoundError: No module named 'cleo'` | Host Poetry install is broken | Ignore Poetry — use `./start-api.sh`. To repair it anyway: `pipx reinstall poetry` |
| `no such file or directory: ./start-api.sh` | You are in a subdirectory | Use `./assarium start` — it works from anywhere |
| `address already in use` | Something already holds the port | `./assarium status` to see what, then `./assarium start --restart` |
| "No connections yet" after seeding | API holds a deleted database file | Restart the API after any `rm -rf ~/.assarium` |
| "driver needed" on a card | Optional driver not installed | The dialog shows the exact `pip install` command; run it in `apps/api`, then restart |
| Pipeline says "profile first" | Refinement is driven by profiling | Click **Profile all** on Datasets |
| Dashboards says "build the model first" | No semantic model | **Semantic model → Rebuild** |
| Sparkle returns a config message | Azure OpenAI not set | See [CREDENTIALS.md](CREDENTIALS.md) |
| Everything slow on first click | Cold DuckDB attach | Click once before presenting |
