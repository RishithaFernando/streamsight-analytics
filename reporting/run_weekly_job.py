"""
StreamSight weekly job (this is what the scheduler runs)
========================================================
  1. ETL load and data quality checks       (etl/etl_pipeline.py)
  2. Rebuild the churn feature table        (etl/run_sql.py sql/churn_features.sql)
  3. Train the churn model, score customers (ml/train_churn_model.py)
  4. Build the weekly HTML report           (reporting/weekly_report.py)

Stops at the first failing step, writes logs/ALERT_weekly_<time>.txt and exits with code 1.
Exit code 0 means everything worked. Works on Windows and Linux.

Usage:  python run_weekly_job.py
"""
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LOG_DIR = BASE / "logs"
PY = sys.executable
STEPS = [
    ("ETL load and data quality checks", [PY, str(BASE / "etl" / "etl_pipeline.py")]),
    ("Rebuild churn feature table", [PY, str(BASE / "etl" / "run_sql.py"), str(BASE / "sql" / "churn_features.sql")]),
    ("Train churn model and score customers", [PY, str(BASE / "ml" / "train_churn_model.py")]),
    ("Build weekly report", [PY, str(BASE / "reporting" / "weekly_report.py")]),
]


def main():
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"weekly_job_{stamp}.log"
    t_job = time.time()

    def log(msg):
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    log(f"Weekly job started ({len(STEPS)} steps)")
    for i, (name, cmd) in enumerate(STEPS, start=1):
        t0 = time.time()
        log(f"Step {i}/{len(STEPS)}: {name}")
        result = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True)
        secs = time.time() - t0
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"----- output of step {i} -----\n{result.stdout}\n{result.stderr}\n")
        if result.returncode != 0:
            tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-8:])
            log(f"Step {i} FAILED after {secs:.0f}s (exit code {result.returncode}). Stopping.")
            alert = LOG_DIR / f"ALERT_weekly_{stamp}.txt"
            alert.write_text(f"StreamSight weekly job failed at step {i}: {name}\n"
                             f"Time: {datetime.now():%Y-%m-%d %H:%M:%S}\nLast output:\n{tail}\n", encoding="utf-8")
            log(f"Alert file written: {alert}")
            return 1
        log(f"Step {i} OK ({secs:.0f}s)")
    log(f"Weekly job finished OK in {time.time() - t_job:.0f}s. Report: {BASE / 'reports' / 'weekly' / 'latest.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
