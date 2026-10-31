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


def test_open_breaks_worklist_lists_filters_and_links(conn, run):
    run([trade("T1", price="100"), trade("T2", quantity="100"), trade("T3")],
        [trade("T1", price="150"), trade("T2", quantity="90"), trade("T4")])
    html = dashboard.render_page(conn)
    assert "Open breaks (worklist)" in html and "Showing 4 of 4 open breaks" in html
    assert '<a href="/?trade_id=T1">T1</a>' in html          # each break links to its lookup
    only_qty = dashboard.render_open_breaks(conn, "QUANTITY_MISMATCH")
    assert "Showing 1 of 1" in only_qty and "T2" in only_qty and "T1" not in only_qty
    # An unknown type is ignored (whitelist), never passed into SQL.
    assert "Showing 4 of 4" in dashboard.render_open_breaks(conn, "x' OR 1=1 --")


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
