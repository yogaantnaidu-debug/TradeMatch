"""The dashboard renders real data, escapes input, and is read-only."""
import threading
import urllib.request

import psycopg
import pytest

import dashboard
from conftest import trade
from tradesync.db import connect


def test_empty_database_shows_hint(conn):
    assert "Run the pipeline first" in dashboard.render_page(conn)


def test_page_shows_recon_numbers(conn, run):
    run([trade("T1"), trade("T2", price="100")], [trade("T1"), trade("T2", price="150")])
    html = dashboard.render_page(conn)
    assert "Match rate" in html and "50.00%" in html
    assert "PRICE_MISMATCH" in html and "internal_rows_reconcile" in html


def test_trade_lookup_and_escaping(conn, run):
    run([trade("T1")], [])
    html = dashboard.render_page(conn, "T1")
    assert "MISSING_IN_CUSTODIAN" in html
    evil = dashboard.render_page(conn, "<script>alert(1)</script>")
    assert "<script>alert(1)" not in evil and "&lt;script&gt;" in evil


def test_read_only_session_refuses_writes(conn):
    # The handler sets this on every connection, so even a bug in the page cannot change data.
    conn.execute("SET default_transaction_read_only = on")
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute("CREATE TABLE should_fail (i int)")


def test_http_server_serves_pages(conn, run):
    run([trade("T1")], [trade("T1")])
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    server = dashboard.make_server("127.0.0.1", 0, lambda: connect(search_path=schema))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/?trade_id=t1").read().decode()
        assert "Trade T1" in body and "MATCHED" in body
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope")
    finally:
        server.shutdown()
