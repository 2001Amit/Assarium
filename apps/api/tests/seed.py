"""
Seed the demo dataset and leave it in place.

`e2e_verify.py` proves the build works and then removes what it created, so it can be
run repeatedly. This does the same walk but keeps the result, so there is something to
show. Run it once before a demo.

    ./assarium demo
"""

from __future__ import annotations

import sys
import time

import httpx
from e2e_verify import make_csvs

BASE = "http://127.0.0.1:8000"
client = httpx.Client(base_url=BASE, timeout=600)

NAME = "Ops extracts"


def main() -> int:
    try:
        client.get("/api/health").raise_for_status()
    except Exception:
        print("The API is not running. Start it with:  ./assarium api")
        return 1

    # Replace any previous seed so repeated runs stay clean rather than piling up.
    for existing in client.get("/api/connections").json():
        if existing["name"] == NAME:
            client.delete(f"/api/connections/{existing['id']}")
            print(f"Removed the previous '{NAME}' connection.")

    print("Creating the connection...")
    connection = client.post("/api/connections", json={
        "name": NAME, "source_id": "files",
        "auth_method": "default", "values": {"label": "Demo"},
    }).json()
    cid = connection["id"]

    files = make_csvs()
    client.post(
        f"/api/connections/{cid}/upload",
        files=[("files", (name, data, "text/csv")) for name, data in files.items()],
    )
    print(f"Uploaded {len(files)} files.")

    nodes = client.get(f"/api/connections/{cid}/browse").json()
    datasets = client.post(
        f"/api/connections/{cid}/datasets", json={"paths": [n["path"] for n in nodes]}
    ).json()
    print(f"Selected {len(datasets)} datasets.")

    print("Profiling...")
    for dataset in datasets:
        detail = client.post(f"/api/datasets/{dataset['id']}/profile").json()
        print(f"   {detail['name']:<22} {str(detail['detected_layer']).upper():<7}"
              f" {detail['layer_confidence']}")

    found = client.post(f"/api/connections/{cid}/relationships").json()
    print(f"Relationships: {found['found']} found.")

    print("Running the pipeline...")
    run = client.post(f"/api/connections/{cid}/runs", json={}).json()
    for _ in range(600):
        state = client.get(f"/api/runs/{run['id']}").json()
        if state["status"] != "running":
            break
        time.sleep(0.5)
    print(f"   {state['status']}")

    client.post(f"/api/connections/{cid}/semantic/build", json={"overwrite_edits": True})
    print("Semantic model built.")

    dashboard = client.post(f"/api/connections/{cid}/dashboards/generate").json()
    print(f"Dashboard generated: {dashboard['name']} ({len(dashboard['tiles'])} tiles).")

    print("\nReady. Open http://localhost:3000")
    return 0


if __name__ == "__main__":
    sys.exit(main())
