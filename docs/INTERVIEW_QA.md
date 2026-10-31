# TradeSync: 40 interview questions, answered from this code

Answers point to real files. **Trap** = a likely follow-up and the honest answer.

## A. Project walkthrough
**1. Explain the project in 60 seconds.**
Two files describe the same trades: our internal book and the custodian's. I load both into
PostgreSQL with COPY as raw text, clean and validate them in SQL (bad rows go to
`rejected_records` with a reason), match them with a FULL OUTER JOIN, and record four break types
with their age. Every stage is a transaction, re-runs are idempotent, data-quality checks are stored,
and each run is logged. On 100k trades the whole pipeline took 6.75 s, and it found all 5,500
seeded breaks with no false positives.

**2. Why does reconciliation matter at a firm like Apollo?**
If our book and the custodian's disagree, our positions, P&L, NAV and risk numbers may be wrong, and
settlement can fail. Breaks have to be found, owned and closed quickly, which is why ageing is tracked.

**3. Walk me through one trade end to end.**
`T00000042` arrives in both CSVs → COPY into `raw_*` (text, with `source_line`) → `clean.sql` trims, uppercases and validates
it, and keeps it if it is the first valid copy → `recon.sql` joins it on `trade_id` → if the prices differ by more than
1bp of the internal price, a `PRICE_MISMATCH` row goes into `breaks` with `first_detected_date` = the as-of date,
and `match_results` says BREAK → `dq.sql` confirms the counts → `break_ageing` shows its bucket.

**4. What was the hardest part?**
Keeping ground truth exact while adding dirty rows. If I had corrupted a real trade's only row, the trade
would show up as "missing" and my evaluation would be wrong. So corrupted rows are extra **copies** of
real rows (`generate_data.py: build_file`). The clean layer must reject the copy and keep the original.

**5. How do you know it works?**
Three ways. 33 pytest tests against real Postgres. Stored DQ checks on every run. `evaluate.py`
compares against `ground_truth.csv` and gives 1.0 precision and recall per type.
*Trap: "1.0 is suspicious."* Yes. The generator and the rules were written together, so 1.0 is
expected. It works as a regression test, not as proof the rules suit real custodian data. The
2,000 near-miss prices inside tolerance at least prove it is not too strict.

## B. SQL
**6. Why FULL OUTER JOIN?**
It keeps rows from both sides even when there is no match, which is how `MISSING_IN_CUSTODIAN` / `MISSING_IN_INTERNAL`
show up (the other side's columns are NULL). An INNER JOIN would hide exactly the breaks we care about most.

**7. Where did you put the batch filter, and why does it matter?**
Inside subqueries before the join (`recon.sql` step 1). Putting `i.batch_id = … AND c.batch_id = …` in
the WHERE clause after the join throws away the NULL side and turns it into an inner join.

**8. Explain the ROW_NUMBER dedup.**
`ROW_NUMBER() OVER (PARTITION BY trade_id ORDER BY source_line)` numbers each trade's copies in
file order. `rn = 1` goes to clean, and `rn > 1` goes to rejected as `DUPLICATE_TRADE_ID`. *Trap: "why not
DISTINCT?"* DISTINCT only removes rows identical in every column, and it cannot tell you which rows were dropped.

**9. ROW_NUMBER vs RANK vs DENSE_RANK?**
On ties, ROW_NUMBER still gives 1, 2, 3, while RANK gives 1, 1, 3 and DENSE_RANK gives 1, 1, 2. For dedup you need exactly one
winner, so ROW_NUMBER, with a unique ORDER BY (`source_line`) so the winner is deterministic.

**10. Why `NOT EXISTS` and not `NOT IN` when resolving breaks?**
If the subquery returns any NULL, `x NOT IN (…)` is never true, so nothing would resolve.
`NOT EXISTS` has no such trap, and the planner turns it into an anti-join.

**11. What does `ON CONFLICT` do?**
If an insert would violate a unique constraint, it either skips the row (`DO NOTHING`, used for raw and clean)
or updates the existing row (`DO UPDATE`, used for match_results and breaks). `EXCLUDED` is the row we tried to insert.

**12. Why is `first_detected_date` missing from the upsert's SET list?**
Deliberately. A break seen again on day 4 must keep its day-1 date, otherwise ageing would reset to 0 every day.
`test_ageing_buckets_and_late_record_resolution` checks it.

**13. What does `count(*) FILTER (WHERE …)` do?**
It counts only matching rows. `recon_summary` gets matched, broken and per-type counts in one scan instead of several queries.

**14. Which indexes did you add and why?**
`sql/indexes.sql`: `trade_id` on both clean tables (the PK is `(batch_id, trade_id)`, which can't serve a
trade_id-only lookup because trade_id is the second column), `(batch_id, status)` on match_results, and `status` on breaks.

**15. What did EXPLAIN ANALYZE show?**
Measured: trade_id lookup went from 3.314 ms (Seq Scan) to 0.015 ms (Index Scan). Batch breaks went from 4.409 to 0.696 ms (Bitmap Index Scan).
The breaks-status query and the big join did **not** improve. *Trap: "then why keep the status index?"*
After one batch all 5,500 breaks are OPEN, so the planner rightly reads the whole tiny table. Once
resolved breaks pile up over months, OPEN becomes a small fraction and the index starts to pay off. I'd re-measure then rather than assume.

**16. Why didn't the index speed up the join?**
The join reads every row of both sides. A hash join (build a hash table of one side, probe it with the other) does that in
one pass. Index lookups would mean 99,000 random reads. Indexes help when you need **few** rows.

**17. EXPLAIN vs EXPLAIN ANALYZE?**
EXPLAIN shows the planner's estimated plan. ANALYZE actually runs the query and adds real times and
row counts. I also used BUFFERS (pages read). Compare estimated rows with actual rows: a big gap means stale statistics.
That is why `benchmark_explain.py` runs `ANALYZE` first.

## C. Data quality
**18. What DQ checks do you run?**
`dq.sql`: raw = clean + rejected per side, every distinct trade has a verdict, no null keys, price > 0,
valid side, and match_results agrees with breaks. Each check is stored in `dq_results` with observed and expected values.

**19. Aren't some checks redundant with the CHECK constraints?**
Partly, on purpose. Constraints stop bad rows from being inserted at all. DQ checks prove after the fact that
the layers reconcile (nothing lost between raw and clean), which a constraint cannot express.

**20. What happens when a DQ check fails?**
The DQ stage commits its results, then `dq_gate()` raises, and the run becomes `FAILED` with `failed_stage = dq_gate`. That
is tested in `test_failed_dq_check_fails_run_but_keeps_results`. *Trap: "but the recon data is already committed?"* Yes.
Consumers must check the run status. In production I'd publish to the reporting views only after a SUCCESS run.

**21. How do you validate without crashing on bad values?**
Regex first, then cast (`clean.sql`). `'2026-02-30'::date` throws an error, and that would abort the transaction.
`pg_input_is_valid()` (PG 16) tests a value without throwing. `CASE` evaluates branches in order, so the cast
runs only after the regex has passed.

**22. Why reject `NaN`?**
Postgres NUMERIC accepts `'NaN'`, and `NaN > 0` is true in Postgres, so it would slip past `price > 0`. The regex rejects it. There is a test for it.

## D. Idempotency and ACID
**23. What does idempotent mean here?**
Running the same batch N times gives the same database as running it once. It is tested by comparing the row counts of
7 tables after 2 runs. On the 100k data, the re-run left every table's count unchanged.

**24. How exactly is it achieved?**
Unique keys at every layer plus `ON CONFLICT`. COPY can't do `ON CONFLICT`, so it goes into a temp
staging table first. `source_line` is deterministic, so the same file gives the same keys.

**25. Same batch_id, different file content?**
Refused. The SHA-256 of both files is stored in `pipeline_runs`. If a previous SUCCESS run of this batch had different
hashes, the run fails at `check_batch`. Otherwise `DO NOTHING` would silently ignore a corrected file.

**26. What does ACID give you in this pipeline?**
Atomicity: a stage commits completely or not at all. Consistency: constraints hold. Isolation: a reader never sees
a half-built clean table. Durability: once committed, it stays committed after a crash.

**27. Why one transaction per stage instead of one for the whole run?**
The run log must survive a failure (it's committed separately), and a failure late in the run shouldn't throw away
earlier good work. Since stages are idempotent, re-running continues safely. *Trap:* the downside is partially
updated layers after a failure, which is why `pipeline_runs.status` is the source of truth.

## E. Failure scenarios
**28. Corrupt file?**
Wrong header: `COPY … HEADER MATCH` fails, the whole load stage rolls back (both files, since they share one transaction),
and the run is logged as FAILED at `load_raw` (tested). Bad values inside a valid file: they load as text and
then go to `rejected_records` with a reason.

**29. Crash midway through recon?**
Postgres rolls back the open transaction, so the partial `match_results` rows vanish. Tested by monkeypatching a recon
stage that inserts a row and then raises. Clean data from the earlier stage stays, and a re-run finishes cleanly.
*Trap: "what if the Python process is killed, so no FAILED status is written?"* Then the run row stays `RUNNING`. That is a
known gap. Next step: a stale-RUNNING sweeper or a scheduler that marks timed-out runs.

**30. Same file loaded twice?**
0 new rows, by design. See question 23.

**31. Late record: the custodian sends the trade a day later?**
Day 1: `MISSING_IN_CUSTODIAN`, OPEN. Next batch: the trade now matches, `recon.sql` step 5 sees the break is no longer
produced and marks it RESOLVED with `resolved_date`. Tested. *Trap:* this assumes each batch is a full snapshot.
With incremental (delta) files, a break would need explicit matching events to resolve.

**32. What if the database is down?**
`psycopg.connect` fails before any stage, so no partial data is written. In Docker, `run.ps1` / `make` wait on the healthcheck first.
Retries with backoff would come from a scheduler such as Airflow, which is not built here.

## F. Scaling
**33. 100x the volume (10M trades/day)?**
First stay in Postgres: partition tables by `as_of_date`, build `to_jsonb` only for rejected rows (clean is the slowest
stage at about 4.5 s for 100k), and possibly parallel COPY. Beyond one machine, move to Spark (a distributed full outer join on
`trade_id`, partitioned by trade date) or to Snowflake (`COPY INTO`, then `MERGE`).

**34. How would Kafka fit?**
For intraday matching: booking events go to a Kafka topic, and a consumer writes to `raw_internal` keyed by `trade_id + version`, so
duplicates and replays are harmless (at-least-once delivery plus idempotent writes ≈ effectively once). The custodian side often stays a daily file.
I have not built this.

**35. Moving to Snowflake: what changes?**
`ON CONFLICT` becomes `MERGE`. Snowflake doesn't enforce PK or UNIQUE constraints, so dedup must be explicit (`ROW_NUMBER` / `QUALIFY`).
`pg_input_is_valid` becomes `TRY_CAST` / `TRY_TO_DATE`. COPY becomes `COPY INTO` from a stage (S3). Indexes don't exist, so you rely on clustering and pruning.

**36. Cloud deployment?**
Postgres as managed RDS or Aurora, files landing in S3, the pipeline as a container on ECS or Batch triggered by Airflow (MWAA),
secrets in Secrets Manager instead of `.env`. None of this is built.

## G. Testing, Docker, CI/CD, Python
**37. How are the tests structured?**
`tests/conftest.py` creates a fresh Postgres **schema** per test (via `search_path`) and drops it afterwards, so tests are isolated
and don't touch real data. The suite covers tolerance boundaries (5 cases), each break type, 14 invalid-row cases (11 distinct reasons), dedup,
idempotency, changed content, corrupt file, crash midway, the DQ gate, ageing and late-record resolution, and a 5k end-to-end run.
33 tests, 85% coverage. *Trap: "why not mock the database?"* The logic lives in SQL, so a mock would test nothing.

**38. Why test exactly at the tolerance?**
Off-by-one bugs live at boundaries (`>` vs `>=`). With 100.00 and 1bp: 100.010000 matches, 100.010001 breaks, and the same holds on the
downside. That only works reliably because NUMERIC is exact.

**39. What do Docker and CI do here?**
`docker-compose.yml` runs Postgres 16 with a healthcheck and a named volume, with credentials from `.env`. The GitHub Actions workflow
starts a Postgres 16 service container, installs the requirements, runs pytest with coverage, then runs a 10k end-to-end smoke test.
The CI workflow passes on GitHub (33 tests, 85% coverage, 10k smoke run with F1 1.0). *Honest:* I couldn't run `docker compose` on my build machine, so locally the tests ran against native PG 16.2.

**40. Python choices worth defending?**
Python only orchestrates, and SQL does the work. `psycopg` 3 with `autocommit=True`, so each `with conn.transaction()` is a
real BEGIN/COMMIT (without it, psycopg opens an implicit transaction and nested blocks become savepoints). Bind parameters
for values. Table names can't be bind parameters, so they come from hard-coded whitelists (no SQL injection).
`Decimal` everywhere for money, `tomllib` from the standard library, and a dataclass for config.

## What is implemented vs NOT implemented
Use this so your resume only claims what exists.

**Implemented and tested**
- Seeded generator (100k default, fixed seed, 4 break types at known rates, near-misses, dirty, duplicate and corrupt rows, ground_truth.csv)
- COPY bulk load via temp staging and `ON CONFLICT`, with `HEADER MATCH`
- Raw / clean / rejected / recon layers in SQL. NUMERIC money. ROW_NUMBER dedup. FULL OUTER JOIN recon
- 4 break types with a configurable relative price tolerance
- Breaks across batches: first_detected_date, OPEN/RESOLVED, ageing view (0-1, 2-5, 6+), summary view
- Transaction per stage, failures recorded in pipeline_runs, file-hash guard
- 7 stored data-quality checks and a DQ gate
- Indexes, plus measured EXPLAIN ANALYZE before/after on 100k rows
- evaluate.py with precision, recall and F1 per type and overall
- 33 pytest tests on real Postgres, 85% coverage

**Written but NOT verified on the build machine**
- Docker Compose setup, the Docker step of `run.ps1`, the Makefile (no `make` on Windows)
- (The GitHub Actions workflow **is** verified: it passed on its first run on GitHub)
- (`.\run.ps1 -NoDocker` against an already-running Postgres 16.2 **was** run end to end and exited 0)

**NOT implemented (don't claim)**
- Kafka, Spark, Snowflake, Airflow, any cloud deployment
- An API or UI, alerting, user roles
- Real custodian formats (SWIFT MT5xx, FIX), FX, ISIN check digit, matching on side, currency or settlement date
- Partitioning, retention, handling of crashed RUNNING runs, performance tuning of the clean stage
- Any test on real market data. The precision and recall figures are on synthetic data only
