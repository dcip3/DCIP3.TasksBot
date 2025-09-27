# TasksBot

TasksBot — это интеграция Telegram-бота, API и мини-приложения WebApp для управления очередями рендеринга Deadline и файлами в Dropbox.

## Возможности
- Telegram-бот на базе aiogram с авторизацией пользователей Deadline и набором команд для мониторинга и управления рендерами.
- REST API на FastAPI (`main_api.py`) для мини-приложения и внешних интеграций.
- Реал-тайм уведомления и прогресс-бар по статусам задач, включая конвертацию EXR → видео.
- Dropbox-интеграция для скачивания исходников и выгрузки результатов.
- Мини-приложение на React/Vite (`mini-app/`), работающее внутри Telegram.
- Docker-compose окружение с Nginx-прокси и HTTPS (через шаблон в `infra/nginx`).

## Структура репозитория
```
.
├── app/                    # Серверная логика бота и API
├── infra/
│   ├── backend/            # Dockerfile backend-сервиса
│   └── nginx/              # Шаблоны конфигурации для Nginx
├── mini-app/               # Исходники Telegram WebApp (React)
├── scripts/
│   └── run_bot_local.py    # Удобный запуск бота без FastAPI/mini-app
├── storage/
│   ├── config.ocio         # OCIO конфиг для конвертации
│   ├── tasks_bot.db        # SQLite база (создается автоматически)
│   ├── conv/               # Конвертированные медиафайлы (gitignored)
│   └── temp/               # Временные данные скачиваний (gitignored)
├── config.ocio             # Конфигурация OpenColorIO для конвертации
├── docker-compose.yml      # Docker Compose окружение
├── main_api.py             # Точка входа (bot + FastAPI)
├── requirements.txt        # Python зависимости
└── README.md
```

## Предварительные требования
- Python 3.11+
- Node.js 18+ (для сборки мини-приложения)
- Docker & Docker Compose (для контейнерного запуска)
- Аккаунт Telegram Bot API, доступ к Deadline API и Dropbox App credentials

## Конфигурация окружения
Создайте файл `.env` (можно скопировать из `.env.example`, если появится) и задайте ключевые переменные:

- `APP_DOMAIN` — домен, на котором будет работать фронт+API (нужен для Nginx и сертификатов).
- `BASE_API_URL` — адрес Deadline RCS API.
- `TG_API_TOKEN` — токен Telegram-бота.
- `PASSWORD_SALT` — соль для PBKDF2-хеширования паролей.
- Блок настроек Dropbox (`DROPBOX_*`).
- Локальные пути и файлы (`DB_PATH`, `TEMP_DIR`, `CONV_DIR`, `OCIO_CONFIG_PATH`, `MINI_APP_URL` и т. д.).

Создайте структуру каталога `storage/` (при первом запуске Docker-compose она создастся автоматически, но для локального запуска удобнее подготовить вручную):

```bash
mkdir -p storage/conv storage/temp
cp path/to/config.ocio storage/config.ocio   # если файл ещё не скопирован
```
База данных `storage/tasks_bot.db` появится автоматически при первом запуске приложения.

## Локальный запуск (Python)
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/run_bot_local.py
```
Бот стартует в Telegram-режиме (без FastAPI и мини-приложения).

## Локальный запуск (Docker Compose)
```bash
docker compose --env-file .env up --build
```
Будут подняты три контейнера:
- `backend` (бот + API)
- `frontend` (мини-приложение на Vite)
- `nginx` (reverse proxy, HTTPS, проксирование /api и фронта)

Убедитесь, что сертификаты Let’s Encrypt лежат по путям `/etc/letsencrypt/live/${APP_DOMAIN}/...` на хосте. `docker-compose.yml` загружает переменные из `.env`, так что файл должен находиться в корне репозитория при запуске.

## Разработка мини-приложения
```bash
cd mini-app
npm install
npm run dev
```
В `.env` мини-приложения задайте `VITE_API_URL`, либо позвольте приложению использовать `window.location.origin` (fallback уже реализован).

## Дополнительные скрипты и утилиты
- `scripts/run_bot_local.py` — быстрый запуск телеграм-бота.
- `infra/nginx/nginx.conf.template` — шаблон Nginx, переменная `${APP_DOMAIN}` подменяется через entrypoint контейнера.
- `infra/backend/Dockerfile` — Dockerfile backend-сервиса.

## Полезные ссылки
- [aiogram](https://docs.aiogram.dev/)
- [Deadline REST API](https://docs.thinkboxsoftware.com/products/deadline/)
- [Dropbox API](https://www.dropbox.com/developers/documentation/http/overview)
- [Telegram WebApp](https://core.telegram.org/bots/webapps)
