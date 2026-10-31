"""Break detection: tolerance boundaries, each break type, ageing and resolution."""
from datetime import date

import pytest

from conftest import query, trade


@pytest.mark.parametrize("custodian_price, expected", [
    # internal price 100, tolerance 0.0001 -> allowed gap is exactly 0.01
    ("100.000000", "MATCHED"),
    ("100.010000", "MATCHED"),         # gap == tolerance: NOT a break (rule is strictly greater)
    ("100.010001", "BREAK"),           # smallest step above the tolerance
    ("99.990000", "MATCHED"),          # same boundary, other direction
    ("99.989999", "BREAK"),
])
def test_price_tolerance_boundary(conn, run, custodian_price, expected):
    run([trade(price="100.000000")], [trade(price=custodian_price)])
    assert query(conn, "SELECT status FROM match_results") == [(expected,)]


def test_tolerance_is_configurable(conn, run, cfg):
    from dataclasses import replace
    from decimal import Decimal
    # A 1 percent gap breaks at 1bp, but not at a 2 percent tolerance.
    run([trade(price="100")], [trade(price="101")], config=replace(cfg, price_tolerance=Decimal("0.02")))
    assert query(conn, "SELECT status FROM match_results") == [("MATCHED",)]


def test_each_break_type(conn, run):
    internal = [
        trade("T_OK"),
        trade("T_PRICE", price="100"),
        trade("T_QTY", quantity="100"),
        trade("T_BOTH", price="100", quantity="100"),
        trade("T_NO_CUST"),
    ]
    custodian = [
        trade("T_OK"),
        trade("T_PRICE", price="105"),
        trade("T_QTY", quantity="90"),
        trade("T_BOTH", price="105", quantity="90"),
        trade("T_NO_INT"),
    ]
    run(internal, custodian)
    breaks = set(query(conn, "SELECT trade_id, break_type FROM breaks"))
    assert breaks == {
        ("T_PRICE", "PRICE_MISMATCH"),
        ("T_QTY", "QUANTITY_MISMATCH"),
        ("T_BOTH", "PRICE_MISMATCH"),
        ("T_BOTH", "QUANTITY_MISMATCH"),     # one trade can carry two breaks
        ("T_NO_CUST", "MISSING_IN_CUSTODIAN"),
        ("T_NO_INT", "MISSING_IN_INTERNAL"),
    }
    status = dict(query(conn, "SELECT trade_id, status FROM match_results"))
    assert status["T_OK"] == "MATCHED"
    assert sum(1 for s in status.values() if s == "BREAK") == 5
    diff = query(conn, "SELECT difference FROM breaks WHERE trade_id = 'T_QTY'")[0][0]
    assert diff == -10


def test_ageing_buckets_and_late_record_resolution(conn, run):
    # Day 1: two trades missing at the custodian.
    internal = [trade("T_LATE"), trade("T_STUCK")]
    run(internal, [], batch_id="D1", as_of=date(2026, 10, 1))
    # Day 4: still missing -> age 3 days -> bucket 2-5.
    run(internal, [], batch_id="D4", as_of=date(2026, 10, 4))
    ages = dict(query(conn, "SELECT trade_id, age_bucket FROM break_ageing"))
    assert ages == {"T_LATE": "2-5", "T_STUCK": "2-5"}

    # Day 8: the late custodian record for T_LATE finally arrives.
    run(internal, [trade("T_LATE")], batch_id="D8", as_of=date(2026, 10, 8))
    rows = dict((r[0], r[1:]) for r in query(
        conn, "SELECT trade_id, status, first_detected_date, resolved_date FROM breaks"))
    assert rows["T_LATE"] == ("RESOLVED", date(2026, 10, 1), date(2026, 10, 8))
    assert rows["T_STUCK"] == ("OPEN", date(2026, 10, 1), None)   # first date kept across batches
    assert query(conn, "SELECT trade_id, age_days, age_bucket FROM break_ageing") == [("T_STUCK", 7, "6+")]

    # recon_summary keeps per-batch history: day 1 still shows 2 breaks.
    summary = dict(query(conn, "SELECT batch_id, broken FROM recon_summary"))
    assert summary == {"D1": 2, "D4": 2, "D8": 1}
