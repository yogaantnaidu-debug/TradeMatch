"""Clean-layer validation and standardisation."""
from decimal import Decimal

import pytest

from conftest import query, trade


def test_cosmetic_dirt_is_fixed_not_rejected(conn, run):
    run([trade("  t1 ", isin=" us0378331005", side="buy", currency="usd")], [trade("T1")])
    row = query(conn, "SELECT trade_id, isin, side, currency, quantity, price FROM clean_internal")
    assert row == [("T1", "US0378331005", "BUY", "USD", Decimal("100"), Decimal("100.00"))]
    assert query(conn, "SELECT status FROM match_results") == [("MATCHED",)]


@pytest.mark.parametrize("bad, reason", [
    ({"trade_id": ""}, "NULL_TRADE_ID"),
    ({"isin": ""}, "NULL_ISIN"),
    ({"isin": "US123"}, "INVALID_ISIN"),
    ({"side": "HOLD"}, "INVALID_SIDE"),
    ({"quantity": "abc"}, "INVALID_QUANTITY"),
    ({"quantity": "-5"}, "NON_POSITIVE_QUANTITY"),
    ({"quantity": "0"}, "NON_POSITIVE_QUANTITY"),
    ({"price": ""}, "INVALID_PRICE"),
    ({"price": "NaN"}, "INVALID_PRICE"),      # NaN is a legal Postgres NUMERIC; we reject it on purpose
    ({"price": "0"}, "NON_POSITIVE_PRICE"),
    ({"currency": "US"}, "INVALID_CURRENCY"),
    ({"trade_date": "2026-02-30"}, "INVALID_TRADE_DATE"),
    ({"trade_date": "31/10/2026"}, "INVALID_TRADE_DATE"),
    ({"counterparty": ""}, "NULL_COUNTERPARTY"),
])
def test_invalid_rows_are_rejected_with_reason(conn, run, bad, reason):
    run([trade("T1"), {**trade("T2"), **bad}], [trade("T1")])
    assert query(conn, "SELECT source, reason FROM rejected_records") == [("INTERNAL", reason)]
    assert query(conn, "SELECT trade_id FROM clean_internal") == [("T1",)]


def test_duplicates_keep_first_copy(conn, run):
    run([trade("T1", price="100"), trade("T1", price="200")], [trade("T1")])
    assert query(conn, "SELECT price, source_line FROM clean_internal") == [(Decimal("100"), 1)]
    assert query(conn, "SELECT reason, source_line FROM rejected_records") == [("DUPLICATE_TRADE_ID", 2)]


def test_raw_equals_clean_plus_rejected(conn, run):
    rows = [trade("T1"), trade("T1"), trade("T2", side="X"), trade("T3")]
    run(rows, rows[:1])
    checks = dict(query(conn, "SELECT check_name, passed FROM dq_results"))
    assert checks["internal_rows_reconcile"] is True
    assert all(checks.values())
