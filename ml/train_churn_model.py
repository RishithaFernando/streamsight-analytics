"""
StreamSight churn model
=======================
1. READ    features from PostgreSQL (table streamsight.churn_features)
2. COMPARE three models with walk-forward validation (train on the past, test on the future)
3. PICK    the best model using the VALIDATION snapshot only, then report it on the TEST snapshot
4. SCORE   today's active customers (the latest snapshot, which has no label yet)
5. WRITE   predictions, metrics and risk-band results back to PostgreSQL for Power BI
6. SAVE    the model file and two charts

Time layout (labelled snapshots s0 < s1 < s2 < s3, plus the latest unlabelled one):
    validation : train on s0,s1        -> test on s2   (used to choose the model)
    final test : train on s0,s1,s2     -> test on s3   (honest estimate, never used to choose)
    production : train on s0..s3       -> score the latest snapshot

Usage:  python train_churn_model.py
"""
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import joblib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

BASE = Path(__file__).resolve().parent.parent
load_dotenv(BASE / ".env")
MODEL_DIR, REPORT_DIR, LOG_DIR = BASE / "models", BASE / "reports", BASE / "logs"
SCHEMA = "streamsight"
CATEGORICAL = ["plan_name", "country", "age_group", "device_type", "acquisition"]
ID_COLS = ["snapshot_date", "subscriber_id", "churned_next_90d"]
SEED = 1
log = logging.getLogger("churn")

PRETTY = {
    "tenure_days": "Tenure", "sessions_30d": "Sessions (30d)", "sessions_prev_30d": "Sessions (prior 30d)",
    "engagement_ratio": "Viewing trend", "minutes_30d": "Minutes watched (30d)",
    "avg_minutes_per_session_30d": "Avg session length", "completion_rate_30d": "Completion rate",
    "active_days_30d": "Active days (30d)", "days_since_last_session": "Days since last session",
    "failed_payments_30d": "Failed payments (30d)", "failed_payments_90d": "Failed payments (90d)",
    "tickets_30d": "Support tickets (30d)", "tickets_90d": "Support tickets (90d)",
    "billing_tickets_90d": "Billing tickets (90d)", "playback_tickets_90d": "Playback tickets (90d)",
    "plan_name": "Plan", "country": "Country", "age_group": "Age group",
    "device_type": "Device", "acquisition": "Channel"}

DDL = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.churn_predictions (
    subscriber_id     INT NOT NULL,
    snapshot_date     DATE NOT NULL,
    churn_probability NUMERIC(6,4) NOT NULL,
    risk_rank         INT NOT NULL,
    risk_band         VARCHAR(10) NOT NULL,
    top_drivers       TEXT,
    model_name        VARCHAR(40) NOT NULL,
    scored_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (subscriber_id, snapshot_date)
);
CREATE TABLE IF NOT EXISTS {SCHEMA}.churn_model_metrics (
    run_id        VARCHAR(20) NOT NULL,
    trained_at    TIMESTAMP NOT NULL,
    model_name    VARCHAR(40) NOT NULL,
    stage         VARCHAR(12) NOT NULL,
    roc_auc       NUMERIC(6,4), pr_auc NUMERIC(6,4), base_rate NUMERIC(6,4),
    lift_top10    NUMERIC(6,2), recall_top20 NUMERIC(6,4),
    train_rows    INT, test_rows INT,
    selected      BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE IF NOT EXISTS {SCHEMA}.churn_band_performance (
    run_id           VARCHAR(20) NOT NULL,
    risk_band        VARCHAR(10) NOT NULL,
    customers        INT NOT NULL,
    actual_churn_pct NUMERIC(6,2) NOT NULL
);
CREATE OR REPLACE VIEW {SCHEMA}.vw_churn_risk AS
SELECT p.subscriber_id, p.snapshot_date, p.churn_probability, p.risk_rank, p.risk_band, p.top_drivers,
       f.plan_name, f.country, f.age_group, f.device_type, f.acquisition, f.tenure_days,
       f.sessions_30d, f.sessions_prev_30d, f.days_since_last_session,
       f.failed_payments_90d, f.tickets_90d
FROM {SCHEMA}.churn_predictions p
JOIN {SCHEMA}.churn_features f
  ON f.subscriber_id = p.subscriber_id AND f.snapshot_date = p.snapshot_date;

CREATE TABLE IF NOT EXISTS {SCHEMA}.churn_feature_importance (
    run_id        VARCHAR(20) NOT NULL,
    rank_no       INT NOT NULL,
    feature       VARCHAR(80) NOT NULL,
    readable_name VARCHAR(100) NOT NULL,
    auc_drop      NUMERIC(8,4) NOT NULL
);
CREATE OR REPLACE VIEW {SCHEMA}.vw_feature_importance_latest AS
SELECT rank_no, readable_name, auc_drop
FROM {SCHEMA}.churn_feature_importance
WHERE run_id = (SELECT MAX(run_id) FROM {SCHEMA}.churn_feature_importance);
"""


# ---------------------------------------------------------------------
def db_params():
    if not os.getenv("PGPASSWORD"):
        raise RuntimeError("PGPASSWORD is not set. Fill in the .env file in the project folder.")
    return dict(host=os.getenv("PGHOST", "localhost"), port=os.getenv("PGPORT", "5432"),
                dbname=os.getenv("PGDATABASE", "streamsight"), user=os.getenv("PGUSER", "postgres"),
                password=os.getenv("PGPASSWORD"))


def load_features():
    p = db_params()
    engine = create_engine(URL.create("postgresql+psycopg2", username=p["user"], password=p["password"],
                                      host=p["host"], port=int(p["port"]), database=p["dbname"]))
    df = pd.read_sql(f"SELECT * FROM {SCHEMA}.churn_features", engine)
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"]).dt.date
    return df


def metrics(y, p):
    order = np.argsort(-p)
    n = len(p)
    top10, top20 = order[: int(n * 0.10)], order[: int(n * 0.20)]
    base = y.mean()
    return {"roc_auc": roc_auc_score(y, p), "pr_auc": average_precision_score(y, p), "base_rate": base,
            "lift_top10": y[top10].mean() / base, "recall_top20": y[top20].sum() / y.sum()}


def candidates():
    return {
        "logistic_regression": lambda: make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000, C=0.5)),
        "random_forest": lambda: RandomForestClassifier(300, min_samples_leaf=30, max_features=0.5,
                                                        random_state=SEED, n_jobs=-1),
        "gradient_boosting": lambda: HistGradientBoostingClassifier(
            max_depth=3, learning_rate=0.04, max_iter=150, min_samples_leaf=40,
            l2_regularization=1.0, random_state=SEED)}


def assign_bands(prob):
    """Rank based bands: top 10% High, next 20% Medium, rest Low."""
    rank = pd.Series(prob).rank(ascending=False, method="first").astype(int).values
    n = len(prob)
    band = np.where(rank <= 0.10 * n, "High", np.where(rank <= 0.30 * n, "Medium", "Low"))
    return rank, band


def describe(col, z):
    for c in CATEGORICAL:
        if col.startswith(c + "_"):
            value = col[len(c) + 1:]
            return f"{PRETTY[c]}: {value}" if z > 0 else f"{PRETTY[c]}: not {value}"
    return f"{PRETTY.get(col, col)} ({'high' if z > 0 else 'low'})"


def readable(col):
    """Human friendly feature name for charts."""
    for c in CATEGORICAL:
        if col.startswith(c + "_"):
            return f"{PRETTY[c]}: {col[len(c) + 1:]}"
    return PRETTY.get(col, col)


def top_drivers(pipe, X, k=3):
    """Per customer: the k features pushing the risk up most (coefficient x standardised value)."""
    z = pipe.named_steps["standardscaler"].transform(X)
    contrib = z * pipe.named_steps["logisticregression"].coef_[0]
    idx = np.argsort(-contrib, axis=1)[:, :k]
    cols, out = list(X.columns), []
    for i in range(len(X)):
        parts = [describe(cols[j], z[i, j]) for j in idx[i] if contrib[i, j] > 0.05]
        out.append("; ".join(parts) if parts else "No strong risk driver")
    return out


# ---------------------------------------------------------------------
def run(run_id):
    df = load_features()
    labelled = df[df.churned_next_90d.notna()].copy()
    snaps = sorted(labelled.snapshot_date.unique())
    score_snap = max(df.snapshot_date.unique())
    if len(snaps) < 4 or score_snap <= snaps[-1]:
        raise RuntimeError(f"Need 4 labelled snapshots plus a newer unlabelled one, found {len(snaps)} labelled.")
    log.info("Loaded %s feature rows. Labelled snapshots: %s | scoring snapshot: %s",
             f"{len(df):,}", [str(s) for s in snaps], score_snap)

    X_all = pd.get_dummies(df.drop(columns=ID_COLS), columns=CATEGORICAL, dtype=float)
    X_lab, y_lab = X_all.loc[labelled.index], labelled.churned_next_90d.astype(int)

    def split(train_snaps, test_snap):
        tr = labelled.snapshot_date.isin(train_snaps)
        te = labelled.snapshot_date == test_snap
        return X_lab[tr], y_lab[tr], X_lab[te], y_lab[te]

    # ---- validation: choose the model ----
    results, val_auc = [], {}
    Xtr, ytr, Xte, yte = split(snaps[:-2], snaps[-2])
    for name, make in candidates().items():
        m = make().fit(Xtr, ytr)
        r = metrics(yte.values, m.predict_proba(Xte)[:, 1])
        val_auc[name] = r["roc_auc"]
        results.append({"model_name": name, "stage": "validation", **r, "train_rows": len(Xtr), "test_rows": len(Xte)})
        log.info("validation %-20s AUC %.3f | PR-AUC %.3f | lift top10%% %.2fx", name, r["roc_auc"], r["pr_auc"], r["lift_top10"])
    chosen = max(val_auc, key=val_auc.get)
    log.info("Chosen model (best validation AUC): %s", chosen)

    # ---- final test: honest estimate on the newest labelled snapshot ----
    Xtr, ytr, Xte, yte = split(snaps[:-1], snaps[-1])
    test_probs = {}
    for name, make in candidates().items():
        m = make().fit(Xtr, ytr)
        test_probs[name] = m
        r = metrics(yte.values, m.predict_proba(Xte)[:, 1])
        results.append({"model_name": name, "stage": "test", **r, "train_rows": len(Xtr), "test_rows": len(Xte)})
        log.info("test       %-20s AUC %.3f | PR-AUC %.3f | lift top10%% %.2fx | recall in top 20%% %.0f%%",
                 name, r["roc_auc"], r["pr_auc"], r["lift_top10"], 100 * r["recall_top20"])
    for r in results:
        r["selected"] = r["model_name"] == chosen

    # ---- do the risk bands mean something? (test snapshot, chosen model) ----
    p_test = test_probs[chosen].predict_proba(Xte)[:, 1]
    _, band_test = assign_bands(p_test)
    bands = (pd.DataFrame({"risk_band": band_test, "churned": yte.values}).groupby("risk_band")
             .agg(customers=("churned", "size"), actual_churn_pct=("churned", lambda s: 100 * s.mean()))
             .reindex(["High", "Medium", "Low"]).round(2).reset_index())
    log.info("Risk bands on the test snapshot (actual churn rate):\n%s", bands.to_string(index=False))

    # ---- feature importance (permutation, on the test snapshot) ----
    imp = permutation_importance(test_probs[chosen], Xte, yte, scoring="roc_auc", n_repeats=10,
                                 random_state=SEED, n_jobs=-1)
    imp_df = (pd.DataFrame({"feature": Xte.columns, "auc_drop": imp.importances_mean})
              .sort_values("auc_drop", ascending=False))

    # ---- production model: train on every labelled snapshot, score the newest customers ----
    final = candidates()[chosen]().fit(X_lab, y_lab)
    score_idx = df.index[df.snapshot_date == score_snap]
    X_score = X_all.loc[score_idx]
    prob = final.predict_proba(X_score)[:, 1]
    rank, band = assign_bands(prob)
    drivers = top_drivers(final, X_score) if chosen == "logistic_regression" else [None] * len(X_score)
    now = datetime.now()
    pred = pd.DataFrame({"subscriber_id": df.loc[score_idx, "subscriber_id"].astype(int).values,
                         "snapshot_date": score_snap, "churn_probability": np.round(prob, 4),
                         "risk_rank": rank, "risk_band": band, "top_drivers": drivers,
                         "model_name": chosen, "scored_at": now})
    log.info("Scored %s active customers: %s", f"{len(pred):,}", pred.risk_band.value_counts().to_dict())

    # ---- write to PostgreSQL (one transaction) ----
    conn = psycopg2.connect(**db_params())
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute(f"DELETE FROM {SCHEMA}.churn_predictions WHERE snapshot_date = %s", (score_snap,))
            execute_values(cur,
                f"INSERT INTO {SCHEMA}.churn_predictions (subscriber_id, snapshot_date, churn_probability, "
                "risk_rank, risk_band, top_drivers, model_name, scored_at) VALUES %s",
                [tuple(None if (isinstance(v, float) and np.isnan(v)) else v for v in row)
                 for row in pred.itertuples(index=False, name=None)])
            execute_values(cur,
                f"INSERT INTO {SCHEMA}.churn_model_metrics (run_id, trained_at, model_name, stage, roc_auc, pr_auc, "
                "base_rate, lift_top10, recall_top20, train_rows, test_rows, selected) VALUES %s",
                [(run_id, now, r["model_name"], r["stage"], float(r["roc_auc"]), float(r["pr_auc"]),
                  float(r["base_rate"]), float(r["lift_top10"]), float(r["recall_top20"]),
                  int(r["train_rows"]), int(r["test_rows"]), bool(r["selected"])) for r in results])
            execute_values(cur,
                f"INSERT INTO {SCHEMA}.churn_band_performance (run_id, risk_band, customers, actual_churn_pct) VALUES %s",
                [(run_id, r.risk_band, int(r.customers), float(r.actual_churn_pct)) for r in bands.itertuples()])
            execute_values(cur,
                f"INSERT INTO {SCHEMA}.churn_feature_importance (run_id, rank_no, feature, readable_name, auc_drop) VALUES %s",
                [(run_id, i + 1, r.feature, readable(r.feature), float(r.auc_drop))
                 for i, r in enumerate(imp_df.head(10).itertuples())])
            cur.execute(f"SELECT COUNT(*) FROM {SCHEMA}.churn_predictions WHERE snapshot_date = %s", (score_snap,))
            if cur.fetchone()[0] != len(pred):
                raise RuntimeError("prediction row count mismatch after insert")
        conn.commit()
        log.info("Predictions and metrics written to PostgreSQL")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # ---- files: model + charts ----
    for d in (MODEL_DIR, REPORT_DIR):
        d.mkdir(exist_ok=True)
    joblib.dump({"model": final, "columns": list(X_all.columns), "model_name": chosen,
                 "trained_at": str(now), "scoring_snapshot": str(score_snap)}, MODEL_DIR / "churn_model.joblib")
    pd.DataFrame(results).to_csv(REPORT_DIR / "model_comparison.csv", index=False)
    imp_df.to_csv(REPORT_DIR / "feature_importance.csv", index=False)
    pred.sort_values("risk_rank").head(50).to_csv(REPORT_DIR / "top50_at_risk.csv", index=False)

    top = imp_df.head(12).iloc[::-1]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.barh(top.feature, top.auc_drop, color="#1f6feb")
    ax.set_xlabel("Drop in AUC when the feature is shuffled (higher = more important)")
    ax.set_title("What drives churn risk (test snapshot)")
    fig.tight_layout(); fig.savefig(REPORT_DIR / "feature_importance.png", dpi=150); plt.close(fig)

    fig, ax = plt.subplots(figsize=(6, 4))
    colors = {"High": "#d73a49", "Medium": "#f0ad4e", "Low": "#2da44e"}
    ax.bar(bands.risk_band, bands.actual_churn_pct, color=[colors[b] for b in bands.risk_band])
    ax.axhline(100 * yte.mean(), color="grey", ls="--", label=f"Average ({100 * yte.mean():.1f}%)")
    for i, v in enumerate(bands.actual_churn_pct):
        ax.text(i, v + 0.3, f"{v:.1f}%", ha="center")
    ax.set_ylabel("Customers who churned within 90 days (%)")
    ax.set_title("Do the risk bands work? (test snapshot)"); ax.legend()
    fig.tight_layout(); fig.savefig(REPORT_DIR / "risk_bands.png", dpi=150); plt.close(fig)
    log.info("Saved model, charts and CSV reports in %s and %s", MODEL_DIR, REPORT_DIR)
    return imp_df, pred


def main():
    run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(LOG_DIR / f"churn_{run_id}.log", encoding="utf-8")])
    try:
        run(run_id)
        log.info("Churn model run %s finished OK", run_id)
        return 0
    except Exception as exc:
        log.error("CHURN MODEL RUN FAILED: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
