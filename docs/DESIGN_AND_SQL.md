# TradeSync: design and SQL explained

## 1. Flow in one paragraph
For one **batch** (one business day's pair of files), `pipeline.py` runs four stages, each in its
own database transaction: **load_raw** (COPY both files as text), **clean** (validate, type,
dedupe, reject with a reason), **recon** (FULL OUTER JOIN, detect breaks, upsert results,
resolve fixed breaks), **data_quality** (store checks). Then a **DQ gate** fails the run if any
check failed. Every run is logged in `pipeline_runs`.

## 2. Tables and why each exists

| Table | Grain (one row per…) | Why it exists |
|---|---|---|
| `raw_internal`, `raw_custodian` | file line per batch | Exact copy of what arrived, all `text`, so a bad value can still be stored and explained instead of crashing the load. PK `(batch_id, source_line)` makes re-loads idempotent. Also an audit trail: "what did the custodian actually send?" |
| `clean_internal`, `clean_custodian` | trade per batch | Typed (`numeric`, `date`), standardised (upper/trim) and de-duplicated. `CHECK` constraints are a second safety net behind the SQL validation. PK `(batch_id, trade_id)`. |
| `rejected_records` | rejected raw line | Nothing is silently dropped. Each row has a `reason` and the original row as `jsonb`. Duplicates land here too (`DUPLICATE_TRADE_ID`), so `raw = clean + rejected` holds exactly. |
| `match_results` | trade per batch | The verdict (MATCHED / BREAK) plus which break types. Kept per batch so history never changes. |
| `breaks` | (trade, break type), **across batches** | The operations worklist. Keeps `first_detected_date` (for ageing), `last_seen_date`, `status` OPEN/RESOLVED. `UNIQUE (trade_id, break_type)` makes it upsertable. |
| `pipeline_runs` | pipeline run | Status, failed stage, error, row counts per layer, timings per stage, file SHA-256. The ops log. |
| `dq_results` | check per run | Data-quality results are stored, so you can query "when did this check first fail?". |
| `break_ageing` (view) | open break | Age in days since first detection and a bucket 0-1 / 2-5 / 6+. |
| `recon_summary` (view) | batch | Totals, match rate and counts per break type. |

Assumptions I made (the brief left them open):
- **A batch is a full snapshot.** Each day's files contain every trade still under reconciliation. That is why a break missing from today's batch counts as RESOLVED.
- **Batches run in date order.** Re-running an *older* batch after a newer one would re-open breaks. Not guarded.
- **Default batch id = as-of date**, default as-of = 2026-10-30, the generator's base date. This keeps runs reproducible.
- **Duplicates keep the first copy in the file.** The others are rejected even if their values differ.
- **Tolerance is relative:** break if `|internal - custodian| > tolerance x internal_price`, with tolerance `0.0001` = 1 basis point. Exactly at the tolerance is a match. Quantity has no tolerance.
- **ISIN check** is shape only (2 letters, 9 alphanumerics, 1 digit). The pipeline does not verify the check digit. (With `--prices`, the generator builds ISINs from tickers with a correct Luhn check digit; they are still synthetic, because real ISINs need licensed CUSIP data.)
- **Only price and quantity are compared.** A mismatched side, currency or ISIN is not one of the 4 break types.

## 3. The main SQL, line by line

### 3.1 Raw load (`tradesync/load_raw.py`)
```sql
CREATE TEMP TABLE stg_raw (source_line bigint GENERATED ALWAYS AS IDENTITY, trade_id text, ...) ON COMMIT DROP;
COPY stg_raw (trade_id, ...) FROM STDIN WITH (FORMAT csv, HEADER MATCH);
INSERT INTO raw_internal (batch_id, source_line, ...) SELECT %s, source_line, ... FROM stg_raw
ON CONFLICT (batch_id, source_line) DO NOTHING;
```
- **Temp table** lives only in this session and is dropped at commit, so nothing leaks.
- **`GENERATED ALWAYS AS IDENTITY`** numbers rows 1, 2, 3… in the order COPY reads them. That gives a line number, and the same file always gives the same numbers.
- **`COPY … FROM STDIN`** streams the file in one command. That is far faster than one INSERT per row.
- **`HEADER MATCH`** (Postgres 15+) makes the load fail if the header names are not exactly our columns. A file with swapped columns is refused instead of loaded wrong.
- **`ON CONFLICT … DO NOTHING`**: COPY itself cannot skip existing rows, so we COPY into staging and then insert, skipping keys that already exist. Loading the same file twice adds 0 rows.

### 3.2 Clean (`sql/clean.sql`), run once per source
**Step 1: standardise and validate**
```sql
CREATE TEMP TABLE stg_validated ON COMMIT DROP AS
WITH std AS (
    SELECT r.source_line, to_jsonb(r) AS raw_row,
           nullif(upper(trim(r.trade_id)), '') AS trade_id, ...
    FROM raw_internal r WHERE r.batch_id = %(batch_id)s)
SELECT std.*, CASE WHEN trade_id IS NULL THEN 'NULL_TRADE_ID' ... END AS reject_reason FROM std;
```
- `trim` removes stray spaces, `upper` fixes `us037…` to `US037…`, and `nullif(x,'')` turns empty strings into NULL so there is one way to say "missing".
- `to_jsonb(r)` keeps the whole original row, so rejected rows can be investigated.
- The `CASE` gives each row the **first** failing reason, in order. It checks the text with regexes (`quantity ~ '^-?[0-9]{1,14}([.][0-9]{1,4})?$'`) **before** any cast. A failed cast like `'abc'::numeric` raises an error, and that would abort the whole transaction instead of rejecting one row.
- The digit limits in the regex match the column types (`numeric(18,4)` / `numeric(18,6)`), so a cast can never overflow.
- `'NaN'` is a valid Postgres numeric, and the regex rejects it on purpose.
- Dates: the regex forces `YYYY-MM-DD`, so `31/10/2026` is rejected rather than guessed. `pg_input_is_valid(trade_date,'date')` (new in PG 16) returns false for `2026-02-30` instead of raising an error.

**Step 2: rank duplicates**
```sql
SELECT v.*, ROW_NUMBER() OVER (PARTITION BY v.trade_id ORDER BY v.source_line) AS rn
FROM stg_validated v WHERE v.reject_reason IS NULL;
```
- `PARTITION BY trade_id` puts all copies of one trade into a group. `ORDER BY source_line` numbers them in file order. `rn = 1` is the first copy.
- Only **valid** rows are ranked. A corrupted copy cannot "win" over a good one.
- Why `ROW_NUMBER` and not `DISTINCT`: `DISTINCT` only removes rows that are identical in every column. We need one row per `trade_id`, plus the list of the extra copies, so they can be reported.

**Steps 3 and 4: write**
```sql
INSERT INTO clean_internal (...) SELECT %(batch_id)s, trade_id, ..., quantity::numeric, price::numeric, ..., trade_date::date ...
FROM stg_ranked WHERE rn = 1 ON CONFLICT (batch_id, trade_id) DO NOTHING;

INSERT INTO rejected_records (...)
SELECT ..., reject_reason, raw_row FROM stg_validated WHERE reject_reason IS NOT NULL
UNION ALL
SELECT ..., 'DUPLICATE_TRADE_ID', raw_row FROM stg_ranked WHERE rn > 1
ON CONFLICT (batch_id, source, source_line) DO NOTHING;
```
- The casts are now safe because step 1 already proved every value is valid.
- `UNION ALL` (not `UNION`) because the two sets never overlap, so there is no need to pay for duplicate removal.

### 3.3 Recon (`sql/recon.sql`)
**Step 1: join**
```sql
SELECT coalesce(i.trade_id, c.trade_id) AS trade_id,
       i.trade_id IS NOT NULL AS in_internal, c.trade_id IS NOT NULL AS in_custodian,
       i.price AS internal_price, c.price AS custodian_price, i.quantity ..., c.quantity ...
FROM (SELECT * FROM clean_internal  WHERE batch_id = %(batch_id)s) i
FULL OUTER JOIN (SELECT * FROM clean_custodian WHERE batch_id = %(batch_id)s) c
  ON c.trade_id = i.trade_id;
```
- **FULL OUTER JOIN** returns three kinds of rows: on both sides, only internal (custodian columns NULL), and only custodian (internal columns NULL). An INNER JOIN would drop the last two kinds, which are exactly the "missing" breaks.
- The batch filter sits **inside** the subqueries. Writing `WHERE i.batch_id = … AND c.batch_id = …` after the join would remove every row where one side is NULL, which quietly turns the query back into an inner join (a classic bug).
- `coalesce` picks whichever side's id is not NULL.

**Step 2: detect breaks.** Four `SELECT`s are glued with `UNION ALL`, one per break type:
- missing: `in_internal AND NOT in_custodian`, and the reverse
- price: `abs(internal_price - custodian_price) > %(tolerance)s * internal_price`, written as a multiplication instead of a division so there is no divide-by-zero risk and the comparison stays exact in NUMERIC
- quantity: `internal_qty <> custodian_qty`

A trade can produce two rows (price **and** quantity).

**Step 3: verdict per trade**
```sql
INSERT INTO match_results (...)
SELECT %(batch_id)s, j.trade_id,
       CASE WHEN count(b.break_type) = 0 THEN 'MATCHED' ELSE 'BREAK' END,
       count(b.break_type),
       coalesce(array_agg(b.break_type ORDER BY b.break_type) FILTER (WHERE b.break_type IS NOT NULL), '{}'),
       %(as_of)s
FROM stg_joined j LEFT JOIN stg_breaks b ON b.trade_id = j.trade_id
GROUP BY j.trade_id
ON CONFLICT (batch_id, trade_id) DO UPDATE SET status = EXCLUDED.status, ...;
```
- The `LEFT JOIN` keeps trades with no breaks. `count(b.break_type)` counts only non-NULL values, so it is 0 for those trades.
- `FILTER (WHERE …)` stops a NULL getting into the array.
- `ON CONFLICT DO UPDATE` (upsert) makes a re-run overwrite the same row instead of adding a new one.

**Step 4: upsert breaks across batches**
```sql
INSERT INTO breaks (..., first_detected_date, last_seen_date, last_batch_id, status) SELECT ..., %(as_of)s, %(as_of)s, %(batch_id)s, 'OPEN' FROM stg_breaks
ON CONFLICT (trade_id, break_type) DO UPDATE SET internal_value = EXCLUDED.internal_value, ...,
    last_seen_date = greatest(breaks.last_seen_date, EXCLUDED.last_seen_date), last_batch_id = EXCLUDED.last_batch_id,
    status = 'OPEN', resolved_date = NULL;
```
`first_detected_date` is deliberately **not** in the `SET` list. A break seen again keeps its original date, and that is what makes ageing work.

**Step 5: resolve**
```sql
UPDATE breaks b SET status = 'RESOLVED', resolved_date = %(as_of)s
WHERE b.status = 'OPEN' AND NOT EXISTS (SELECT 1 FROM stg_breaks s WHERE s.trade_id = b.trade_id AND s.break_type = b.break_type);
```
An open break that today's batch no longer produces has been fixed, for example by a late custodian record. `NOT EXISTS` is used rather than `NOT IN` because `NOT IN` returns nothing at all if the subquery contains a NULL.

### 3.4 Data quality (`sql/dq.sql`)
One `INSERT … SELECT` over a `UNION ALL` of checks. Each check yields `observed` and `expected`, and `passed = observed = expected`:
- `internal_rows_reconcile` / `custodian_rows_reconcile`: clean + rejected = raw (nothing lost)
- `every_trade_reconciled`: match_results rows = distinct trade_ids across both clean tables
- `no_null_keys`, `price_positive`, `valid_side`: should be 0
- `breaks_consistent`: trades marked BREAK = distinct trades with an OPEN break from this batch

The DQ stage commits its results first. Then `dq_gate()` raises if any check failed, so the run is marked FAILED **and** the evidence is kept.

### 3.5 Views (`sql/views.sql`)
`break_ageing` computes `ref.as_of - first_detected_date`, where `ref.as_of` is the latest **successful business date**, not `now()`. Re-running an old day therefore gives the same answer. `recon_summary` uses `count(*) FILTER (WHERE …)` to count several conditions in one pass, and `'X' = ANY(break_types)` to test array membership.

## 4. Design decisions

**Transactions per stage.** *Requirement:* a failure must not leave half-written data. *Options:* one transaction for the whole run, one per stage, or none. *Chosen:* one per stage, plus a separately committed `pipeline_runs` row. *Why:* a crash in recon rolls back only recon (tested: the half-inserted row disappears), and the run row survives to record the failure. Because every stage is idempotent, recovery is just "run it again". *Trade-off:* after a failure, earlier layers are committed while later ones are not. Consumers must check `pipeline_runs.status`.

**PostgreSQL vs Snowflake.** *Requirement:* run locally, free, in CI, and support ACID upserts. *Options:* PostgreSQL, Snowflake, DuckDB. *Chosen:* PostgreSQL. *Why:* real transactions, `ON CONFLICT`, constraints, `EXPLAIN ANALYZE`, and it runs in Docker and GitHub Actions for free. *Trade-off:* it is a single-node row store. For billions of rows and heavy analytics, Snowflake (columnar, separate compute and storage) would scale better. There, `ON CONFLICT` becomes `MERGE`, and Snowflake does not enforce PK/unique constraints, so dedup must be explicit.

**SQL vs pandas vs Spark.** *Requirement:* set-based joins and validation on 100k+ rows. *Chosen:* SQL in the database. *Why:* the data never leaves Postgres, the joins are optimised by the planner, the logic is readable by analysts, and it runs inside transactions. pandas would pull everything into Python memory and has no ACID guarantees. Spark is built for clusters and terabytes, which is overkill at 100k rows. *Trade-off:* SQL is harder to unit-test in isolation, so every test runs against real Postgres.

**NUMERIC vs float.** *Requirement:* exact money and a deterministic tolerance boundary. *Chosen:* `numeric(18,4)` for quantity, `numeric(18,6)` for price, and `Decimal` in Python (the tolerance is read from TOML as a string). *Why:* binary floats cannot represent 0.1 exactly, so `100.01 - 100.00` is not exactly `0.01` and an "exactly at tolerance" trade could flip between match and break. The boundary test proves the behaviour. *Trade-off:* numeric arithmetic is slower than float. That does not matter at this size.

**Layered tables (raw, clean, recon).** *Why:* raw is the untouched evidence, so you can replay it. Clean is the trusted typed data. Recon holds the business result. Each layer can be checked against the one before (the row-count DQ checks). *Trade-off:* more storage and more SQL.

**COPY vs INSERT.** COPY streams the whole file in one command. Row-by-row INSERTs pay a network round trip and a statement parse for each row. Measured here: 2 x 99,990 rows loaded in 1.1 s. INSERT was not benchmarked, so no comparison number is claimed. *Trade-off:* COPY cannot do `ON CONFLICT`, hence the staging table.

**Batch files vs Kafka.** *Requirement:* custodians send daily files. *Chosen:* batch. *Why:* that matches how the data arrives, and it is simple, replayable and easy to test. *Trade-off:* breaks are found once a day, not in seconds. Kafka would matter for intraday or real-time matching (see section 6).

**Idempotency approach.** Three parts: (1) natural keys plus unique constraints at every layer (`(batch_id, source_line)`, `(batch_id, trade_id)`, `(trade_id, break_type)`); (2) `ON CONFLICT DO NOTHING / DO UPDATE` on every write; (3) the SHA-256 of both files is stored per run, and the same `batch_id` with **different** content is refused instead of being silently skipped. The alternative, "delete the batch and reload", is simpler but loses the break history and leaves a window where data is missing.

## 5. Honest limitations
- Verified environments: Docker Desktop (`postgres:16`, PG 16.15), native PostgreSQL 16.2 on Windows, and GitHub Actions (`postgres:16`). The Makefile has not been run (there is no `make` on the Windows build machine). CI covers the same commands.
- The precision, recall and F1 scores of 1.0 are against seeded breaks whose rules match the pipeline's rules. `--prices` makes tickers, dates and prices real, but the breaks and dirty rows are still generated. No public dataset of internal vs custodian trades exists, and real data has no ground truth to score against.
- The clean stage is the slowest (about 4.5 s for 2 x 99,990 rows). Likely causes are `to_jsonb` on every row (it is only needed for rejects), regex checks and temp tables. This has not been profiled.
- Breaks resolve by absence, which assumes full-snapshot batches processed in order. A partial file would wrongly resolve breaks.
- No ISIN check digit, no FX conversion, and no matching on side, currency or settlement date.
- No partitioning, so tables grow forever. No retention policy.
- No scheduler, no alerting, and no user authentication or roles beyond the DB login.
- Coverage is 86%. The CLI `main()` functions and printing are not covered by tests.
- The demo dashboard (`dashboard.py`) is local-only and has no login. It opens a new DB connection per page view, which is fine for a demo but would need pooling under real traffic.

## 6. What I would build next
- **Airflow** (or Dagster): run daily, with one task per stage (the stages are already separate idempotent functions), retries, SLAs and alerts on FAILED runs or failed DQ checks.
- **Kafka**: stream intraday trade events from the booking system, so the internal side is near real time. The consumer would write to `raw_internal` with an idempotency key (`trade_id` + version) so replays are harmless.
- **Spark**: only when volume reaches hundreds of millions of rows. The same FULL OUTER JOIN becomes a DataFrame `join(how="full_outer")` on data partitioned by `trade_date`.
- **Snowflake**: the warehouse for history and analytics. Load files with `COPY INTO` from S3 and turn the upserts into `MERGE`. Dedup still with `ROW_NUMBER()` / `QUALIFY`.
- Smaller next steps: partition by `as_of_date`, build `raw_row` JSON only for rejects, add more break types (side, currency, settlement date), and add a break-assignment workflow (owner, comments).
