-- Extra indexes beyond the primary keys / unique constraints in schema.sql.
-- benchmark_explain.py drops and recreates exactly these to measure before/after.

-- The clean tables' primary key is (batch_id, trade_id). A search by trade_id alone
-- (e.g. "show me this trade across all batches") cannot use it well, because trade_id
-- is the second column. These indexes serve that lookup.
CREATE INDEX IF NOT EXISTS idx_clean_internal_trade_id  ON clean_internal (trade_id);
CREATE INDEX IF NOT EXISTS idx_clean_custodian_trade_id ON clean_custodian (trade_id);

-- Operations screens ask "which trades in this batch broke?" and "which breaks are open?".
CREATE INDEX IF NOT EXISTS idx_match_results_batch_status ON match_results (batch_id, status);
CREATE INDEX IF NOT EXISTS idx_breaks_status ON breaks (status);
