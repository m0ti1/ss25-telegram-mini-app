# SS25 API и подключение Telegram

## Запуск сервера

Требуется Python 3.10+ (используется только стандартная библиотека):

```bash
python server.py
```

Для локальной проверки двух ботов используйте long polling:

```bash
python local_run.py
```

Скрипт проверяет оба токена через `getMe`, сбрасывает webhook и запускает API вместе с двумя polling-потоками. Long polling нужен для локальной проверки; для production используйте HTTPS webhook.

Скопируйте `.env.example` в переменные окружения и укажите токены двух ботов и `SS25_ADMIN_SECRET`. Токены и секрет нельзя размещать в `app.js`, HTML или репозитории.

## API

- `GET /api/health` — проверка сервера.
- `GET /api/settings` — текущие курс, комиссии и округление (`X-SS25-Admin`).
- `POST /api/settings` — обновление настроек администратором (`X-SS25-Admin`).
- `POST /api/calculate` — расчёт без создания заказа.
- `GET /api/orders` — список заказов для админки (`X-SS25-Admin`).
- `POST /api/orders` — создание заказа.
- `POST /api/orders/{id}/status` — изменение статуса заказа (`X-SS25-Admin`).
- `POST /api/orders/{id}/tracking` — добавить трек-номер (`X-SS25-Admin`).
- `GET /api/reviews` — очередь заданий на проверку (`X-SS25-Admin`).
- `POST /api/reviews/{id}/check` — принять задание или вернуть на доработку (`X-SS25-Admin`).
- `POST /telegram/training` — webhook учебного бота.
- `POST /telegram/client` — webhook клиентского бота.

## Пример расчёта

```json
POST /api/calculate
{
  "product_price_cny": 1200,
  "delivery_price_cny": 100,
  "delivery_type": "standard"
}
```

Ответ содержит базу, комиссию, итог в юанях, итог в рублях, курс и шаг округления. Рубли округляются только вверх: `13 201 → 13 210`.

## Webhook

После публикации сервера за HTTPS задайте Telegram webhook на два URL:

```text
https://your-domain.example/telegram/training
https://your-domain.example/telegram/client
```

Для production нужно добавить авторизацию API, CSRF/прокси-политику, резервные копии SQLite или PostgreSQL и реальный платёжный webhook. Ручное подтверждение оплаты в текущем MVP сохраняется намеренно: клиентская кнопка «Я оплатил» не меняет статус сама.
