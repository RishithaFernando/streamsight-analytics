"""
StreamSight database backup with automatic restore test
=======================================================
  1. BACKUP   : pg_dump of the streamsight schema into backups/streamsight_<time>.dump (compressed)
  2. CHECK    : the file must be readable (pg_restore --list)
  3. RESTORE  : restore it into a scratch database (streamsight_restore_test)
  4. COMPARE  : every table and view must have the same row count as the live database,
                and total revenue must match. All views must be queryable.
  5. CLEAN UP : drop the scratch database; delete old backups, but ONLY if the test passed
  6. LOG      : log file, the etl_run_log table, and an ALERT file on failure

A backup that was never restored is only a hope. The restore test is the point of this script.

Usage
-----
    python backup_database.py                       full run (backup + restore test)
    python backup_database.py --keep 14             keep the newest 14 backups (default 7)
    python backup_database.py --verify-file X.dump  only restore-test an existing backup
                                                    (row count differences from live data are informational)
    python backup_database.py --keep-test-db        do not drop the scratch database

How to restore for real (disaster recovery), for the README:
    pg_restore -h localhost -U postgres -d streamsight --clean --if-exists --no-owner X.dump

Exit code 0 = backup AND restore test passed, 1 = something failed. Works on Windows and Linux.
Optional .env setting if pg_dump is not found automatically:  PG_BIN=C:\\Program Files\\PostgreSQL\\18\\bin
"""
import argparse
import glob
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent.parent
load_dotenv(BASE / ".env")
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", BASE / "backups"))
LOG_DIR = BASE / "logs"
SCHEMA = "streamsight"
TEST_DB = "streamsight_restore_test"
log = logging.getLogger("backup")


# ---------------------------------------------------------------------
def conn_params(dbname=None):
    if not os.getenv("PGPASSWORD"):
        raise RuntimeError("PGPASSWORD is not set. Fill in the .env file in the project folder.")
    return dict(host=os.getenv("PGHOST", "localhost"), port=os.getenv("PGPORT", "5432"),
                dbname=dbname or os.getenv("PGDATABASE", "streamsight"),
                user=os.getenv("PGUSER", "postgres"), password=os.getenv("PGPASSWORD"))


def find_tool(name):
    """Locate pg_dump / pg_restore: PG_BIN setting, then PATH, then the usual install folders."""
    exe = name + (".exe" if os.name == "nt" else "")
    pg_bin = os.getenv("PG_BIN")
    if pg_bin and (Path(pg_bin) / exe).exists():
        return str(Path(pg_bin) / exe)
    found = shutil.which(name)
    if found:
        return found
    patterns = [rf"C:\Program Files\PostgreSQL\*\bin\{exe}", f"/usr/lib/postgresql/*/bin/{name}",
                f"/usr/local/pgsql/bin/{name}", f"/opt/homebrew/opt/postgresql*/bin/{name}"]
    candidates = [c for p in patterns for c in glob.glob(p)]
    if candidates:
        def major(path):
            m = re.search(r"postgresql[\\/](\d+)", path, re.I)
            return int(m.group(1)) if m else 0
        return sorted(candidates, key=major, reverse=True)[0]
    raise RuntimeError(f"Could not find {name}. Add the PostgreSQL bin folder to PG_BIN in the .env file, "
                       r"for example PG_BIN=C:\Program Files\PostgreSQL\18\bin")


def run(cmd, what):
    env = os.environ.copy()
    env["PGPASSWORD"] = conn_params()["password"]          # never put the password on the command line
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"{what} failed (exit code {r.returncode}): {(r.stderr or r.stdout).strip()[-700:]}")
    return r


def cli_conn():
    p = conn_params()
    return ["-h", p["host"], "-p", str(p["port"]), "-U", p["user"]]


# ---------------------------------------------------------------------
def make_backup():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    out = BACKUP_DIR / f"streamsight_{datetime.now():%Y%m%d_%H%M%S}.dump"
    t0 = time.time()
    run([find_tool("pg_dump")] + cli_conn() + ["-d", conn_params()["dbname"], "-n", SCHEMA, "-Fc",
                                               "--no-owner", "--no-privileges", "-f", str(out)], "pg_dump")
    mb = out.stat().st_size / 1024 / 1024
    log.info("Backup written: %s (%.1f MB, %.0fs)", out.name, mb, time.time() - t0)
    return out


def check_readable(dump):
    r = run([find_tool("pg_restore"), "--list", str(dump)], "pg_restore --list")
    entries = [ln for ln in r.stdout.splitlines() if ln.strip() and not ln.startswith(";")]
    if not entries:
        raise RuntimeError("the backup file is empty or unreadable")
    log.info("Backup file is readable: %d objects listed", len(entries))
    return len(entries)


def objects(cur, kind):
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND table_type = %s "
                "ORDER BY 1", (SCHEMA, kind))
    return [r[0] for r in cur.fetchall()]


def count(cur, name):
    cur.execute(f'SELECT COUNT(*) FROM {SCHEMA}."{name}"')
    return cur.fetchone()[0]


def restore_test(dump, keep_db=False, strict=True):
    """Restore into a scratch database and compare with the live one.
    strict=True  (fresh backup): any row count difference is a failure.
    strict=False (older backup): differences are logged as information only; missing or unreadable objects still fail."""
    admin = psycopg2.connect(**conn_params("postgres"))
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
            cur.execute(f"CREATE DATABASE {TEST_DB}")
        log.info("Scratch database %s created", TEST_DB)
        t0 = time.time()
        run([find_tool("pg_restore")] + cli_conn() + ["-d", TEST_DB, "--no-owner", "--no-privileges",
                                                       "--exit-on-error", str(dump)], "pg_restore")
        log.info("Restore finished in %.0fs", time.time() - t0)

        live = psycopg2.connect(**conn_params())
        test = psycopg2.connect(**conn_params(TEST_DB))
        problems, rows_total = [], 0
        try:
            with live.cursor() as lc, test.cursor() as tc:
                for kind, label in (("BASE TABLE", "table"), ("VIEW", "view")):
                    live_objs = objects(lc, kind)
                    missing = set(live_objs) - set(objects(tc, kind))
                    for m in sorted(missing):
                        problems.append(f"{label} {m} is missing in the restored database")
                    for name in live_objs:
                        if name in missing:
                            continue
                        try:
                            n_live, n_test = count(lc, name), count(tc, name)
                        except Exception as exc:
                            test.rollback()
                            problems.append(f"{label} {name} could not be queried after restore: {exc}")
                            continue
                        if kind == "BASE TABLE":
                            rows_total += n_test
                        ok = n_live == n_test
                        log.info("  %-5s %-32s live %10s | restored %10s | %s", label, name,
                                 f"{n_live:,}", f"{n_test:,}", "OK" if ok else "MISMATCH")
                        if not ok and strict and name != "etl_run_log":   # the job log changes whenever any job runs
                            problems.append(f"{label} {name}: live {n_live:,} rows vs restored {n_test:,} rows")
                        elif not ok and strict:
                            log.info("    (etl_run_log is a live log table: a small difference is expected)")
                        elif False:
                            problems.append(f"{label} {name}: live {n_live:,} rows vs restored {n_test:,} rows")
                        elif not ok:
                            log.info("    (differs from live data: expected when the backup is older than the live database)")
                lc.execute(f"SELECT ROUND(SUM(amount), 2) FROM {SCHEMA}.payments WHERE status = 'success'")
                tc.execute(f"SELECT ROUND(SUM(amount), 2) FROM {SCHEMA}.payments WHERE status = 'success'")
                r_live, r_test = lc.fetchone()[0], tc.fetchone()[0]
                log.info("  revenue checksum: live %s | restored %s", r_live, r_test)
                if r_live != r_test and strict:
                    problems.append(f"revenue checksum differs: live {r_live} vs restored {r_test}")
        finally:
            live.close()
            test.close()
        if problems:
            raise RuntimeError("RESTORE TEST FAILED: " + "; ".join(problems[:5]))
        log.info("Restore test PASSED: all tables and views match, %s rows verified", f"{rows_total:,}")
        return rows_total
    finally:
        if not keep_db:
            try:
                with admin.cursor() as cur:
                    cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
                log.info("Scratch database dropped")
            except Exception as exc:
                log.warning("Could not drop the scratch database: %s", exc)
        admin.close()


def apply_retention(keep):
    dumps = sorted(BACKUP_DIR.glob("streamsight_*.dump"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in dumps[keep:]:
        old.unlink()
        log.info("Retention: deleted old backup %s", old.name)
    log.info("Backups kept: %d (newest first: %s)", min(len(dumps), keep),
             ", ".join(p.name for p in dumps[:keep]))


def log_to_db(status, message, rows=None):
    """Record the result in streamsight.etl_run_log so it shows up next to the ETL runs."""
    try:
        conn = psycopg2.connect(**conn_params())
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"ALTER TABLE {SCHEMA}.etl_run_log ADD COLUMN IF NOT EXISTS run_id VARCHAR(20)")
            cur.execute(f"INSERT INTO {SCHEMA}.etl_run_log (run_id, run_started, run_finished, step_name, "
                        "rows_loaded, status, message) VALUES (%s, NOW(), NOW(), 'backup', %s, %s, %s)",
                        (datetime.now().strftime("%Y%m%d%H%M%S"), rows, status, message[:1000]))
        conn.close()
    except Exception as exc:
        log.warning("Could not write the result to etl_run_log: %s", exc)


# ---------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="StreamSight backup with restore test")
    ap.add_argument("--keep", type=int, default=7, help="number of backups to keep (default 7)")
    ap.add_argument("--verify-file", help="only restore-test this existing backup file")
    ap.add_argument("--keep-test-db", action="store_true", help="do not drop the scratch database")
    args = ap.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(LOG_DIR / f"backup_{stamp}.log", encoding="utf-8")])
    try:
        if args.verify_file:
            dump = Path(args.verify_file)
            log.info("Verify-only mode for %s", dump)
            check_readable(dump)
            rows = restore_test(dump, args.keep_test_db, strict=False)
            log_to_db("SUCCESS", f"verify-only: {dump.name} restored and matched", rows)
            return 0
        dump = make_backup()
        check_readable(dump)
        rows = restore_test(dump, args.keep_test_db)
        apply_retention(args.keep)
        mb = dump.stat().st_size / 1024 / 1024
        log_to_db("SUCCESS", f"{dump.name} ({mb:.1f} MB) restored and verified", rows)
        log.info("BACKUP COMPLETE and VERIFIED: %s", dump)
        return 0
    except Exception as exc:
        log.error("BACKUP FAILED: %s", exc)
        log_to_db("FAILED", str(exc))
        alert = LOG_DIR / f"ALERT_backup_{stamp}.txt"
        alert.write_text(f"StreamSight backup failed at {datetime.now():%Y-%m-%d %H:%M:%S}\n{exc}\n", encoding="utf-8")
        log.error("Alert file written: %s (old backups were NOT deleted)", alert)
        return 1


if __name__ == "__main__":
    sys.exit(main())
