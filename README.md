# TasksBot

A Telegram bot with FastAPI backend and WebApp for monitoring Deadline render jobs and managing project files in Dropbox.

## Features

- **Telegram Bot** - Built on aiogram v3 with Deadline authentication and job management commands
- **REST API** - FastAPI backend for WebApp and external integrations
- **Real-time Notifications** - Job completion alerts and progress updates
- **Video Conversion** - Automated EXR → MP4 conversion with OCIO color management
- **Dropbox Integration** - Download source files and upload rendered media
- **Telegram WebApp** - React/Vite frontend for modern UI inside Telegram
- **Docker Support** - Complete containerized environment with Nginx reverse proxy

## Quick Start

### 1. Clone and Setup

```bash
git clone <repository-url>
cd DCIP3.TasksBot
cp .env.example .env
```

### 2. Configure Environment

Edit `.env` file with your credentials (see [Configuration](#configuration) below).

### 3. Run with Docker

```bash
docker compose up --build
```

### 4. Run Locally (Development)

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## Configuration

All configuration is done via `.env` file. Copy `.env.example` and fill in your values.

### Application Settings

| Variable | Description | Example |
|----------|-------------|---------|
| `APP_DOMAIN` | Your application domain | `example.com` |
| `BASE_API_URL` | Deadline REST API endpoint | `https://deadline.local:8443/api` |
| `HTTP_TIMEOUT` | HTTP request timeout (seconds) | `30` |
| `DEV_MODE` | Development mode (disables auth checks) | `false` (production) / `true` (dev only) |
| `CORS_ORIGINS` | Allowed CORS origins (comma-separated) | `https://example.com` |

**Security Notes:**
- `DEV_MODE` must be `false` in production!
- `CORS_ORIGINS` should match your `MINI_APP_URL` domain
- Multiple origins: `https://domain1.com,https://domain2.com`

### Telegram Settings

| Variable | Description | How to Get |
|----------|-------------|------------|
| `TG_API_TOKEN` | Telegram bot token | [@BotFather](https://t.me/botfather) |
| `MINI_APP_URL` | WebApp URL | Your deployed frontend URL |

**Example:**
```bash
TG_API_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
MINI_APP_URL=https://tasksbot.example.com
```

### Dropbox Settings

| Variable | Description | How to Get |
|----------|-------------|------------|
| `DROPBOX_APP_KEY` | App key | [Dropbox App Console](https://www.dropbox.com/developers/apps) |
| `DROPBOX_APP_SECRET` | App secret | Dropbox App Console |
| `DROPBOX_REFRESH_TOKEN` | OAuth refresh token | Generate via OAuth flow |
| `DROPBOX_TEAM_MEMBER_ID` | Team member ID | Dropbox API |
| `DROPBOX_ROOT_NAMESPACE_ID` | Team namespace ID | Dropbox API |
| `DROPBOX_ROOT_MARKER` | Folder marker in paths | `Team Folder` |

**Getting Dropbox Credentials:**
1. Create app at [Dropbox App Console](https://www.dropbox.com/developers/apps)
2. Generate OAuth token with required scopes
3. For team folders, use team member ID and namespace ID

### Storage Settings

| Variable | Description | Default |
|----------|-------------|---------|
| `DB_PATH` | SQLite database file | `storage/tasks_bot.db` |
| `TEMP_DIR` | Temporary files directory | `storage/temp` |
| `CONV_DIR` | Converted videos directory | `storage/conv` |
| `OCIO_CONFIG_PATH` | OCIO config file path | `storage/config.ocio` |
| `FFMPEG_PATH` | FFmpeg executable path | `ffmpeg` |

**Setup:**
```bash
mkdir -p storage/conv storage/temp
# Copy your OCIO config if needed
cp /path/to/config.ocio storage/config.ocio
```

### Preview Rendering Settings

| Variable | Description | Default |
|----------|-------------|---------|
| `PREVIEW_APPLY_COLOR_TRANSFORM` | Enable OCIO color transform | `True` |
| `PREVIEW_INPUT_SPACE` | Input color space | `ACEScg` |
| `PREVIEW_DISPLAY` | Display device | `sRGB` |
| `PREVIEW_VIEW` | View transform | `ACES 1.0 SDR-video` |
| `PREVIEW_LUT_SIZE` | LUT cube size | `65` |
| `PREVIEW_COLOR_MODE` | Transform mode: `lut` or `cpu` | `lut` |
| `PREVIEW_PYTHON_EXECUTABLE` | Python on workers | `python` |
| `FFMPEG_PATH` | FFmpeg on workers | `ffmpeg` |

**Color Transform Modes:**
- `lut` - Generate LUT on worker (faster, recommended)
- `cpu` - Apply OCIO transform on CPU (slower, more accurate)

### Performance Settings

| Variable | Description | Default |
|----------|-------------|---------|
| `MAX_CONCURRENT_DOWNLOADS` | Parallel Dropbox downloads | `2` |
| `MIN_FREE_SPACE_BYTES` | Minimum free disk space | `10737418240` (10GB) |
| `JOB_WATCHER_INTERVAL_NORMAL` | Job check interval (seconds) | `60` |
| `JOB_WATCHER_INTERVAL_PREVIEW` | Preview job interval (seconds) | `15` |

### Security Settings

| Variable | Description | Required |
|----------|-------------|----------|
| `PASSWORD_SALT` | Password hashing salt | ✅ Change default |
| `ENCRYPTION_KEY` | Fernet encryption key | ✅ Generate new |

**Generate encryption key:**
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Repository Structure

```
.
├── app/
│   ├── auth/              # Authentication and session management
│   ├── bot/               # Telegram bot handlers and FSM
│   ├── core/              # Configuration, database, utilities
│   │   ├── bot_core.py    # Bot instance and global state
│   │   ├── config.py      # Settings and environment variables
│   │   ├── database.py    # SQLite database operations
│   │   ├── ttl_cache.py   # TTL cache for memory management
│   │   └── utils.py       # Utility functions and decorators
│   ├── integrations/      # API routes, Dropbox, video processing
│   ├── services/          # Business logic and external APIs
│   └── storage/           # Storage helpers
├── infra/
│   ├── backend/           # Backend Dockerfile
│   └── nginx/             # Nginx configuration templates
├── mini-app/              # React/Vite WebApp
├── scripts/               # Utility scripts
├── storage/               # Data storage (gitignored)
│   ├── tasks_bot.db       # SQLite database (auto-created)
│   ├── temp/              # Temporary downloads
│   └── conv/              # Converted videos
├── docker-compose.yml     # Docker Compose configuration
├── main.py                # Application entry point
└── requirements.txt       # Python dependencies
```

## Running the Application

### Docker Compose (Recommended)

```bash
# Start all services
docker compose up -d

# View logs
docker compose logs -f backend

# Stop services
docker compose down
```

**Services:**
- `backend` - Telegram bot + FastAPI (port 8000)
- `frontend` - Vite dev server (port 3000)
- `nginx` - Reverse proxy with HTTPS

### Local Development

**Backend:**
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

**Frontend:**
```bash
cd mini-app
npm install
npm run dev
```

**Bot Only (No WebApp):**
```bash
python scripts/run_bot_local.py
```

## Usage

### Telegram Bot Commands

- `/start` - Initialize bot and show main menu
- `/login` - Authenticate with Deadline credentials
- `/logout` - Clear session and logout

**Main Menu Buttons:**
- **Jobs** - View and manage render jobs
- **Workers** - Check worker/slave status
- **Realtime** - Auto-updating job progress
- **Settings** - Configure notifications
- **Clear** - Clear chat history

### Job Actions

For each job you can:
- **Preview** - Generate video preview (via Deadline or server)
- **Suspend/Resume** - Control job execution
- **Requeue** - Restart failed tasks
- **Delete** - Remove job from queue
- **Tasks** - View detailed task list

### Notifications

Enable notifications in **Settings**:
- **Receive notifications** - Toggle on/off
- **All jobs** - Get notified about all completed jobs
- **My jobs only** - Only your jobs

## API Endpoints

FastAPI provides REST API at `/api/`:

- `POST /api/auth/login` - Authenticate user
- `GET /api/jobs` - List all jobs
- `GET /api/jobs/{job_id}` - Get job details
- `GET /api/jobs/{job_id}/tasks` - Get job tasks
- `PUT /api/jobs/{job_id}/requeue` - Requeue job
- `DELETE /api/jobs/{job_id}` - Delete job
- `GET /api/slaves` - List workers
- `POST /api/jobs/{job_id}/create-video` - Generate preview

**Documentation:**
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Preview Video Generation

TasksBot can generate MP4 previews from EXR sequences in two ways:

### 1. Deadline Render (Recommended)

- Submits CommandLine job to Deadline
- Runs FFmpeg on render workers
- Applies OCIO color transform (optional)
- Faster and doesn't load the bot server

### 2. Server Render

- Downloads EXRs from Dropbox
- Converts and assembles video on bot server
- Uploads result back to Dropbox
- Use when workers are unavailable

**Color Management:**
- Supports OCIO color transforms
- Converts ACEScg → sRGB by default
- Configurable via `PREVIEW_*` environment variables

## Troubleshooting

### Bot doesn't start

Check logs:
```bash
docker compose logs backend
```

Common issues:
- Missing environment variables
- Invalid Telegram token
- Database file permissions

### Authentication fails

- Verify Deadline API is accessible
- Check `BASE_API_URL` is correct
- Ensure credentials are valid

### CORS errors in WebApp

Ensure `CORS_ORIGINS` matches `MINI_APP_URL`:
```bash
# Both should use same domain and protocol
MINI_APP_URL=https://example.com
CORS_ORIGINS=https://example.com
```

### Memory issues

- TTL cache prevents memory leaks
- Check cache stats in logs (every hour)
- Old notifications auto-deleted after 1 hour

### Video conversion fails

- Check `OCIO_CONFIG_PATH` exists
- Verify `FFMPEG_PATH` is correct
- Ensure enough disk space (`MIN_FREE_SPACE_BYTES`)

## Development

### Code Style

```bash
# Format code
black app/

# Lint
flake8 app/
```

### Testing

```bash
pytest
```

### Adding New Features

1. Update settings in `app/core/config.py`
2. Add handlers in `app/bot/handlers.py`
3. Implement services in `app/services/`
4. Update API routes in `app/integrations/api_routes.py`

## Security

**Important:**
- `DEV_MODE=false` in production (disables auth bypass)
- `CORS_ORIGINS` should list only trusted domains
- Change default `PASSWORD_SALT` and `ENCRYPTION_KEY`
- Use HTTPS in production
- Keep `.env` file secure and out of git

**Memory Management:**
- Notification cache auto-clears every hour
- Maximum 10,000 cached notifications
- Prevents memory leaks from unbounded growth

## Dependencies

**Core:**
- Python 3.11+
- aiogram 3.2+ (Telegram bot framework)
- FastAPI 0.104+ (REST API)
- aiohttp 3.8+ (Async HTTP client)

**Media Processing:**
- OpenEXR 1.3+ (EXR file handling)
- OpenColorIO 2.3+ (Color management)
- Pillow 10.0+ (Image processing)
- FFmpeg (external, required on workers)

**Storage:**
- aiosqlite 0.19+ (Async SQLite)
- SQLite 3 (Database)

**Full list:** See `requirements.txt`

## License

[Your License Here]

## Support

For issues and questions:
- Check logs: `docker compose logs backend`
- Review configuration in `.env`
- See `SECURITY_FIX.md` for recent security updates

## Links

- [aiogram Documentation](https://docs.aiogram.dev/)
- [Deadline REST API](https://docs.thinkboxsoftware.com/products/deadline/)
- [Dropbox API](https://www.dropbox.com/developers/documentation/http/overview)
- [Telegram WebApp](https://core.telegram.org/bots/webapps)
- [OpenColorIO](https://opencolorio.org/)
