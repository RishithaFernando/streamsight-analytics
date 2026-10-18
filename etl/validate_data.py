"""
Sanity checks for the synthetic StreamSight data.
Part 1: integrity checks (must all pass).
Part 2: analytics checks (patterns should look realistic, not flat).
Run:  python validate_data.py
"""
import pandas as pd
from pathlib import Path

D = Path(__file__).resolve().parent.parent / "data"
sub = pd.read_csv(D / "subscribers.csv", parse_dates=["signup_date", "churn_date"])
ses = pd.read_csv(D / "viewing_sessions.csv", parse_dates=["session_start"])
pay = pd.read_csv(D / "payments.csv", parse_dates=["payment_date"])
tic = pd.read_csv(D / "support_tickets.csv", parse_dates=["created_at"])
con = pd.read_csv(D / "content.csv", parse_dates=["added_date"])
plans = pd.read_csv(D / "plans.csv")
END = pd.Timestamp("2026-09-30")

print("=" * 60, "\nPART 1: INTEGRITY CHECKS\n" + "=" * 60)
checks = []
def check(name, ok):
    checks.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")

check("unique subscriber ids", sub.subscriber_id.is_unique)
check("all session subscribers exist", ses.subscriber_id.isin(sub.subscriber_id).all())
check("all session content exists", ses.content_id.isin(con.content_id).all())
check("all payment subscribers exist", pay.subscriber_id.isin(sub.subscriber_id).all())
check("all ticket subscribers exist", tic.subscriber_id.isin(sub.subscriber_id).all())
m = ses.merge(sub[["subscriber_id", "signup_date", "churn_date"]], on="subscriber_id")
check("no session before signup", (m.session_start.dt.normalize() >= m.signup_date).all())
check("no session after churn", ((m.churn_date.isna()) | (m.session_start.dt.normalize() <= m.churn_date)).all())
m = pay.merge(sub[["subscriber_id", "signup_date", "churn_date"]], on="subscriber_id")
check("no payment before signup", (m.payment_date >= m.signup_date).all())
check("no payment after churn", ((m.churn_date.isna()) | (m.payment_date <= m.churn_date)).all())
check("no future dates", ses.session_start.max() < END + pd.Timedelta(days=1) and pay.payment_date.max() <= END)
check("churned have churn_date, active do not",
      ((sub.status == "churned") == sub.churn_date.notna()).all())
check("session started after content was added",
      (ses.merge(con[["content_id", "added_date"]], on="content_id").pipe(
          lambda x: (x.session_start.dt.normalize() >= x.added_date - pd.Timedelta(days=7)).all())))
print("\nIntegrity:", "ALL PASSED" if all(checks) else "SOME FAILED")

print("\n" + "=" * 60, "\nPART 2: ANALYTICS SANITY (look for differences, not flat numbers)\n" + "=" * 60)

# 1 signups by quarter
q = sub.groupby(sub.signup_date.dt.to_period("Q")).size()
print("\n1) Signups per quarter (should grow, with a Q4 bump):\n", q.to_string())

# 2 active subscribers at month end
rows = []
for me in pd.date_range("2024-03-31", "2026-09-30", freq="3ME"):
    act = ((sub.signup_date <= me) & (sub.churn_date.isna() | (sub.churn_date > me))).sum()
    rows.append((me.date(), act))
print("\n2) Active subscribers at quarter end (should grow, then flatten):")
print(pd.DataFrame(rows, columns=["date", "active"]).to_string(index=False))

# 3 churn by group
sub["churned"] = (sub.status == "churned").astype(int)
for col in ["current_plan_id", "acquisition", "country", "age_group"]:
    print(f"\n3) Churned share by {col}:")
    print((sub.groupby(col)["churned"].mean() * 100).round(1).to_string())

# 4 survival by tenure
tenure_m = ((sub.churn_date.fillna(END) - sub.signup_date).dt.days / 30.4)
print("\n4) Survival after N months (only people who had the chance to reach N months):")
for n in [1, 3, 6, 12]:
    eligible = sub[sub.signup_date <= END - pd.Timedelta(days=int(n * 30.4))]
    surv = (((eligible.churn_date.fillna(END) - eligible.signup_date).dt.days / 30.4) >= n).mean()
    print(f"   {n:>2} months: {surv:.1%}")

# 5 monthly churn events around price increase
ch = sub[sub.churn_date.notna()]
mc = ch.groupby(ch.churn_date.dt.to_period("M")).size()
print("\n5) Churn events per month, around the price increase in Jul 2025:")
print(mc["2025-03":"2025-12"].to_string())
print("\n   Churn reasons overall (%):")
print((sub.churn_reason.value_counts(normalize=True) * 100).round(1).to_string())
price_win = ch[(ch.churn_date >= "2025-07-01") & (ch.churn_date < "2025-11-01")]
print("   'Price' share Jul-Oct 2025: %.1f%%  vs other months: %.1f%%" % (
    (price_win.churn_reason == "Price").mean() * 100,
    (ch.drop(price_win.index).churn_reason == "Price").mean() * 100))

# 6 revenue
ok = pay[pay.status == "success"]
rev = ok.groupby(ok.payment_date.dt.to_period("M")).amount.sum().round(0)
print("\n6) Monthly revenue (success only), around Jul 2025:")
print(rev["2025-04":"2025-10"].to_string())
sub_c = sub.set_index("subscriber_id")
pay2 = pay.merge(sub[["subscriber_id", "country"]], on="subscriber_id")
pay2["south_asia"] = pay2.country.isin(["Sri Lanka", "India"])
print("\n   Failed payment rate: South Asia %.1f%% vs others %.1f%%" % (
    (pay2[pay2.south_asia].status == "failed").mean() * 100, (pay2[~pay2.south_asia].status == "failed").mean() * 100))

# 7 viewing patterns
ses["hour"] = ses.session_start.dt.hour
ses["dow"] = ses.session_start.dt.dayofweek
print("\n7a) Sessions by hour (share %, evening should peak):")
print((ses.hour.value_counts(normalize=True).sort_index() * 100).round(1).to_string())
print("\n7b) Sessions by weekday (Mon=0..Sun=6, weekend should be higher), avg per day:")
per_day = ses.groupby([ses.session_start.dt.normalize(), "dow"]).size().reset_index(name="n")
print(per_day.groupby("dow").n.mean().round(0).to_string())
dev_hour = ses.groupby(["device_type", "hour"]).size().unstack(0)
print("\n7c) Peak hour by device:", dev_hour.idxmax().to_dict())
print("\n7d) Avg minutes per session by device:")
print(ses.groupby("device_type").minutes_watched.mean().round(0).to_string())

# 8 content
ses2 = ses.merge(con[["content_id", "genre", "is_original", "title"]], on="content_id")
ses2 = ses2.merge(sub[["subscriber_id", "age_group"]], on="subscriber_id")
tab = pd.crosstab(ses2.age_group, ses2.genre, normalize="index").mul(100).round(1)
print("\n8a) Genre share (%) by age group (rows should differ):")
print(tab.to_string())
print("\n8b) Originals are %.0f%% of catalogue but %.0f%% of viewing" % (
    con.is_original.mean() * 100, ses2.is_original.mean() * 100))
print("\n8c) Top 5 titles by sessions:")
print(ses2.title.value_counts().head(5).to_string())

# 9 outage dips
daily = ses.groupby(ses.session_start.dt.normalize()).size()
print("\n9) Sessions on outage days vs the same weekday the week before and after:")
for o in ["2024-08-17", "2025-02-09", "2025-12-14", "2026-06-21"]:
    o = pd.Timestamp(o)
    base = (daily.get(o - pd.Timedelta(days=7), 0) + daily.get(o + pd.Timedelta(days=7), 0)) / 2
    print(f"   {o.date()}: {daily.get(o, 0):>5} vs {base:>7.0f} typical")
tday = tic.groupby(tic.created_at.dt.normalize()).size()
print("   Ticket count on outage days:", {str(o): int(tday.get(pd.Timestamp(o), 0)) for o in ["2024-08-17", "2025-02-09", "2025-12-14", "2026-06-21"]},
      "| typical day:", round(tday.median(), 1))

# 10 engagement before churn
last = ses.merge(sub[["subscriber_id", "churn_date", "churn_reason"]], on="subscriber_id")
v = last[(last.churn_reason.notna()) & (last.churn_reason != "Payment Failure")]
v = v.assign(days_before=(v.churn_date - v.session_start.dt.normalize()).dt.days)
print("\n10) Sessions per voluntary churner in the 30 days before churn vs days 60-90 before:")
nvol = ((sub.churn_reason.notna()) & (sub.churn_reason != "Payment Failure")).sum()
print("   0-30 days: %.1f   60-90 days: %.1f" % (
    (v.days_before.between(0, 30)).sum() / nvol, (v.days_before.between(60, 90)).sum() / nvol))
print("\nTicket categories (%):")
print((tic.category.value_counts(normalize=True) * 100).round(1).to_string())
