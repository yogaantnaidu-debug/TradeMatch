"""--prices: real market data as the base for seeded trades."""
import csv
import random
import re
from datetime import date, timedelta
from decimal import Decimal

import evaluate
import generate_data
from conftest import query
from tradesync.pipeline import run_pipeline

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")   # same rule as clean.sql


def write_rows(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def kaggle_like_folder(tmp_path, n_days=30):
    """The three layouts seen on Kaggle, plus rows that must be skipped."""
    d = tmp_path / "prices"
    d.mkdir()
    days = [date(2017, 10, 1) + timedelta(days=i) for i in range(n_days)]
    # "Huge Stock Market Dataset" style: aapl.us.txt, ticker from the file name
    write_rows(d / "aapl.us.txt", ["Date", "Open", "High", "Low", "Close", "Volume", "OpenInt"],
               [[x.isoformat(), "150", "152.5", "149.25", "151", "1000", "0"] for x in days])
    # Yahoo style: MSFT.csv with Adj Close
    write_rows(d / "MSFT.csv", ["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"],
               [[x.isoformat(), "80", "81", "79.5", "80.5", "80.5", "1000"] for x in days])
    # One combined file with a ticker column (S&P 500 "all_stocks" style)
    write_rows(d / "all_stocks.csv", ["date", "open", "high", "low", "close", "volume", "Name"],
               [[x.isoformat(), "30", "31", "29", "30", "1", "IBM"] for x in days]
               + [["2017-10-02", "1", "1", "0.5", "1", "1", "PENNY"],     # below 1.00 -> skipped
                  ["2017-10-02", "9", "9", "9", "9", "1", "BRK-A"],      # not a plain ticker -> skipped
                  ["not-a-date", "9", "9", "9", "9", "1", "IBM"]])       # malformed -> skipped
    (d / "empty.us.txt").write_text("")                                  # empty files exist on Kaggle
    return d


def test_isin_check_digit_matches_a_real_isin():
    assert generate_data.isin_check_digit("US037833100") == "5"          # Apple: US0378331005
    isin = generate_data.isin_for_ticker("AAPL")
    assert ISIN_RE.match(isin) and isin == "USAAPL00000" + generate_data.isin_check_digit("USAAPL00000")


def test_load_prices_reads_kaggle_layouts(tmp_path):
    rows = generate_data.load_prices(kaggle_like_folder(tmp_path), max_tickers=10, days=20,
                                     rng=random.Random(1))
    assert {r[0] for r in rows} == {"AAPL", "MSFT", "IBM"}
    assert len(rows) == 3 * 20                                            # only the last 20 days each
    assert min(r[1] for r in rows) == date(2017, 10, 11)


def test_ticker_sample_is_capped_and_reproducible(tmp_path):
    folder = kaggle_like_folder(tmp_path)
    a = generate_data.load_prices(folder, max_tickers=2, days=5, rng=random.Random(7))
    b = generate_data.load_prices(folder, max_tickers=2, days=5, rng=random.Random(7))
    assert a == b and len({r[0] for r in a}) == 2


def test_end_to_end_with_real_prices(conn, cfg, tmp_path):
    folder = kaggle_like_folder(tmp_path)
    out = tmp_path / "out"
    stats = generate_data.generate(3000, 3, out, cfg.price_tolerance, prices_path=folder)
    assert stats["tickers"] == 3

    # Every clean-looking internal trade sits inside its real day's low-high range.
    ranges = {"AAPL": (Decimal("149.25"), Decimal("152.5")), "MSFT": (Decimal("79.5"), Decimal("81")),
              "IBM": (Decimal("29"), Decimal("31"))}
    by_isin = {generate_data.isin_for_ticker(t): r for t, r in ranges.items()}
    with open(out / "internal_trades.csv", newline="") as f:
        checked = 0
        for row in csv.DictReader(f):
            isin = row["isin"].strip().upper()
            if isin in by_isin and re.fullmatch(r"\d+\.\d+", row["price"]):
                lo, hi = by_isin[isin]
                assert lo <= Decimal(row["price"]) <= hi
                checked += 1
    assert checked > 2500

    run_pipeline(conn, "RP", date(2026, 10, 30), out / "internal_trades.csv",
                 out / "custodian_trades.csv", cfg)
    results = evaluate.score(evaluate.read_truth(out / "ground_truth.csv"), evaluate.read_detected(conn, None))
    for btype, r in results.items():
        assert (r["precision"], r["recall"]) == (1.0, 1.0), btype
    assert all(p for (p,) in query(conn, "SELECT passed FROM dq_results"))
