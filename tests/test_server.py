import json
import os
import tempfile
import threading
import urllib.request
import sys
from pathlib import Path

os.environ["SS25_DB_PATH"] = str(Path(tempfile.gettempdir()) / "ss25-smoke.sqlite3")
os.environ["SS25_ADMIN_SECRET"] = "smoke-secret"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server


def request(method, url, payload=None, headers=None):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    request_obj = urllib.request.Request(url, data=body, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(request_obj) as response:
        return response.status, json.loads(response.read())


def main():
    try:
        Path(os.environ["SS25_DB_PATH"]).unlink()
    except FileNotFoundError:
        pass
    server.init_db()
    http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{http.server_port}"
    try:
        status, health = request("GET", base + "/api/health")
        assert status == 200 and health["ok"] is True
        _, standard = request("POST", base + "/api/calculate", {"product_price_cny": 1200, "delivery_price_cny": 100, "delivery_type": "standard"})
        _, express = request("POST", base + "/api/calculate", {"product_price_cny": 1200, "delivery_price_cny": 100, "delivery_type": "express"})
        assert standard["total_rub"] == 17880
        assert express["total_rub"] == 18690
        order_data = {"product_type": "обувь", "model": "Nike Dunk", "poizon_url": "https://poizon.example/item", "eur_size": "42", "quantity": 1, "city": "Казань", "address": "Баумана 10", "delivery_type": "standard", "product_price_cny": 1200, "delivery_price_cny": 100}
        status, order = request("POST", base + "/api/orders", order_data)
        assert status == 201 and order["calculation"]["total_rub"] == 17880
        admin = {"X-SS25-Admin": "smoke-secret"}
        status, orders = request("GET", base + "/api/orders", headers=admin)
        assert status == 200 and len(orders) == 1
        status, changed = request("POST", base + f"/api/orders/{order['id']}/status", {"status": "Оплачен"}, admin)
        assert status == 200 and changed["status"] == "Оплачен"
        print("SS25 smoke test: PASS")
    finally:
        http.shutdown()


if __name__ == "__main__":
    main()
