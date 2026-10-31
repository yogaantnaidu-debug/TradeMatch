"""End-to-end: generator -> real Postgres pipeline -> evaluate against ground truth."""
import hashlib
from datetime import date

import evaluate
import generate_data
from conftest import query
from tradesync.pipeline import run_pipeline


def test_generator_is_deterministic(tmp_path, cfg):
    a = generate_data.generate(500, 1, tmp_path / "a", cfg.price_tolerance)
    b = generate_data.generate(500, 1, tmp_path / "b", cfg.price_tolerance)
    assert a == b
    for name in ("internal_trades.csv", "custodian_trades.csv", "ground_truth.csv"):
        h = [hashlib.sha256((tmp_path / d / name).read_bytes()).hexdigest() for d in "ab"]
        assert h[0] == h[1]


def test_end_to_end_against_ground_truth(conn, cfg, tmp_path):
    size = 5000
    generate_data.generate(size, 7, tmp_path, cfg.price_tolerance)
    run_id = run_pipeline(conn, "IT", date(2026, 10, 30), tmp_path / "internal_trades.csv",
                          tmp_path / "custodian_trades.csv", cfg)

    assert query(conn, "SELECT status FROM pipeline_runs WHERE run_id = %s", (run_id,)) == [("SUCCESS",)]
    assert all(p for (p,) in query(conn, "SELECT passed FROM dq_results"))

    truth = evaluate.read_truth(tmp_path / "ground_truth.csv")
    detected = evaluate.read_detected(conn, None)
    results = evaluate.score(truth, detected)
    assert len(truth) == sum(int(size * r) for r in generate_data.BREAK_RATES.values())
    for btype, r in results.items():
        assert (r["precision"], r["recall"]) == (1.0, 1.0), btype
    evaluate.print_report(results)


def test_score_math():
    truth = {("A", "PRICE_MISMATCH"), ("B", "PRICE_MISMATCH")}
    detected = {("A", "PRICE_MISMATCH"), ("C", "PRICE_MISMATCH")}
    r = evaluate.score(truth, detected)["PRICE_MISMATCH"]
    assert (r["tp"], r["fp"], r["fn"]) == (1, 1, 1)
    assert r["precision"] == r["recall"] == r["f1"] == 0.5
