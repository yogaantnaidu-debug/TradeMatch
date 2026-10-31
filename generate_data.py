"""Generate synthetic internal + custodian trade files with KNOWN breaks.

Because we choose which trades break, we can later measure whether the pipeline
finds exactly those (see evaluate.py). A fixed seed makes every run identical.

Outputs (in data/):
  internal_trades.csv, custodian_trades.csv, ground_truth.csv

Optional --prices: use REAL market data (e.g. a Kaggle daily-price dataset) for the
tickers, trade dates and prices, instead of random ones. Breaks are still seeded by
us, because real data has no ground truth to score against.
"""
import argparse
import csv
import random
import string
from collections import deque
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from tradesync.config import load_config

FIELDS = ["trade_id", "isin", "side", "quantity", "price", "currency", "trade_date", "counterparty"]

# Fractions of all trades. They are mutually exclusive, so each seeded trade has
# exactly one expected break type and ground truth is unambiguous.
BREAK_RATES = {
    "PRICE_MISMATCH": 0.020,
    "QUANTITY_MISMATCH": 0.015,
    "MISSING_IN_CUSTODIAN": 0.010,
    "MISSING_IN_INTERNAL": 0.010,
}
# Price differs but stays INSIDE the tolerance. These must NOT become breaks,
# so they catch a pipeline that is too strict.
NEAR_MISS_RATE = 0.020

# Cosmetic dirt: the clean layer must FIX these (not reject them).
COSMETIC_RATE = 0.03
# Extra copies of real rows. The clean layer must keep one copy and reject the rest as duplicates.
DUPLICATE_RATE = 0.005
# Extra corrupted copies of real rows. The clean layer must reject them with a reason.
# They are copies (not replacements) so the real trade still exists and ground truth is unchanged.
CORRUPT_RATE = 0.005

COUNTRIES = ["US", "GB", "DE", "FR", "JP", "CA"]
CURRENCIES = ["USD", "GBP", "EUR", "JPY", "CAD"]
COUNTERPARTIES = ["GOLDMAN", "JPMORGAN", "MORGANSTANLEY", "BARCLAYS", "CITI", "UBS", "BNPPARIBAS"]


def random_isin(rng: random.Random) -> str:
    # Shape only (2 letters + 9 alphanumerics + 1 digit); the real ISIN check digit is not computed.
    body = "".join(rng.choices(string.ascii_uppercase + string.digits, k=9))
    return rng.choice(COUNTRIES) + body + str(rng.randint(0, 9))


def isin_check_digit(body: str) -> str:
    """Real ISIN check digit (Luhn) for the first 11 characters, e.g. US037833100 -> 5."""
    digits = "".join(str(int(ch, 36)) for ch in body)  # A=10 ... Z=35, digits stay digits
    total = 0
    # Double every second digit starting from the RIGHTMOST one.
    for pos, ch in enumerate(reversed(digits)):
        d = int(ch) * (2 if pos % 2 == 0 else 1)
        total += d // 10 + d % 10
    return str((10 - total % 10) % 10)


def isin_for_ticker(ticker: str) -> str:
    """A valid-format US ISIN built from the ticker.

    The datasets give tickers, not ISINs, and real ISINs need the CUSIP, which is
    licensed data. So this ISIN is synthetic but deterministic (same ticker -> same
    ISIN) and carries a correct check digit.
    """
    body = "US" + ticker.ljust(9, "0")
    return body + isin_check_digit(body)


PRICE_TICKER_COLUMNS = ("ticker", "symbol", "name")


def _read_price_file(path: Path, days: int, out: dict[str, deque]) -> None:
    """Add the last `days` valid rows per ticker from one CSV/TXT price file.

    Handles the common Kaggle layouts: one file per ticker (aapl.us.txt, AAPL.csv,
    ticker taken from the file name) or one file with a ticker/symbol/name column.
    Needs Date, Low and High columns (any letter case).
    """
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return  # some Kaggle files are empty
        cols = {h.strip().lower(): i for i, h in enumerate(header)}
        if not {"date", "low", "high"} <= cols.keys():
            return
        tcol = next((cols[c] for c in PRICE_TICKER_COLUMNS if c in cols), None)
        file_ticker = path.name.split(".")[0].upper()
        for row in reader:
            try:
                ticker = (row[tcol].strip().upper() if tcol is not None else file_ticker)
                day = date.fromisoformat(row[cols["date"]].strip()[:10])
                low, high = Decimal(row[cols["low"]]), Decimal(row[cols["high"]])
            except (IndexError, ValueError, ArithmeticError):
                continue  # skip malformed rows rather than fail the whole load
            # Only plain tickers that fit an ISIN body (drops BRK-A, BF.B, ...). Prices below 1.00
            # are skipped: at 4 decimals a seeded price break could round away to nothing and
            # the ground truth would be wrong (the random generator uses 1-500 for the same reason).
            if not (ticker.isalnum() and len(ticker) <= 9 and Decimal(1) <= low <= high):
                continue
            out.setdefault(ticker, deque(maxlen=days)).append((day, low, high))


def load_prices(path: Path, max_tickers: int, days: int, rng: random.Random) -> list[tuple]:
    """Return (ticker, date, low, high) rows: the most recent `days` rows of up to `max_tickers` tickers."""
    files = sorted(p for p in path.rglob("*") if p.suffix.lower() in (".csv", ".txt")) if path.is_dir() else [path]
    by_ticker: dict[str, deque] = {}
    for p in files:
        _read_price_file(p, days, by_ticker)
    if not by_ticker:
        raise SystemExit(f"no usable price rows in {path} (need Date, Low, High columns)")
    tickers = sorted(by_ticker)  # sorted first so the seeded sample is reproducible
    if len(tickers) > max_tickers:
        tickers = sorted(rng.sample(tickers, max_tickers))
    return [(t, d, lo, hi) for t in tickers for d, lo, hi in by_ticker[t]]


def make_trades(n: int, rng: random.Random, base_date: date, prices: list[tuple] | None = None) -> list[dict]:
    if prices:
        return make_trades_from_prices(n, rng, prices)
    isins = [random_isin(rng) for _ in range(2000)]
    trades = []
    for i in range(1, n + 1):
        trades.append({
            "trade_id": f"T{i:08d}",
            "isin": rng.choice(isins),
            "side": rng.choice(["BUY", "SELL"]),
            "quantity": str(rng.randint(1, 100_000)),
            "price": f"{rng.uniform(1, 500):.4f}",
            "currency": rng.choice(CURRENCIES),
            "trade_date": (base_date - timedelta(days=rng.randint(0, 9))).isoformat(),
            "counterparty": rng.choice(COUNTERPARTIES),
        })
    return trades


def make_trades_from_prices(n: int, rng: random.Random, prices: list[tuple]) -> list[dict]:
    """Trades on real tickers and dates, each priced inside that day's real low-high range."""
    isins = {t: isin_for_ticker(t) for t in {p[0] for p in prices}}
    trades = []
    for i in range(1, n + 1):
        ticker, day, low, high = rng.choice(prices)
        price = (low + (high - low) * Decimal(str(rng.random()))).quantize(Decimal("0.0001"))
        trades.append({
            "trade_id": f"T{i:08d}",
            "isin": isins[ticker],
            "side": rng.choice(["BUY", "SELL"]),
            "quantity": str(rng.randint(1, 100_000)),
            "price": str(price),
            "currency": "USD",          # US listings
            "trade_date": day.isoformat(),
            "counterparty": rng.choice(COUNTERPARTIES),
        })
    return trades


def shift_price(price: str, fraction: Decimal) -> str:
    p = Decimal(price)
    # ROUND_DOWN keeps near-miss differences from being rounded up past the tolerance.
    return str((p * (1 + fraction)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN))


def add_cosmetic_dirt(row: dict, rng: random.Random) -> dict:
    row = dict(row)
    kind = rng.choice(["lower_isin", "lower_side", "padded", "lower_ccy"])
    if kind == "lower_isin":
        row["isin"] = row["isin"].lower()
    elif kind == "lower_side":
        row["side"] = row["side"].lower()
    elif kind == "padded":
        row["trade_id"] = f"  {row['trade_id']} "
        row["isin"] = f" {row['isin']}"
    else:
        row["currency"] = row["currency"].lower()
    return row


def corrupt(row: dict, rng: random.Random) -> dict:
    row = dict(row)
    kind = rng.choice(["bad_date", "bad_date_fmt", "negative_qty", "null_trade_id",
                       "null_isin", "zero_price", "bad_side", "null_price"])
    if kind == "bad_date":
        row["trade_date"] = "2026-02-30"
    elif kind == "bad_date_fmt":
        row["trade_date"] = "31/10/2026"
    elif kind == "negative_qty":
        row["quantity"] = "-" + row["quantity"]
    elif kind == "null_trade_id":
        row["trade_id"] = ""          # empty unquoted CSV field -> NULL in Postgres COPY
    elif kind == "null_isin":
        row["isin"] = ""
    elif kind == "zero_price":
        row["price"] = "0"
    elif kind == "bad_side":
        row["side"] = "HOLD"
    else:
        row["price"] = ""
    return row


def build_file(rows: list[dict], rng: random.Random) -> list[dict]:
    """Add dirt, duplicates and corrupt copies, then shuffle like a real feed."""
    out = [add_cosmetic_dirt(r, rng) if rng.random() < COSMETIC_RATE else r for r in rows]
    dupes = [dict(r) for r in rng.sample(out, int(len(out) * DUPLICATE_RATE))]
    bad = [corrupt(r, rng) for r in rng.sample(out, int(len(out) * CORRUPT_RATE))]
    out = out + dupes + bad
    rng.shuffle(out)
    return out


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def generate(size: int, seed: int, out_dir: Path, tolerance: Decimal,
             base_date: date = date(2026, 10, 30), prices_path: Path | None = None,
             max_tickers: int = 500, days: int = 20) -> dict:
    rng = random.Random(seed)
    prices = load_prices(prices_path, max_tickers, days, rng) if prices_path else None
    trades = make_trades(size, rng, base_date, prices)

    # Pick disjoint sets of trades for each seeded break type.
    ids = list(range(size))
    rng.shuffle(ids)
    picks, start = {}, 0
    for btype, rate in list(BREAK_RATES.items()) + [("NEAR_MISS", NEAR_MISS_RATE)]:
        k = int(size * rate)
        picks[btype] = set(ids[start:start + k])
        start += k

    internal, custodian, truth = [], [], []
    for idx, t in enumerate(trades):
        c = dict(t)
        if idx in picks["PRICE_MISMATCH"]:
            # 5x to 50x the tolerance, either direction: always a clear break.
            frac = tolerance * Decimal(rng.randint(5, 50)) * rng.choice([1, -1])
            c["price"] = shift_price(t["price"], frac)
        elif idx in picks["QUANTITY_MISMATCH"]:
            c["quantity"] = str(int(t["quantity"]) + rng.choice([1, -1]) * rng.randint(1, 100))
            if int(c["quantity"]) <= 0:
                c["quantity"] = str(int(t["quantity"]) + rng.randint(1, 100))
        elif idx in picks["NEAR_MISS"]:
            frac = tolerance * Decimal(rng.randint(1, 9)) / 10  # 10-90 of the tolerance
            c["price"] = shift_price(t["price"], frac)

        if idx not in picks["MISSING_IN_INTERNAL"]:
            internal.append(t)
        if idx not in picks["MISSING_IN_CUSTODIAN"]:
            custodian.append(c)
        for btype in BREAK_RATES:
            if idx in picks[btype]:
                truth.append({"trade_id": t["trade_id"], "break_type": btype})

    out_dir.mkdir(parents=True, exist_ok=True)
    internal_rows = build_file(internal, rng)
    custodian_rows = build_file(custodian, rng)
    write_csv(out_dir / "internal_trades.csv", internal_rows, FIELDS)
    write_csv(out_dir / "custodian_trades.csv", custodian_rows, FIELDS)
    write_csv(out_dir / "ground_truth.csv", truth, ["trade_id", "break_type"])
    stats = {"trades": size, "internal_rows": len(internal_rows),
             "custodian_rows": len(custodian_rows), "seeded_breaks": len(truth)}
    if prices:
        stats.update(price_source=str(prices_path), tickers=len({p[0] for p in prices}),
                     price_rows=len(prices))
    return stats


def main() -> None:
    cfg = load_config()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--size", type=int, default=cfg.size)
    ap.add_argument("--seed", type=int, default=cfg.seed)
    ap.add_argument("--out", type=Path, default=cfg.data_dir)
    ap.add_argument("--prices", type=Path,
                    help="real daily prices: a CSV/TXT file or a folder of them (Date, Low, High columns)")
    ap.add_argument("--tickers", type=int, default=500, help="max tickers sampled from --prices")
    ap.add_argument("--days", type=int, default=20, help="most recent trading days used per ticker")
    args = ap.parse_args()
    stats = generate(args.size, args.seed, args.out, cfg.price_tolerance,
                     prices_path=args.prices, max_tickers=args.tickers, days=args.days)
    print(f"Generated {stats} in {args.out}")


if __name__ == "__main__":
    main()
