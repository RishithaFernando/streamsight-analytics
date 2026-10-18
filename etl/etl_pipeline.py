"""
StreamSight ETL pipeline
========================
Extract  : read the CSV files from the data folder
Validate : data quality checks BEFORE touching the database
Load     : load all tables inside ONE transaction (all or nothing)
Verify   : reconcile row counts and totals after loading
Log      : every step is written to the log file and to streamsight.etl_run_log

Safe to re-run: tables are truncated and reloaded inside the same transaction,
so a failed run never leaves the database half loaded.

Usage
-----
    python etl_pipeline.py                  # full run
    python etl_pipeline.py --validate-only  # only run the quality checks

Exit code is 0 on success and 1 on failure (useful for cron and monitoring).
Database settings come from the .env file (see .env.example).
"""
import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import psycopg2
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent.parent
load_dotenv(BASE / ".env")
DATA_DIR = Path(os.getenv("DATA_DIR", BASE / "data"))
LOG_DIR = BASE / "logs"
SCHEMA = "streamsight"

# ---------------------------------------------------------------------
# Table definitions (load order matters: parents first)
# ---------------------------------------------------------------------
TABLES = [
    {"name": "plans", "pk": ["plan_id"],
     "cols": ["plan_id", "plan_name", "monthly_price", "max_screens", "max_quality"],
     "not_null": ["plan_id", "plan_name", "monthly_price"], "non_negative": ["monthly_price"],
     "dates": [], "allowed": {}, "fks": []},
    {"name": "plan_price_history", "pk": ["plan_id", "effective_from"],
     "cols": ["plan_id", "effective_from", "monthly_price"],
     "not_null": ["plan_id", "effective_from", "monthly_price"], "non_negative": ["monthly_price"],
     "dates": ["effective_from"], "allowed": {}, "fks": [("plan_id", "plans")]},
    {"name": "subscribers", "pk": ["subscriber_id"],
     "cols": ["subscriber_id", "signup_date", "country", "city", "age_group", "gender", "device_type",
              "acquisition", "current_plan_id", "status", "churn_date", "churn_reason"],
     "not_null": ["subscriber_id", "signup_date", "country", "age_group", "device_type", "acquisition",
                  "current_plan_id", "status"],
     "non_negative": [], "dates": ["signup_date", "churn_date"],
     "allowed": {"status": ["active", "churned"]}, "fks": [("current_plan_id", "plans")]},
    {"name": "content", "pk": ["content_id"],
     "cols": ["content_id", "title", "content_type", "genre", "release_year", "language", "is_original",
              "added_date", "duration_min"],
     "not_null": ["content_id", "title", "content_type", "genre", "duration_min"],
     "non_negative": ["duration_min"], "dates": ["added_date"],
     "allowed": {"content_type": ["movie", "series"]}, "fks": []},
    {"name": "viewing_sessions", "pk": ["session_id"],
     "cols": ["session_id", "subscriber_id", "content_id", "session_start", "minutes_watched",
              "device_type", "completed"],
     "not_null": ["session_id", "subscriber_id", "content_id", "session_start", "minutes_watched"],
     "non_negative": ["minutes_watched"], "dates": ["session_start"], "allowed": {},
     "fks": [("subscriber_id", "subscribers"), ("content_id", "content")]},
    {"name": "payments", "pk": ["payment_id"],
     "cols": ["payment_id", "subscriber_id", "plan_id", "payment_date", "amount", "status"],
     "not_null": ["payment_id", "subscriber_id", "plan_id", "payment_date", "amount", "status"],
     "non_negative": ["amount"], "dates": ["payment_date"],
     "allowed": {"status": ["success", "failed"]},
     "fks": [("subscriber_id", "subscribers"), ("plan_id", "plans")]},
    {"name": "support_tickets", "pk": ["ticket_id"],
     "cols": ["ticket_id", "subscriber_id", "created_at", "category", "resolved", "resolution_hours"],
     "not_null": ["ticket_id", "subscriber_id", "created_at", "category", "resolved"],
     "non_negative": ["resolution_hours"], "dates": ["created_at"], "allowed": {},
     "fks": [("subscriber_id", "subscribers")]},
]

log = logging.getLogger("etl")


# ---------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------
def connect(autocommit=False):
    if not os.getenv("PGPASSWORD"):
        raise RuntimeError("PGPASSWORD is not set. Copy .env.example to .env and fill in your PostgreSQL password.")
    conn = psycopg2.connect(
        host=os.getenv("PGHOST", "localhost"), port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE", "streamsight"), user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD"))
    conn.autocommit = autocommit
    return conn


class RunLog:
    """Writes one row per step into streamsight.etl_run_log (separate autocommit connection,
    so log rows survive even when the load transaction is rolled back)."""

    def __init__(self, run_id, conn):
        self.run_id, self.conn = run_id, conn
        with conn.cursor() as cur:
            cur.execute(f"ALTER TABLE {SCHEMA}.etl_run_log ADD COLUMN IF NOT EXISTS run_id VARCHAR(20)")

    def step(self, name, status, rows=None, message=None, started=None):
        started = started or datetime.now()
        with self.conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {SCHEMA}.etl_run_log "
                "(run_id, run_started, run_finished, step_name, rows_loaded, status, message) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (self.run_id, started, datetime.now(), name,
                 None if rows is None else int(rows), status, message))

    def last_success_rows(self, step_name):
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT rows_loaded FROM {SCHEMA}.etl_run_log WHERE step_name=%s AND status='SUCCESS' "
                "AND run_id <> %s ORDER BY log_id DESC LIMIT 1", (step_name, self.run_id))
            r = cur.fetchone()
            return r[0] if r else None


# ---------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------
def validate_table(spec, df, keys):
    """Return (errors, warnings) for one table."""
    errors, warnings = [], []
    name = spec["name"]

    if set(df.columns) != set(spec["cols"]):
        missing = set(spec["cols"]) - set(df.columns)
        extra = set(df.columns) - set(spec["cols"])
        errors.append(f"column mismatch (missing={sorted(missing)}, unexpected={sorted(extra)})")
        return errors, warnings
    if len(df) == 0:
        errors.append("file is empty")
        return errors, warnings

    for c in spec["not_null"]:
        n = int(df[c].isna().sum())
        if n:
            errors.append(f"{c}: {n} null values")

    dup = int(df.duplicated(subset=spec["pk"]).sum())
    if dup:
        errors.append(f"{dup} duplicate primary keys {spec['pk']}")

    for c, allowed in spec["allowed"].items():
        bad = int((~df[c].isin(allowed) & df[c].notna()).sum())
        if bad:
            errors.append(f"{c}: {bad} values outside {allowed}")

    for c in spec["non_negative"]:
        vals = pd.to_numeric(df[c], errors="coerce")
        bad = int(((vals < 0) | (vals.isna() & df[c].notna())).sum())
        if bad:
            errors.append(f"{c}: {bad} negative or non numeric values")

    for c in spec["dates"]:
        parsed = pd.to_datetime(df[c], errors="coerce")
        bad = int((parsed.isna() & df[c].notna()).sum())
        if bad:
            errors.append(f"{c}: {bad} unparseable dates")
        future = int((parsed > pd.Timestamp.now() + pd.Timedelta(days=1)).sum())
        if future:
            warnings.append(f"{c}: {future} dates in the future")

    for col, ref in spec["fks"]:
        bad = int((~df[col].isin(keys[ref])).sum())
        if bad:
            errors.append(f"{col}: {bad} rows with no matching {ref} record (orphans)")

    return errors, warnings


def business_rules(dfs):
    """Cross-table rules. Returns {table: [error messages]}."""
    out = {}
    sub = dfs["subscribers"]
    signup = pd.to_datetime(sub.set_index("subscriber_id")["signup_date"])
    churn = pd.to_datetime(sub.set_index("subscriber_id")["churn_date"])

    e = []
    n = int(((sub.status == "churned") != sub.churn_date.notna()).sum())
    if n:
        e.append(f"{n} rows where status and churn_date disagree")
    n = int((churn.reindex(sub.subscriber_id).values < signup.reindex(sub.subscriber_id).values).sum())
    if n:
        e.append(f"{n} subscribers churned before they signed up")
    out["subscribers"] = e

    for table, col, label in [("viewing_sessions", "session_start", "sessions"),
                              ("payments", "payment_date", "payments"),
                              ("support_tickets", "created_at", "tickets")]:
        df = dfs[table]
        d = pd.to_datetime(df[col]).dt.normalize()
        s = df.subscriber_id.map(signup)
        c = df.subscriber_id.map(churn)
        e = []
        n = int((d < s).sum())
        if n:
            e.append(f"{n} {label} dated before the subscriber signed up")
        if table != "support_tickets":
            n = int((d > c).sum())
            if n:
                e.append(f"{n} {label} dated after the subscriber churned")
        out[table] = e
    return out


# ---------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------
def run(args, run_id):
    t_start = time.time()
    run_log = RunLog(run_id, connect(autocommit=True))
    log.info("Run %s started. Data folder: %s", run_id, DATA_DIR)

    # ---------- 1. Extract + validate ----------
    dfs, keys, all_errors = {}, {}, []
    for spec in TABLES:
        name = spec["name"]
        started = datetime.now()
        path = DATA_DIR / f"{name}.csv"
        if not path.exists():
            msg = f"file not found: {path}"
            log.error("%-20s %s", name, msg)
            run_log.step(f"validate:{name}", "FAILED", None, msg, started)
            all_errors.append((name, msg))
            continue
        df = pd.read_csv(path)
        dfs[name] = df
        errors, warnings = validate_table(spec, df, keys)
        if len(spec["pk"]) == 1 and not errors:
            keys[name] = set(df[spec["pk"][0]])
        elif len(spec["pk"]) == 1:
            keys[name] = set(df[spec["pk"][0]].dropna()) if spec["pk"][0] in df else set()

        prev = run_log.last_success_rows(f"load:{name}")
        if prev and len(df) < 0.5 * prev:
            warnings.append(f"row count dropped from {prev:,} to {len(df):,} compared with the last successful load")

        status = "FAILED" if errors else ("WARNING" if warnings else "SUCCESS")
        msg = "; ".join(errors + warnings) if (errors or warnings) else "all checks passed"
        log.info("%-20s %-8s %9s rows | %s", name, status, f"{len(df):,}", msg)
        run_log.step(f"validate:{name}", status, len(df), msg[:1000], started)
        all_errors += [(name, e) for e in errors]

    if len(dfs) == len(TABLES) and not all_errors:
        started = datetime.now()
        rules = business_rules(dfs)
        errs = [(t, m) for t, ms in rules.items() for m in ms]
        status = "FAILED" if errs else "SUCCESS"
        msg = "; ".join(f"{t}: {m}" for t, m in errs) if errs else "all business rules passed"
        log.info("%-20s %-8s %s", "business_rules", status, msg)
        run_log.step("validate:business_rules", status, None, msg[:1000], started)
        all_errors += errs

    if all_errors:
        raise RuntimeError(f"Validation failed with {len(all_errors)} error(s). Nothing was loaded. "
                           f"First error: {all_errors[0][0]}: {all_errors[0][1]}")

    if args.validate_only:
        log.info("Validate-only mode: stopping before load. All checks passed.")
        run_log.step("pipeline", "SUCCESS", None, "validate-only run",
                     datetime.fromtimestamp(t_start))
        return

    # ---------- 2. Load (single transaction) ----------
    conn = connect(autocommit=False)
    results = []
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE " + ", ".join(f"{SCHEMA}.{t['name']}" for t in TABLES))
            log.info("Tables truncated (inside transaction, not yet committed)")
            for spec in TABLES:
                name = spec["name"]
                started = datetime.now()
                cols = ", ".join(dfs[name].columns)
                with open(DATA_DIR / f"{name}.csv", "r", encoding="utf-8") as f:
                    cur.copy_expert(f"COPY {SCHEMA}.{name} ({cols}) FROM STDIN WITH (FORMAT csv, HEADER true)", f)
                cur.execute(f"SELECT COUNT(*) FROM {SCHEMA}.{name}")
                n_db = cur.fetchone()[0]
                if n_db != len(dfs[name]):
                    raise RuntimeError(f"{name}: loaded {n_db:,} rows but the file has {len(dfs[name]):,}")
                results.append((name, n_db, started))
                log.info("%-20s loaded %9s rows", name, f"{n_db:,}")

            # reconciliation: totals in the database must match the files
            cur.execute(f"SELECT ROUND(SUM(amount),2) FROM {SCHEMA}.payments WHERE status='success'")
            db_rev = float(cur.fetchone()[0])
            file_rev = float(dfs["payments"].loc[dfs["payments"].status == "success", "amount"].sum())
            if abs(db_rev - file_rev) > 0.05:
                raise RuntimeError(f"revenue mismatch: database {db_rev} vs file {file_rev}")
            cur.execute(f"SELECT COUNT(*) FROM {SCHEMA}.subscribers WHERE status='churned'")
            if cur.fetchone()[0] != int((dfs["subscribers"].status == "churned").sum()):
                raise RuntimeError("churned subscriber count mismatch")
            log.info("Reconciliation passed (row counts, revenue %.2f, churned count)", db_rev)

            cur.execute(f"SELECT MAX(session_start), (SELECT MAX(payment_date) FROM {SCHEMA}.payments) "
                        f"FROM {SCHEMA}.viewing_sessions")
            last_session, last_payment = cur.fetchone()
        conn.commit()
        log.info("Transaction committed")
    except Exception:
        conn.rollback()
        log.error("Transaction rolled back. The database still holds the previous data.")
        raise
    finally:
        conn.close()

    for name, n, started in results:
        run_log.step(f"load:{name}", "SUCCESS", n, "loaded and reconciled", started)

    # ---------- 3. Post load: statistics + freshness ----------
    started = datetime.now()
    with run_log.conn.cursor() as cur:
        for spec in TABLES:
            cur.execute(f"ANALYZE {SCHEMA}.{spec['name']}")
    total = sum(n for _, n, _ in results)
    fresh = f"latest session {last_session}, latest payment {last_payment}"
    run_log.step("post_load:analyze", "SUCCESS", None, "table statistics refreshed", started)
    secs = time.time() - t_start
    run_log.step("pipeline", "SUCCESS", total, f"{secs:.1f}s total; {fresh}", datetime.fromtimestamp(t_start))
    log.info("Run %s finished OK: %s rows in %.1fs | %s", run_id, f"{total:,}", secs, fresh)


def main():
    parser = argparse.ArgumentParser(description="StreamSight ETL pipeline")
    parser.add_argument("--validate-only", action="store_true", help="run quality checks, do not load")
    args = parser.parse_args()

    run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(LOG_DIR / f"etl_{run_id}.log", encoding="utf-8")])
    try:
        run(args, run_id)
        return 0
    except Exception as exc:
        log.error("PIPELINE FAILED: %s", exc)
        try:
            RunLog(run_id, connect(autocommit=True)).step("pipeline", "FAILED", None, str(exc)[:1000])
        except Exception as log_exc:
            log.error("Could not write the failure to etl_run_log: %s", log_exc)
        alert = LOG_DIR / f"ALERT_{run_id}.txt"
        alert.write_text(f"StreamSight ETL run {run_id} failed at {datetime.now():%Y-%m-%d %H:%M:%S}\n{exc}\n",
                         encoding="utf-8")
        log.error("Alert file written: %s", alert)
        return 1


if __name__ == "__main__":
    sys.exit(main())
