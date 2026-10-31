-- TradeSync schema. Every statement is IF NOT EXISTS so the pipeline can apply it on every run.
-- Note: SQL files must not contain the percent character, because psycopg treats it as a parameter marker.

-- ---------------------------------------------------------------------------
-- RAW layer: the files exactly as received. Every column is text so a bad value
-- (e.g. a date like 2026-02-30) can still be loaded and then explained in rejected_records,
-- instead of crashing the load.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_internal (
    batch_id     text        NOT NULL,
    source_line  bigint      NOT NULL,   -- row number in the file, so a re-load maps to the same key
    trade_id     text,
    isin         text,
    side         text,
    quantity     text,
    price        text,
    currency     text,
    trade_date   text,
    counterparty text,
    loaded_at    timestamptz NOT NULL DEFAULT now(),
    -- Idempotency: loading the same file for the same batch twice hits this key and is skipped.
    PRIMARY KEY (batch_id, source_line)
);

CREATE TABLE IF NOT EXISTS raw_custodian (LIKE raw_internal INCLUDING ALL);

-- ---------------------------------------------------------------------------
-- CLEAN layer: typed, standardised, one row per (batch, trade).
-- NUMERIC (exact decimal) for money: float cannot store 0.1 exactly and would make
-- tolerance checks at the boundary unpredictable.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clean_internal (
    batch_id     text          NOT NULL,
    trade_id     text          NOT NULL,
    isin         text          NOT NULL CHECK (isin ~ '^[A-Z]{2}[A-Z0-9]{9}[0-9]$'),
    side         text          NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity     numeric(18,4) NOT NULL CHECK (quantity > 0),
    price        numeric(18,6) NOT NULL CHECK (price > 0),
    currency     text          NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    trade_date   date          NOT NULL,
    counterparty text          NOT NULL,
    source_line  bigint        NOT NULL,
    cleaned_at   timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (batch_id, trade_id)
);

CREATE TABLE IF NOT EXISTS clean_custodian (LIKE clean_internal INCLUDING ALL);

-- Rows that failed validation or were duplicates, with the reason. Nothing is silently dropped:
-- raw rows = clean rows + rejected rows is checked by data quality.
CREATE TABLE IF NOT EXISTS rejected_records (
    batch_id    text        NOT NULL,
    source      text        NOT NULL CHECK (source IN ('INTERNAL', 'CUSTODIAN')),
    source_line bigint      NOT NULL,
    trade_id    text,
    reason      text        NOT NULL,
    raw_row     jsonb       NOT NULL,   -- the original row, for investigation
    rejected_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (batch_id, source, source_line)
);

-- ---------------------------------------------------------------------------
-- RECON layer
-- ---------------------------------------------------------------------------
-- One row per trade per batch: did it match?
CREATE TABLE IF NOT EXISTS match_results (
    batch_id    text NOT NULL,
    trade_id    text NOT NULL,
    status      text NOT NULL CHECK (status IN ('MATCHED', 'BREAK')),
    break_count int  NOT NULL DEFAULT 0,
    as_of_date  date NOT NULL,
    PRIMARY KEY (batch_id, trade_id)
);

-- One row per (trade, break type), kept ACROSS batches so we know when a break was first seen
-- (for ageing) and when it was resolved.
CREATE TABLE IF NOT EXISTS breaks (
    break_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trade_id            text    NOT NULL,
    break_type          text    NOT NULL CHECK (break_type IN
                          ('PRICE_MISMATCH', 'QUANTITY_MISMATCH', 'MISSING_IN_CUSTODIAN', 'MISSING_IN_INTERNAL')),
    internal_value      numeric,
    custodian_value     numeric,
    difference          numeric,
    first_detected_date date    NOT NULL,
    last_seen_date      date    NOT NULL,
    last_batch_id       text    NOT NULL,
    status              text    NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'RESOLVED')),
    resolved_date       date,
    UNIQUE (trade_id, break_type)
);

-- ---------------------------------------------------------------------------
-- OPERATIONS: one row per pipeline run, plus its data-quality results.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id              text        NOT NULL,
    as_of_date            date        NOT NULL,
    status                text        NOT NULL CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED')),
    failed_stage          text,
    error_message         text,
    internal_file_sha256  text,
    custodian_file_sha256 text,
    rows_in_internal      int,
    rows_in_custodian     int,
    rows_clean_internal   int,
    rows_clean_custodian  int,
    rows_rejected         int,
    matched               int,
    broken                int,
    stage_timings_ms      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    started_at            timestamptz NOT NULL DEFAULT now(),
    finished_at           timestamptz
);

CREATE TABLE IF NOT EXISTS dq_results (
    run_id     bigint      NOT NULL REFERENCES pipeline_runs (run_id),
    check_name text        NOT NULL,
    passed     boolean     NOT NULL,
    observed   text,
    expected   text,
    checked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, check_name)
);
