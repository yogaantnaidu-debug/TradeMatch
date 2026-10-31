"""Idempotency, transactions and failure handling."""
import pytest

import tradesync.pipeline as pipeline
from conftest import query, trade, write_trades

TABLES = ["raw_internal", "raw_custodian", "clean_internal", "clean_custodian",
          "rejected_records", "match_results", "breaks"]


def counts(conn):
    return {t: query(conn, f"SELECT count(*) FROM {t}")[0][0] for t in TABLES}


def test_same_batch_twice_creates_no_duplicates(conn, run):
    internal = [trade("T1"), trade("T2", price="100"), trade("T2"), trade("T3", side="X")]
    custodian = [trade("T1"), trade("T2", price="120"), trade("T4")]
    run(internal, custodian)
    first = counts(conn)
    run(internal, custodian)
    assert counts(conn) == first
    assert query(conn, "SELECT status FROM pipeline_runs ORDER BY run_id") == [("SUCCESS",), ("SUCCESS",)]


def test_same_batch_id_with_different_content_is_refused(conn, run):
    run([trade("T1")], [trade("T1")])
    with pytest.raises(pipeline.PipelineError, match="different files"):
        run([trade("T1", price="999")], [trade("T1")])
    last = query(conn, "SELECT status, failed_stage FROM pipeline_runs ORDER BY run_id DESC LIMIT 1")
    assert last == [("FAILED", "check_batch")]
    assert query(conn, "SELECT price FROM clean_internal")[0][0] == 100   # original data untouched


def test_corrupt_file_rolls_back_whole_load_stage(conn, cfg, tmp_path):
    good = write_trades(tmp_path / "good.csv", [trade("T1")])
    bad = tmp_path / "bad.csv"
    bad.write_text("trade_id,isin,price\nT1,US0378331005,100\n")   # wrong header
    with pytest.raises(Exception):
        pipeline.run_pipeline(conn, "B1", pipeline.date(2026, 10, 30), good, bad, cfg)
    # The internal file loaded fine, but it was in the same transaction, so it was rolled back too.
    assert query(conn, "SELECT count(*) FROM raw_internal") == [(0,)]
    status, stage, error = query(conn, "SELECT status, failed_stage, error_message FROM pipeline_runs")[0]
    assert (status, stage) == ("FAILED", "load_raw")
    assert error


def test_crash_midway_rolls_back_that_stage_only(conn, run, monkeypatch):
    def exploding_recon(c, ctx):
        c.execute("INSERT INTO match_results (batch_id, trade_id, status, as_of_date) "
                  "VALUES ('B1', 'HALF_DONE', 'MATCHED', '2026-10-30')")
        raise RuntimeError("simulated crash in the middle of recon")

    stages = [(n, exploding_recon if n == "recon" else f) for n, f in pipeline.STAGES]
    monkeypatch.setattr(pipeline, "STAGES", stages)
    with pytest.raises(RuntimeError):
        run([trade("T1")], [trade("T1")])
    assert query(conn, "SELECT count(*) FROM match_results") == [(0,)]     # half-done write is gone
    assert query(conn, "SELECT count(*) FROM clean_internal") == [(1,)]    # earlier stage stays committed
    assert query(conn, "SELECT status, failed_stage FROM pipeline_runs") == [("FAILED", "recon")]

    # Recovery = just re-run. Earlier stages are idempotent, so this finishes cleanly.
    monkeypatch.undo()
    run([trade("T1")], [trade("T1")])
    assert query(conn, "SELECT trade_id, status FROM match_results") == [("T1", "MATCHED")]


def test_failed_dq_check_fails_run_but_keeps_results(conn, run, monkeypatch):
    def bad_check(c, ctx):
        c.execute("INSERT INTO dq_results (run_id, check_name, passed, observed, expected) "
                  "VALUES (%s, 'forced_failure', false, '1', '0')", (ctx["run_id"],))
        return {}

    monkeypatch.setattr(pipeline, "STAGES", pipeline.STAGES + [("extra_dq", bad_check)])
    with pytest.raises(pipeline.PipelineError, match="forced_failure"):
        run([trade("T1")], [trade("T1")])
    assert query(conn, "SELECT status, failed_stage FROM pipeline_runs") == [("FAILED", "dq_gate")]
    assert ("forced_failure", False) in query(conn, "SELECT check_name, passed FROM dq_results")
