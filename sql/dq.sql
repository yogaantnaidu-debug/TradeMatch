-- DATA QUALITY checks for one run. Params: %(run_id)s, %(batch_id)s.
-- Each check produces an observed and an expected number; passed = (observed = expected).
-- Results are stored, not just printed, so you can query the history of every run.
INSERT INTO dq_results (run_id, check_name, passed, observed, expected)
SELECT %(run_id)s, check_name, observed = expected, observed::text, expected::text
FROM (
    -- Nothing silently lost: every raw row is either clean or rejected (with a reason).
    SELECT 'internal_rows_reconcile' AS check_name,
           (SELECT count(*) FROM clean_internal WHERE batch_id = %(batch_id)s)
         + (SELECT count(*) FROM rejected_records WHERE batch_id = %(batch_id)s AND source = 'INTERNAL') AS observed,
           (SELECT count(*) FROM raw_internal WHERE batch_id = %(batch_id)s) AS expected
    UNION ALL
    SELECT 'custodian_rows_reconcile',
           (SELECT count(*) FROM clean_custodian WHERE batch_id = %(batch_id)s)
         + (SELECT count(*) FROM rejected_records WHERE batch_id = %(batch_id)s AND source = 'CUSTODIAN'),
           (SELECT count(*) FROM raw_custodian WHERE batch_id = %(batch_id)s)
    UNION ALL
    -- Every distinct trade from either side got a MATCHED/BREAK verdict.
    SELECT 'every_trade_reconciled',
           (SELECT count(*) FROM match_results WHERE batch_id = %(batch_id)s),
           (SELECT count(*) FROM (SELECT trade_id FROM clean_internal  WHERE batch_id = %(batch_id)s
                                  UNION
                                  SELECT trade_id FROM clean_custodian WHERE batch_id = %(batch_id)s) u)
    UNION ALL
    SELECT 'no_null_keys',
           (SELECT count(*) FROM clean_internal  WHERE batch_id = %(batch_id)s AND (trade_id IS NULL OR isin IS NULL))
         + (SELECT count(*) FROM clean_custodian WHERE batch_id = %(batch_id)s AND (trade_id IS NULL OR isin IS NULL)),
           0
    UNION ALL
    SELECT 'price_positive',
           (SELECT count(*) FROM clean_internal  WHERE batch_id = %(batch_id)s AND price <= 0)
         + (SELECT count(*) FROM clean_custodian WHERE batch_id = %(batch_id)s AND price <= 0),
           0
    UNION ALL
    SELECT 'valid_side',
           (SELECT count(*) FROM clean_internal  WHERE batch_id = %(batch_id)s AND side NOT IN ('BUY', 'SELL'))
         + (SELECT count(*) FROM clean_custodian WHERE batch_id = %(batch_id)s AND side NOT IN ('BUY', 'SELL')),
           0
    UNION ALL
    -- The two recon tables agree: trades flagged BREAK = trades with an open break from this batch.
    SELECT 'breaks_consistent',
           (SELECT count(*) FROM match_results WHERE batch_id = %(batch_id)s AND status = 'BREAK'),
           (SELECT count(DISTINCT trade_id) FROM breaks WHERE last_batch_id = %(batch_id)s AND status = 'OPEN')
) checks
ON CONFLICT (run_id, check_name) DO UPDATE
SET passed = EXCLUDED.passed, observed = EXCLUDED.observed, expected = EXCLUDED.expected;
