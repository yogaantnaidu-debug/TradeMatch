"""Database helpers: connect and run .sql files."""
import re
from pathlib import Path

import psycopg

from tradesync.config import ROOT, database_url

SQL_DIR = ROOT / "sql"


def connect(url: str | None = None, search_path: str | None = None) -> psycopg.Connection:
    # search_path lets tests run in a throwaway schema without touching the real tables.
    options = f"-c search_path={search_path}" if search_path else ""
    # autocommit=True means: no hidden open transaction. Each `with conn.transaction():`
    # block is then a real BEGIN ... COMMIT, which is what "one transaction per stage" needs.
    return psycopg.connect(url or database_url(), options=options, autocommit=True)


def split_statements(sql_text: str) -> list[str]:
    """Split a .sql file on semicolons that end a line.

    psycopg sends parameterised queries one statement at a time, so a file
    with several statements must be split. Our SQL files never put a ';'
    at the end of a line inside a string or comment, so this simple rule is enough.
    """
    parts = re.split(r";\s*$", sql_text, flags=re.MULTILINE)
    statements = []
    for part in parts:
        # Drop comment-only chunks (e.g. the trailing comment at the end of a file).
        code = "\n".join(l for l in part.splitlines() if not l.strip().startswith("--")).strip()
        if code:
            statements.append(part.strip())
    return statements


def run_sql_file(conn: psycopg.Connection, name: str, params: dict | None = None,
                 replacements: dict[str, str] | None = None) -> None:
    """Run every statement in sql/<name>.

    `replacements` swaps tokens like __RAW_TABLE__ for table names. Table names
    cannot be bind parameters in SQL, so callers pass only hard-coded, whitelisted names.
    """
    text = (SQL_DIR / name).read_text(encoding="utf-8")
    for token, value in (replacements or {}).items():
        text = text.replace(token, value)
    with conn.cursor() as cur:
        for stmt in split_statements(text):
            cur.execute(stmt, params)


def scalar(conn: psycopg.Connection, query: str, params=None):
    with conn.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
        return row[0] if row else None
