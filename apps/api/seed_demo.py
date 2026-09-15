import os
import random
import string
from datetime import datetime, timedelta
import pandas as pd
import httpx
import uuid

API_BASE = "http://127.0.0.1:8000/api"

def generate_data(data_dir: str):
    print("Generating e-commerce dummy data...")
    os.makedirs(data_dir, exist_ok=True)
    
    # 1. Customers
    num_customers = 500
    customers = pd.DataFrame({
        "customer_id": [f"CUST-{i:04d}" for i in range(1, num_customers + 1)],
        "name": ["".join(random.choices(string.ascii_letters, k=8)) for _ in range(num_customers)],
        "segment": random.choices(["Retail", "Wholesale", "Corporate"], weights=[0.7, 0.2, 0.1], k=num_customers),
        "signup_date": [datetime.now() - timedelta(days=random.randint(100, 1000)) for _ in range(num_customers)],
        "lifetime_value": [round(random.uniform(10, 5000), 2) for _ in range(num_customers)]
    })
    customers.to_csv(f"{data_dir}/customers.csv", index=False)
    
    # 2. Products
    num_products = 50
    products = pd.DataFrame({
        "product_id": [f"PROD-{i:03d}" for i in range(1, num_products + 1)],
        "category": random.choices(["Electronics", "Apparel", "Home", "Sports"], k=num_products),
        "price": [round(random.uniform(15.0, 899.99), 2) for _ in range(num_products)],
        "cost": [round(random.uniform(5.0, 400.0), 2) for _ in range(num_products)],
    })
    products.to_csv(f"{data_dir}/products.csv", index=False)
    
    # 3. Orders
    num_orders = 3000
    order_dates = [datetime.now() - timedelta(days=random.randint(0, 365)) for _ in range(num_orders)]
    orders = pd.DataFrame({
        "order_id": [f"ORD-{i:05d}" for i in range(1, num_orders + 1)],
        "customer_id": random.choices(customers["customer_id"], k=num_orders),
        "order_date": order_dates,
        "status": random.choices(["Completed", "Pending", "Cancelled", "Refunded"], weights=[0.8, 0.1, 0.05, 0.05], k=num_orders),
        "channel": random.choices(["Web", "Mobile", "In-Store", "Partner"], k=num_orders)
    })
    orders.to_csv(f"{data_dir}/orders.csv", index=False)
    
    # 4. Order Lines
    order_lines_data = []
    for _, order in orders.iterrows():
        # 1 to 4 items per order
        for i in range(random.randint(1, 4)):
            prod = products.sample(1).iloc[0]
            qty = random.randint(1, 5)
            order_lines_data.append({
                "order_id": order["order_id"],
                "product_id": prod["product_id"],
                "quantity": qty,
                "unit_price": prod["price"],
                "total_amount": round(prod["price"] * qty, 2),
                "discount": round(random.uniform(0, 10), 2) if random.random() > 0.8 else 0.0
            })
    
    order_lines = pd.DataFrame(order_lines_data)
    order_lines.to_csv(f"{data_dir}/order_lines.csv", index=False)
    
    print(f"Generated {len(customers)} customers, {len(products)} products, {len(orders)} orders, {len(order_lines)} order lines.")


def run_pipeline():
    upload_id = str(uuid.uuid4())
    upload_dir = os.path.expanduser(f"~/.assarium/uploads/{upload_id}")
    
    # 1. Generate data
    generate_data(upload_dir)
    
    with httpx.Client(base_url=API_BASE, timeout=120.0) as client:
        # 2. Create Connection
        print("Creating connection...")
        res = client.post("/connections", json={
            "source_id": "files",
            "auth_method": "default",
            "values": {
                "label": "Demo E-Commerce Data",
                "upload_id": upload_id
            }
        })
        res.raise_for_status()
        conn_id = res.json()["id"]
        print(f"Connection created: {conn_id}")
        
        # 3. Select Datasets
        print("Selecting datasets...")
        res = client.post(f"/connections/{conn_id}/datasets", json={
            "paths": [
                ["customers.csv"],
                ["products.csv"],
                ["orders.csv"],
                ["order_lines.csv"]
            ]
        })
        res.raise_for_status()
        datasets = res.json()
        print(f"Selected {len(datasets)} datasets.")
        
        # 4. Profile Datasets
        print("Profiling datasets...")
        for ds in datasets:
            res = client.post(f"/datasets/{ds['id']}/profile")
            res.raise_for_status()
            print(f" - Profiled {ds['name']} (Layer: {res.json()['detected_layer']})")
            
        # 5. Detect Relationships
        print("Detecting relationships...")
        res = client.post(f"/connections/{conn_id}/relationships")
        res.raise_for_status()
        print(f"Found relationships: {res.json()}")
        
        # 6. Run Medallion Pipeline
        print("Running Medallion Pipeline...")
        res = client.post(f"/connections/{conn_id}/pipeline/run")
        res.raise_for_status()
        run_id = res.json()["id"]
        
        # Wait for pipeline to finish
        import time
        while True:
            res = client.get(f"/pipeline/runs/{run_id}")
            status = res.json()["status"]
            if status in ["finished", "failed"]:
                print(f"Pipeline finished with status: {status}")
                break
            time.sleep(1)
            
        # 7. Build Semantic Model
        print("Building Semantic Model...")
        res = client.post(f"/connections/{conn_id}/semantic/build", json={"overwrite_edits": True})
        res.raise_for_status()
        
        # 8. Generate Dashboard
        print("Generating Dashboard...")
        res = client.post(f"/connections/{conn_id}/dashboards/generate")
        res.raise_for_status()
        print("All done! Go check the UI.")


if __name__ == "__main__":
    run_pipeline()
