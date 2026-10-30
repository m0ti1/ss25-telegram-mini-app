"""Run the SS25 API and both Telegram bots locally with long polling."""
from __future__ import annotations

import os
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def load_env() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_env()

import server  # noqa: E402


STOP = threading.Event()


def telegram_call(token: str, method: str, params: dict | None = None) -> dict:
    query = ""
    if params:
        query = "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}{query}")
    with urllib.request.urlopen(request, timeout=40) as response:
        result = json.loads(response.read())
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram API error"))
    return result["result"]


def bot_info(token: str) -> dict:
    return telegram_call(token, "getMe")


def configure_mini_app(token: str, url: str, text: str) -> None:
    telegram_call(token, "setChatMenuButton", {"menu_button": json.dumps({"type": "web_app", "text": text, "web_app": {"url": url}})})


def with_query(url: str, key: str, value: str) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urllib.parse.urlencode({key: value})}"


def poll(name: str, token: str, kind: str) -> None:
    offset = 0
    try:
        telegram_call(token, "deleteWebhook", {"drop_pending_updates": "false"})
    except Exception as exc:
        print(f"[{name}] webhook reset failed: {exc}")
    while not STOP.is_set():
        try:
            updates = telegram_call(token, "getUpdates", {"timeout": 25, "offset": offset, "allowed_updates": '["message","callback_query"]'})
            for update in updates:
                offset = max(offset, int(update["update_id"]) + 1)
                try:
                    server.telegram_webhook(kind, update)
                except Exception as exc:
                    print(f"[{name}] update failed: {exc}")
        except Exception as exc:
            print(f"[{name}] polling error: {exc}")
            STOP.wait(3)


def main() -> None:
    server.init_db()
    training_token = os.getenv("SS25_TRAINING_BOT_TOKEN", "")
    client_token = os.getenv("SS25_CLIENT_BOT_TOKEN", "")
    if not training_token or not client_token:
        raise SystemExit("Set SS25_TRAINING_BOT_TOKEN and SS25_CLIENT_BOT_TOKEN in .env")
    try:
        client = bot_info(client_token)
    except Exception as exc:
        client = {"username": "unavailable"}
        print(f"[client] Telegram check failed; local API will still start: {exc}")
    try:
        training = bot_info(training_token)
    except Exception as exc:
        training = {"username": "unavailable"}
        print(f"[training] Telegram check failed; local API will still start: {exc}")
    mini_url = os.getenv("SS25_MINI_APP_URL", "")
    if mini_url.startswith("https://"):
        for name, token, label, url in (
            ("client", client_token, "Открыть заказ", with_query(mini_url, "client", "1")),
            ("training", training_token, "Открыть обучение", mini_url),
        ):
            try:
                configure_mini_app(token, url, label)
                print(f"[{name}] Mini App menu configured: {url}")
            except Exception as exc:
                print(f"[{name}] Mini App menu setup failed: {exc}")
    http = server.ThreadingHTTPServer((server.HOST, server.PORT), server.Handler)
    http_thread = threading.Thread(target=http.serve_forever, name="ss25-http", daemon=True)
    http_thread.start()
    threads = [
        threading.Thread(target=poll, args=("client", client_token, "client"), name="ss25-client", daemon=True),
        threading.Thread(target=poll, args=("training", training_token, "training"), name="ss25-training", daemon=True),
    ]
    for thread in threads:
        thread.start()
    print(f"SS25 local server: http://{server.HOST}:{server.PORT}")
    print(f"Client bot: @{client.get('username', 'unknown')} (orders)")
    print(f"Training bot: @{training.get('username', 'unknown')} (manager training)")
    print(f"Admin chat IDs: {sorted(server.ADMIN_IDS)}")
    print("Long polling is active. Press Ctrl+C to stop.")
    try:
        while not STOP.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    STOP.set()
    http.shutdown()


if __name__ == "__main__":
    main()
