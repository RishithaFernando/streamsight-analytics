"""
StreamSight synthetic data generator (v2, realistic patterns).

Creates CSV files for a Netflix-style subscription streaming service.
All data is SYNTHETIC. Patterns are built in on purpose so that analytics
show realistic differences (not "high everywhere" or "low everywhere"):

  - signups grow over time, with seasonality, weekends and promo campaigns
  - churn is highest in the first months and depends on plan, acquisition,
    country and engagement; a price increase in Jul 2025 causes a churn spike
  - viewing peaks in the evening and at weekends; hours differ by device
  - genre taste differs by age group; new originals spike then fade
  - payment failures are higher in some countries; failed payments can
    lead to involuntary churn
  - voluntary churners watch less in the weeks before they leave
  - support tickets spike on outage days and after the price increase

Run:  python generate_data.py
"""
import numpy as np
import pandas as pd
from pathlib import Path

SEED = 42
N_SUBS = 5000
START = pd.Timestamp("2024-01-01")
END = pd.Timestamp("2026-09-30")
PRICE_HIKE = pd.Timestamp("2025-07-01")
OUTAGES = [pd.Timestamp(d) for d in ["2024-08-17", "2025-02-09", "2025-12-14", "2026-06-21"]]

BASE_CHURN = 0.030      # base monthly churn hazard (before factors)
BASE_SESSIONS = 13.0    # base sessions per month for an average subscriber

OUT = Path(__file__).resolve().parent.parent / "data"
OUT.mkdir(exist_ok=True)
rng = np.random.default_rng(SEED)

month_starts = pd.date_range(START, END, freq="MS")
N_MONTHS = len(month_starts)
month_days = month_starts.days_in_month.values
month_moy = month_starts.month.values - 1
END64 = END.to_datetime64()
START64 = START.to_datetime64()


def month_index(dti):
    dti = pd.DatetimeIndex(dti)
    return ((dti.year - START.year) * 12 + dti.month - 1).values


# =====================================================================
# 1. Plans and price history
# =====================================================================
plans = pd.DataFrame({
    "plan_id": [1, 2, 3],
    "plan_name": ["Basic", "Standard", "Premium"],
    "monthly_price": [5.99, 11.99, 17.99],       # current price
    "max_screens": [1, 2, 4],
    "max_quality": ["720p", "1080p", "4K"],
})
price_old = np.array([4.99, 9.99, 14.99])
price_new = np.array([5.99, 11.99, 17.99])
plan_price_history = pd.DataFrame({
    "plan_id": [1, 2, 3, 1, 2, 3],
    "effective_from": [START.date()] * 3 + [PRICE_HIKE.date()] * 3,
    "monthly_price": list(price_old) + list(price_new),
})

# =====================================================================
# 2. Content catalogue
# =====================================================================
GENRES = ["Drama", "Comedy", "Action", "Thriller", "Documentary", "Sci-Fi", "Romance", "Animation", "Horror"]
LANGS = ["English", "Hindi", "Sinhala", "Tamil", "Korean", "Spanish"]
ADJ = ["Silent", "Midnight", "Broken", "Golden", "Hidden", "Last", "Crimson", "Wild", "Frozen", "Lost",
       "Electric", "Burning", "Distant", "Final", "Secret", "Iron", "Hollow", "Rising", "Shattered", "Endless",
       "Dark", "Bright", "Savage", "Gentle", "Forgotten", "Neon", "Paper", "Silver", "Velvet", "Restless",
       "Quiet", "Stolen", "Sacred", "Cold", "Fallen", "Blue", "Ancient", "Rogue", "Lonely", "Raging"]
NOUN = ["Harbor", "Protocol", "Kingdom", "Signal", "Horizon", "Garden", "Empire", "Echo", "River", "Tower",
        "Promise", "Orbit", "Season", "Station", "Memory", "Island", "Code", "Highway", "Circle", "Legacy",
        "Shadow", "Letter", "Frontier", "Mirror", "Voyage", "Harvest", "Crown", "Engine", "Border", "Archive",
        "Storm", "Street", "Festival", "Witness", "Bridge", "Fortune", "Summit", "Parade", "Trail", "Verdict"]
N_C = 600
combo = rng.choice(len(ADJ) * len(NOUN), N_C, replace=False)
titles = []
for c in combo:
    t = f"{ADJ[c // len(NOUN)]} {NOUN[c % len(NOUN)]}"
    titles.append("The " + t if rng.random() < 0.45 else t)

is_original = rng.random(N_C) < 0.25
is_catalogue = rng.random(N_C) < 0.55            # already on the platform at launch
span_days = (END - START).days
added_days = np.where(is_catalogue, 0, rng.integers(7, span_days - 14, N_C))
added_date = START + pd.to_timedelta(added_days, unit="D")
new_orig = np.where(is_original & ~is_catalogue)[0]
hits = rng.choice(new_orig, 8, replace=False)     # big hit originals

content = pd.DataFrame({
    "content_id": np.arange(1, N_C + 1),
    "title": titles,
    "content_type": rng.choice(["movie", "series"], N_C, p=[0.5, 0.5]),
    "genre": rng.choice(GENRES, N_C, p=[.22, .16, .14, .12, .08, .08, .08, .07, .05]),
    "release_year": np.where(is_catalogue, rng.integers(2000, 2024, N_C),
                             added_date.year - rng.integers(0, 2, N_C)),
    "language": rng.choice(LANGS, N_C, p=[.50, .15, .08, .07, .12, .08]),
    "is_original": is_original,
    "added_date": added_date.date,
})
content["duration_min"] = np.where(content["content_type"] == "movie",
                                   np.clip(rng.normal(108, 20, N_C), 70, 175).astype(int),
                                   rng.integers(28, 62, N_C))
genre_idx = content["genre"].map({g: i for i, g in enumerate(GENRES)}).values
lang_idx = content["language"].map({g: i for i, g in enumerate(LANGS)}).values
is_series = (content["content_type"] == "series").values
dur = content["duration_min"].values

pop = np.minimum(rng.pareto(1.3, N_C) + 1, 25)
pop = pop * np.where(is_original, 1.5, 1.0)
pop[hits] *= 3.0
boost_amp = np.where(is_original, 5.0, 1.5)
boost_amp[hits] = 12.0

# taste by age group (rows) and genre (columns)
AGE_GROUPS = ["18-24", "25-34", "35-44", "45-54", "55+"]
GENRE_PREF = np.array([
    [0.9, 1.2, 1.2, 1.0, 0.5, 1.3, 0.8, 1.5, 1.4],
    [1.1, 1.2, 1.2, 1.2, 0.8, 1.1, 1.0, 0.9, 1.0],
    [1.3, 1.0, 1.0, 1.2, 1.1, 0.9, 1.1, 0.8, 0.7],
    [1.4, 0.9, 0.8, 1.1, 1.4, 0.7, 1.2, 0.6, 0.5],
    [1.4, 0.9, 0.7, 1.0, 1.6, 0.5, 1.3, 0.5, 0.3]])
# language taste by region cluster: 0 = West, 1 = South Asia
LANG_PREF = np.array([
    [1.0, 0.4, 0.1, 0.2, 1.1, 1.2],
    [1.0, 3.0, 4.0, 3.0, 0.8, 0.3]])
GL = np.zeros((5, 2, N_C))
for a in range(5):
    for c in range(2):
        GL[a, c] = GENRE_PREF[a][genre_idx] * LANG_PREF[c][lang_idx]

# =====================================================================
# 3. Subscribers
# =====================================================================
N = N_SUBS
days = pd.date_range(START, END, freq="D")
nd = len(days)
trend = np.linspace(1.0, 2.3, nd)
MONTH_SIGNUP = np.array([1.15, .95, .95, 1.0, .95, 1.0, 1.05, 1.05, .95, 1.0, 1.1, 1.25])
w = trend * MONTH_SIGNUP[days.month.values - 1] * np.where(days.dayofweek.values >= 5, 1.15, 1.0)
promo_day = np.zeros(nd, bool)
partner_day = np.zeros(nd, bool)
for a, b, mult, kind in [("2024-11-20", "2024-12-10", 1.8, "promo"), ("2025-11-20", "2025-12-10", 1.8, "promo"),
                         ("2025-03-01", "2025-03-21", 1.5, "partner"), ("2026-03-01", "2026-03-21", 1.5, "partner")]:
    sel = (days >= a) & (days <= b)
    w[sel] *= mult
    (promo_day if kind == "promo" else partner_day)[sel] = True
sd_idx = np.sort(rng.choice(nd, N, p=w / w.sum()))      # sorted => ids follow signup order
signup_dti = days[sd_idx]
signup_np = signup_dti.values
signup_m = month_index(signup_dti)
bill_day = np.minimum(signup_dti.day.values, 28)

# acquisition channel depends on campaigns
ACQ = ["Organic", "Social Media", "Referral", "Paid Ads", "Partner Bundle"]
promo, partner = promo_day[sd_idx], partner_day[sd_idx]
acq = np.empty(N, dtype=object)
for mask, pp in [(~promo & ~partner, [.30, .24, .14, .20, .12]), (promo, [.18, .27, .09, .38, .08]),
                 (partner, [.22, .15, .08, .15, .40])]:
    acq[mask] = rng.choice(ACQ, mask.sum(), p=pp)
acq_idx = pd.Series(acq).map({a: i for i, a in enumerate(ACQ)}).values

COUNTRIES = ["Sri Lanka", "India", "United Kingdom", "United States", "Australia", "Singapore"]
country = rng.choice(COUNTRIES, N, p=[.28, .24, .12, .17, .07, .12])
CITIES = {"Sri Lanka": ["Colombo", "Kandy", "Galle", "Negombo", "Jaffna"],
          "India": ["Mumbai", "Chennai", "Delhi", "Bengaluru"],
          "United Kingdom": ["London", "Manchester", "Cardiff"],
          "United States": ["New York", "Austin", "Seattle"],
          "Australia": ["Sydney", "Melbourne"], "Singapore": ["Singapore"]}
city = np.empty(N, dtype=object)
for c in COUNTRIES:
    m_ = country == c
    city[m_] = rng.choice(CITIES[c], m_.sum())
south_asia = np.isin(country, ["Sri Lanka", "India"])

age_idx = rng.choice(5, N, p=[.22, .33, .22, .14, .09])
age_group = np.array(AGE_GROUPS)[age_idx]
DEVICES = ["Mobile", "Smart TV", "Laptop", "Tablet", "Console"]
DEV_P = np.array([[.55, .15, .20, .05, .05], [.40, .30, .20, .05, .05], [.25, .45, .18, .07, .05],
                  [.15, .55, .15, .10, .05], [.10, .55, .10, .23, .02]])
dev_idx = np.array([rng.choice(5, p=DEV_P[a]) for a in age_idx])

plan_id = np.where(south_asia,
                   rng.choice([1, 2, 3], N, p=[.55, .35, .10]),
                   rng.choice([1, 2, 3], N, p=[.22, .43, .35]))
plan_i = plan_id - 1

# latent engagement (drives viewing volume and churn)
eng = (np.array([.15, .05, .0, -.05, -.10])[age_idx] + np.array([-.2, 0, .25])[plan_i]
       + np.where(acq == "Referral", .15, 0) + np.where(acq == "Paid Ads", -.10, 0)
       + rng.normal(0, .6, N))

# =====================================================================
# 4. Monthly billing + churn simulation
# =====================================================================
TEN_F = np.array([2.0, 1.5, 1.25, 1.1, 1.0, .95, .9, .85, .8, .75, .7, .7, .6])
PLAN_F = np.array([1.35, 1.0, 0.7])
ACQ_F = np.array([0.9, 1.05, 0.7, 1.4, 1.25])
AGE_F = np.array([1.2, 1.05, 1.0, 0.92, 0.85])
CTRY_F = pd.Series(country).map({"Sri Lanka": 1.10, "India": 1.15, "United Kingdom": .95,
                                 "United States": .95, "Australia": .90, "Singapore": .95}).values
CHURN_SEASON = np.array([1.15, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.9])
SHOCK = {18: 1.9, 19: 1.5, 20: 1.25}                 # Jul-Sep 2025 after the price increase
SHOCK_SENS = np.array([1.0, 0.8, 0.5])

churned = np.zeros(N, bool)
involuntary = np.zeros(N, bool)
churn_date = np.full(N, np.datetime64("NaT"), dtype="datetime64[ns]")
pay_parts = []

for m in range(N_MONTHS):
    ms = month_starts[m]
    elig = np.where((signup_m <= m) & (~churned))[0]
    if len(elig) == 0:
        continue
    t = m - signup_m[elig]
    pay_date = np.where(t == 0, signup_np[elig],
                        (ms + pd.to_timedelta(bill_day[elig] - 1, unit="D")).values)
    valid = pay_date <= END64
    elig, pay_date, t = elig[valid], pay_date[valid], t[valid]
    pi = plan_i[elig]
    price = np.where(pay_date >= PRICE_HIKE.to_datetime64(), price_new[pi], price_old[pi])

    fail_p = np.where(south_asia[elig], .055, .025)
    failed = rng.random(len(elig)) < fail_p
    retry = failed & (rng.random(len(elig)) < .6)
    invol = failed & ~retry

    pay_parts.append(pd.DataFrame({"subscriber_id": elig + 1, "plan_id": pi + 1, "payment_date": pay_date,
                                   "amount": price, "status": np.where(failed, "failed", "success")}))
    if retry.any():
        rd = pay_date[retry] + np.timedelta64(3, "D")
        ok_r = rd <= END64
        pay_parts.append(pd.DataFrame({"subscriber_id": elig[retry][ok_r] + 1, "plan_id": pi[retry][ok_r] + 1,
                                       "payment_date": rd[ok_r], "amount": price[retry][ok_r],
                                       "status": "success"}))

    h = BASE_CHURN * TEN_F[np.minimum(t, 12)]
    h = h * (1 + (SHOCK.get(m, 1.0) - 1) * SHOCK_SENS[pi])
    h = h * CHURN_SEASON[month_moy[m]] * PLAN_F[pi] * ACQ_F[acq_idx[elig]] * AGE_F[age_idx[elig]] * CTRY_F[elig] * np.exp(-0.9 * eng[elig])
    h = np.clip(h, 0, 0.6)
    vol = (~invol) & (rng.random(len(elig)) < h)
    off = np.where(invol, rng.integers(3, 11, len(elig)),
                   np.where(retry, rng.integers(4, 28, len(elig)), rng.integers(2, 28, len(elig))))
    cdate = pay_date + off.astype("timedelta64[D]")
    flag = (vol | invol) & (cdate <= END64)
    idx = elig[flag]
    churned[idx] = True
    churn_date[idx] = cdate[flag]
    involuntary[idx] = invol[flag]

payments = pd.concat(pay_parts, ignore_index=True).sort_values(["payment_date", "subscriber_id"]).reset_index(drop=True)
payments.insert(0, "payment_id", np.arange(1, len(payments) + 1))
payments["payment_date"] = pd.to_datetime(payments["payment_date"]).dt.date

end_np = np.where(churned, churn_date, END64)
end_m = month_index(end_np)
churn_m = np.where(churned, end_m, -1)

# churn reasons
reason = np.full(N, None, dtype=object)
reason[involuntary] = "Payment Failure"
vi = np.where(churned & ~involuntary)[0]
p_price = 0.18 + 0.45 * np.isin(churn_m[vi], [18, 19, 20, 21]) + 0.10 * (plan_i[vi] == 0)
p_content = 0.28 + 0.20 * (eng[vi] < -.2)
raw = np.column_stack([p_price, p_content, np.full(len(vi), .12), np.full(len(vi), .12)])
cum = np.cumsum(raw / raw.sum(1, keepdims=True), axis=1)
pick = (rng.random(len(vi))[:, None] > cum).sum(1)
reason[vi] = np.array(["Price", "Content", "Technical Issues", "Other"])[np.minimum(pick, 3)]

subscribers = pd.DataFrame({
    "subscriber_id": np.arange(1, N + 1),
    "signup_date": signup_dti.strftime("%Y-%m-%d"),
    "country": country, "city": city, "age_group": age_group,
    "gender": rng.choice(["Female", "Male", "Other"], N, p=[.48, .48, .04]),
    "device_type": np.array(DEVICES)[dev_idx], "acquisition": acq,
    "current_plan_id": plan_id,
    "status": np.where(churned, "churned", "active"),
    "churn_date": pd.Series(pd.to_datetime(churn_date)).dt.strftime("%Y-%m-%d"),
    "churn_reason": reason,
})

# =====================================================================
# 5. Viewing sessions
# =====================================================================
counts = end_m - signup_m + 1
sm_sub = np.repeat(np.arange(N), counts)
offs = np.cumsum(counts) - counts
sm_mth = signup_m[sm_sub] + (np.arange(counts.sum()) - np.repeat(offs, counts))
t_sm = sm_mth - signup_m[sm_sub]
mid = month_starts[sm_mth].values + np.timedelta64(14, "D")
d_to_churn = (end_np[sm_sub] - mid).astype("timedelta64[D]").astype(float)
vol_churner = (churned & ~involuntary)[sm_sub]
decay = np.where(vol_churner, np.clip(0.25 + 0.75 * d_to_churn / 75, 0.25, 1.0), 1.0)
VIEW_SEASON = np.array([1.05, .95, .95, 1.0, 1.0, 1.05, 1.1, 1.1, .95, 1.0, 1.05, 1.2])
lam = BASE_SESSIONS * np.exp(eng[sm_sub]) * VIEW_SEASON[month_moy[sm_mth]] * (1 + 0.3 * np.exp(-t_sm / 2)) * decay
n_sess = np.minimum(rng.poisson(lam), 120)

s_sub = np.repeat(sm_sub, n_sess)
s_m = np.repeat(sm_mth, n_sess)
WK = np.array([.9, .9, .95, 1.0, 1.15, 1.35, 1.3])      # Mon..Sun
s_day = np.empty(len(s_sub), dtype="datetime64[ns]")
for m in np.unique(s_m):
    sel = np.where(s_m == m)[0]
    d_arr = pd.date_range(month_starts[m], periods=month_days[m], freq="D")
    wp = WK[d_arr.dayofweek.values]
    s_day[sel] = d_arr.values[rng.choice(len(d_arr), len(sel), p=wp / wp.sum())]
keep = (s_day >= signup_np[s_sub]) & (s_day <= end_np[s_sub])
s_sub, s_day = s_sub[keep], s_day[keep]

# device used (70% primary device)
s_dev = dev_idx[s_sub].copy()
alt = rng.random(len(s_sub)) > 0.7
s_dev[alt] = rng.choice(4, alt.sum())

# hour of day by device and weekend
H = np.arange(24)


def gauss(mu, sd):
    d = np.minimum(np.abs(H - mu), 24 - np.abs(H - mu))
    return np.exp(-0.5 * (d / sd) ** 2)


prof = [0.05 + .5 * gauss(8, 1.5) + .6 * gauss(13, 1.5) + .9 * gauss(22, 2.2),      # Mobile
        0.03 + 1.0 * gauss(21, 2.2) + .25 * gauss(14, 3),                           # Smart TV
        0.04 + .4 * gauss(13, 2) + 1.0 * gauss(20, 2.2) + .3 * gauss(23.5, 1.5),    # Laptop
        0.04 + .5 * gauss(15, 2.5) + .9 * gauss(21, 2),                             # Tablet
        0.03 + .3 * gauss(16, 3) + 1.0 * gauss(22, 2.2)]                            # Console
is_wkend = pd.DatetimeIndex(s_day).dayofweek.values >= 5
s_hour = np.zeros(len(s_sub), int)
for d_ in range(5):
    for wk in (0, 1):
        sel = np.where((s_dev == d_) & (is_wkend == bool(wk)))[0]
        if len(sel) == 0:
            continue
        p = prof[d_] * (1 + 0.8 * gauss(14, 3.5)) if wk else prof[d_]
        s_hour[sel] = rng.choice(24, len(sel), p=p / p.sum())

# outage evenings: many users cannot watch
out_mask = np.zeros(len(s_sub), bool)
for o in OUTAGES:
    out_mask |= (s_day == o.to_datetime64()) & (s_hour >= 18)
drop = out_mask & (rng.random(len(s_sub)) < 0.55)
s_sub, s_day, s_dev, s_hour = s_sub[~drop], s_day[~drop], s_dev[~drop], s_hour[~drop]

# content choice (weekly new-release boost, taste by age group and region)
n_s = len(s_sub)
week = ((s_day - START64).astype("timedelta64[D]").astype(int)) // 7
s_age = age_idx[s_sub]
s_cl = south_asia[s_sub].astype(int)
cid = np.zeros(n_s, int)
grp = pd.DataFrame({"w": week, "a": s_age, "c": s_cl}).groupby(["w", "a", "c"]).indices
wb_cache = {}
for (wk_, a_, c_), pos in grp.items():
    if wk_ not in wb_cache:
        sd = wk_ * 7
        avail = added_days <= sd + 6
        age_d = np.clip(sd - added_days, 0, None)
        wb_cache[wk_] = pop * avail * (1 + boost_amp * np.exp(-age_d / 21.0))
    wts = wb_cache[wk_] * GL[a_, c_]
    cid[pos] = rng.choice(N_C, len(pos), p=wts / wts.sum())

# minutes watched
BETA = np.array([[2.0, 2.2], [2.8, 1.4], [2.4, 1.6], [2.4, 1.8], [2.6, 1.5]])
BINGE = np.array([.20, .50, .35, .30, .40])
frac = rng.beta(BETA[s_dev, 0], BETA[s_dev, 1])
extra = np.where(is_series[cid] & (rng.random(n_s) < BINGE[s_dev]), rng.geometric(0.55, n_s), 0)
minutes = np.clip((dur[cid] * (frac + extra)).astype(int), 2, 600)
completed = frac >= 0.9
start_ts = s_day + (s_hour * 60 + rng.integers(0, 60, n_s)).astype("timedelta64[m]")

viewing_sessions = pd.DataFrame({
    "subscriber_id": s_sub + 1, "content_id": cid + 1, "session_start": start_ts,
    "minutes_watched": minutes, "device_type": np.array(DEVICES)[s_dev], "completed": completed,
}).sort_values("session_start").reset_index(drop=True)
viewing_sessions.insert(0, "session_id", np.arange(1, len(viewing_sessions) + 1))

# =====================================================================
# 6. Support tickets
# =====================================================================
CATS = ["Billing", "Playback Issue", "Account Access", "Content Request", "App Bug"]
CAT_P = [.26, .28, .14, .08, .24]
tk = []   # (sub_idx, created_ts, category)

# baseline tickets (more in the weeks before a voluntary churn)
pre_churn = vol_churner & (d_to_churn < 45)
lam_t = 0.07 * (1 + 3 * pre_churn)
n_t = rng.poisson(lam_t)
t_sub = np.repeat(sm_sub, n_t)
t_m = np.repeat(sm_mth, n_t)
t_day = np.empty(len(t_sub), dtype="datetime64[ns]")
for m in np.unique(t_m):
    sel = np.where(t_m == m)[0]
    t_day[sel] = (month_starts[m].to_datetime64() + rng.integers(0, month_days[m], len(sel)).astype("timedelta64[D]"))
okt = (t_day >= signup_np[t_sub]) & (t_day <= end_np[t_sub])
t_sub, t_day = t_sub[okt], t_day[okt]
tk.append((t_sub, t_day + rng.integers(8 * 60, 23 * 60, len(t_sub)).astype("timedelta64[m]"),
           rng.choice(CATS, len(t_sub), p=CAT_P)))

# billing tickets after failed payments
fp_rows = payments[payments["status"] == "failed"]
pick = rng.random(len(fp_rows)) < 0.25
fs = fp_rows["subscriber_id"].values[pick] - 1
fd = pd.to_datetime(fp_rows["payment_date"]).values[pick] + rng.integers(0, 5, pick.sum()).astype("timedelta64[D]")
tk.append((fs, fd + np.timedelta64(11, "h"), np.full(len(fs), "Billing")))

# cancellation tickets
cs = np.where(churned & ~involuntary & (rng.random(N) < .55))[0]
tk.append((cs, churn_date[cs] + np.timedelta64(10, "h"), np.full(len(cs), "Cancellation")))

# outage days: playback complaints
for o in OUTAGES:
    act = np.where((signup_np <= o.to_datetime64()) & (end_np >= o.to_datetime64()) & (rng.random(N) < 0.035))[0]
    tk.append((act, o.to_datetime64() + rng.integers(18 * 60, 24 * 60, len(act)).astype("timedelta64[m]"),
               np.full(len(act), "Playback Issue")))

# price increase: billing questions in the first ten days
act = np.where((signup_np <= PRICE_HIKE.to_datetime64()) & (end_np >= PRICE_HIKE.to_datetime64()) & (rng.random(N) < 0.07))[0]
tk.append((act, PRICE_HIKE.to_datetime64() + rng.integers(0, 10 * 24 * 60, len(act)).astype("timedelta64[m]"),
           np.full(len(act), "Billing")))

support_tickets = pd.DataFrame({
    "subscriber_id": np.concatenate([a + 1 for a, _, _ in tk]),
    "created_at": np.concatenate([b for _, b, _ in tk]),
    "category": np.concatenate([c for _, _, c in tk]),
}).sort_values("created_at").reset_index(drop=True)
support_tickets.insert(0, "ticket_id", np.arange(1, len(support_tickets) + 1))
SCALE = {"Billing": 6, "Playback Issue": 10, "Account Access": 5, "Content Request": 30, "App Bug": 24, "Cancellation": 2}
scale = support_tickets["category"].map(SCALE).values
res_h = np.round(rng.gamma(2.0, scale), 1)
recent = support_tickets["created_at"] > (END - pd.Timedelta(days=3))
resolved = rng.random(len(support_tickets)) < np.where(recent, 0.5, 0.97)
support_tickets["resolved"] = resolved
support_tickets["resolution_hours"] = np.where(resolved, res_h, np.nan)

# =====================================================================
# 7. Save
# =====================================================================
tables = {"plans": plans, "plan_price_history": plan_price_history, "subscribers": subscribers,
          "content": content, "viewing_sessions": viewing_sessions, "payments": payments,
          "support_tickets": support_tickets}
for name, df in tables.items():
    df.to_csv(OUT / f"{name}.csv", index=False)

print("Generated synthetic data in", OUT)
for name, df in tables.items():
    print(f"  {name:20s} {len(df):>9,d} rows")
print(f"Overall churned: {(subscribers['status'] == 'churned').mean():.1%} of all subscribers ever signed up")
