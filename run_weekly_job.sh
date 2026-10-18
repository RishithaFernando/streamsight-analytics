#!/usr/bin/env bash
# Weekly job for Linux / WSL / cron. Example crontab line (every Monday at 07:00):
#   0 7 * * 1 /home/YOUR_USER/streamsight/run_weekly_job.sh
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs
PY=python3
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; fi
"$PY" reporting/run_weekly_job.py >> logs/cron.log 2>&1
