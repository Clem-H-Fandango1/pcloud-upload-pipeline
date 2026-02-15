# pCloud Upload Pipeline

A self-hosted upload pipeline with a web dashboard for automatically uploading files to pCloud (or any rclone-supported cloud storage).

## How It Works

```
[File Browser] → [Staging] → [Uploads] → [pCloud]
     copy/move     stability    rclone
     via dashboard  check       upload
```

1. **Browse & queue** files from your local storage via the web dashboard
2. **Staging watcher** checks files are fully written (not being copied), then moves them to the upload queue
3. **Upload watcher** uploads files to your cloud remote via rclone with progress tracking, checksums, and retries
4. **Dashboard** shows real-time progress, upload speed, ETA, and transfer history

## Features

- Web dashboard with authentication and file browser
- Real-time upload progress with speed and ETA
- SQLite transfer history
- Copy or move files into the pipeline
- Parallel copy operations (configurable 1-8)
- Failed upload retry
- Orphaned temp file detection and cleanup
- inotify-based watchers with periodic scan fallback

## Requirements

- Linux (Debian/Ubuntu)
- Docker with compose plugin
- rclone (with a configured remote)
- sqlite3, inotify-tools (installed automatically)

## Quick Install

```bash
git clone https://github.com/Clem-H-Fandango1/pcloud-upload-pipeline.git
cd pcloud-upload-pipeline
sudo bash install.sh
```

The installer will prompt for:
- **Pipeline base path** - where staging, uploads, and logs are stored
- **Browse paths** - directories the file browser can access
- **Dashboard password**
- **rclone remote** - e.g. `pcloud:/Auto-Uploads`
- **Dashboard port** - default 5001

## Manual Setup

1. Copy `.env.example` to `.env` and edit values
2. Install system deps: `apt install inotify-tools sqlite3 lsof`
3. Copy scripts to `/usr/local/bin/` and make executable
4. Copy systemd services to `/etc/systemd/system/`
5. Create `/etc/pcloud-pipeline.env` with your config
6. `systemctl enable --now pcloud-staging-watcher pcloud-upload-watcher`
7. `docker compose up -d --build`

## Uninstall

```bash
sudo bash uninstall.sh
```

Removes services, scripts, and config. Your pipeline data is preserved.

## Configuration

All configuration is via environment variables in `/etc/pcloud-pipeline.env`:

| Variable | Description | Default |
|---|---|---|
| `PIPELINE_BASE` | Base directory for pipeline data | - |
| `RCLONE_REMOTE` | rclone destination (e.g. `pcloud:/uploads`) | - |
| `ALLOWED_PATHS` | Comma-separated browse paths | `/data` |
| `DASHBOARD_PASSWORD` | Dashboard login password | `changeme` |
| `DASHBOARD_PORT` | Dashboard port | `5001` |
| `TZ` | Timezone | `UTC` |

## Architecture

```
pcloud-upload-pipeline/
├── docker/
│   ├── Dockerfile          # Python 3.11 Flask dashboard
│   └── app.py              # Dashboard application
├── scripts/
│   ├── pcloud-staging-watcher.sh   # inotify + periodic scan
│   └── pcloud-upload-watcher.sh    # rclone upload handler
├── systemd/
│   ├── pcloud-staging-watcher.service
│   └── pcloud-upload-watcher.service
├── docker-compose.yml
├── install.sh
├── uninstall.sh
└── .env.example
```

## License

MIT
