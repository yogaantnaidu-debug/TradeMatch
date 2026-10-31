"""EXPLAIN ANALYZE the main queries with and without the indexes in sql/indexes.sql.

Run after the pipeline has loaded the 100k dataset:
    python benchmark_explain.py
Each query runs 5 times per setting; we report the median execution time so one
slow run (cold cache) does not distort the result. Output also goes to results/.
"""
import re
import statistics
from pathlib import Path

from tradesync.config import ROOT
from tradesync.db import SQL_DIR, connect, run_sql_file, scalar

RUNS = 5

QUERIES = {
    "lookup one trade by trade_id (all batches)":
        "SELECT * FROM clean_internal WHERE trade_id = 'T00050000'",
    "breaks in a batch (match_results by status)":
        "SELECT trade_id, break_types FROM match_results "
        "WHERE batch_id = '{batch}' AND status = 'BREAK'",
    "open breaks by type (breaks by status)":
        "SELECT break_type, count(*) FROM breaks WHERE status = 'OPEN' GROUP BY break_type",
    "recon FULL OUTER JOIN (count)":
        "SELECT count(*) FROM (SELECT * FROM clean_internal WHERE batch_id = '{batch}') i "
        "FULL OUTER JOIN (SELECT * FROM clean_custodian WHERE batch_id = '{batch}') c "
        "ON c.trade_id = i.trade_id",
}


def index_names() -> list[str]:
    return re.findall(r"CREATE INDEX IF NOT EXISTS (\w+)", (SQL_DIR / "indexes.sql").read_text())


def explain(conn, sql: str) -> tuple[float, str]:
    times, plan = [], ""
    for _ in range(RUNS):
        with conn.cursor() as cur:
            cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql)
            lines = [r[0] for r in cur.fetchall()]
        times.append(float(re.search(r"Execution Time: ([\d.]+) ms", lines[-1]).group(1)))
        plan = "\n".join(lines)
    return statistics.median(times), plan


def main() -> None:
    out: list[str] = []
    with connect() as conn:
        batch = scalar(conn, "SELECT batch_id FROM pipeline_runs WHERE status = 'SUCCESS' "
                             "ORDER BY run_id DESC LIMIT 1")
        rows = scalar(conn, "SELECT count(*) FROM clean_internal")
        out.append(f"batch={batch}  clean_internal rows={rows}  runs per query={RUNS} (median)\n")
        results = {}
        for label, setup in (("before (no extra indexes)", "drop"), ("after (with indexes)", "create")):
            if setup == "drop":
                for name in index_names():
                    conn.execute(f"DROP INDEX IF EXISTS {name}")
            else:
                run_sql_file(conn, "indexes.sql")
            conn.execute("ANALYZE")   # fresh statistics so the planner can choose fairly
            out.append(f"===== {label} =====")
            for qname, q in QUERIES.items():
                ms, plan = explain(conn, q.format(batch=batch))
                results.setdefault(qname, []).append(ms)
                out.append(f"--- {qname}: median {ms:.3f} ms\n{plan}\n")

    summary = [f"{'query':48} {'before ms':>10} {'after ms':>10}"]
    for qname, (before, after) in results.items():
        summary.append(f"{qname:48} {before:>10.3f} {after:>10.3f}")
    text = "\n".join(summary) + "\n\n" + "\n".join(out)
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "explain_analyze.txt").write_text(text, encoding="utf-8")
    print(text if len(text) < 4000 else "\n".join(summary) + "\n\nfull plans: results/explain_analyze.txt")


if __name__ == "__main__":
    main()
