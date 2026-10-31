"""Compare the breaks the pipeline found with the breaks the generator seeded.

precision = of the breaks we reported, how many were real      (TP / (TP + FP))
recall    = of the real breaks, how many we reported            (TP / (TP + FN))
F1        = one number that balances both (harmonic mean)
"""
import argparse
import csv
from pathlib import Path

from tradesync.config import load_config
from tradesync.db import connect

BREAK_TYPES = ["PRICE_MISMATCH", "QUANTITY_MISMATCH", "MISSING_IN_CUSTODIAN", "MISSING_IN_INTERNAL"]


def read_truth(path: Path) -> set[tuple[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return {(r["trade_id"], r["break_type"]) for r in csv.DictReader(f)}


def read_detected(conn, batch_id: str | None) -> set[tuple[str, str]]:
    with conn.cursor() as cur:
        if batch_id is None:  # default: the latest successful batch
            cur.execute("SELECT batch_id FROM pipeline_runs WHERE status = 'SUCCESS' "
                        "ORDER BY run_id DESC LIMIT 1")
            row = cur.fetchone()
            if row is None:
                raise SystemExit("no successful pipeline run found; run the pipeline first")
            batch_id = row[0]
        cur.execute("SELECT trade_id, break_type FROM breaks "
                    "WHERE status = 'OPEN' AND last_batch_id = %s", (batch_id,))
        return set(cur.fetchall())


def score(truth: set, detected: set) -> dict[str, dict]:
    """Per break type and overall. Pairs are (trade_id, break_type), so the type must also be right."""
    results = {}
    for btype in BREAK_TYPES + ["OVERALL"]:
        t = truth if btype == "OVERALL" else {x for x in truth if x[1] == btype}
        d = detected if btype == "OVERALL" else {x for x in detected if x[1] == btype}
        tp, fp, fn = len(t & d), len(d - t), len(t - d)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        results[btype] = {"tp": tp, "fp": fp, "fn": fn,
                          "precision": precision, "recall": recall, "f1": f1}
    return results


def print_report(results: dict) -> None:
    print(f"{'break_type':22} {'TP':>6} {'FP':>5} {'FN':>5} {'precision':>10} {'recall':>8} {'F1':>7}")
    for btype, r in results.items():
        print(f"{btype:22} {r['tp']:>6} {r['fp']:>5} {r['fn']:>5} "
              f"{r['precision']:>10.4f} {r['recall']:>8.4f} {r['f1']:>7.4f}")


def main() -> None:
    cfg = load_config()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--truth", type=Path, default=cfg.ground_truth_file)
    ap.add_argument("--batch-id")
    args = ap.parse_args()
    with connect() as conn:
        results = score(read_truth(args.truth), read_detected(conn, args.batch_id))
    print_report(results)


if __name__ == "__main__":
    main()
