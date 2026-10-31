"""Shared test fixtures.

Every test gets its own empty Postgres schema, so tests never see each other's
rows (or your real data in `public`) and can run in any order.
"""
import csv
import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tradesync.config import load_config
from tradesync.db import connect
from tradesync.load_raw import COLUMNS
from tradesync.pipeline import run_pipeline


@pytest.fixture
def conn():
    schema = f"test_{uuid.uuid4().hex[:10]}"
    admin = connect()
    admin.execute(f"CREATE SCHEMA {schema}")
    c = connect(search_path=schema)
    try:
        yield c
    finally:
        c.close()
        admin.execute(f"DROP SCHEMA {schema} CASCADE")
        admin.close()


@pytest.fixture
def cfg():
    # Fixed tolerance for tests so a change to config.toml does not silently change test meaning.
    return replace(load_config(), price_tolerance=Decimal("0.0001"))


def trade(trade_id="T1", isin="US0378331005", side="BUY", quantity="100", price="100.00",
          currency="USD", trade_date="2026-10-30", counterparty="GOLDMAN") -> dict:
    return dict(trade_id=trade_id, isin=isin, side=side, quantity=quantity, price=price,
                currency=currency, trade_date=trade_date, counterparty=counterparty)


def write_trades(path: Path, rows: list[dict]) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return path


@pytest.fixture
def run(conn, cfg, tmp_path):
    """run(internal_rows, custodian_rows, batch_id=..., as_of=...) -> run_id"""
    def _run(internal, custodian, batch_id="B1", as_of=date(2026, 10, 30), config=None):
        i = write_trades(tmp_path / f"{batch_id}_internal.csv", internal)
        c = write_trades(tmp_path / f"{batch_id}_custodian.csv", custodian)
        return run_pipeline(conn, batch_id, as_of, i, c, config or cfg)
    return _run


def query(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()
