-- Reporting views. CREATE OR REPLACE so the pipeline can apply them every run.

-- Age of every OPEN break, measured against the latest successful business date
-- (not the wall clock, so re-running an old day gives the same answer).
CREATE OR REPLACE VIEW break_ageing AS
WITH ref AS (
    SELECT coalesce(max(as_of_date), current_date) AS as_of
    FROM pipeline_runs
    WHERE status = 'SUCCESS'
)
SELECT
    b.break_id,
    b.trade_id,
    b.break_type,
    b.first_detected_date,
    ref.as_of - b.first_detected_date AS age_days,   -- date minus date = whole days
    CASE
        WHEN ref.as_of - b.first_detected_date <= 1 THEN '0-1'
        WHEN ref.as_of - b.first_detected_date <= 5 THEN '2-5'
        ELSE '6+'
    END AS age_bucket
FROM breaks b
CROSS JOIN ref
WHERE b.status = 'OPEN';

-- One row per batch. Uses match_results (one row per trade per batch), so the history
-- of old batches does not change when later batches resolve their breaks.
CREATE OR REPLACE VIEW recon_summary AS
SELECT
    batch_id,
    as_of_date,
    count(*)                                         AS total_trades,
    count(*) FILTER (WHERE status = 'MATCHED')       AS matched,
    count(*) FILTER (WHERE status = 'BREAK')         AS broken,
    round(100.0 * count(*) FILTER (WHERE status = 'MATCHED') / count(*), 2) AS match_rate_pct,
    count(*) FILTER (WHERE 'PRICE_MISMATCH'       = ANY (break_types)) AS price_mismatch,
    count(*) FILTER (WHERE 'QUANTITY_MISMATCH'    = ANY (break_types)) AS quantity_mismatch,
    count(*) FILTER (WHERE 'MISSING_IN_CUSTODIAN' = ANY (break_types)) AS missing_in_custodian,
    count(*) FILTER (WHERE 'MISSING_IN_INTERNAL'  = ANY (break_types)) AS missing_in_internal
FROM match_results
GROUP BY batch_id, as_of_date;
