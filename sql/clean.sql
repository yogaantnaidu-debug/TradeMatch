-- CLEAN stage for one source. Run twice per batch: once for INTERNAL, once for CUSTODIAN.
-- Tokens __RAW_TABLE__, __CLEAN_TABLE__, __SOURCE__ are replaced by Python with whitelisted names.
-- %(batch_id)s is a real bind parameter.

-- Step 1: standardise every raw row (trim, uppercase, '' -> NULL) and decide if it is valid.
-- We validate with regexes BEFORE casting, because a failed cast such as '2026-02-30'::date
-- would abort the whole transaction instead of rejecting one row.
CREATE TEMP TABLE stg_validated ON COMMIT DROP AS
WITH std AS (
    SELECT
        r.source_line,
        to_jsonb(r)                              AS raw_row,
        nullif(upper(trim(r.trade_id)), '')      AS trade_id,
        nullif(upper(trim(r.isin)), '')          AS isin,
        nullif(upper(trim(r.side)), '')          AS side,
        nullif(trim(r.quantity), '')             AS quantity,
        nullif(trim(r.price), '')                AS price,
        nullif(upper(trim(r.currency)), '')      AS currency,
        nullif(trim(r.trade_date), '')           AS trade_date,
        nullif(upper(trim(r.counterparty)), '')  AS counterparty
    FROM __RAW_TABLE__ r
    WHERE r.batch_id = %(batch_id)s
)
SELECT
    std.*,
    -- CASE stops at the first true branch, so each row gets exactly one (the first) reason,
    -- and the ::numeric casts only run on strings the regex already proved are numbers.
    CASE
        WHEN trade_id IS NULL                                         THEN 'NULL_TRADE_ID'
        WHEN isin IS NULL                                             THEN 'NULL_ISIN'
        WHEN isin !~ '^[A-Z]{2}[A-Z0-9]{9}[0-9]$'                     THEN 'INVALID_ISIN'
        WHEN side IS NULL OR side NOT IN ('BUY', 'SELL')              THEN 'INVALID_SIDE'
        WHEN quantity IS NULL
          OR quantity !~ '^-?[0-9]{1,14}([.][0-9]{1,4})?$'            THEN 'INVALID_QUANTITY'
        WHEN quantity::numeric <= 0                                   THEN 'NON_POSITIVE_QUANTITY'
        WHEN price IS NULL
          OR price !~ '^-?[0-9]{1,12}([.][0-9]{1,6})?$'               THEN 'INVALID_PRICE'
        WHEN price::numeric <= 0                                      THEN 'NON_POSITIVE_PRICE'
        WHEN currency IS NULL OR currency !~ '^[A-Z]{3}$'             THEN 'INVALID_CURRENCY'
        -- Regex forces ISO format (so 31/10/2026 is rejected, not guessed),
        -- pg_input_is_valid (new in Postgres 16) rejects impossible dates like 2026-02-30 without an error.
        WHEN trade_date IS NULL
          OR trade_date !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
          OR NOT pg_input_is_valid(trade_date, 'date')                THEN 'INVALID_TRADE_DATE'
        WHEN counterparty IS NULL                                     THEN 'NULL_COUNTERPARTY'
    END AS reject_reason
FROM std;

-- Step 2: among VALID rows, number the copies of each trade_id. rn = 1 is the first one in the file.
CREATE TEMP TABLE stg_ranked ON COMMIT DROP AS
SELECT
    v.*,
    ROW_NUMBER() OVER (PARTITION BY v.trade_id ORDER BY v.source_line) AS rn
FROM stg_validated v
WHERE v.reject_reason IS NULL;

-- Step 3: keep one typed row per trade. ON CONFLICT makes a re-run of the same batch a no-op.
INSERT INTO __CLEAN_TABLE__
    (batch_id, trade_id, isin, side, quantity, price, currency, trade_date, counterparty, source_line)
SELECT
    %(batch_id)s, trade_id, isin, side,
    quantity::numeric, price::numeric, currency, trade_date::date, counterparty, source_line
FROM stg_ranked
WHERE rn = 1
ON CONFLICT (batch_id, trade_id) DO NOTHING;

-- Step 4: everything else goes to rejected_records with a reason (invalid rows + extra duplicate copies).
INSERT INTO rejected_records (batch_id, source, source_line, trade_id, reason, raw_row)
SELECT %(batch_id)s, '__SOURCE__', source_line, trade_id, reject_reason, raw_row
FROM stg_validated
WHERE reject_reason IS NOT NULL
UNION ALL
SELECT %(batch_id)s, '__SOURCE__', source_line, trade_id, 'DUPLICATE_TRADE_ID', raw_row
FROM stg_ranked
WHERE rn > 1
ON CONFLICT (batch_id, source, source_line) DO NOTHING;

-- Drop now so the second call (other source) in the same transaction can recreate them.
DROP TABLE stg_ranked;
DROP TABLE stg_validated;
