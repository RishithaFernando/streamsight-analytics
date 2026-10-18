-- =====================================================================
-- StreamSight analytics views (the TRANSFORM layer for dashboards)
-- Power BI reads these views instead of the raw tables.
-- Safe to re-run (CREATE OR REPLACE).
-- =====================================================================
SET search_path TO streamsight;

-- 1. Monthly business KPIs ---------------------------------------------
CREATE OR REPLACE VIEW vw_monthly_kpis AS
WITH months AS (
    SELECT gs::date AS month_start,
           (gs + INTERVAL '1 month' - INTERVAL '1 day')::date AS month_end
    FROM generate_series(
            date_trunc('month', (SELECT MIN(signup_date) FROM subscribers)::timestamp),
            date_trunc('month', (SELECT MAX(payment_date) FROM payments)::timestamp),
            INTERVAL '1 month') AS gs
)
SELECT
    m.month_start,
    COALESCE(n.new_subscribers, 0)                         AS new_subscribers,
    COALESCE(c.churned_subscribers, 0)                     AS churned_subscribers,
    a0.active                                              AS active_at_start,
    a1.active                                              AS active_at_end,
    ROUND(100.0 * COALESCE(c.churned_from_opening, 0) / NULLIF(a0.active, 0), 2) AS monthly_churn_pct,
    COALESCE(p.revenue, 0)                                 AS revenue,
    COALESCE(p.paying_subscribers, 0)                      AS paying_subscribers,
    ROUND(COALESCE(p.revenue, 0) / NULLIF(p.paying_subscribers, 0), 2) AS arpu,
    COALESCE(p.failed_payments, 0)                         AS failed_payments,
    ROUND(100.0 * COALESCE(p.failed_payments, 0) / NULLIF(p.attempts, 0), 2) AS payment_failure_pct
FROM months m
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS active FROM subscribers s
    WHERE s.signup_date < m.month_start AND (s.churn_date IS NULL OR s.churn_date >= m.month_start)) a0 ON TRUE
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS active FROM subscribers s
    WHERE s.signup_date <= m.month_end AND (s.churn_date IS NULL OR s.churn_date > m.month_end)) a1 ON TRUE
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS new_subscribers FROM subscribers s
    WHERE s.signup_date BETWEEN m.month_start AND m.month_end) n ON TRUE
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS churned_subscribers,
           COUNT(*) FILTER (WHERE s.signup_date < m.month_start) AS churned_from_opening
    FROM subscribers s
    WHERE s.churn_date BETWEEN m.month_start AND m.month_end) c ON TRUE
LEFT JOIN LATERAL (
    SELECT SUM(amount) FILTER (WHERE status = 'success')                    AS revenue,
           COUNT(DISTINCT subscriber_id) FILTER (WHERE status = 'success')  AS paying_subscribers,
           COUNT(*) FILTER (WHERE status = 'failed')                        AS failed_payments,
           COUNT(*)                                                         AS attempts
    FROM payments pay
    WHERE pay.payment_date BETWEEN m.month_start AND m.month_end) p ON TRUE;

-- 2. Monthly viewing / engagement ---------------------------------------
CREATE OR REPLACE VIEW vw_monthly_engagement AS
SELECT
    date_trunc('month', session_start)::date                     AS month_start,
    COUNT(*)                                                     AS sessions,
    COUNT(DISTINCT subscriber_id)                                AS active_viewers,
    ROUND(SUM(minutes_watched) / 60.0, 0)                        AS hours_watched,
    ROUND(SUM(minutes_watched) / 60.0 / COUNT(DISTINCT subscriber_id), 1) AS hours_per_viewer,
    ROUND(AVG(minutes_watched), 1)                               AS avg_minutes_per_session
FROM viewing_sessions
GROUP BY 1;

-- 3. Subscriber dimension (for churn by plan / country / age / channel) --
CREATE OR REPLACE VIEW vw_subscriber_base AS
SELECT
    s.subscriber_id,
    s.signup_date,
    date_trunc('month', s.signup_date)::date                     AS signup_month,
    s.country, s.city, s.age_group, s.gender, s.device_type, s.acquisition,
    p.plan_name,
    p.monthly_price                                              AS current_price,
    s.status,
    CASE WHEN s.status = 'churned' THEN 1 ELSE 0 END             AS is_churned,
    s.churn_date,
    s.churn_reason,
    (COALESCE(s.churn_date, (SELECT MAX(payment_date) FROM payments)) - s.signup_date) AS tenure_days,
    CASE
        WHEN COALESCE(s.churn_date, (SELECT MAX(payment_date) FROM payments)) - s.signup_date < 90  THEN '0-3 months'
        WHEN COALESCE(s.churn_date, (SELECT MAX(payment_date) FROM payments)) - s.signup_date < 180 THEN '3-6 months'
        WHEN COALESCE(s.churn_date, (SELECT MAX(payment_date) FROM payments)) - s.signup_date < 365 THEN '6-12 months'
        ELSE '12+ months'
    END                                                          AS tenure_band
FROM subscribers s
JOIN plans p ON p.plan_id = s.current_plan_id;

-- 4. When do people watch? (weekday x hour x device heatmap) -------------
CREATE OR REPLACE VIEW vw_viewing_heatmap AS
SELECT
    device_type,
    EXTRACT(ISODOW FROM session_start)::int                      AS weekday_no,
    TO_CHAR(session_start, 'Dy')                                 AS weekday_name,
    EXTRACT(HOUR FROM session_start)::int                        AS hour_of_day,
    COUNT(*)                                                     AS sessions,
    ROUND(SUM(minutes_watched) / 60.0, 1)                        AS hours_watched
FROM viewing_sessions
GROUP BY 1, 2, 3, 4;

-- 5. Content performance --------------------------------------------------
CREATE OR REPLACE VIEW vw_content_performance AS
SELECT
    c.content_id, c.title, c.content_type, c.genre, c.language, c.is_original, c.added_date,
    COUNT(vs.session_id)                                         AS sessions,
    COUNT(DISTINCT vs.subscriber_id)                             AS unique_viewers,
    ROUND(COALESCE(SUM(vs.minutes_watched), 0) / 60.0, 1)        AS hours_watched,
    ROUND(100.0 * AVG(vs.completed::int), 1)                     AS completion_rate_pct,
    ROUND(AVG(vs.minutes_watched), 1)                            AS avg_minutes_per_session
FROM content c
LEFT JOIN viewing_sessions vs ON vs.content_id = c.content_id
GROUP BY c.content_id;

-- 6. Genre taste by age group ----------------------------------------------
CREATE OR REPLACE VIEW vw_genre_by_age AS
WITH x AS (
    SELECT s.age_group, c.genre, COUNT(*) AS sessions
    FROM viewing_sessions vs
    JOIN subscribers s ON s.subscriber_id = vs.subscriber_id
    JOIN content c     ON c.content_id    = vs.content_id
    GROUP BY 1, 2
)
SELECT age_group, genre, sessions,
       ROUND(100.0 * sessions / SUM(sessions) OVER (PARTITION BY age_group), 1) AS share_of_age_group_pct
FROM x;

-- 7. Why do people leave? ---------------------------------------------------
CREATE OR REPLACE VIEW vw_churn_reasons AS
SELECT date_trunc('month', churn_date)::date AS churn_month,
       churn_reason,
       COUNT(*) AS churned_subscribers
FROM subscribers
WHERE status = 'churned'
GROUP BY 1, 2;

-- 8. Daily activity: spot outages and anomalies -------------------------------
CREATE OR REPLACE VIEW vw_daily_activity AS
WITH days AS (
    SELECT d::date AS activity_date
    FROM generate_series((SELECT MIN(session_start)::date FROM viewing_sessions),
                         (SELECT MAX(session_start)::date FROM viewing_sessions),
                         INTERVAL '1 day') AS d
),
s AS (SELECT session_start::date AS d, COUNT(*) AS sessions FROM viewing_sessions GROUP BY 1),
t AS (SELECT created_at::date AS d, COUNT(*) AS tickets FROM support_tickets GROUP BY 1)
SELECT days.activity_date,
       COALESCE(s.sessions, 0) AS sessions,
       COALESCE(t.tickets, 0)  AS support_tickets
FROM days
LEFT JOIN s ON s.d = days.activity_date
LEFT JOIN t ON t.d = days.activity_date;
