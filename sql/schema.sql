-- StreamSight: subscription streaming analytics (PostgreSQL)
-- All data is synthetic and generated for portfolio purposes.

DROP SCHEMA IF EXISTS streamsight CASCADE;
CREATE SCHEMA streamsight;
SET search_path TO streamsight;

CREATE TABLE plans (
    plan_id        INT PRIMARY KEY,
    plan_name      VARCHAR(30) NOT NULL UNIQUE,
    monthly_price  NUMERIC(8,2) NOT NULL,      -- current price
    max_screens    INT NOT NULL,
    max_quality    VARCHAR(10) NOT NULL
);

CREATE TABLE plan_price_history (
    plan_id        INT NOT NULL REFERENCES plans(plan_id),
    effective_from DATE NOT NULL,
    monthly_price  NUMERIC(8,2) NOT NULL,
    PRIMARY KEY (plan_id, effective_from)
);

CREATE TABLE subscribers (
    subscriber_id   INT PRIMARY KEY,
    signup_date     DATE NOT NULL,
    country         VARCHAR(40) NOT NULL,
    city            VARCHAR(60),
    age_group       VARCHAR(10) NOT NULL,
    gender          VARCHAR(10),
    device_type     VARCHAR(20) NOT NULL,
    acquisition     VARCHAR(30) NOT NULL,
    current_plan_id INT NOT NULL REFERENCES plans(plan_id),
    status          VARCHAR(10) NOT NULL CHECK (status IN ('active','churned')),
    churn_date      DATE,
    churn_reason    VARCHAR(30)
);

CREATE TABLE content (
    content_id     INT PRIMARY KEY,
    title          VARCHAR(120) NOT NULL,
    content_type   VARCHAR(10) NOT NULL CHECK (content_type IN ('movie','series')),
    genre          VARCHAR(30) NOT NULL,
    release_year   INT NOT NULL,
    language       VARCHAR(20) NOT NULL,
    is_original    BOOLEAN NOT NULL,
    added_date     DATE NOT NULL,
    duration_min   INT NOT NULL
);

CREATE TABLE viewing_sessions (
    session_id      BIGINT PRIMARY KEY,
    subscriber_id   INT NOT NULL REFERENCES subscribers(subscriber_id),
    content_id      INT NOT NULL REFERENCES content(content_id),
    session_start   TIMESTAMP NOT NULL,
    minutes_watched INT NOT NULL,
    device_type     VARCHAR(20) NOT NULL,
    completed       BOOLEAN NOT NULL
);

CREATE TABLE payments (
    payment_id     BIGINT PRIMARY KEY,
    subscriber_id  INT NOT NULL REFERENCES subscribers(subscriber_id),
    plan_id        INT NOT NULL REFERENCES plans(plan_id),
    payment_date   DATE NOT NULL,
    amount         NUMERIC(8,2) NOT NULL,
    status         VARCHAR(10) NOT NULL CHECK (status IN ('success','failed'))
);

CREATE TABLE support_tickets (
    ticket_id        BIGINT PRIMARY KEY,
    subscriber_id    INT NOT NULL REFERENCES subscribers(subscriber_id),
    created_at       TIMESTAMP NOT NULL,
    category         VARCHAR(30) NOT NULL,
    resolved         BOOLEAN NOT NULL,
    resolution_hours NUMERIC(7,1)
);

-- ETL monitoring (used from Day 2)
CREATE TABLE etl_run_log (
    log_id         SERIAL PRIMARY KEY,
    run_started    TIMESTAMP NOT NULL DEFAULT NOW(),
    run_finished   TIMESTAMP,
    step_name      VARCHAR(60) NOT NULL,
    rows_loaded    INT,
    status         VARCHAR(10) NOT NULL,
    message        TEXT
);

CREATE INDEX idx_sessions_sub   ON viewing_sessions(subscriber_id);
CREATE INDEX idx_sessions_time  ON viewing_sessions(session_start);
CREATE INDEX idx_sessions_cont  ON viewing_sessions(content_id);
CREATE INDEX idx_payments_sub   ON payments(subscriber_id);
CREATE INDEX idx_payments_date  ON payments(payment_date);
CREATE INDEX idx_tickets_sub    ON support_tickets(subscriber_id);
