"""SS25 backend MVP.

Dependency-free HTTP API and Telegram webhook handlers. Keep bot tokens in
environment variables; this module is intentionally small enough to deploy
behind a reverse proxy before moving to a production framework.
"""
from __future__ import annotations

import json
import math
import os
import secrets
import sqlite3
import hmac
import hashlib
import time
import urllib.parse
import urllib.request
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("SS25_DB_PATH", ROOT / "ss25.sqlite3"))
HOST = os.getenv("SS25_HOST", "127.0.0.1")
PORT = int(os.getenv("SS25_PORT", "8080"))
TRAINING_TOKEN = os.getenv("SS25_TRAINING_BOT_TOKEN", "")
CLIENT_TOKEN = os.getenv("SS25_CLIENT_BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("SS25_ADMIN_IDS", "").split(",") if x.strip().isdigit()}
ADMIN_SECRET = os.getenv("SS25_ADMIN_SECRET", "")
MINI_APP_URL = os.getenv("SS25_MINI_APP_URL", f"http://{HOST}:{PORT}/mini")

DEFAULT_SETTINGS = {"rate": 12.5, "standard": 10.0, "express": 15.0, "rounding": 10}
ORDER_STATUSES = [
    "Черновик", "Ожидает подтверждения", "Ожидает оплаты", "Оплата на проверке",
    "Оплачен", "Товар выкуплен", "На складе", "Проверка и фото",
    "Передан в доставку", "Получен", "Возврат запрошен",
]
TRAINING_DAYS = [
    {"day": 1, "title": "Роль менеджера", "task": "Напишите ответ клиенту, который прислал только ссылку на товар."},
    {"day": 2, "title": "Товар и источник", "task": "Определите категорию товара и перечислите недостающие данные."},
    {"day": 3, "title": "Сбор данных", "task": "Составьте полную карточку заказа по переписке."},
    {"day": 4, "title": "Расчёт стоимости", "task": "Рассчитайте стандартную и быструю доставку с округлением вверх."},
    {"day": 5, "title": "Сроки и этапы", "task": "Объясните клиенту этапы заказа без неподтверждённых обещаний."},
    {"day": 6, "title": "Консультация", "task": "Проведите клиента от ссылки до подтверждения заказа."},
    {"day": 7, "title": "Подбор товара", "task": "Уточните размер, цвет, количество, бюджет и срок."},
    {"day": 8, "title": "Стиль переписки", "task": "Перепишите пять слабых ответов коротко и понятно."},
    {"day": 9, "title": "Возражения", "task": "Ответьте на возражения по цене, сроку и комиссии."},
    {"day": 10, "title": "Возвраты", "task": "Объясните правила возврата в день получения товара."},
    {"day": 11, "title": "Создание заказа", "task": "Проверьте все обязательные поля карточки заказа."},
    {"day": 12, "title": "Оплата и статусы", "task": "Опишите передачу заказа администратору на проверку оплаты."},
    {"day": 13, "title": "Полная симуляция", "task": "Проведите сложный диалог и передайте контекст без потерь."},
    {"day": 14, "title": "Финальный экзамен", "task": "Решите итоговый кейс: расчёт, диалог, заказ и возврат."},
]


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value REAL NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER UNIQUE NOT NULL,
                role TEXT NOT NULL DEFAULT 'candidate', name TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS progress (
                user_id INTEGER NOT NULL REFERENCES users(id), day INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'completed', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, day)
            );
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, client_telegram_id INTEGER, manager_telegram_id INTEGER,
                product_type TEXT NOT NULL, model TEXT NOT NULL, poizon_url TEXT NOT NULL,
                eur_size TEXT, quantity INTEGER NOT NULL, city TEXT NOT NULL, address TEXT NOT NULL,
                delivery_type TEXT NOT NULL, product_price_cny REAL NOT NULL, delivery_price_cny REAL NOT NULL,
                commission_percent REAL NOT NULL, commission_cny REAL NOT NULL, total_cny REAL NOT NULL,
                total_rub INTEGER NOT NULL, status TEXT NOT NULL, tracking_number TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
                day INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'new', score REAL, comment TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, checked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS conversations (
                chat_id INTEGER NOT NULL, bot TEXT NOT NULL, state TEXT NOT NULL,
                data TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(chat_id, bot)
            );
            """
        )
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value))
        # Keep older local databases compatible with the reviewed-submission flow.
        for statement in (
            "ALTER TABLE progress ADD COLUMN answer TEXT",
            "ALTER TABLE progress ADD COLUMN submitted_at TEXT",
            "ALTER TABLE progress ADD COLUMN checked_at TEXT",
        ):
            try:
                conn.execute(statement)
            except sqlite3.OperationalError:
                pass
        conn.execute("UPDATE progress SET status='accepted' WHERE status='completed'")


def settings() -> dict[str, float]:
    with db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    result = DEFAULT_SETTINGS.copy()
    result.update({row["key"]: row["value"] for row in rows})
    return result


def calculate(product: float, delivery: float, delivery_type: str) -> dict[str, Any]:
    cfg = settings()
    percent = float(cfg["express"] if delivery_type == "express" else cfg["standard"])
    base = float(product) + float(delivery)
    commission = base * percent / 100
    total_cny = base + commission
    step = max(1, int(cfg["rounding"]))
    total_rub = int(math.ceil(total_cny * float(cfg["rate"]) / step) * step)
    return {
        "base_cny": round(base, 2), "commission_percent": percent,
        "commission_cny": round(commission, 2), "total_cny": round(total_cny, 2),
        "total_rub": total_rub, "rate": float(cfg["rate"]), "rounding": step,
    }


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def validate_init_data(init_data: str, bot_token: str) -> dict[str, Any] | None:
    """Validate Telegram Mini App initData and return its fields."""
    if not init_data or not bot_token:
        return None
    try:
        values = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received = values.pop("hash", "")
        auth_date = int(values.get("auth_date", "0"))
        if not received or not auth_date or abs(time.time() - auth_date) > 86400:
            return None
        check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            return None
        if values.get("user"):
            values["user"] = json.loads(values["user"])
        return values
    except (ValueError, json.JSONDecodeError):
        return None


def mini_user(handler: BaseHTTPRequestHandler, bot_token: str) -> tuple[int, dict[str, Any]] | None:
    init_data = handler.headers.get("X-Telegram-Init-Data", "")
    fields = validate_init_data(init_data, bot_token)
    if not fields or not isinstance(fields.get("user"), dict) or not fields["user"].get("id"):
        return None
    user = fields["user"]
    telegram_id = int(user["id"])
    upsert_user(telegram_id, user.get("first_name"))
    return telegram_id, user


def mini_admin(handler: BaseHTTPRequestHandler) -> tuple[int, dict[str, Any]] | None:
    auth = mini_user(handler, TRAINING_TOKEN)
    if not auth or auth[0] not in ADMIN_IDS:
        return None
    return auth


def training_snapshot(telegram_id: int) -> dict[str, Any]:
    user_id = upsert_user(telegram_id)
    with db() as conn:
        rows = conn.execute("SELECT day, status, answer, submitted_at, checked_at FROM progress WHERE user_id=? ORDER BY day", (user_id,)).fetchall()
        reviews = conn.execute("SELECT id, day, status, score, comment, created_at, checked_at FROM reviews WHERE user_id=? ORDER BY day", (user_id,)).fetchall()
    by_day = {int(row["day"]): dict(row) for row in rows}
    completed = sum(1 for row in rows if row["status"] == "accepted")
    unlocked = min(completed + 1, 14)
    return {"completed": completed, "unlocked_day": unlocked, "progress": by_day, "reviews": [dict(row) for row in reviews]}


def telegram_send(token: str, chat_id: int, text: str, keyboard: list[list[dict[str, str]]] | None = None) -> None:
    if not token:
        return
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json_bytes(payload), headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=15):
        pass


def upsert_user(telegram_id: int, name: str | None = None) -> int:
    with db() as conn:
        role = "admin" if telegram_id in ADMIN_IDS else "candidate"
        conn.execute(
            "INSERT INTO users(telegram_id, role, name) VALUES(?, ?, ?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET name=COALESCE(excluded.name, users.name)",
            (telegram_id, role, name),
        )
        return int(conn.execute("SELECT id FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()[0])


def conversation(chat_id: int, bot: str) -> tuple[str, dict[str, Any]]:
    with db() as conn:
        row = conn.execute("SELECT state, data FROM conversations WHERE chat_id=? AND bot=?", (chat_id, bot)).fetchone()
    if not row:
        return "idle", {}
    return str(row["state"]), json.loads(row["data"])


def set_conversation(chat_id: int, bot: str, state: str, data: dict[str, Any]) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO conversations(chat_id, bot, state, data) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(chat_id, bot) DO UPDATE SET state=excluded.state, data=excluded.data, updated_at=CURRENT_TIMESTAMP",
            (chat_id, bot, state, json.dumps(data, ensure_ascii=False)),
        )


def client_keyboard(options: list[tuple[str, str]]) -> list[list[dict[str, str]]]:
    return [[{"text": title, "callback_data": callback}] for title, callback in options]


def create_order(data: dict[str, Any], client_id: int | None = None) -> tuple[str, dict[str, Any]]:
    calc = calculate(float(data["product_price_cny"]), float(data["delivery_price_cny"]), data["delivery_type"])
    order_id = "SS25-" + secrets.token_hex(3).upper()
    with db() as conn:
        conn.execute(
            "INSERT INTO orders(id, client_telegram_id, manager_telegram_id, product_type, model, poizon_url, eur_size, quantity, city, address, delivery_type, product_price_cny, delivery_price_cny, commission_percent, commission_cny, total_cny, total_rub, status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, client_id, data.get("manager_telegram_id"), data["product_type"], data["model"], data["poizon_url"], data.get("eur_size"), int(data["quantity"]), data["city"], data["address"], data["delivery_type"], float(data["product_price_cny"]), float(data["delivery_price_cny"]), calc["commission_percent"], calc["commission_cny"], calc["total_cny"], calc["total_rub"], "Ожидает подтверждения"),
        )
    return order_id, calc


def order_summary(order_id: str, calc: dict[str, Any], data: dict[str, Any]) -> str:
    delivery = "быстрая" if data["delivery_type"] == "express" else "стандартная"
    return (f"SS25 / ЗАКАЗ {order_id}\n\n"
            f"{data['product_type']}: {data['model']}\n"
            f"Размер EUR: {data.get('eur_size') or '—'}\n"
            f"Количество: {data['quantity']}\n"
            f"Город: {data['city']}\nАдрес: {data['address']}\n"
            f"Доставка: {delivery}\n\n"
            f"Товар и доставка: {calc['base_cny']} ¥\n"
            f"Комиссия: {calc['commission_percent']}% ({calc['commission_cny']} ¥)\n"
            f"К оплате: {calc['total_rub']:,} ₽".replace(",", " "))


def client_callback(chat_id: int, callback_data: str) -> None:
    state, data = conversation(chat_id, "client")
    if callback_data == "new_order":
        set_conversation(chat_id, "client", "product_type", {})
        telegram_send(CLIENT_TOKEN, chat_id, "Выберите тип товара.", client_keyboard([(x, f"type:{x}") for x in ("обувь", "одежда", "аксессуары", "техника")]))
    elif callback_data.startswith("type:"):
        data["product_type"] = callback_data.split(":", 1)[1]
        set_conversation(chat_id, "client", "model", data)
        telegram_send(CLIENT_TOKEN, chat_id, "Напишите модель товара.")
    elif callback_data == "delivery:standard" or callback_data == "delivery:express":
        data["delivery_type"] = callback_data.split(":", 1)[1]
        set_conversation(chat_id, "client", "product_price_cny", data)
        telegram_send(CLIENT_TOKEN, chat_id, "Введите стоимость товара в ¥ числом.")
    elif callback_data == "confirm_order":
        order_id = data.get("order_id")
        with db() as conn:
            conn.execute("UPDATE orders SET status='Ожидает оплаты', updated_at=CURRENT_TIMESTAMP WHERE id=? AND client_telegram_id=?", (order_id, chat_id))
        set_conversation(chat_id, "client", "paid", {"order_id": order_id})
        telegram_send(CLIENT_TOKEN, chat_id, f"Заказ {order_id} подтверждён. Следуйте инструкции администратора для оплаты. После перевода нажмите «Я оплатил».", [[{"text": "Я оплатил", "callback_data": "paid"}]])
    elif callback_data == "paid":
        order_id = data.get("order_id")
        with db() as conn:
            conn.execute("UPDATE orders SET status='Оплата на проверке', updated_at=CURRENT_TIMESTAMP WHERE id=? AND client_telegram_id=?", (order_id, chat_id))
        telegram_send(CLIENT_TOKEN, chat_id, f"Сообщение по заказу {order_id} получено. Администратор проверит оплату в течение 24 часов.")
        for admin_id in ADMIN_IDS:
            telegram_send(CLIENT_TOKEN, admin_id, f"Новая проверка оплаты: {order_id}.", [[{"text": "Открыть админку", "callback_data": "admin"}]])
    elif callback_data == "my_orders":
        with db() as conn:
            rows = conn.execute("SELECT id, model, status, total_rub, tracking_number FROM orders WHERE client_telegram_id=? ORDER BY created_at DESC LIMIT 10", (chat_id,)).fetchall()
        if not rows:
            telegram_send(CLIENT_TOKEN, chat_id, "Заказов пока нет.")
        else:
            telegram_send(CLIENT_TOKEN, chat_id, "\n\n".join(f"{r['id']} · {r['model']}\n{r['status']} · {r['total_rub']:,} ₽".replace(",", " ") + (f"\nТрек: {r['tracking_number']}" if r['tracking_number'] else "") for r in rows))


def client_text(chat_id: int, text: str) -> None:
    state, data = conversation(chat_id, "client")
    if state == "model": data["model"] = text; state = "poizon_url"; reply = "Пришлите ссылку на товар в Poizon."
    elif state == "poizon_url": data["poizon_url"] = text; state = "eur_size"; reply = "Введите размер EUR или напишите «—»."
    elif state == "eur_size": data["eur_size"] = "" if text.strip() == "—" else text; state = "quantity"; reply = "Введите количество товара."
    elif state == "quantity":
        try: data["quantity"] = max(1, int(text))
        except ValueError: telegram_send(CLIENT_TOKEN, chat_id, "Введите количество целым числом."); return
        state = "city"; reply = "Введите город доставки."
    elif state == "city": data["city"] = text; state = "address"; reply = "Введите полный адрес доставки."
    elif state == "address": data["address"] = text; state = "delivery_type"; reply = "Выберите тип доставки."; set_conversation(chat_id, "client", state, data); telegram_send(CLIENT_TOKEN, chat_id, reply, client_keyboard([("Стандартная / 10%", "delivery:standard"), ("Быстрая / 15%", "delivery:express")])); return
    elif state == "product_price_cny":
        try: data["product_price_cny"] = float(text.replace(",", "."))
        except ValueError: telegram_send(CLIENT_TOKEN, chat_id, "Введите стоимость числом, например 1200."); return
        state = "delivery_price_cny"; reply = "Введите стоимость доставки в ¥."
    elif state == "delivery_price_cny":
        try: data["delivery_price_cny"] = float(text.replace(",", "."))
        except ValueError: telegram_send(CLIENT_TOKEN, chat_id, "Введите стоимость доставки числом."); return
        order_id, calc = create_order(data, chat_id); data["order_id"] = order_id; data["calc"] = calc; state = "confirm"; reply = order_summary(order_id, calc, data); set_conversation(chat_id, "client", state, data); telegram_send(CLIENT_TOKEN, chat_id, reply, [[{"text": "Подтвердить данные", "callback_data": "confirm_order"}] ]); return
    else: telegram_send(CLIENT_TOKEN, chat_id, "Используйте кнопки меню или нажмите /start."); return
    set_conversation(chat_id, "client", state, data); telegram_send(CLIENT_TOKEN, chat_id, reply)


def training_message(chat_id: int, update: dict[str, Any]) -> None:
    user = update.get("message", {}).get("from", {})
    upsert_user(chat_id, user.get("first_name"))
    snapshot = training_snapshot(chat_id)
    day = snapshot["unlocked_day"]
    telegram_send(TRAINING_TOKEN, chat_id, f"SS25 / DAY {day:02d}\n\nProgress: {snapshot['completed']}/14.\nOpen the Mini App to submit the task for manager review.", [[{"text": "Open training", "web_app": {"url": MINI_APP_URL}}]])


def client_message(chat_id: int, update: dict[str, Any]) -> None:
    upsert_user(chat_id, update.get("message", {}).get("from", {}).get("first_name"))
    text = str(update.get("message", {}).get("text", "")).strip()
    if text and not text.startswith("/"):
        client_text(chat_id, text); return
    telegram_send(CLIENT_TOKEN, chat_id, "SS25 / CLIENT BOT\n\nОткройте Mini App для заказа, расчёта и отслеживания.", [[{"text": "Открыть заказ", "web_app": {"url": MINI_APP_URL + "?client=1"}}, {"text": "Мои заказы", "callback_data": "my_orders"}]])


def telegram_webhook(kind: str, payload: dict[str, Any]) -> None:
    callback = payload.get("callback_query")
    if callback and callback.get("from", {}).get("id"):
        chat_id = int(callback["message"]["chat"]["id"])
        data = str(callback.get("data", ""))
        if kind == "training" and data.startswith("complete:"):
            telegram_send(TRAINING_TOKEN, chat_id, "Open the Mini App: completion now requires a written answer and manager review.")
            return
        if kind == "client":
            client_callback(chat_id, data)
            return
    message = payload.get("message") or payload.get("callback_query", {}).get("message")
    if not message or not message.get("chat", {}).get("id"):
        return
    chat_id = int(message["chat"]["id"])
    if kind == "training":
        training_message(chat_id, payload)
    else:
        client_message(chat_id, payload)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def send_json(self, value: Any, status: int = 200) -> None:
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def admin_allowed(self) -> bool:
        """Require an admin secret when one is configured; local MVP stays open."""
        return not ADMIN_SECRET or secrets.compare_digest(self.headers.get("X-SS25-Admin", ""), ADMIN_SECRET)

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "/index.html":
            self.send_file(ROOT / "index.html")
        elif self.path == "/mini" or self.path == "/mini/":
            self.send_file(ROOT / "mini.html")
        elif self.path == "/mini.js":
            self.send_file(ROOT / "mini.js")
        elif self.path in {"/styles.css", "/app.js"}:
            self.send_file(ROOT / self.path.lstrip("/"))
        elif self.path == "/api/health":
            self.send_json({"ok": True, "service": "ss25", "db": str(DB_PATH)})
        elif self.path == "/api/settings":
            if not self.admin_allowed(): self.send_json({"error": "unauthorized"}, 401); return
            self.send_json(settings())
        elif self.path == "/api/orders":
            if not self.admin_allowed(): self.send_json({"error": "unauthorized"}, 401); return
            with db() as conn:
                rows = [dict(row) for row in conn.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()]
            self.send_json(rows)
        elif self.path == "/api/reviews":
            if not self.admin_allowed(): self.send_json({"error": "unauthorized"}, 401); return
            with db() as conn:
                rows = [dict(row) for row in conn.execute("SELECT * FROM reviews ORDER BY created_at DESC").fetchall()]
            self.send_json(rows)
        elif self.path == "/api/mini/training":
            auth = mini_user(self, TRAINING_TOKEN)
            if not auth: self.send_json({"error": "invalid_telegram_auth"}, 401); return
            telegram_id, user = auth
            snapshot = training_snapshot(telegram_id)
            day = snapshot["unlocked_day"]
            self.send_json({"user": {"id": telegram_id, "name": user.get("first_name", "")}, "days": TRAINING_DAYS, **snapshot})
        elif self.path == "/api/mini/client/orders":
            auth = mini_user(self, CLIENT_TOKEN)
            if not auth: self.send_json({"error": "invalid_telegram_auth"}, 401); return
            telegram_id, _ = auth
            with db() as conn:
                rows = [dict(row) for row in conn.execute("SELECT * FROM orders WHERE client_telegram_id=? ORDER BY created_at DESC", (telegram_id,)).fetchall()]
            self.send_json(rows)
        elif self.path == "/api/mini/admin/reviews":
            if not self.admin_allowed() and not mini_admin(self): self.send_json({"error": "unauthorized"}, 401); return
            with db() as conn:
                rows = [dict(row) for row in conn.execute("SELECT reviews.id, reviews.user_id, users.telegram_id, users.name, reviews.day, reviews.status, reviews.score, reviews.comment, progress.answer, progress.submitted_at FROM reviews JOIN users ON users.id=reviews.user_id LEFT JOIN progress ON progress.user_id=reviews.user_id AND progress.day=reviews.day ORDER BY reviews.created_at DESC").fetchall()]
            self.send_json(rows)
        else:
            self.send_json({"error": "not_found"}, 404)

    def send_file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            self.send_json({"error": "not_found"}, 404); return
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        try:
            data = self.read_json()
            if self.path == "/api/calculate":
                self.send_json(calculate(float(data["product_price_cny"]), float(data["delivery_price_cny"]), data["delivery_type"]))
                return
            if self.path == "/api/mini/training/submit":
                auth = mini_user(self, TRAINING_TOKEN)
                if not auth: self.send_json({"error": "invalid_telegram_auth"}, 401); return
                telegram_id, _ = auth
                day = int(data.get("day", 0)); answer = str(data.get("answer", "")).strip()
                if not 1 <= day <= len(TRAINING_DAYS) or not answer:
                    self.send_json({"error": "day_and_answer_required"}, 400); return
                snapshot = training_snapshot(telegram_id)
                if day != snapshot["unlocked_day"]:
                    self.send_json({"error": "day_locked", "unlocked_day": snapshot["unlocked_day"]}, 409); return
                user_id = upsert_user(telegram_id)
                with db() as conn:
                    conn.execute("INSERT INTO progress(user_id, day, status, answer, submitted_at, updated_at) VALUES(?, ?, 'pending', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) ON CONFLICT(user_id, day) DO UPDATE SET status='pending', answer=excluded.answer, submitted_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP", (user_id, day - 1, answer))
                    conn.execute("INSERT INTO reviews(user_id, day, status) SELECT ?, ?, 'new' WHERE NOT EXISTS (SELECT 1 FROM reviews WHERE user_id=? AND day=? AND status IN ('new', 'accepted'))", (user_id, day - 1, user_id, day - 1))
                for admin_id in ADMIN_IDS:
                    telegram_send(TRAINING_TOKEN, admin_id, f"Новая работа на проверку: день {day}, пользователь {telegram_id}. Откройте Mini App админки.", [[{"text": "Открыть проверку", "web_app": {"url": MINI_APP_URL + "?admin=1"}}]])
                self.send_json({"ok": True, "status": "pending"}, 201); return
            if self.path == "/api/mini/admin/reviews":
                if not self.admin_allowed() and not mini_admin(self): self.send_json({"error": "unauthorized"}, 401); return
                with db() as conn:
                    rows = [dict(row) for row in conn.execute("SELECT reviews.id, reviews.user_id, users.telegram_id, users.name, reviews.day, reviews.status, reviews.score, reviews.comment, progress.answer, progress.submitted_at FROM reviews JOIN users ON users.id=reviews.user_id LEFT JOIN progress ON progress.user_id=reviews.user_id AND progress.day=reviews.day ORDER BY reviews.created_at DESC").fetchall()]
                self.send_json(rows); return
            if self.path == "/api/settings":
                if not self.admin_allowed(): self.send_json({"error": "unauthorized"}, 401); return
                if set(data) - set(DEFAULT_SETTINGS):
                    self.send_json({"error": "invalid_settings"}, 400); return
                with db() as conn:
                    for key, value in data.items():
                        conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP", (key, float(value)))
                self.send_json(settings()); return
            if self.path in {"/api/orders", "/api/mini/client/orders"}:
                if self.path == "/api/mini/client/orders":
                    auth = mini_user(self, CLIENT_TOKEN)
                    if not auth: self.send_json({"error": "invalid_telegram_auth"}, 401); return
                    data["client_telegram_id"] = auth[0]
                required = {"product_type", "model", "poizon_url", "quantity", "city", "address", "delivery_type", "product_price_cny", "delivery_price_cny"}
                if not required.issubset(data):
                    self.send_json({"error": "missing_fields", "fields": sorted(required - set(data))}, 400); return
                calc = calculate(float(data["product_price_cny"]), float(data["delivery_price_cny"]), data["delivery_type"])
                order_id = "SS25-" + secrets.token_hex(3).upper()
                with db() as conn:
                    conn.execute("INSERT INTO orders(id, client_telegram_id, manager_telegram_id, product_type, model, poizon_url, eur_size, quantity, city, address, delivery_type, product_price_cny, delivery_price_cny, commission_percent, commission_cny, total_cny, total_rub, status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (order_id, data.get("client_telegram_id"), data.get("manager_telegram_id"), data["product_type"], data["model"], data["poizon_url"], data.get("eur_size"), int(data["quantity"]), data["city"], data["address"], data["delivery_type"], float(data["product_price_cny"]), float(data["delivery_price_cny"]), calc["commission_percent"], calc["commission_cny"], calc["total_cny"], calc["total_rub"], "Ожидает подтверждения"))
                self.send_json({"id": order_id, "status": "Ожидает подтверждения", "calculation": calc}, 201); return
            if self.path.startswith("/telegram/"):
                kind = self.path.rsplit("/", 1)[-1]
                if kind not in {"training", "client"}:
                    self.send_json({"error": "unknown_bot"}, 404); return
                telegram_webhook(kind, data); self.send_json({"ok": True}); return
            if self.path.startswith("/api/orders/") and self.path.endswith("/status"):
                if not self.admin_allowed(): self.send_json({"error": "unauthorized"}, 401); return
                order_id = self.path.split("/")[3]
                status = data.get("status")
                if status not in ORDER_STATUSES:
                    self.send_json({"error": "invalid_status"}, 400); return
                with db() as conn:
                    conn.execute("UPDATE orders SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, order_id))
                self.send_json({"id": order_id, "status": status}); return
            if self.path.startswith("/api/orders/") and self.path.endswith("/tracking"):
                if not self.admin_allowed(): self.send_json({"error": "unauthorized"}, 401); return
                order_id = self.path.split("/")[3]
                tracking = str(data.get("tracking_number", "")).strip()
                if not tracking:
                    self.send_json({"error": "tracking_required"}, 400); return
                with db() as conn:
                    conn.execute("UPDATE orders SET tracking_number=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (tracking, order_id))
                self.send_json({"id": order_id, "tracking_number": tracking}); return
            if self.path.startswith("/api/reviews/") and self.path.endswith("/check"):
                if not self.admin_allowed() and not mini_admin(self): self.send_json({"error": "unauthorized"}, 401); return
                review_id = self.path.split("/")[3]
                status = data.get("status")
                if status not in {"accepted", "revision"}:
                    self.send_json({"error": "invalid_review_status"}, 400); return
                score = data.get("score")
                if score is not None and not 0 <= float(score) <= 100:
                    self.send_json({"error": "invalid_score"}, 400); return
                with db() as conn:
                    conn.execute("UPDATE reviews SET status=?, score=?, comment=?, checked_at=CURRENT_TIMESTAMP WHERE id=?", (status, score, data.get("comment"), review_id))
                    review = conn.execute("SELECT user_id, day FROM reviews WHERE id=?", (review_id,)).fetchone()
                    if review:
                        conn.execute("UPDATE progress SET status=?, checked_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND day=?", ("accepted" if status == "accepted" else "revision", review["user_id"], review["day"]))
                        if status == "accepted":
                            user = conn.execute("SELECT telegram_id FROM users WHERE id=?", (review["user_id"],)).fetchone()
                            if user:
                                telegram_send(TRAINING_TOKEN, int(user["telegram_id"]), f"Задание дня {int(review['day']) + 1} проверено. Следующий блок открыт.", [[{"text": "Открыть обучение", "web_app": {"url": MINI_APP_URL}}]])
                self.send_json({"id": int(review_id), "status": status, "score": score}); return
            self.send_json({"error": "not_found"}, 404)
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": "invalid_request", "detail": str(exc)}, 400)


if __name__ == "__main__":
    init_db()
    print(f"SS25 server listening on http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
