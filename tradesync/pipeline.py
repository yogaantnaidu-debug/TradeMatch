"""Run the TradeSync pipeline for one batch.

    python -m tradesync.pipeline --batch-id 2026-10-30

Each stage runs in its own transaction. If a stage fails, Postgres rolls back
everything that stage did, and the failure is written to pipeline_runs.
Earlier stages stay committed; that is safe because every stage is idempotent,
so simply re-running the batch continues where it left off.
"""
import argparse
import hashlib
import json
import time
from datetime import date
from pathlib import Path

import psycopg

from tradesync.config import Config, load_config
from tradesync.db import connect, run_sql_file, scalar
from tradesync.load_raw import load_file


class PipelineError(Exception):
    pass


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def apply_schema(conn: psycopg.Connection) -> None:
    with conn.transaction():
        run_sql_file(conn, "schema.sql")
        run_sql_file(conn, "indexes.sql")
        run_sql_file(conn, "views.sql")


def check_same_content(conn, batch_id: str, internal_sha: str, custodian_sha: str) -> None:
    """Same batch_id + different file content = someone sent a corrected file.

    Silently skipping it (because the keys already exist) would hide the correction,
    so we stop and ask for a new batch_id instead.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT internal_file_sha256, custodian_file_sha256 FROM pipeline_runs "
            "WHERE batch_id = %s AND status = 'SUCCESS' ORDER BY run_id DESC LIMIT 1",
            (batch_id,),
        )
        prev = cur.fetchone()
    if prev and prev != (internal_sha, custodian_sha):
        raise PipelineError(
            f"batch {batch_id} was already loaded from different files; use a new batch id"
        )


def load_stage(conn, ctx: dict) -> dict:
    return {
        "rows_in_internal": load_file(conn, "raw_internal", ctx["internal_file"], ctx["batch_id"]),
        "rows_in_custodian": load_file(conn, "raw_custodian", ctx["custodian_file"], ctx["batch_id"]),
    }


def clean_stage(conn, ctx: dict) -> dict:
    params = {"batch_id": ctx["batch_id"]}
    for source, raw, clean in (("INTERNAL", "raw_internal", "clean_internal"),
                               ("CUSTODIAN", "raw_custodian", "clean_custodian")):
        run_sql_file(conn, "clean.sql", params, {
            "__RAW_TABLE__": raw, "__CLEAN_TABLE__": clean, "__SOURCE__": source})
    b = ctx["batch_id"]
    return {
        "rows_clean_internal": scalar(conn, "SELECT count(*) FROM clean_internal WHERE batch_id = %s", (b,)),
        "rows_clean_custodian": scalar(conn, "SELECT count(*) FROM clean_custodian WHERE batch_id = %s", (b,)),
        "rows_rejected": scalar(conn, "SELECT count(*) FROM rejected_records WHERE batch_id = %s", (b,)),
    }


def recon_stage(conn, ctx: dict) -> dict:
    run_sql_file(conn, "recon.sql", {"batch_id": ctx["batch_id"], "as_of": ctx["as_of"],
                                     "tolerance": ctx["cfg"].price_tolerance})
    b = ctx["batch_id"]
    q = "SELECT count(*) FROM match_results WHERE batch_id = %s AND status = %s"
    return {"matched": scalar(conn, q, (b, "MATCHED")), "broken": scalar(conn, q, (b, "BREAK"))}


def dq_stage(conn, ctx: dict) -> dict:
    run_sql_file(conn, "dq.sql", {"run_id": ctx["run_id"], "batch_id": ctx["batch_id"]})
    return {}


STAGES = [
    ("load_raw", load_stage),
    ("clean", clean_stage),
    ("recon", recon_stage),
    ("data_quality", dq_stage),
]


def dq_gate(conn, run_id: int) -> None:
    """Fail the run if any data-quality check failed.

    This runs AFTER the dq stage commits, so the failed check results stay stored
    and can be inspected; the run itself is marked FAILED.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT check_name, observed, expected FROM dq_results "
                    "WHERE run_id = %s AND NOT passed", (run_id,))
        failed = cur.fetchall()
    if failed:
        raise PipelineError(f"data quality checks failed: {failed}")


def run_pipeline(conn: psycopg.Connection, batch_id: str, as_of: date,
                 internal_file: Path, custodian_file: Path, cfg: Config) -> int:
    """Run all stages for one batch. Returns run_id. Raises if a stage fails."""
    apply_schema(conn)
    internal_sha, custodian_sha = sha256_of(internal_file), sha256_of(custodian_file)

    # The run row is committed on its own first, so it survives a later rollback
    # and can record the failure.
    with conn.transaction():
        run_id = scalar(
            conn,
            "INSERT INTO pipeline_runs (batch_id, as_of_date, status, internal_file_sha256, "
            "custodian_file_sha256) VALUES (%s, %s, 'RUNNING', %s, %s) RETURNING run_id",
            (batch_id, as_of, internal_sha, custodian_sha),
        )

    ctx = {"run_id": run_id, "batch_id": batch_id, "as_of": as_of, "cfg": cfg,
           "internal_file": internal_file, "custodian_file": custodian_file}
    timings: dict[str, int] = {}
    stage = "check_batch"
    try:
        check_same_content(conn, batch_id, internal_sha, custodian_sha)
        for stage, fn in STAGES:
            t0 = time.perf_counter()
            with conn.transaction():          # one transaction per stage
                counts = fn(conn, ctx)
            timings[stage] = round((time.perf_counter() - t0) * 1000)
            update_run(conn, run_id, counts, timings)
        stage = "dq_gate"
        dq_gate(conn, run_id)
    except Exception as exc:
        # The failed stage's transaction has already been rolled back by the `with` block.
        with conn.transaction():
            conn.execute(
                "UPDATE pipeline_runs SET status = 'FAILED', failed_stage = %s, error_message = %s, "
                "stage_timings_ms = %s, finished_at = now() WHERE run_id = %s",
                (stage, str(exc)[:2000], json.dumps(timings), run_id),
            )
        raise
    with conn.transaction():
        conn.execute(
            "UPDATE pipeline_runs SET status = 'SUCCESS', finished_at = now() WHERE run_id = %s",
            (run_id,),
        )
    return run_id


def update_run(conn, run_id: int, counts: dict, timings: dict) -> None:
    # Column names come from our own stage functions, never from user input.
    sets = ", ".join(f"{k} = %({k})s" for k in counts)
    params = {**counts, "timings": json.dumps(timings), "run_id": run_id}
    with conn.transaction():
        conn.execute(
            f"UPDATE pipeline_runs SET {sets + ', ' if sets else ''}stage_timings_ms = %(timings)s "
            "WHERE run_id = %(run_id)s",
            params,
        )


def main() -> None:
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Run TradeSync for one batch")
    ap.add_argument("--batch-id", help="defaults to the as-of date")
    ap.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 10, 30),
                    help="business date of the batch (YYYY-MM-DD)")
    ap.add_argument("--internal", type=Path, default=cfg.internal_file)
    ap.add_argument("--custodian", type=Path, default=cfg.custodian_file)
    args = ap.parse_args()
    batch_id = args.batch_id or args.as_of.isoformat()

    with connect() as conn:
        t0 = time.perf_counter()
        try:
            run_id = run_pipeline(conn, batch_id, args.as_of, args.internal, args.custodian, cfg)
        except Exception as exc:
            print(f"Pipeline FAILED for batch {batch_id}: {exc}")
            raise SystemExit(1)
        print(f"Pipeline SUCCESS: run_id={run_id} batch={batch_id} "
              f"total={time.perf_counter() - t0:.2f}s")
        print_run(conn, run_id)


def print_query(conn, title: str, query: str, params=None) -> None:
    with conn.cursor() as cur:
        cur.execute(query, params)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    print(f"\n{title}")
    widths = [max(len(str(c)), *(len(str(r[i])) for r in rows)) if rows else len(c)
              for i, c in enumerate(cols)]
    print("  " + "  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    for r in rows:
        print("  " + "  ".join(str(v).ljust(w) for v, w in zip(r, widths)))


def print_run(conn, run_id: int) -> None:
    print_query(conn, "Pipeline run",
                "SELECT run_id, batch_id, status, rows_in_internal AS raw_int, rows_in_custodian AS raw_cust, "
                "rows_clean_internal AS clean_int, rows_clean_custodian AS clean_cust, rows_rejected AS rejected, "
                "matched, broken, stage_timings_ms FROM pipeline_runs WHERE run_id = %s", (run_id,))
    print_query(conn, "Recon summary",
                "SELECT s.* FROM recon_summary s JOIN pipeline_runs r ON r.batch_id = s.batch_id "
                "WHERE r.run_id = %s", (run_id,))
    print_query(conn, "Open break ageing",
                "SELECT age_bucket, break_type, count(*) AS open_breaks FROM break_ageing "
                "GROUP BY 1, 2 ORDER BY 1, 2")
    print_query(conn, "Rejected rows by reason",
                "SELECT source, reason, count(*) AS rows FROM rejected_records r "
                "JOIN pipeline_runs p ON p.batch_id = r.batch_id WHERE p.run_id = %s "
                "GROUP BY 1, 2 ORDER BY 1, 2", (run_id,))
    print_query(conn, "Data quality",
                "SELECT check_name, passed, observed, expected FROM dq_results WHERE run_id = %s "
                "ORDER BY check_name", (run_id,))


if __name__ == "__main__":
    main()
