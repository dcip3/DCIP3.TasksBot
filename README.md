# TasksBot

TasksBot is a Telegram bot for monitoring Deadline render jobs, managing queues, and distributing previews via Dropbox. The project bundles an asynchronous Python backend and pipeline integrations for EXR-to-video processing.

## Key Features
- aiogram 3.x bot with FSM-based flows for authentication and job control
- Telegram bot backend with integrations for Deadline and Dropbox
- EXR → MP4 conversion with OCIO color management (server pipeline uploads to Dropbox)
- Two preview pipelines: render via Deadline or locally on the bot host
- Granular notification settings (all jobs vs. owned jobs)
- Auto preview on job completion (per-user scope and default method)
- Docker Compose stack for containerized deployment

## Architecture Overview
- **Telegram bot** (`app/bot/handlers/`) — modular routers (`auth`, `jobs`, `preview`, `settings`, `common`)
- **Core services** (`app/services/`, `app/core/`) — Deadline, Dropbox, data utilities
- **Integrations** (`app/integrations/`) — video helpers, Dropbox SDK clients, external APIs
- **Entry point** (`main.py`) — starts the bot and lifecycle hooks
- **Infrastructure** (`docker-compose.yml`, `Dockerfile`) — container build and deployment

## Getting Started

### Prerequisites
- Python ≥ 3.11
- FFmpeg available in `$PATH` on the bot host; Deadline workers need ffmpeg in `$PATH` or `FFMPEG_PATH`
- Docker & Docker Compose for containerized deployment

### 1. Clone & Configure Environment
```bash
git clone <repository-url>
cd DCIP3.TasksBot
cp .env.example .env
```
Fill in `.env` (see [Configuration](#configuration)).

### 2. Run with Docker
```bash
docker compose up --build
docker compose logs -f tasksbot   # follow bot logs
```

### 3. Local Development
Bot:
```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## Configuration
All configuration lives in `.env`. Use the tables below as a checklist.

### Core / Security
| Variable | Description | Example |
|----------|-------------|---------|
| `DEADLINE_API_URL` | Deadline REST endpoint (should include scheme + port) | `https://renderfarm.local:4434/api` |
| `PASSWORD_SALT` | Salt for hashing stored credentials | `change-me` |
| `ENCRYPTION_KEY` | Fernet key for secrets in storage | `generated-with-fernet` |

```bash
# Generate a fresh Fernet key
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### Telegram
| Variable | Description | How to obtain | Example |
|----------|-------------|---------------|---------|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | @BotFather | `123456789:ABCdef...` |

```bash
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
```

### Dropbox
| Variable | Description | Source |
|----------|-------------|--------|
| `DROPBOX_APP_KEY` | Dropbox app key | Dropbox App Console |
| `DROPBOX_APP_SECRET` | Dropbox app secret | Dropbox App Console |
| `DROPBOX_REFRESH_TOKEN` | OAuth refresh token with required scopes | Generated via OAuth flow |
| `DROPBOX_TEAM_MEMBER_ID` | Team member acting on files | Dropbox API |
| `DROPBOX_ROOT_NAMESPACE_ID` | Namespace ID of the shared team folder | Dropbox API |
| `DROPBOX_ROOT_MARKER` | Marker used to locate Dropbox paths in Deadline outputs | `Team Folder` |

1. Create an app at [Dropbox App Console](https://www.dropbox.com/developers/apps).  
2. Grant the necessary scopes and generate a long-lived refresh token.  
3. For team setups, capture the member and namespace IDs via the Dropbox API.

### Storage & Media
| Variable | Description | Default / Example |
|----------|-------------|-------------------|
| `SQLITE_DB_PATH` | SQLite database location | `data/app.db` |
| `TEMP_DIR` | Temporary downloads | `data/temp` |
| `CONV_DIR` | Converted previews | `data/conv` |
| `OCIO_CONFIG_PATH` | Path to OCIO config used during previews | `data/config.ocio` |
| `FFMPEG_PATH` | FFmpeg binary on Deadline workers | `ffmpeg` |
| `PREVIEW_*` | Fine-tunes color pipeline (apply transform, LUT size, etc.) | see `.env.example` |
```bash
mkdir -p data/temp data/conv
cp /path/to/config.ocio data/config.ocio
```

### Preview Upload (Worker -> Bot)
Enable this if you want Deadline workers to POST the finished preview directly to the bot host.
This is useful when renders are stored on worker-local disks.

| Variable | Description | Example |
|----------|-------------|---------|
| `PREVIEW_UPLOAD_ENABLED` | Enable preview upload endpoint | `true` |
| `PREVIEW_UPLOAD_URL` | Public URL workers POST to | `http://your-public-ip:8081/preview-upload` |
| `PREVIEW_UPLOAD_BIND_HOST` | Bind host on the bot container | `0.0.0.0` |
| `PREVIEW_UPLOAD_PORT` | Port for upload endpoint | `8081` |
| `PREVIEW_UPLOAD_TOKEN_TTL` | Token TTL in seconds | `1800` |
| `PREVIEW_UPLOAD_MAX_MB` | Max upload size | `100` |
| `PREVIEW_UPLOAD_INSECURE` | Allow insecure TLS (self-signed) | `false` |

Open the upload port in your firewall, and keep the endpoint private to your workers.
If you do not have TLS, the upload will be plain HTTP; tokens are short-lived but still sensitive.
Ensure Docker exposes `PREVIEW_UPLOAD_PORT` so workers can reach the endpoint.

Restart the service after any configuration change (`docker compose restart tasksbot` or relaunch `python main.py`).

## Telegram Bot Usage

### Commands
| Command | Purpose |
|---------|---------|
| `/start` | Show main menu |
| `/login` | Authenticate with Deadline credentials |
| `/logout` | Sign out and clear session |
| `/help` | Show help |

### Main Menu
- **📂 Jobs** — paginated batch list with actions (preview, suspend/resume, requeue, delete, tasks)
- **🖥️ Workers** — render node status overview
- **⚙️ Settings** — notification preferences, preview defaults, auto preview

### Preview Workflow
1. Check Dropbox for existing preview video.  
2. If absent, let the user choose between Deadline render or local server workflow.  
3. For the local workflow: download EXRs, assemble MP4, upload to Dropbox, send to chat.  
4. Clean up temporary files when finished.

Single-frame outputs are sent as photos instead of videos.

Auto preview can be enabled in Settings; it uses the notification scope and default preview method.

## Project Structure
```
.
├── app/
│   ├── auth/                  # Authentication and session helpers
│   ├── bot/
│   │   ├── handlers/          # aiogram routers
│   │   └── job_helpers.py     # job aggregation and formatting
│   ├── core/                  # config, bot_core, shared utilities
│   ├── integrations/          # Dropbox, video processing, external clients
│   ├── services/              # Deadline access, business logic
│   └── storage/               # Data access objects and persistence helpers
├── data/                      # App data (db, temp, converted previews)
├── Dockerfile
├── scripts/                   # Local run and maintenance scripts
├── docker-compose.yml
├── main.py
└── requirements.txt
```

## Development Workflow
- **Formatting**: `black app/`
- **Linting**: `flake8 app/`
- **Tests**: `pytest`
- **Quick bot run**: `python main.py`
- **Handlers**: add routers under `app/bot/handlers/` and register them via `register_handlers()` in `__init__.py`.

## Troubleshooting
- **Bot fails to start** — check `docker compose logs tasksbot`, confirm `.env` keys are present, and verify the Telegram token.
- **Authorization errors** — confirm `DEADLINE_API_URL` is reachable and credentials are correct.
- **Preview generation issues** — verify `OCIO_CONFIG_PATH` and `FFMPEG_PATH`, and ensure Dropbox paths resolve for the render outputs.

## Support
Open issues or PRs in the repository or contact the team directly.
