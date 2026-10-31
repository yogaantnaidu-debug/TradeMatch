-- RECON stage for one batch.
-- Params: %(batch_id)s, %(as_of)s (business date), %(tolerance)s (relative price tolerance).

-- Step 1: put both sides next to each other. FULL OUTER JOIN keeps trades that exist on only
-- one side (the other side's columns are NULL), which is how "missing" breaks are found.
-- An INNER JOIN would silently drop exactly those trades.
CREATE TEMP TABLE stg_joined ON COMMIT DROP AS
SELECT
    coalesce(i.trade_id, c.trade_id) AS trade_id,
    i.trade_id IS NOT NULL           AS in_internal,
    c.trade_id IS NOT NULL           AS in_custodian,
    i.price                          AS internal_price,
    c.price                          AS custodian_price,
    i.quantity                       AS internal_qty,
    c.quantity                       AS custodian_qty
FROM (SELECT * FROM clean_internal  WHERE batch_id = %(batch_id)s) i
FULL OUTER JOIN
     (SELECT * FROM clean_custodian WHERE batch_id = %(batch_id)s) c
  ON c.trade_id = i.trade_id;

-- Step 2: one row per detected break. A trade can have more than one (price AND quantity).
CREATE TEMP TABLE stg_breaks ON COMMIT DROP AS
SELECT trade_id, 'MISSING_IN_CUSTODIAN' AS break_type,
       internal_qty AS internal_value, NULL::numeric AS custodian_value
FROM stg_joined WHERE in_internal AND NOT in_custodian
UNION ALL
SELECT trade_id, 'MISSING_IN_INTERNAL', NULL::numeric, custodian_qty
FROM stg_joined WHERE in_custodian AND NOT in_internal
UNION ALL
-- Break only when the gap is strictly GREATER than tolerance x internal price.
-- NUMERIC is exact, so a gap exactly equal to the tolerance is reliably NOT a break.
SELECT trade_id, 'PRICE_MISMATCH', internal_price, custodian_price
FROM stg_joined
WHERE in_internal AND in_custodian
  AND abs(internal_price - custodian_price) > %(tolerance)s * internal_price
UNION ALL
SELECT trade_id, 'QUANTITY_MISMATCH', internal_qty, custodian_qty
FROM stg_joined
WHERE in_internal AND in_custodian AND internal_qty <> custodian_qty;

-- Step 3: MATCHED / BREAK for every trade in the batch. Upsert = safe to re-run.
INSERT INTO match_results (batch_id, trade_id, status, break_count, break_types, as_of_date)
SELECT
    %(batch_id)s,
    j.trade_id,
    CASE WHEN count(b.break_type) = 0 THEN 'MATCHED' ELSE 'BREAK' END,
    count(b.break_type),
    coalesce(array_agg(b.break_type ORDER BY b.break_type) FILTER (WHERE b.break_type IS NOT NULL), '{}'),
    %(as_of)s
FROM stg_joined j
LEFT JOIN stg_breaks b ON b.trade_id = j.trade_id
GROUP BY j.trade_id
ON CONFLICT (batch_id, trade_id) DO UPDATE
SET status      = EXCLUDED.status,
    break_count = EXCLUDED.break_count,
    break_types = EXCLUDED.break_types,
    as_of_date  = EXCLUDED.as_of_date;

-- Step 4: record breaks across batches. If the break already exists we keep its
-- first_detected_date (that is what ageing measures) and just refresh the values.
INSERT INTO breaks (trade_id, break_type, internal_value, custodian_value, difference,
                    first_detected_date, last_seen_date, last_batch_id, status)
SELECT trade_id, break_type, internal_value, custodian_value,
       custodian_value - internal_value,
       %(as_of)s, %(as_of)s, %(batch_id)s, 'OPEN'
FROM stg_breaks
ON CONFLICT (trade_id, break_type) DO UPDATE
SET internal_value  = EXCLUDED.internal_value,
    custodian_value = EXCLUDED.custodian_value,
    difference      = EXCLUDED.difference,
    last_seen_date  = greatest(breaks.last_seen_date, EXCLUDED.last_seen_date),
    last_batch_id   = EXCLUDED.last_batch_id,
    status          = 'OPEN',
    resolved_date   = NULL;

-- Step 5: an open break that is no longer found in this batch has been fixed
-- (e.g. a late custodian record finally arrived). Assumes each batch is a full snapshot.
UPDATE breaks b
SET status = 'RESOLVED', resolved_date = %(as_of)s
WHERE b.status = 'OPEN'
  AND NOT EXISTS (SELECT 1 FROM stg_breaks s
                  WHERE s.trade_id = b.trade_id AND s.break_type = b.break_type);

DROP TABLE stg_breaks;
DROP TABLE stg_joined;
