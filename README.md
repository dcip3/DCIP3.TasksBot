# TasksBot

TasksBot is a Telegram bot for monitoring Deadline render jobs, managing queue actions, and delivering previews from Dropbox or worker uploads.

## What It Does
- Authenticates users against Deadline REST (RCS) and stores encrypted credentials in SQLite.
- Shows jobs/workers in Telegram and supports actions: suspend, resume, requeue, delete.
- Builds previews in two ways:
  - on Deadline workers (preview job), or
  - on the bot host (download frames -> convert -> upload -> send).
- Supports auto-preview when jobs complete.
- Accepts direct worker-to-bot preview uploads via HTTP endpoint with one-time tokens.

## Current Architecture

### App layers
- `app/bot/handlers/` - Telegram routers (`auth`, `jobs`, `preview`, `settings`, `common`).
- `app/services/` - business/application services:
  - `deadline.py`
  - `dropbox.py`
  - `job_watcher.py`
  - `preview/pipeline.py`
  - `preview/render.py`
  - `preview/runtime.py`
- `app/storage/` - persistence layer:
  - `database.py` (connection + schema)
  - `user_settings.py` (user preferences CRUD)
- `app/auth/service.py` - Deadline auth/session logic.
- `app/core/` - runtime wiring and shared runtime utilities (`lifecycle`, `bot_core`, config, upload server, etc.).
- `app/integrations/` - Dropbox and media conversion helpers.

### Runtime flow
1. `main.py` starts aiogram polling.
2. `app/core/lifecycle.py` handles startup/shutdown, scheduler jobs, and background job watcher.
3. `app/services/job_watcher.py` polls Deadline and triggers notifications/auto-preview.
4. Preview runtime and delivery logic lives in `app/services/preview/runtime.py`.

## Getting Started

### Prerequisites
- Python 3.11+
- FFmpeg available in `PATH` (or configured via `FFMPEG_PATH`)
- Docker + Docker Compose (optional)

### 1. Clone and configure
```bash
git clone <repository-url>
cd DCIP3.TasksBot
cp .env.example .env
```

### 2. Run with Docker
```bash
docker compose up --build
docker compose logs -f tasksbot
```

## Automatic Deploy

The repository includes a minimal GitHub Actions deploy workflow at `.github/workflows/deploy.yml`.

### One-time VDS setup
1. Create a dedicated deploy user if needed.
2. Clone the repository to `/opt/docker/tasksbot`.
3. Copy `.env.example` to `.env` and fill in real secrets.
4. Make sure the deploy user can run Docker commands.
5. Generate an SSH key that GitHub Actions will use to connect to the server.

Example setup on the VDS:

```bash
ssh-keygen -t ed25519 -C "deploy" -f ~/.ssh/deploy_key
cat ~/.ssh/deploy_key.pub >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

### GitHub Actions secrets
Add these repository secrets:
- `VDS_HOST`
- `VDS_USER`
- `VDS_SSH_KEY`

`VDS_SSH_KEY` must contain the private key from `~/.ssh/deploy_key`.

If the repository is private, make sure the clone in `/opt/docker/tasksbot` is already configured so `git pull origin main` works on the server.

### Deploy flow
After setup, every `git push origin main` will:
1. trigger GitHub Actions,
2. SSH into the VDS,
3. run:

```bash
cd /opt/docker/tasksbot
git pull origin main
docker compose up -d --build --remove-orphans
```

### 3. Run locally
```bash
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## Configuration (.env)

### Required
| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram token from BotFather |
| `DEADLINE_API_URL` | Deadline REST endpoint (including scheme and `/api`) |
| `ENCRYPTION_KEY` | Fernet key for encrypting stored credentials |
| `DROPBOX_APP_KEY` | Dropbox app key |
| `DROPBOX_APP_SECRET` | Dropbox app secret |
| `DROPBOX_REFRESH_TOKEN` | Dropbox OAuth refresh token |
| `DROPBOX_TEAM_MEMBER_ID` | Dropbox team member ID |
| `DROPBOX_ROOT_NAMESPACE_ID` | Dropbox root namespace ID |

Generate key:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### Common optional
| Variable | Default | Purpose |
|---|---|---|
| `DEADLINE_TLS_VERIFY` | `True` | TLS verification for Deadline requests |
| `SQLITE_DB_PATH` | `data/app.db` | SQLite path |
| `TEMP_DIR` | `data/temp` | Temporary files |
| `CONV_DIR` | `data/conv` | Converted media |
| `OCIO_CONFIG_PATH` | `data/config.ocio` | OCIO config path |
| `JOB_WATCHER_INTERVAL_NORMAL` | `60` | Polling interval (seconds) |
| `JOB_WATCHER_INTERVAL_PREVIEW` | `5` | Faster polling when preview jobs are active |

### Preview upload (worker -> bot)
| Variable | Default | Purpose |
|---|---|---|
| `PREVIEW_UPLOAD_ENABLED` | `False` | Enable upload endpoint |
| `PREVIEW_UPLOAD_URL` | - | Public URL workers should post to |
| `PREVIEW_UPLOAD_BIND_HOST` | `0.0.0.0` | Bind host |
| `PREVIEW_UPLOAD_PORT` | `8081` | Bind port |
| `PREVIEW_UPLOAD_TOKEN_TTL` | `43200` | One-time token TTL (seconds) |
| `PREVIEW_UPLOAD_MAX_MB` | `100` | Max upload size |
| `PREVIEW_UPLOAD_INSECURE` | `False` | Allow insecure TLS |

Notes:
- Expose `PREVIEW_UPLOAD_PORT` from Docker/network to workers.
- Tokens are short-lived and persisted in SQLite.
- Upload handling uses async DB + async file I/O.

## Telegram Usage

### Commands
| Command | Description |
|---|---|
| `/start` | Open main menu |
| `/login` | Authorize with Deadline credentials |
| `/logout` | Clear stored credentials |
| `/help` | Show help |

### Main menu
- `📂 Jobs` - list jobs and perform actions.
- `🖥️ Workers` - list worker states.
- `⚙️ Settings` - notification scope, preview defaults, auto-preview settings.

## Preview Modes

### 1) Deadline preview
- Submits a preview job through Deadline.
- Worker runs `scripts/deadline_preview_worker.py`.
- Result is delivered to Telegram and optionally removed from Deadline queue.

### 2) Server preview
- Bot downloads image sequence from Dropbox.
- Converts/assembles video locally.
- Uploads video to Dropbox and sends to Telegram.

## Worker Setup Scripts
- `scripts/worker_setup.ps1` - installs Python + preview dependencies on Windows workers.
- `scripts/worker_setup.bat` - wrapper for PowerShell installer.
- `scripts/deadline_preview_worker.py` - worker-side preview converter.

## Project Structure
```text
.
├── app
│   ├── auth
│   │   ├── __init__.py
│   │   └── service.py
│   ├── bot
│   │   ├── handlers
│   │   │   ├── __init__.py
│   │   │   ├── auth.py
│   │   │   ├── common.py
│   │   │   ├── jobs.py
│   │   │   ├── preview.py
│   │   │   └── settings.py
│   │   └── job_helpers.py
│   ├── core
│   │   ├── bot_core.py
│   │   ├── config.py
│   │   ├── lifecycle.py
│   │   ├── maintenance.py
│   │   ├── preview_upload.py
│   │   └── ...
│   ├── integrations
│   │   ├── dropbox_helpers.py
│   │   └── video_helpers.py
│   ├── services
│   │   ├── deadline.py
│   │   ├── dropbox.py
│   │   ├── job_watcher.py
│   │   └── preview
│   │       ├── pipeline.py
│   │       ├── render.py
│   │       └── runtime.py
│   └── storage
│       ├── database.py
│       └── user_settings.py
├── data
├── scripts
├── Dockerfile
├── docker-compose.yml
├── main.py
└── requirements.txt
```

## Development

Quick checks:
```bash
python -m compileall -q app main.py
```

If tests are added:
```bash
pytest
```

## Troubleshooting
- Bot does not start: verify `.env`, check `docker compose logs -f tasksbot`.
- Auth fails: check `DEADLINE_API_URL`, TLS settings, and Deadline credentials.
- Preview issues: check `OCIO_CONFIG_PATH`, `FFMPEG_PATH`, Dropbox permissions/path mapping.
- Worker upload not reaching bot: validate `PREVIEW_UPLOAD_URL`, open port, and token TTL.

## Notes
- The project uses Deadline credentials as the auth model (no separate local users table).
- Package imports are direct module imports (service-barrel exports were intentionally removed).
