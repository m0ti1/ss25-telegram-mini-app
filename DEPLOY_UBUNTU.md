# SS25: полный деплой на Ubuntu

Ниже описан деплой текущего проекта: два Telegram-бота (заказы и обучение), один Python backend, SQLite и Mini App. Оба бота могут работать на одном сервере. Для небольшой нагрузки это самый простой вариант.

## 0. Что получится

- `https://app.example.ru/mini.html` — Mini App для Telegram.
- `https://app.example.ru/api/...` — API для Mini App.
- `ss25.service` — один systemd-сервис, который поднимает HTTP API и long polling обоих ботов.
- Nginx принимает HTTPS и передаёт API на `127.0.0.1:8080`.

Telegram требует HTTPS URL для Web App, а `initData` нужно проверять на сервере. GitHub Pages подходит только для статического frontend; этот backend должен быть на VPS. Источник: https://core.telegram.org/bots/webapps.

## 1. Минимальный сервер

Для текущей нагрузки достаточно:

- Ubuntu Server 22.04 LTS или 24.04 LTS, x86_64;
- 1 vCPU;
- 1 GB RAM (лучше 2 GB);
- 15–25 GB SSD;
- публичный IPv4;
- домен или поддомен, например `app.example.ru`;
- открытые TCP-порты 22, 80 и 443.

Один сервер обслуживает оба бота. Разделять их имеет смысл при росте нагрузки, отдельной команде или необходимости независимых релизов. SQLite оставляем на одном сервере; для нескольких экземпляров приложения позже нужна PostgreSQL.

## 2. DNS и доступ

У регистратора создай DNS A-запись:

```text
app.example.ru  A  IP_АДРЕС_СЕРВЕРА
```

Подожди обновления DNS. На своём компьютере проверь:

```powershell
nslookup app.example.ru
```

IP в ответе должен совпадать с IP VPS.

## 3. Первый вход на сервер

Подключись по SSH под пользователем Ubuntu:

```bash
ssh ubuntu@IP_АДРЕС_СЕРВЕРА
```

Обнови систему и установи пакеты:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git python3 python3-venv python3-pip nginx certbot python3-certbot-nginx ufw ca-certificates curl
```

Включи firewall:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw --force enable
sudo ufw status
```

## 4. Пользователь приложения

Не запускай приложение от root:

```bash
sudo adduser --system --group --home /opt/ss25 ss25
sudo mkdir -p /opt/ss25
sudo chown -R ss25:ss25 /opt/ss25
```

## 5. Клонирование проекта

```bash
sudo -u ss25 git clone https://github.com/m0ti1/ss25-telegram-mini-app.git /opt/ss25/app
cd /opt/ss25/app
sudo -u ss25 python3 -m venv /opt/ss25/venv
sudo -u ss25 /opt/ss25/venv/bin/python -m pip install --upgrade pip
```

Проект использует только стандартную библиотеку Python, поэтому дополнительных pip-пакетов не требуется.

## 6. Создание `.env`

Создай файл только с правами владельца:

```bash
sudo -u ss25 nano /opt/ss25/app/.env
```

Вставь, заменив домен и секрет:

```dotenv
SS25_HOST=127.0.0.1
SS25_PORT=8080
SS25_DB_PATH=/opt/ss25/app/ss25.sqlite3
SS25_CLIENT_BOT_TOKEN=ТОКЕН_БОТА_ЗАКАЗОВ
SS25_TRAINING_BOT_TOKEN=ТОКЕН_БОТА_ОБУЧЕНИЯ
SS25_ADMIN_IDS=904082065
SS25_ADMIN_SECRET=СЛОЖНЫЙ_СЕКРЕТ_МИНИМУМ_32_СИМВОЛА
SS25_MINI_APP_URL=https://app.example.ru/mini.html
```

Защити файл:

```bash
sudo chown ss25:ss25 /opt/ss25/app/.env
sudo chmod 600 /opt/ss25/app/.env
```

Не добавляй `.env` в Git. Токены, которые уже публиковались в переписке, после запуска лучше перевыпустить через `@BotFather` и заменить на сервере.

## 7. Проверка backend вручную

```bash
sudo -u ss25 /opt/ss25/venv/bin/python -m py_compile /opt/ss25/app/server.py /opt/ss25/app/local_run.py
sudo -u ss25 bash -c 'cd /opt/ss25/app && set -a && . ./.env && set +a && /opt/ss25/venv/bin/python tests/test_server.py'
```

Ожидаемый результат:

```text
SS25 smoke test: PASS
```

## 8. systemd: оба бота и API

Создай сервис:

```bash
sudo nano /etc/systemd/system/ss25.service
```

Содержимое:

```ini
[Unit]
Description=SS25 API and Telegram bots
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ss25
Group=ss25
WorkingDirectory=/opt/ss25/app
EnvironmentFile=/opt/ss25/app/.env
ExecStart=/opt/ss25/venv/bin/python /opt/ss25/app/local_run.py
Restart=always
RestartSec=5
TimeoutStopSec=20
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Активируй сервис:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ss25
sudo systemctl status ss25 --no-pager
```

Логи:

```bash
sudo journalctl -u ss25 -f
```

Проверка локального API:

```bash
curl http://127.0.0.1:8080/api/health
```

Ожидается JSON с `"ok": true`.

## 9. Nginx до выпуска сертификата

Создай конфигурацию:

```bash
sudo nano /etc/nginx/sites-available/ss25
```

Вставь:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name app.example.ru;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
```

Активируй сайт и проверь конфигурацию:

```bash
sudo ln -s /etc/nginx/sites-available/ss25 /etc/nginx/sites-enabled/ss25
sudo nginx -t
sudo systemctl reload nginx
```

Проверь в браузере:

```text
http://app.example.ru/mini.html
```

## 10. HTTPS через Let's Encrypt

Запусти Certbot:

```bash
sudo certbot --nginx -d app.example.ru
```

Выбери перенаправление HTTP -> HTTPS. Проверь продление:

```bash
sudo certbot renew --dry-run
```

Теперь Mini App должен открываться по:

```text
https://app.example.ru/mini.html
```

## 11. Обнови URL Mini App и перезапусти

После получения сертификата проверь `.env`:

```bash
grep SS25_MINI_APP_URL /opt/ss25/app/.env
```

Должно быть:

```text
SS25_MINI_APP_URL=https://app.example.ru/mini.html
```

Перезапусти:

```bash
sudo systemctl restart ss25
sudo systemctl status ss25 --no-pager
sudo journalctl -u ss25 -n 100 --no-pager
```

При доступном Telegram API в логах появятся имена обоих ботов и настройка Mini App menu. Если исходящий доступ к Telegram временно недоступен, API всё равно стартует, а polling подключится после восстановления сети.

## 12. Проверка снаружи

```bash
curl -I https://app.example.ru/mini.html
curl https://app.example.ru/api/health
```

Должно быть `200 OK`.

Проверь доступность статики:

```bash
curl -I https://app.example.ru/styles.css
curl -I https://app.example.ru/mini.js
```

## 13. Telegram

1. Открой клиентского бота и отправь `/start`.
2. Открой учебного бота и отправь `/start`.
3. Нажми кнопку меню.
4. Клиентский бот должен открыть `mini.html?client=1`.
5. Учебный бот должен открыть `mini.html`.
6. Администратор `904082065` открывает очередь проверок через ссылку с `?admin=1` из уведомления.

Проверяй именно через кнопку внутри Telegram, а не обычным браузером: сервер использует Telegram `initData` для авторизации.

## 14. Обновление проекта

```bash
cd /opt/ss25/app
sudo -u ss25 git pull --ff-only
sudo systemctl restart ss25
sudo systemctl status ss25 --no-pager
```

Перед обновлением базы сделай копию:

```bash
sudo install -o ss25 -g ss25 -m 600 /opt/ss25/app/ss25.sqlite3 /opt/ss25/app/backups/ss25-$(date +%F-%H%M).sqlite3
```

Создай каталог backup один раз:

```bash
sudo -u ss25 mkdir -p /opt/ss25/app/backups
```

## 15. Диагностика

```bash
sudo systemctl status ss25 --no-pager
sudo journalctl -u ss25 -n 200 --no-pager
sudo nginx -t
sudo systemctl status nginx --no-pager
curl http://127.0.0.1:8080/api/health
curl https://app.example.ru/api/health
```

Типичные причины:

- `502 Bad Gateway`: `ss25.service` не запущен или слушает не `127.0.0.1:8080`.
- `Mini App не открывается`: DNS или HTTPS-сертификат не настроены.
- `401 invalid_telegram_auth`: приложение открыто не через Telegram или URL ведёт на другой домен.
- Боты не отвечают: проверь `journalctl`, интернет с VPS и отсутствие второго polling-процесса с теми же токенами.
- Кнопка не обновилась: перезапусти сервис и отправь `/start`; старые сообщения Telegram не изменяются.

## 16. Что следует сделать перед production

- перевыпустить токены ботов, если они когда-либо публиковались;
- заменить SQLite на PostgreSQL при росте нагрузки;
- подключить платёжный провайдер вместо ручной смены статуса оплаты;
- добавить резервные копии базы и мониторинг;
- ограничить админские API отдельной авторизацией и аудитом действий;
- не хранить `.env` в GitHub и не передавать токены в клиентский JavaScript.
