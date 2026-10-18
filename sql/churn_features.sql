-- =====================================================================
-- StreamSight churn feature table
-- One row per (snapshot_date, subscriber) who was ACTIVE on that date.
-- Features use ONLY data up to the snapshot date (no peeking at the future).
-- Label: churned_next_90d = 1 if the subscriber churned within 90 days AFTER
--        the snapshot, 0 if not, NULL if the 90 days are not fully observed yet
--        (those rows are used for scoring today's customers, not for training).
--
-- NOT used as features on purpose (they would leak the answer):
--   status, churn_date, churn_reason
-- Re-run this script after every ETL load.
-- =====================================================================
SET search_path TO streamsight;

-- A view that depends on the table must be dropped first and recreated at the end.
-- When run through run_sql.py all of this happens in ONE transaction, so readers never see a gap.
DROP VIEW IF EXISTS vw_churn_risk;
DROP TABLE IF EXISTS churn_features;

CREATE TABLE churn_features AS
WITH snapshots AS (
    SELECT unnest(ARRAY['2025-09-30', '2025-12-31', '2026-03-31', '2026-06-30', '2026-09-30']::date[]) AS snapshot_date
),
data_end AS (
    SELECT MAX(session_start)::date AS d FROM viewing_sessions
),
base AS (   -- who was an active customer on each snapshot date
    SELECT sn.snapshot_date, s.subscriber_id, s.signup_date, s.churn_date,
           s.country, s.age_group, s.device_type, s.acquisition, p.plan_name
    FROM snapshots sn
    JOIN subscribers s ON s.signup_date <= sn.snapshot_date
                      AND (s.churn_date IS NULL OR s.churn_date > sn.snapshot_date)
    JOIN plans p ON p.plan_id = s.current_plan_id
)
SELECT
    b.snapshot_date,
    b.subscriber_id,
    -- profile
    b.plan_name, b.country, b.age_group, b.device_type, b.acquisition,
    (b.snapshot_date - b.signup_date)                                   AS tenure_days,
    -- viewing behaviour
    v.sessions_30d,
    v.sessions_prev_30d,
    -- ratio of last 30 days to the 30 days before; only meaningful once the customer has 60+ days of history,
    -- so newer customers get a neutral 1.0 (otherwise a tiny 'previous' window inflates the ratio)
    CASE WHEN (b.snapshot_date - b.signup_date) >= 60
         THEN ROUND((v.sessions_30d + 1.0) / (v.sessions_prev_30d + 1.0), 3)
         ELSE 1.0 END                                                   AS engagement_ratio,
    v.minutes_30d,
    ROUND(v.avg_minutes_per_session_30d, 1)                             AS avg_minutes_per_session_30d,
    ROUND(v.completion_rate_30d, 3)                                     AS completion_rate_30d,
    v.active_days_30d,
    COALESCE(b.snapshot_date - v.last_session_date, b.snapshot_date - b.signup_date) AS days_since_last_session,
    -- payments
    pay.failed_payments_30d,
    pay.failed_payments_90d,
    -- support
    t.tickets_30d,
    t.tickets_90d,
    t.billing_tickets_90d,
    t.playback_tickets_90d,
    -- label
    CASE
        WHEN b.snapshot_date + 90 > (SELECT d FROM data_end) THEN NULL
        WHEN b.churn_date > b.snapshot_date AND b.churn_date <= b.snapshot_date + 90 THEN 1
        ELSE 0
    END                                                                 AS churned_next_90d
FROM base b
LEFT JOIN LATERAL (
    SELECT
        COUNT(*) FILTER (WHERE vs.session_start >= b.snapshot_date - 29)                                  AS sessions_30d,
        COUNT(*) FILTER (WHERE vs.session_start >= b.snapshot_date - 59
                           AND vs.session_start <  b.snapshot_date - 29)                                  AS sessions_prev_30d,
        COALESCE(SUM(vs.minutes_watched) FILTER (WHERE vs.session_start >= b.snapshot_date - 29), 0)      AS minutes_30d,
        COALESCE(AVG(vs.minutes_watched) FILTER (WHERE vs.session_start >= b.snapshot_date - 29), 0)      AS avg_minutes_per_session_30d,
        COALESCE(AVG(vs.completed::int)  FILTER (WHERE vs.session_start >= b.snapshot_date - 29), 0)      AS completion_rate_30d,
        COUNT(DISTINCT vs.session_start::date) FILTER (WHERE vs.session_start >= b.snapshot_date - 29)    AS active_days_30d,
        MAX(vs.session_start)::date                                                                       AS last_session_date
    FROM viewing_sessions vs
    WHERE vs.subscriber_id = b.subscriber_id
      AND vs.session_start < b.snapshot_date + 1
) v ON TRUE
LEFT JOIN LATERAL (
    SELECT
        COUNT(*) FILTER (WHERE status = 'failed' AND payment_date > b.snapshot_date - 30) AS failed_payments_30d,
        COUNT(*) FILTER (WHERE status = 'failed' AND payment_date > b.snapshot_date - 90) AS failed_payments_90d
    FROM payments p
    WHERE p.subscriber_id = b.subscriber_id AND p.payment_date <= b.snapshot_date
) pay ON TRUE
LEFT JOIN LATERAL (
    SELECT
        COUNT(*) FILTER (WHERE tk.created_at >= b.snapshot_date - 29)                                 AS tickets_30d,
        COUNT(*) FILTER (WHERE tk.created_at >= b.snapshot_date - 89)                                 AS tickets_90d,
        COUNT(*) FILTER (WHERE tk.created_at >= b.snapshot_date - 89 AND tk.category = 'Billing')     AS billing_tickets_90d,
        COUNT(*) FILTER (WHERE tk.created_at >= b.snapshot_date - 89 AND tk.category = 'Playback Issue') AS playback_tickets_90d
    FROM support_tickets tk
    WHERE tk.subscriber_id = b.subscriber_id AND tk.created_at < b.snapshot_date + 1
) t ON TRUE;

ALTER TABLE churn_features ADD PRIMARY KEY (snapshot_date, subscriber_id);
CREATE INDEX idx_churn_features_label ON churn_features (snapshot_date, churned_next_90d);
ANALYZE churn_features;

-- Recreate the Power BI view (only possible once the model has created churn_predictions;
-- the model script also creates it, so the definition is identical in both places).
DO $do$
BEGIN
    IF to_regclass('streamsight.churn_predictions') IS NOT NULL THEN
        EXECUTE $v$
            CREATE OR REPLACE VIEW streamsight.vw_churn_risk AS
            SELECT p.subscriber_id, p.snapshot_date, p.churn_probability, p.risk_rank, p.risk_band, p.top_drivers,
                   f.plan_name, f.country, f.age_group, f.device_type, f.acquisition, f.tenure_days,
                   f.sessions_30d, f.sessions_prev_30d, f.days_since_last_session,
                   f.failed_payments_90d, f.tickets_90d
            FROM streamsight.churn_predictions p
            JOIN streamsight.churn_features f
              ON f.subscriber_id = p.subscriber_id AND f.snapshot_date = p.snapshot_date
        $v$;
    END IF;
END
$do$;
