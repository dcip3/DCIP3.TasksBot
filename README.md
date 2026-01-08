# TasksBot

TasksBot is a Telegram bot for monitoring Deadline render jobs, managing queues, and distributing previews via Dropbox. The project bundles an asynchronous Python backend and pipeline integrations for EXR-to-video processing.

## Key Features
- aiogram 3.x bot with FSM-based flows for authentication and job control
- Telegram bot backend with integrations for Deadline and Dropbox
- EXR → MP4 conversion with OCIO color management and Dropbox upload
- Two preview pipelines: render via Deadline or locally on the bot host
- Granular notification settings (all jobs vs. owned jobs)
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
- FFmpeg available in `$PATH` (or set `FFMPEG_PATH`)
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
docker compose logs -f backend   # follow bot logs
```

### 3. Local Development
Backend + bot:
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
| `BASE_API_URL` | Deadline REST endpoint (should include scheme + port) | `https://deadline.local:8443/api` |
| `HTTP_TIMEOUT` | Timeout (seconds) for external HTTP calls | `30` |
| `DEV_MODE` | Development flag (must be `false` in production) | `false` |
| `PASSWORD_SALT` | Salt for hashing stored credentials | `change-me` |
| `ENCRYPTION_KEY` | Fernet key for secrets in storage | `generated-with-fernet` |

```bash
# Generate a fresh Fernet key
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### Telegram
| Variable | Description | How to obtain | Example |
|----------|-------------|---------------|---------|
| `TG_API_TOKEN` | Telegram bot token | @BotFather | `123456789:ABCdef...` |

```bash
TG_API_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
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
| `DB_PATH` | SQLite database location | `data/app.db` |
| `TEMP_DIR` | Temporary downloads | `data/temp` |
| `CONV_DIR` | Converted previews | `data/conv` |
| `OCIO_CONFIG_PATH` | Path to OCIO config used during previews | `data/config.ocio` |
| `FFMPEG_PATH` | FFmpeg binary (local or on workers) | `ffmpeg` |
| `PREVIEW_*` | Fine-tunes color pipeline (apply transform, LUT size, etc.) | see `.env.example` |
| `MAX_CONCURRENT_DOWNLOADS` | Parallel Dropbox downloads | `2` |
| `MIN_FREE_SPACE_BYTES` | Minimum disk space before aborting (bytes) | `10737418240` |

```bash
mkdir -p data/temp data/conv
cp /path/to/config.ocio data/config.ocio
```

Restart the service after any configuration change (`docker compose restart backend` or relaunch `python main.py`).

## Telegram Bot Usage

### Commands
| Command | Purpose |
|---------|---------|
| `/start` | Show main menu |
| `/login` | Authenticate with Deadline credentials |
| `/logout` | Sign out and clear session |
| `/cancel` | Abort the current FSM step |

### Main Menu
- **📂 Jobs** — paginated batch list with actions (preview, suspend/resume, requeue, delete, tasks)
- **🖥️ Workers** — render node status overview
- **⚙️ Settings** — notification preferences and preview defaults
- **ℹ️ Help** — show help and commands

### Preview Workflow
1. Check Dropbox for existing preview video.  
2. If absent, let the user choose between Deadline render or local server workflow.  
3. For the local workflow: download EXRs, assemble MP4, upload to Dropbox, send to chat.  
4. Clean up temporary files when finished.

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
- **Bot fails to start** — inspect `docker compose logs backend`, verify `.env`, confirm Telegram token.
- **Authentication issues** — ensure Deadline API (`BASE_API_URL`) is reachable and credentials are valid.
- **Preview failures** — check `OCIO_CONFIG_PATH`, `FFMPEG_PATH`, free space (`MIN_FREE_SPACE_BYTES`), and Dropbox EXR availability.

## Support & License
Open issues or PRs in the repository or contact the team directly. Specify the project license here (e.g., MIT) once finalized.
