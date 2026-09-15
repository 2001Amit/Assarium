# Assarium

A self-serve data platform. Connect a source, and it profiles the data, works out which
medallion layer it is already at, refines it, builds a governed semantic model, and
generates dashboards — without a data analyst in the loop.

The point of difference: **it refuses questions it cannot answer correctly** rather than
answering them approximately. Every number traces back to the SQL that produced it.

## Run it

```bash
./assarium start
```

Then open **http://localhost:3000**.

`./assarium` works from **any directory in the repo** — the root, `apps/`, `apps/api/`
or `apps/web/`. It resolves its own location, so you never have to remember where you
are.

```bash
./assarium status    # what is running, and where
./assarium stop      # stop both
./assarium verify    # 55 end-to-end assertions
./assarium demo      # seed the demo data and leave it in place
./assarium logs      # tail the API log
./assarium api       # just the API, in the foreground
./assarium web       # just the web app, in the foreground
```

Everything is safe to re-run. If a port is busy you get told what is holding it, by name
and pid, instead of a bind error; add `--restart` to take the port over.

Poetry is not required, and is deliberately not used — the scripts run the project's own
virtualenv at `apps/api/.venv` directly. Poetry breaks itself whenever the Python it was
installed against moves, and that should not sit between you and running the product.

## Check it works

```bash
./assarium verify
```

Creates three deliberately-different CSVs, runs the whole product against them, asserts
55 things, and **cleans up after itself** so it can be run as often as you like. Run it
before any demo.

Because it cleans up, it leaves nothing to look at. To put the demo data in place and
keep it:

```bash
./assarium demo
```

## Layout

```
assarium        The one command. Symlinked into apps/, apps/api/ and apps/web/
apps/api        FastAPI backend — connectors, profiling, medallion, semantic layer, AI
apps/web        Next.js frontend
docs/           Everything below

start-api.sh    What `assarium api` runs      stop.sh     What `assarium stop` runs
start-web.sh    What `assarium web` runs      verify.sh   What `assarium verify` runs
```

## Documentation

| | |
|---|---|
| [docs/DEMO.md](docs/DEMO.md) | 12-minute walkthrough with what to say at each step |
| [docs/CREDENTIALS.md](docs/CREDENTIALS.md) | Every source: which credentials, where to get them, where they go |
| [docs/SCALE.md](docs/SCALE.md) | Measured throughput and limits |
| [docs/PLAN.md](docs/PLAN.md) | Architecture, the rules the code enforces, known gaps |

## Configuration

Copy `.env.example` to `apps/api/.env` and fill in what you need. Nothing is required to
run locally with file uploads and DuckDB.

**Source credentials do not go in `.env`.** Postgres passwords, Snowflake keys and SAS
tokens are entered in the Connect dialog in the browser, encrypted before they touch
disk, and never returned to the browser.

## Before you expose this

**There is no authentication yet.** Anyone who can reach the API has full access to every
connection and dataset. Fine on localhost; not fine on a shared network.
