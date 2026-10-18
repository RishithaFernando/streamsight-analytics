"""
Run a .sql file against the StreamSight database (works on Windows and Linux, no psql needed).
Usage:  python run_sql.py ../sql/churn_features.sql
"""
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent.parent
load_dotenv(BASE / ".env")


def main():
    if len(sys.argv) != 2:
        print("Usage: python run_sql.py <file.sql>")
        return 2
    path = Path(sys.argv[1])
    if not path.exists():
        print(f"File not found: {path}")
        return 2
    if not os.getenv("PGPASSWORD"):
        print("PGPASSWORD is not set. Fill in the .env file first.")
        return 2
    conn = psycopg2.connect(host=os.getenv("PGHOST", "localhost"), port=os.getenv("PGPORT", "5432"),
                            dbname=os.getenv("PGDATABASE", "streamsight"), user=os.getenv("PGUSER", "postgres"),
                            password=os.getenv("PGPASSWORD"))
    try:
        with conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
        conn.commit()
        print(f"OK: {path.name} executed")
        return 0
    except Exception as exc:
        conn.rollback()
        print(f"FAILED: {path.name}: {exc}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
