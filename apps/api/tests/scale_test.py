"""
Scale test: find where this actually slows down, and where it stops.

Generates progressively larger datasets, pushes each through the whole pipeline, and
reports wall time and peak memory per stage. The point is to produce honest numbers,
not flattering ones.

    python tests/scale_test.py [rows ...]
"""

from __future__ import annotations

import csv
import datetime
import gc
import os
import random
import resource
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
client = httpx.Client(base_url=BASE, timeout=3600)


def peak_rss_mb() -> float:
    """Peak RSS of this process. Not the server's, but tracks the client-side cost."""
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux kilobytes.
    return usage / (1024 * 1024) if sys.platform == "darwin" else usage / 1024


def server_rss_mb() -> float:
    try:
        import subprocess

        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", _server_pid()],
            capture_output=True, text=True, check=True,
        )
        return int(out.stdout.strip()) / 1024
    except Exception:
        return 0.0


def _server_pid() -> str:
    import subprocess

    out = subprocess.run(
        ["pgrep", "-f", "uvicorn app.main:app"], capture_output=True, text=True
    )
    return out.stdout.split()[0] if out.stdout.strip() else "0"


def generate_csv(path: Path, rows: int) -> int:
    """A realistically messy orders extract: text amounts, blanks, duplicates."""
    random.seed(7)
    d0 = datetime.date(2025, 1, 1)
    statuses = ["shipped", "pending", "cancelled", "shipped", "delivered"]
    channels = ["web", "mobile", "partner", "retail"]
    regions = ["EMEA", "AMER", "APAC", "LATAM"]

    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["order_id", "customer_id", "order_date", "amount_usd", "status",
             "channel", "region", "quantity", "discount_pct", "ingested_at"]
        )
        for i in range(rows):
            writer.writerow([
                f"ORD-{i:09d}",
                f"CUST-{random.randint(1, max(2, rows // 40)):07d}",
                (d0 + datetime.timedelta(days=random.randint(0, 730))).isoformat(),
                f"{random.uniform(5, 9000):.2f}" if random.random() > 0.08 else "",
                random.choice(statuses),
                random.choice(channels) if random.random() > 0.05 else "  ",
                random.choice(regions),
                random.randint(1, 40),
                f"{random.uniform(0, 0.45):.4f}",
                "2026-09-01T02:15:00Z",
            ])
    return path.stat().st_size


def run(rows: int) -> dict:
    print(f"\n{'=' * 76}\n{rows:,} ROWS\n{'=' * 76}")
    result: dict = {"rows": rows}
    tmp = Path(f"/tmp/assarium-scale-{rows}.csv")

    t = time.perf_counter()
    size = generate_csv(tmp, rows)
    result["csv_mb"] = size / 1e6
    print(f"  generate csv          {time.perf_counter() - t:>8.1f}s   {size / 1e6:,.0f} MB")

    connection = client.post("/api/connections", json={
        "name": f"Scale {rows}", "source_id": "files",
        "auth_method": "default", "values": {"label": f"scale-{rows}"},
    }).json()
    cid = connection["id"]

    t = time.perf_counter()
    with open(tmp, "rb") as handle:
        client.post(f"/api/connections/{cid}/upload",
                    files=[("files", (tmp.name, handle, "text/csv"))])
    result["upload_s"] = time.perf_counter() - t
    print(f"  upload                {result['upload_s']:>8.1f}s")

    nodes = client.get(f"/api/connections/{cid}/browse").json()
    t = time.perf_counter()
    datasets = client.post(f"/api/connections/{cid}/datasets",
                           json={"paths": [n["path"] for n in nodes]}).json()
    result["select_s"] = time.perf_counter() - t
    print(f"  select + describe     {result['select_s']:>8.1f}s")

    t = time.perf_counter()
    profile = client.post(f"/api/datasets/{datasets[0]['id']}/profile").json()
    result["profile_s"] = time.perf_counter() - t
    result["layer"] = profile.get("detected_layer")
    result["row_count"] = profile.get("profile", {}).get("row_count")
    result["sampled"] = profile.get("profile", {}).get("sampled_rows")
    print(f"  profile               {result['profile_s']:>8.1f}s"
          f"   layer={result['layer']} sampled={result['sampled']:,} of {result['row_count']:,}")

    t = time.perf_counter()
    started = client.post(f"/api/connections/{cid}/runs", json={}).json()
    state = {}
    for _ in range(7200):
        state = client.get(f"/api/runs/{started['id']}").json()
        if state["status"] != "running":
            break
        time.sleep(0.5)
    result["pipeline_s"] = time.perf_counter() - t
    result["pipeline_status"] = state.get("status")
    print(f"  pipeline (b/s/g)      {result['pipeline_s']:>8.1f}s   {state.get('status')}")
    for step in state.get("steps", []):
        print(f"      {step['kind']:<7} {str(step['rows_out'] or '-'):>12}  "
              f"{(step['duration_ms'] or 0) / 1000:>7.1f}s  {step['status']}")

    client.post(f"/api/connections/{cid}/semantic/build", json={"overwrite_edits": True})
    dashboard = client.post(f"/api/connections/{cid}/dashboards/generate").json()

    t = time.perf_counter()
    data = client.post(f"/api/dashboards/{dashboard['id']}/data", json={"filters": []}).json()
    result["dashboard_s"] = time.perf_counter() - t
    result["tile_errors"] = [x["title"] for x in data.get("tiles", []) if x.get("error")]
    print(f"  dashboard (8 tiles)   {result['dashboard_s']:>8.1f}s"
          f"   errors={len(result['tile_errors'])}")

    t = time.perf_counter()
    client.post(f"/api/connections/{cid}/semantic/query", json={
        "measures": ["orders_total_amount_usd"] if False else
                    [m for m in ["scale_orders.total_amount_usd"]],
    })
    result["server_rss_mb"] = server_rss_mb()
    print(f"  server RSS            {result['server_rss_mb']:>8.0f} MB")

    warehouse_dir = Path.home() / ".assarium" / "warehouse"
    on_disk = sum(f.stat().st_size for f in warehouse_dir.glob("*.duckdb*"))
    result["warehouse_mb"] = on_disk / 1e6
    print(f"  warehouse on disk     {result['warehouse_mb']:>8.0f} MB")

    tmp.unlink(missing_ok=True)
    gc.collect()
    return result


def main() -> int:
    sizes = [int(a) for a in sys.argv[1:]] or [100_000, 1_000_000]
    results = [run(size) for size in sizes]

    print(f"\n{'=' * 96}\nSUMMARY\n{'=' * 96}")
    header = (f"{'rows':>12} {'csv':>8} {'upload':>8} {'profile':>9} "
              f"{'pipeline':>9} {'dash':>7} {'wh disk':>9} {'srv RSS':>9}")
    print(header)
    for r in results:
        print(f"{r['rows']:>12,} {r['csv_mb']:>7,.0f}M {r['upload_s']:>7.1f}s "
              f"{r['profile_s']:>8.1f}s {r['pipeline_s']:>8.1f}s "
              f"{r['dashboard_s']:>6.2f}s {r['warehouse_mb']:>8,.0f}M "
              f"{r['server_rss_mb']:>8,.0f}M")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONHASHSEED", "0")
    sys.exit(main())
