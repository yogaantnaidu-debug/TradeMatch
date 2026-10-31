"""Bulk-load a CSV file into a raw_* table with COPY."""
from pathlib import Path

import psycopg

COLUMNS = ("trade_id", "isin", "side", "quantity", "price", "currency", "trade_date", "counterparty")
RAW_TABLES = ("raw_internal", "raw_custodian")


def load_file(conn: psycopg.Connection, table: str, path: Path, batch_id: str) -> int:
    """Load one file and return how many raw rows the batch now has.

    Why a staging table: COPY is the fastest way to get rows into Postgres, but it
    cannot skip rows that already exist. So we COPY into a temp table first, then
    INSERT ... ON CONFLICT DO NOTHING into the real table. Loading the same file twice
    therefore adds zero rows.

    Must be called inside a transaction (the pipeline wraps each stage in one).
    """
    if table not in RAW_TABLES:  # table names cannot be bind parameters, so whitelist them
        raise ValueError(f"unknown raw table {table}")
    cols = ", ".join(COLUMNS)
    with conn.cursor() as cur:
        # The identity column numbers rows in file order as COPY reads them -> source_line.
        cur.execute(
            "CREATE TEMP TABLE stg_raw (source_line bigint GENERATED ALWAYS AS IDENTITY, "
            + ", ".join(f"{c} text" for c in COLUMNS)
            + ") ON COMMIT DROP"
        )
        # HEADER MATCH (Postgres 15+) fails the load if the header names differ from our
        # columns, so a file with swapped or missing columns is rejected instead of loaded wrong.
        with cur.copy(f"COPY stg_raw ({cols}) FROM STDIN WITH (FORMAT csv, HEADER MATCH)") as copy:
            with open(path, "rb") as f:
                while chunk := f.read(1 << 20):
                    copy.write(chunk)
        cur.execute(
            f"INSERT INTO {table} (batch_id, source_line, {cols}) "
            f"SELECT %s, source_line, {cols} FROM stg_raw "
            "ON CONFLICT (batch_id, source_line) DO NOTHING",
            (batch_id,),
        )
        cur.execute("DROP TABLE stg_raw")
        cur.execute(f"SELECT count(*) FROM {table} WHERE batch_id = %s", (batch_id,))
        return cur.fetchone()[0]
