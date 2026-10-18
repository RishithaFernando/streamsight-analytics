#!/usr/bin/env bash
# Daily backup with restore test for Linux / WSL / cron. Example crontab line (every day at 02:00):
#   0 2 * * * /home/YOUR_USER/streamsight/run_backup.sh
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs
PY=python3
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; fi
"$PY" backup/backup_database.py >> logs/backup_cron.log 2>&1
