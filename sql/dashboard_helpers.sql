-- Small helper views for the Power BI dashboard (run once in pgAdmin, after the churn model has run).
SET search_path TO streamsight;

-- Latest model validation: how often each risk band really churned (sorted High, Medium, Low)
CREATE OR REPLACE VIEW vw_band_performance_latest AS
SELECT risk_band,
       CASE risk_band WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END AS band_order,
       customers,
       actual_churn_pct
FROM churn_band_performance
WHERE run_id = (SELECT MAX(run_id) FROM churn_band_performance);

-- Latest model quality numbers (selected model, test stage) for KPI cards
CREATE OR REPLACE VIEW vw_model_metrics_latest AS
SELECT model_name, roc_auc, pr_auc, base_rate, lift_top10, recall_top20, trained_at
FROM churn_model_metrics
WHERE run_id = (SELECT MAX(run_id) FROM churn_model_metrics)
  AND selected AND stage = 'test';
