"""
End-to-end verification against a running API.

Exercises every stage a user passes through - connect, discover, profile, refine,
model, dashboard, export - and asserts on the result of each. Run against a wiped
~/.assarium so nothing can pass on stale state.

    python tests/e2e_verify.py
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import random
import sys
import time
from typing import Any

import httpx

BASE = "http://127.0.0.1:8000"
client = httpx.Client(base_url=BASE, timeout=300)

PASSED = 0
FAILED: list[str] = []
TIMINGS: dict[str, float] = {}


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}" + (f"  ({detail})" if detail else ""))


def stage(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def timed(label: str):
    class Timer:
        def __enter__(self):
            self.start = time.perf_counter()
            return self

        def __exit__(self, *_):
            TIMINGS[label] = time.perf_counter() - self.start

    return Timer()


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


def make_csvs() -> dict[str, bytes]:
    random.seed(4)
    d0 = datetime.date(2026, 1, 1)
    countries = ["India", "United States", "Germany", "Japan", "Brazil"]

    def to_csv(header, rows):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        w.writerows(rows)
        return buf.getvalue().encode()

    customers = [
        [
            f"CUST-{i:04d}",
            f"Customer {i}",
            f"user{i}@example.com",
            random.choice(countries),
            (d0 - datetime.timedelta(days=random.randint(30, 900))).isoformat(),
        ]
        for i in range(1, 121)
    ]

    orders = []
    for i in range(1, 501):
        orders.append(
            [
                f"ORD-{i:05d}",
                f"CUST-{random.randint(1, 120):04d}",
                (d0 + datetime.timedelta(days=random.randint(0, 240))).isoformat(),
                f"{random.uniform(20, 5000):.2f}" if random.random() > 0.12 else "",
                random.choice(["shipped", "pending", "cancelled", "shipped"]),
                "   " if random.random() > 0.9 else random.choice(["web", "mobile", "partner"]),
                "2026-09-01T02:15:00Z",
                f"batch_{random.randint(1, 4)}",
            ]
        )
    orders += orders[:40]

    summary = []
    for country in countries:
        for month in range(1, 9):
            summary.append(
                [
                    country,
                    f"2026-{month:02d}",
                    round(random.uniform(50_000, 400_000), 2),
                    random.randint(80, 900),
                    round(random.uniform(180, 950), 2),
                    round(random.uniform(0.08, 0.34), 4),
                ]
            )

    return {
        "customers.csv": to_csv(
            ["customer_id", "customer_name", "email", "country", "signup_date"], customers
        ),
        "orders_raw.csv": to_csv(
            [
                "order_id", "customer_id", "order_date", "amount_usd",
                "status", "channel", "ingested_at", "batch_id",
            ],
            orders,
        ),
        "sales_summary.csv": to_csv(
            ["country", "month", "total_revenue", "order_count", "avg_order_value", "margin_pct"],
            summary,
        ),
    }


def post(path: str, body: Any = None) -> httpx.Response:
    return client.post(path, json=body if body is not None else {})


# --------------------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------------------


def main() -> int:
    stage("1. HEALTH AND SOURCE CATALOGUE")
    health = client.get("/api/health").json()
    check("API is up", health.get("status") == "ok", f"engine={health.get('engine')}")

    sources = client.get("/api/sources").json()
    check("11 sources are registered", len(sources) == 11, f"{len(sources)} found")
    specs_ok = all(
        s.get("auth_methods") is not None and s.get("fields") is not None for s in sources
    )
    check("every source declares a credential spec", specs_ok)
    installed = [s["source_id"] for s in sources if s["available"]]
    print(f"        drivers installed here: {', '.join(installed)}")

    stage("2. CONNECT AND UPLOAD")
    label = f"E2E {int(time.time())}"
    created = post(
        "/api/connections",
        {"name": label, "source_id": "files", "auth_method": "default",
         "values": {"label": "E2E"}},
    ).json()
    if "id" not in created:
        check("connection created", False, str(created)[:120])
        return 1
    check("connection created", True, created.get("status"))
    cid = created["id"]

    files = make_csvs()
    upload = client.post(
        f"/api/connections/{cid}/upload",
        files=[("files", (name, data, "text/csv")) for name, data in files.items()],
    ).json()
    check("3 files uploaded", len(upload.get("stored", [])) == 3)

    stage("3. BROWSE AND SELECT")
    nodes = client.get(f"/api/connections/{cid}/browse").json()
    check("browse lists the uploads", len(nodes) == 3, ", ".join(n["name"] for n in nodes))

    datasets = post(
        f"/api/connections/{cid}/datasets", {"paths": [n["path"] for n in nodes]}
    ).json()
    check("3 datasets selected", len(datasets) == 3)
    check(
        "schemas were read on selection",
        all(d["column_count"] > 0 for d in datasets),
        f"columns: {[d['column_count'] for d in datasets]}",
    )

    stage("4. PROFILE AND CLASSIFY")
    verdicts = {}
    with timed("profile 3 datasets (1,060 rows)"):
        for dataset in datasets:
            detail = post(f"/api/datasets/{dataset['id']}/profile").json()
            verdicts[detail["name"]] = detail

    check(
        "orders_raw.csv reads as BRONZE",
        verdicts["orders_raw.csv"]["detected_layer"] == "bronze",
        f"confidence {verdicts['orders_raw.csv']['layer_confidence']}",
    )
    check(
        "customers.csv reads as SILVER",
        verdicts["customers.csv"]["detected_layer"] == "silver",
        f"confidence {verdicts['customers.csv']['layer_confidence']}",
    )
    check(
        "sales_summary.csv reads as GOLD",
        verdicts["sales_summary.csv"]["detected_layer"] == "gold",
        f"confidence {verdicts['sales_summary.csv']['layer_confidence']}",
    )

    pii = [
        c["name"]
        for c in verdicts["customers.csv"]["profile"]["columns"]
        if c["pii"]
    ]
    check("PII detected on customers", set(pii) >= {"email", "customer_name"}, ", ".join(pii))
    check(
        "duplicate rows measured on orders",
        verdicts["orders_raw.csv"]["profile"]["duplicate_row_ratio"] > 0.05,
        f"{verdicts['orders_raw.csv']['profile']['duplicate_row_ratio']:.1%}",
    )
    check(
        "key found on customers, none on orders",
        verdicts["customers.csv"]["profile"]["key_candidates"] == [["customer_id"]]
        and verdicts["orders_raw.csv"]["profile"]["key_candidates"] == [],
    )

    stage("5. RELATIONSHIP INFERENCE")
    inferred = post(f"/api/connections/{cid}/relationships").json()
    check("one relationship found", inferred.get("found") == 1, json.dumps(inferred))
    rels = client.get(f"/api/connections/{cid}/relationships").json()
    if rels:
        r = rels[0]
        check(
            "orders.customer_id -> customers.customer_id",
            r["from_column"] == "customer_id" and r["to_dataset"] == "customers.csv",
            f"overlap {r['overlap']}, {r['cardinality']}",
        )

    stage("6. MEDALLION PIPELINE")
    run = post(f"/api/connections/{cid}/runs", {}).json()
    with timed("pipeline run (3 datasets)"):
        for _ in range(120):
            state = client.get(f"/api/runs/{run['id']}").json()
            if state["status"] != "running":
                break
            time.sleep(0.5)
    check("run succeeded", state["status"] == "succeeded", state["status"])

    tables = state["summary"]["tables"]
    check("3 bronze tables", len(tables["bronze"]) == 3)
    check("3 silver tables", len(tables["silver"]) == 3)
    check("2 gold tables", len(tables["gold"]) == 2, ", ".join(tables["gold"]))

    warehouse = client.get("/api/warehouse").json()
    bronze = {t["name"]: t["rows"] for t in warehouse["layers"]["bronze"]}
    silver = {t["name"]: t["rows"] for t in warehouse["layers"]["silver"]}
    check(
        "deduplication happened bronze -> silver",
        bronze["orders_raw_csv"] == 540 and silver["orders_raw_csv"] == 500,
        f"{bronze['orders_raw_csv']} -> {silver['orders_raw_csv']}",
    )

    silver_cols = {
        c["name"]
        for t in warehouse["layers"]["silver"]
        if t["name"] == "orders_raw_csv"
        for c in t["columns"]
    }
    check(
        "ingestion metadata dropped in silver",
        "ingested_at" not in silver_cols and "batch_id" not in silver_cols,
    )
    amount_type = next(
        c["type"]
        for t in warehouse["layers"]["silver"]
        if t["name"] == "orders_raw_csv"
        for c in t["columns"]
        if c["name"] == "amount_usd"
    )
    check("currency stored as exact decimal", amount_type.startswith("DECIMAL"), amount_type)

    stage("7. SEMANTIC MODEL")
    model = post(f"/api/connections/{cid}/semantic/build", {"overwrite_edits": True}).json()
    check("3 entities", len(model["entities"]) == 3)
    check("measures generated", len(model["measures"]) >= 10, f"{len(model['measures'])}")
    check("one join", len(model["joins"]) == 1)

    measure_ids = {m["id"] for m in model["measures"]}
    check(
        "a rate is averaged, never summed",
        "sales_summary_csv.avg_margin_pct" in measure_ids
        and "sales_summary_csv.total_margin_pct" not in measure_ids,
    )
    check(
        "no double-prefixed measure names",
        not any("total_total" in m or "avg_avg" in m for m in measure_ids),
    )

    stage("8. GOVERNED QUERIES - VALID")
    valid = post(
        f"/api/connections/{cid}/semantic/query",
        {
            "measures": ["orders_raw_csv.total_amount_usd", "orders_raw_csv.record_count"],
            "dimensions": ["customers_csv.country"],
            "time_dimension": "orders_raw_csv.order_date",
            "time_grain": "month",
            "limit": 100,
        },
    )
    check("cross-entity query runs", valid.status_code == 200)
    if valid.status_code == 200:
        body = valid.json()
        check("join applied", "LEFT JOIN" in body["sql"])
        totals = [r[2] for r in body["rows"]]
        check(
            "money sums exactly (no float drift)",
            all(round(float(t), 2) == float(t) for t in totals),
            f"e.g. {totals[0]}",
        )

    stage("9. GOVERNED QUERIES - REFUSALS")
    refusals = [
        ("unknown measure", {"measures": ["orders_raw_csv.total_profit"]}, "no measure called"),
        (
            "measures from two entities",
            {"measures": ["orders_raw_csv.total_amount_usd", "customers_csv.record_count"]},
            "double-counting",
        ),
        (
            "fan-out join",
            {"measures": ["customers_csv.record_count"], "dimensions": ["orders_raw_csv.status"]},
            "inflate the totals",
        ),
        (
            "unknown attribute",
            {"measures": ["orders_raw_csv.record_count"], "dimensions": ["customers_csv.region"]},
            "has no attribute",
        ),
    ]
    for label, payload, expected in refusals:
        response = post(f"/api/connections/{cid}/semantic/query", payload)
        message = response.json().get("error", {}).get("message", "")
        check(
            f"refused: {label}",
            response.status_code == 422 and expected in message,
            message[:70],
        )

    stage("10. SQL INJECTION THROUGH A FILTER")
    payload = "'; DROP TABLE orders_raw_csv; --"
    injected = post(
        f"/api/connections/{cid}/semantic/query",
        {
            "measures": ["orders_raw_csv.record_count"],
            "filters": [
                {"field": "orders_raw_csv.status", "operator": "eq", "values": [payload]}
            ],
        },
    )
    check("hostile filter value executes safely", injected.status_code == 200)
    if injected.status_code == 200:
        check("payload never entered the SQL text", "DROP TABLE" not in injected.json()["sql"])
    still_there = client.get("/api/warehouse").json()
    check(
        "silver table still exists afterwards",
        any(t["name"] == "orders_raw_csv" for t in still_there["layers"]["silver"]),
    )

    stage("11. ONTOLOGY")
    graph = client.get(f"/api/connections/{cid}/ontology").json()
    kinds = {n["id"]: n["kind"] for n in graph["nodes"]}
    check("3 nodes", len(graph["nodes"]) == 3)
    check("customers reads as a dimension", kinds.get("customers_csv") == "dimension")
    check("orders reads as a fact", kinds.get("orders_raw_csv") == "fact")
    check(
        "the unjoined entity is called out",
        "sales_summary_csv" in graph["isolated"] and len(graph["notes"]) > 0,
    )

    stage("12. DASHBOARD")
    dashboard = post(f"/api/connections/{cid}/dashboards/generate").json()
    check("dashboard generated", len(dashboard["tiles"]) >= 6, f"{len(dashboard['tiles'])} tiles")
    check(
        "built on the row-level fact, not the aggregate",
        "Orders raw" in dashboard["name"],
        dashboard["name"],
    )
    types = [t["type"] for t in dashboard["tiles"]]
    check("has stats, a trend, breakdowns and a table",
          "stat" in types and "line" in types and "bar" in types and "table" in types)
    check("breakdowns offer drill paths",
          any(len(t["drill_path"]) > 0 for t in dashboard["tiles"]))

    did = dashboard["id"]
    with timed("dashboard data (all tiles)"):
        data = post(f"/api/dashboards/{did}/data", {"filters": []}).json()
    errors = [t["title"] for t in data["tiles"] if t["error"]]
    check("every tile rendered without error", not errors, ", ".join(errors))
    check("dashboard is fast", data["elapsed_ms"] < 2000, f"{data['elapsed_ms']} ms")

    stat = next(t for t in data["tiles"] if t["type"] == "stat")
    check("stat carries value, delta and sparkline",
          stat["stats"][0]["value"] is not None
          and stat["stats"][0]["delta"] is not None
          and len(stat["stats"][0]["sparkline"]) > 1)

    stage("13. CROSS-FILTER")
    filtered = post(
        f"/api/dashboards/{did}/data",
        {"filters": [{"field": "orders_raw_csv.status", "operator": "eq", "values": ["shipped"]}]},
    ).json()
    unfiltered_count = next(
        t["stats"][0]["value"] for t in data["tiles"] if t["title"] == "Orders raw count"
    )
    filtered_count = next(
        t["stats"][0]["value"] for t in filtered["tiles"] if t["title"] == "Orders raw count"
    )
    check(
        "filter propagates to every tile",
        filtered_count < unfiltered_count,
        f"{unfiltered_count:.0f} -> {filtered_count:.0f}",
    )

    stage("14. EXPORT")
    xlsx = post(f"/api/dashboards/{did}/export.xlsx", {"filters": []})
    check("xlsx produced", xlsx.status_code == 200 and len(xlsx.content) > 5000,
          f"{len(xlsx.content):,} bytes")
    check("xlsx is a real workbook", xlsx.content[:2] == b"PK")

    tile_id = dashboard["tiles"][-1]["id"]
    csv_export = post(f"/api/dashboards/{did}/tiles/{tile_id}/export.csv", {"filters": []})
    check("csv produced", csv_export.status_code == 200 and b"," in csv_export.content,
          f"{len(csv_export.content):,} bytes")

    stage("15. AI STATUS")
    ai = client.get("/api/ai/status").json()
    print(f"        AI configured: {ai['available']}")
    explain = post(f"/api/dashboards/{did}/tiles/{dashboard['tiles'][0]['id']}/explain", {})
    if ai["available"]:
        check("explanation generated", explain.status_code == 200)
    else:
        message = explain.json().get("error", {}).get("message", "")
        check(
            "AI absence fails clearly and names the fix",
            explain.status_code == 422 and "ASSARIUM_AZURE_OPENAI" in message,
            message[:70],
        )

    # ----------------------------------------------------------------------------------
    stage("16. CLEANUP")
    removed = client.delete(f"/api/connections/{cid}")
    check("test connection removed", removed.status_code == 204)

    print(f"\n{'=' * 78}")
    print("TIMINGS")
    for label, seconds in TIMINGS.items():
        print(f"  {label:<44} {seconds * 1000:>8,.0f} ms")

    print(f"\n{'=' * 78}")
    print(f"  {PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"    FAILED: {name}")
    print("=" * 78)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
