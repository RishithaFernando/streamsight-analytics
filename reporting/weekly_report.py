"""
StreamSight automated weekly report
===================================
Builds a self-contained HTML report (charts embedded, open it in any browser, or print to PDF):

  1. Week at a glance      : key numbers for the last 7 days vs the 7 days before
  2. Trends                : last 12 weeks (new vs churned, revenue)
  3. Anomaly check         : days where viewing dropped well below normal, with ticket counts
  4. Churn risk            : customers by risk band and the top 10 to contact first
  5. Pipeline health       : latest ETL runs and any validation warnings

"As of" date = the latest viewing session in the database (so it also works on historical data).
Output: reports/weekly/weekly_report_<date>.html  and  reports/weekly/latest.html
Exit code 0 on success, 1 on failure (for schedulers).

Usage:  python weekly_report.py            (add --lookback 30 to change the anomaly window)
"""
import argparse
import base64
import io
import logging
import os
import shutil
import sys
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

BASE = Path(__file__).resolve().parent.parent
load_dotenv(BASE / ".env")
OUT_DIR, LOG_DIR = BASE / "reports" / "weekly", BASE / "logs"
S = "streamsight"
NAVY, BLUE, RED, AMBER, TEAL, GREY = "#0B1F3A", "#2F6FED", "#E5484D", "#F5A623", "#1FA187", "#5B6B7F"
log = logging.getLogger("weekly_report")


def get_engine():
    if not os.getenv("PGPASSWORD"):
        raise RuntimeError("PGPASSWORD is not set. Fill in the .env file in the project folder.")
    return create_engine(URL.create(
        "postgresql+psycopg2", username=os.getenv("PGUSER", "postgres"), password=os.getenv("PGPASSWORD"),
        host=os.getenv("PGHOST", "localhost"), port=int(os.getenv("PGPORT", "5432")),
        database=os.getenv("PGDATABASE", "streamsight")))


def q(engine, sql, **params):
    return pd.read_sql(text(sql), engine, params=params)


# ---------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------
def window_metrics(engine, start, end):
    sql = f"""
    SELECT
      (SELECT COUNT(*) FROM {S}.subscribers WHERE signup_date BETWEEN :s AND :e)  AS new_subscribers,
      (SELECT COUNT(*) FROM {S}.subscribers WHERE churn_date BETWEEN :s AND :e)   AS churned_subscribers,
      (SELECT COUNT(*) FROM {S}.subscribers
         WHERE signup_date <= :e AND (churn_date IS NULL OR churn_date > :e))     AS active_subscribers,
      (SELECT COALESCE(SUM(amount), 0) FROM {S}.payments
         WHERE status = 'success' AND payment_date BETWEEN :s AND :e)             AS revenue,
      (SELECT COUNT(*) FROM {S}.payments
         WHERE status = 'failed' AND payment_date BETWEEN :s AND :e)              AS failed_payments,
      (SELECT COUNT(*) FROM {S}.viewing_sessions
         WHERE session_start >= CAST(:s AS date) AND session_start < CAST(:e AS date) + 1) AS sessions,
      (SELECT COALESCE(SUM(minutes_watched), 0) / 60.0 FROM {S}.viewing_sessions
         WHERE session_start >= CAST(:s AS date) AND session_start < CAST(:e AS date) + 1) AS hours_watched,
      (SELECT COUNT(DISTINCT subscriber_id) FROM {S}.viewing_sessions
         WHERE session_start >= CAST(:s AS date) AND session_start < CAST(:e AS date) + 1) AS active_viewers,
      (SELECT COUNT(*) FROM {S}.support_tickets
         WHERE created_at >= CAST(:s AS date) AND created_at < CAST(:e AS date) + 1)       AS tickets
    """
    return q(engine, sql, s=start, e=end).iloc[0].astype(float)


def weekly_series(engine, as_of, weeks=12):
    sql = f"""
    WITH w AS (SELECT generate_series(0, :n - 1) AS weeks_ago)
    SELECT w.weeks_ago,
      (SELECT COUNT(*) FROM {S}.subscribers
         WHERE signup_date <= :a AND (CAST(:a AS date) - signup_date) / 7 = w.weeks_ago)  AS new_subscribers,
      (SELECT COUNT(*) FROM {S}.subscribers
         WHERE churn_date <= :a AND (CAST(:a AS date) - churn_date) / 7 = w.weeks_ago)    AS churned_subscribers,
      (SELECT COALESCE(SUM(amount), 0) FROM {S}.payments
         WHERE status = 'success' AND payment_date <= :a
           AND (CAST(:a AS date) - payment_date) / 7 = w.weeks_ago)                       AS revenue
    FROM w ORDER BY w.weeks_ago DESC
    """
    df = q(engine, sql, a=as_of, n=weeks)
    df["week_end"] = [as_of - timedelta(days=7 * int(k)) for k in df.weeks_ago]
    return df


def daily_series(engine, as_of, days):
    sess = q(engine, f"""SELECT session_start::date AS d, COUNT(*) AS sessions FROM {S}.viewing_sessions
                         WHERE session_start >= CAST(:a AS date) - :n AND session_start < CAST(:a AS date) + 1
                         GROUP BY 1""", a=as_of, n=days)
    tick = q(engine, f"""SELECT created_at::date AS d, COUNT(*) AS tickets FROM {S}.support_tickets
                         WHERE created_at >= CAST(:a AS date) - :n AND created_at < CAST(:a AS date) + 1
                         GROUP BY 1""", a=as_of, n=days)
    idx = pd.date_range(pd.Timestamp(as_of) - pd.Timedelta(days=days), pd.Timestamp(as_of), freq="D")
    df = pd.DataFrame(index=idx)
    df["sessions"] = pd.Series(sess.sessions.values, index=pd.to_datetime(sess.d)).reindex(idx).fillna(0)
    df["tickets"] = pd.Series(tick.tickets.values, index=pd.to_datetime(tick.d)).reindex(idx).fillna(0)
    base = pd.concat([df.sessions.shift(7 * k) for k in (1, 2, 3, 4)], axis=1).mean(axis=1)
    df["expected"] = base
    df["drop_pct"] = (1 - df.sessions / df.expected) * 100
    df["typical_tickets"] = df.tickets.shift(1).rolling(28).median()
    df["view_drop"] = df.sessions < 0.80 * df.expected
    df["ticket_spike"] = df.tickets >= np.maximum(15, 3 * df.typical_tickets)
    df["flag"] = df.view_drop | df.ticket_spike
    return df


# ---------------------------------------------------------------------
# Charts (embedded as base64 PNG)
# ---------------------------------------------------------------------
def b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def style(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#C5CEDA")
    ax.tick_params(colors=GREY, labelsize=8)
    ax.grid(axis="y", color="#EEF2F7")
    ax.set_axisbelow(True)


def chart_new_vs_churned(ws):
    fig, ax = plt.subplots(figsize=(6, 3))
    ax.plot(ws.week_end, ws.new_subscribers, color=BLUE, lw=2.2, label="New")
    ax.plot(ws.week_end, ws.churned_subscribers, color=RED, lw=2.2, label="Churned")
    style(ax); ax.legend(frameon=False, fontsize=8, loc="upper left")
    ax.set_title("New vs churned subscribers per week", fontsize=10, color=NAVY, loc="left", fontweight="bold")
    fig.autofmt_xdate(rotation=0, ha="center")
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%d %b"))
    return b64(fig)


def chart_revenue(ws):
    fig, ax = plt.subplots(figsize=(6, 3))
    colors = [BLUE] * (len(ws) - 1) + [NAVY]
    ax.bar(range(len(ws)), ws.revenue, color=colors, width=0.65)
    ax.set_xticks(range(len(ws)))
    ax.set_xticklabels([d.strftime("%d %b") for d in ws.week_end])
    style(ax)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"${v / 1000:.0f}K"))
    ax.set_title("Revenue per week", fontsize=10, color=NAVY, loc="left", fontweight="bold")
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
    return b64(fig)


def chart_daily(df, lookback):
    d = df.iloc[-lookback:]
    fig, ax = plt.subplots(figsize=(12, 3))
    ax.plot(d.index, d.sessions, color=BLUE, lw=1.6, label="Sessions")
    ax.plot(d.index, d.expected, color="#8A97A8", lw=1.2, ls="--", label="Expected (same weekday, prior 4 weeks)")
    f = d[d.flag]
    ax.scatter(f.index, f.sessions, color=RED, zorder=5, s=36, label="Flagged day (ticket spike or viewing drop)")
    style(ax); ax.legend(frameon=False, fontsize=8, loc="upper left", ncol=3)
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%d %b"))
    ax.set_title(f"Daily viewing sessions, last {lookback} days", fontsize=10, color=NAVY, loc="left", fontweight="bold")
    return b64(fig)


# ---------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------
CSS = """
body{font-family:'Segoe UI',Arial,sans-serif;background:#EEF2F7;color:#0B1F3A;margin:0}
.wrap{max-width:1100px;margin:0 auto;padding:0 20px 30px}
.head{background:#0B1F3A;color:#fff;padding:22px 28px;border-radius:0 0 12px 12px}
.head h1{margin:0;font-size:24px}.head p{margin:6px 0 0;color:#B8C4D6;font-size:13px}
h2{font-size:15px;margin:26px 0 10px;color:#0B1F3A}
.grid{font-size:0}.grid .card{display:inline-block;vertical-align:top;width:24%;margin:0 1.333% 12px 0;box-sizing:border-box;font-size:13px}.grid .card:nth-child(4n){margin-right:0}
.card{background:#fff;border:1px solid #E1E7EF;border-radius:10px;padding:14px 16px}
.card .l{font-size:12px;color:#5B6B7F}.card .v{font-size:24px;font-weight:600;margin:4px 0}
.up-good,.down-good{color:#1FA187}.up-bad,.down-bad{color:#E5484D}.flat{color:#5B6B7F}
.d{font-size:12px}
.two{font-size:0}.two .col{display:inline-block;vertical-align:top;width:49%;margin-right:2%;box-sizing:border-box;font-size:13px}.two .col:nth-child(2n){margin-right:0}
.box{background:#fff;border:1px solid #E1E7EF;border-radius:10px;padding:12px}
.box img{width:100%}.col.box{padding:10px}
table{border-collapse:collapse;width:100%;font-size:12.5px;background:#fff}
th{background:#0B1F3A;color:#fff;text-align:left;padding:7px 9px;font-weight:600}
td{padding:6px 9px;border-bottom:1px solid #EEF2F7}
.ok{color:#1FA187;font-weight:600}.bad{color:#E5484D;font-weight:600}.warn{color:#C77F0A;font-weight:600}
.note{font-size:12px;color:#5B6B7F;margin:6px 0}.foot{margin-top:28px;font-size:11px;color:#8A97A8;text-align:center}
@media print{body{background:#fff}.card,.box{break-inside:avoid}}
"""


def kpi_card(label, cur, prev, fmt, up_is_good):
    value = fmt(cur)
    if prev in (0, None) or np.isnan(prev):
        delta, cls = "no prior week", "flat"
    else:
        pct = (cur - prev) / prev * 100
        arrow = "&#9650;" if pct > 0.05 else ("&#9660;" if pct < -0.05 else "&#9644;")
        good = (pct > 0) == up_is_good
        cls = "flat" if abs(pct) < 1.0 else ("up-good" if good else "up-bad")
        delta = f"{arrow} {abs(pct):.1f}% vs previous 7 days"
    return f'<div class="card"><div class="l">{label}</div><div class="v">{value}</div><div class="d {cls}">{delta}</div></div>'


def df_table(df, pct_cols=(), money_cols=()):
    head = "".join(f"<th>{escape(str(c))}</th>" for c in df.columns)
    rows = []
    for _, r in df.iterrows():
        cells = []
        for c in df.columns:
            v = r[c]
            if pd.isna(v):
                v = "-"
            if v == "-":
                pass
            elif c in pct_cols:
                v = f"{float(v):.1f}%"
            elif c in money_cols:
                v = f"${float(v):,.0f}"
            cells.append(f"<td>{escape(str(v))}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><tr>{head}</tr>{''.join(rows)}</table>"


def build_report(engine, lookback):
    as_of = q(engine, f"SELECT MAX(session_start)::date AS d FROM {S}.viewing_sessions").d.iloc[0]
    cur_s, cur_e = as_of - timedelta(days=6), as_of
    prv_s, prv_e = as_of - timedelta(days=13), as_of - timedelta(days=7)
    cur, prv = window_metrics(engine, cur_s, cur_e), window_metrics(engine, prv_s, prv_e)
    log.info("As-of date %s | active subscribers %.0f | new %.0f | churned %.0f",
             as_of, cur.active_subscribers, cur.new_subscribers, cur.churned_subscribers)

    ws = weekly_series(engine, as_of, 12)
    daily = daily_series(engine, as_of, lookback + 28)

    fmt_n = lambda v: f"{v:,.0f}"
    fmt_m = lambda v: f"${v:,.0f}"
    cards = "".join([
        kpi_card("Active subscribers", cur.active_subscribers, prv.active_subscribers, fmt_n, True),
        kpi_card("New subscribers", cur.new_subscribers, prv.new_subscribers, fmt_n, True),
        kpi_card("Churned subscribers", cur.churned_subscribers, prv.churned_subscribers, fmt_n, False),
        kpi_card("Revenue", cur.revenue, prv.revenue, fmt_m, True),
        kpi_card("Failed payments", cur.failed_payments, prv.failed_payments, fmt_n, False),
        kpi_card("Viewing hours", cur.hours_watched, prv.hours_watched, fmt_n, True),
        kpi_card("Active viewers", cur.active_viewers, prv.active_viewers, fmt_n, True),
        kpi_card("Support tickets", cur.tickets, prv.tickets, fmt_n, False)])

    # anomalies
    an = daily.iloc[-lookback:]
    an = an[an.flag].copy()
    if len(an):
        signal = np.where(an.ticket_spike & an.view_drop, "Ticket spike + viewing drop",
                          np.where(an.ticket_spike, "Ticket spike", "Viewing drop"))
        t = pd.DataFrame({"Date": an.index.strftime("%a %d %b %Y"), "Sessions": an.sessions.astype(int),
                          "Expected": an.expected.round(0).astype(int),
                          "Tickets": an.tickets.astype(int), "Typical tickets": an.typical_tickets.round(0).astype(int),
                          "Signal": signal})
        anomaly_html = (f'<p class="note"><span class="warn">{len(an)} day(s) flagged.</span> A day is flagged when support tickets '
                        f"reach at least 3x the usual level (minimum 15), or when sessions are 20% or more below the average "
                        f"for the same weekday over the previous four weeks.</p>" + df_table(t))
    else:
        anomaly_html = f'<p class="note"><span class="ok">No anomalies</span> in the last {lookback} days.</p>'

    # churn risk
    snap = q(engine, f"SELECT MAX(snapshot_date) AS d FROM {S}.churn_predictions").d.iloc[0]
    bands = q(engine, f"""SELECT risk_band AS "Risk band", COUNT(*) AS "Customers",
                          ROUND(AVG(churn_probability) * 100, 1) AS "Avg churn probability"
                          FROM {S}.churn_predictions WHERE snapshot_date = :d GROUP BY 1
                          ORDER BY CASE risk_band WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END""", d=snap)
    top = q(engine, f"""SELECT subscriber_id AS "Customer", plan_name AS "Plan", country AS "Country",
                        ROUND(churn_probability * 100, 1) AS "Churn risk", days_since_last_session AS "Days since last watch",
                        top_drivers AS "Main risk drivers"
                        FROM {S}.vw_churn_risk ORDER BY risk_rank LIMIT 10""")
    mm = q(engine, f"SELECT lift_top10, recall_top20 FROM {S}.vw_model_metrics_latest")
    model_note = ""
    if len(mm):
        model_note = (f'<p class="note">Model check (test period): the 10% highest-risk customers churned '
                      f'{float(mm.lift_top10.iloc[0]):.1f}x more often than average; the top 20% of the list contains '
                      f'{float(mm.recall_top20.iloc[0]) * 100:.0f}% of the customers who left.</p>')

    # pipeline health
    runs = q(engine, f"""SELECT run_id AS "Run", status AS "Status", COALESCE(rows_loaded::text, '-') AS "Rows loaded",
                         to_char(run_finished, 'YYYY-MM-DD HH24:MI') AS "Finished", LEFT(message, 70) AS "Note"
                         FROM {S}.etl_run_log WHERE step_name = 'pipeline' ORDER BY log_id DESC LIMIT 5""")
    last_run = runs.Run.iloc[0] if len(runs) else None
    warn = pd.DataFrame()
    if last_run:
        warn = q(engine, f"""SELECT step_name AS "Step", status AS "Status", LEFT(message, 90) AS "Message"
                             FROM {S}.etl_run_log WHERE run_id = :r AND status NOT IN ('SUCCESS') ORDER BY log_id""", r=last_run)
    status_html = ""
    if len(runs):
        ok = runs.Status.iloc[0] == "SUCCESS"
        status_html = (f'<p class="note">Latest pipeline run: <span class="{"ok" if ok else "bad"}">{runs.Status.iloc[0]}</span> '
                       f'({runs.Finished.iloc[0]}). Latest data: {as_of}.</p>')
    warn_html = ('<p class="note"><span class="ok">No validation warnings</span> in the latest run.</p>' if warn.empty
                 else df_table(warn))

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>StreamSight weekly report {as_of}</title>
<style>{CSS}</style></head><body>
<div class="head"><div class="wrap" style="padding:0"><h1>StreamSight weekly report</h1>
<p>Week ending {as_of:%A %d %B %Y} &nbsp;|&nbsp; compared with the 7 days before &nbsp;|&nbsp; generated {generated}</p></div></div>
<div class="wrap">
<h2>1. Week at a glance</h2><div class="grid">{cards}</div>
<h2>2. Trends, last 12 weeks</h2>
<div class="two"><div class="col box"><img src="{chart_new_vs_churned(ws)}"></div><div class="col box"><img src="{chart_revenue(ws)}"></div></div>
<h2>3. Anomaly check</h2>
<div class="box"><img src="{chart_daily(daily, lookback)}"></div>{anomaly_html}
<h2>4. Churn risk (customers active on {snap})</h2>
{df_table(bands, pct_cols=("Avg churn probability",))}{model_note}
<p class="note">Top 10 customers to contact first:</p>{df_table(top, pct_cols=("Churn risk",))}
<h2>5. Pipeline health</h2>{status_html}{df_table(runs) if len(runs) else ""}<p class="note">Validation warnings from the latest run:</p>{warn_html}
<div class="foot">StreamSight | synthetic data generated for portfolio purposes | report generated automatically</div>
</div></body></html>"""
    return as_of, html


def main():
    ap = argparse.ArgumentParser(description="StreamSight weekly report")
    ap.add_argument("--lookback", type=int, default=120, help="days to scan for viewing anomalies")
    args = ap.parse_args()
    LOG_DIR.mkdir(exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(LOG_DIR / f"report_{run_id}.log", encoding="utf-8")])
    try:
        as_of, html = build_report(get_engine(), args.lookback)
        path = OUT_DIR / f"weekly_report_{as_of}.html"
        path.write_text(html, encoding="utf-8")
        shutil.copyfile(path, OUT_DIR / "latest.html")
        log.info("Report written: %s (%.0f KB)", path, path.stat().st_size / 1024)
        return 0
    except Exception as exc:
        log.error("WEEKLY REPORT FAILED: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
