# TasksBot

TasksBot combines a Telegram bot, a FastAPI backend, and a Telegram WebApp to help teams monitor Deadline render jobs and manage project files stored in Dropbox.

## Features
- Telegram bot built on aiogram v3 with Deadline-aware authentication and a rich set of commands for render management.
- REST API served by FastAPI (`main_api.py`) for the WebApp and external integrations.
- Real-time notifications, progress updates, and automated EXR → video conversions.
- Dropbox integration for downloading source plates and uploading rendered media.
- Telegram WebApp built with React/Vite (`mini-app/`) to provide a modern UI inside Telegram.
- Docker Compose environment with Nginx reverse proxy and HTTPS templates (`infra/nginx`).

## Repository Structure
```
.
├── app/
│   ├── auth/               # Authentication helpers and session storage
│   ├── bot/                # Aiogram handlers, FSM, and routers
│   ├── core/               # Configuration, database, and shared utilities
│   ├── integrations/       # REST routes, Dropbox helpers, media pipelines
│   ├── services/           # Business logic and external API clients
│   └── storage/            # Helpers for local storage directories
├── infra/
│   ├── backend/            # Backend Dockerfile
│   └── nginx/              # Nginx configuration templates
├── mini-app/               # Telegram WebApp (React + Vite)
├── scripts/
│   └── run_bot_local.py    # Local bot runner without FastAPI/WebApp
├── storage/
│   ├── config.ocio         # OCIO configuration for color conversion
│   ├── tasks_bot.db        # SQLite database (created on first run)
│   ├── conv/               # Converted media artifacts (gitignored)
│   └── temp/               # Temporary download data (gitignored)
├── docker-compose.yml      # Docker Compose environment
├── main_api.py             # Entry point (bot + FastAPI app)
├── requirements.txt        # Python dependencies
└── README.md
```

## Prerequisites
- Python 3.11+
- Node.js 18+ (for the WebApp)
- Docker & Docker Compose (optional but recommended)
- Credentials for Telegram Bot API, Deadline REST API, and Dropbox App

## Environment Configuration
Use `.env.example` as a template:

- `APP_DOMAIN`, `BASE_API_URL`, `HTTP_TIMEOUT` — public endpoints and Deadline connectivity.
- `TG_API_TOKEN` — Telegram bot token.
- `DROPBOX_*` — Dropbox App credentials and namespace details.
- `DB_PATH`, `CREDENTIALS_FILE`, `TEMP_DIR`, `CONV_DIR`, `OCIO_CONFIG_PATH` — local storage paths.
- `FFMPEG_PATH` — path to the `ffmpeg` executable on Deadline workers (if it isn't in `PATH`).
- `MAX_CONCURRENT_DOWNLOADS`, `MIN_FREE_SPACE_BYTES` — background download limits.
- `PASSWORD_SALT` — PBKDF2 salt for user credentials stored in SQLite.

Prepare the `storage/` directory before the first local launch:

```bash
mkdir -p storage/conv storage/temp
cp path/to/config.ocio storage/config.ocio  # Only if the file is not yet present
```
The SQLite database (`storage/tasks_bot.db`) is created automatically the first time the app runs.

## Local Run (Python)
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/run_bot_local.py
```
The bot runs in polling mode without starting FastAPI or the WebApp.

## Local Run (Docker Compose)
```bash
docker compose --env-file .env up --build
```
Docker Compose launches three containers:
- `backend` (Telegram bot + FastAPI)
- `frontend` (Vite-based WebApp)
- `nginx` (reverse proxy with HTTPS support)

Make sure Let’s Encrypt certificates are available under `/etc/letsencrypt/live/${APP_DOMAIN}/...` on the host. `docker-compose.yml` reads `.env`, so keep that file in the project root.

## WebApp Development
```bash
cd mini-app
npm install
npm run dev
```
Configure `VITE_API_URL` inside `mini-app/.env` or rely on the built-in fallback to `window.location.origin`.

## Tooling & Scripts
- `scripts/run_bot_local.py` — quick launcher for the Telegram bot.
- `infra/nginx/nginx.conf.template` — Nginx template with `${APP_DOMAIN}` placeholders.
- `infra/backend/Dockerfile` — backend Dockerfile used by Compose.

## Dependencies
All runtime dependencies live in `requirements.txt`: aiogram 3, FastAPI, APScheduler, aiosqlite, aiofiles, OpenEXR, OpenColorIO, numpy, Pillow, and more. Running `pip install -r requirements.txt` also installs recommended dev tools (`pytest`, `pytest-asyncio`, `black`, `flake8`).

## Useful Links
- [aiogram Documentation](https://docs.aiogram.dev/)
- [Deadline REST API](https://docs.thinkboxsoftware.com/products/deadline/)
- [Dropbox HTTP API](https://www.dropbox.com/developers/documentation/http/overview)
- [Telegram WebApp Docs](https://core.telegram.org/bots/webapps)
