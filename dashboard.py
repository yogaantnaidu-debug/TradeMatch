"""Read-only demo dashboard for TradeSync.

    python dashboard.py            -> http://127.0.0.1:8000

A thin viewer over the tables and views the pipeline already builds. It contains
no business logic: every number on the page comes from a SELECT on recon_summary,
break_ageing, pipeline_runs, dq_results, etc. Built on the standard library
(http.server) so the project gains no new dependency. In production this role
would be played by a BI tool (Tableau / Power BI) pointed at the same views.
"""
import argparse
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from tradesync.db import connect

BREAK_TYPES = ["PRICE_MISMATCH", "QUANTITY_MISMATCH", "MISSING_IN_CUSTODIAN", "MISSING_IN_INTERNAL"]


def fetch(conn, sql: str, params=None) -> tuple[list[str], list[tuple]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [d.name for d in cur.description], cur.fetchall()


def esc(value) -> str:
    # Every value is escaped: trade ids and raw rows come from files we don't control.
    if isinstance(value, list):   # Postgres text[] arrives as a Python list
        value = ", ".join(map(str, value)) or "-"
    elif isinstance(value, dict):  # jsonb stage timings -> "load_raw 436, clean 3180"
        value = ", ".join(f"{k} {v}" for k, v in value.items())
    return html.escape("" if value is None else str(value))


def table(cols: list[str], rows: list[tuple], empty: str = "No rows.") -> str:
    if not rows:
        return f'<p class="muted">{esc(empty)}</p>'
    head = "".join(f"<th>{esc(c)}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{esc(v)}</td>" for v in r) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def tile(label: str, value) -> str:
    return f'<div class="tile"><div class="label">{esc(label)}</div><div class="value">{esc(value)}</div></div>'


def section(title: str, inner: str) -> str:
    return f"<section><h2>{esc(title)}</h2>{inner}</section>"


def render_trade(conn, trade_id: str) -> str:
    """Everything we know about one trade, across all layers. trade_id is a bind parameter."""
    parts = []
    for title, sql in [
        ("Internal (clean)", "SELECT batch_id, isin, side, quantity, price, currency, trade_date, counterparty "
                             "FROM clean_internal WHERE trade_id = %s ORDER BY batch_id"),
        ("Custodian (clean)", "SELECT batch_id, isin, side, quantity, price, currency, trade_date, counterparty "
                              "FROM clean_custodian WHERE trade_id = %s ORDER BY batch_id"),
        ("Match result", "SELECT batch_id, status, break_types FROM match_results "
                         "WHERE trade_id = %s ORDER BY batch_id"),
        ("Breaks", "SELECT break_type, internal_value, custodian_value, difference, status, "
                   "first_detected_date, resolved_date FROM breaks WHERE trade_id = %s"),
        ("Rejected rows", "SELECT batch_id, source, source_line, reason FROM rejected_records "
                          "WHERE trade_id = %s ORDER BY batch_id, source, source_line"),
    ]:
        cols, rows = fetch(conn, sql, (trade_id,))
        parts.append(f"<h3>{esc(title)}</h3>" + table(cols, rows, "Not found."))
    return section(f"Trade {trade_id}", "".join(parts))


def render_page(conn, trade_id: str | None = None) -> str:
    if fetch(conn, "SELECT to_regclass('pipeline_runs')")[1][0][0] is None:
        return page('<p class="muted">No data yet. Run the pipeline first: '
                    "<code>python -m tradesync.pipeline</code></p>")

    _, latest = fetch(conn, "SELECT batch_id, as_of_date, total_trades, matched, broken, match_rate_pct, "
                            + ", ".join(t.lower() for t in BREAK_TYPES)
                            + " FROM recon_summary ORDER BY as_of_date DESC, batch_id DESC LIMIT 1")
    out = []
    if latest:
        b = latest[0]
        out.append(f'<p class="muted">Latest batch <b>{esc(b[0])}</b> (as of {esc(b[1])})</p>')
        out.append('<div class="tiles">' + tile("Trades", f"{b[2]:,}") + tile("Matched", f"{b[3]:,}")
                   + tile("Breaks", f"{b[4]:,}") + tile("Match rate", f"{b[5]}%") + "</div>")
        # Bars scaled to the largest type, so the biggest one fills the row.
        counts = dict(zip(BREAK_TYPES, b[6:10]))
        top = max(counts.values()) or 1
        bars = "".join(
            f'<div class="bar-row"><span class="bar-label">{esc(t)}</span>'
            f'<span class="bar"><span style="width:{100 * n / top:.1f}%"></span></span>'
            f'<span class="bar-num">{n:,}</span></div>' for t, n in counts.items())
        out.append(section("Breaks by type (latest batch)", bars))

    out.append(section("Open break ageing", table(*fetch(conn, """
        SELECT break_type,
               count(*) FILTER (WHERE age_bucket = '0-1') AS "0-1 days",
               count(*) FILTER (WHERE age_bucket = '2-5') AS "2-5 days",
               count(*) FILTER (WHERE age_bucket = '6+')  AS "6+ days",
               count(*) AS total
        FROM break_ageing GROUP BY break_type ORDER BY break_type"""), "No open breaks.")))

    out.append(section("Recent pipeline runs", table(*fetch(conn, """
        SELECT run_id, batch_id, status, failed_stage, rows_in_internal AS raw_int,
               rows_clean_internal AS clean_int, rows_rejected AS rejected, matched, broken,
               stage_timings_ms AS "stage ms", round(extract(epoch FROM finished_at - started_at)::numeric, 2) AS seconds
        FROM pipeline_runs ORDER BY run_id DESC LIMIT 10"""))))

    out.append(section("Data quality (latest run)", table(*fetch(conn, """
        SELECT check_name, CASE WHEN passed THEN 'PASS' ELSE 'FAIL' END AS result, observed, expected
        FROM dq_results WHERE run_id = (SELECT max(run_id) FROM dq_results) ORDER BY check_name"""))))

    out.append(section("Rejected rows by reason (latest batch)", table(*fetch(conn, """
        SELECT reason,
               count(*) FILTER (WHERE source = 'INTERNAL')  AS internal,
               count(*) FILTER (WHERE source = 'CUSTODIAN') AS custodian
        FROM rejected_records
        WHERE batch_id = (SELECT batch_id FROM pipeline_runs WHERE status = 'SUCCESS'
                          ORDER BY run_id DESC LIMIT 1)
        GROUP BY reason ORDER BY reason"""))))

    _, sample = fetch(conn, "SELECT trade_id FROM breaks WHERE status = 'OPEN' ORDER BY trade_id LIMIT 1")
    hint = f"e.g. {sample[0][0]}" if sample else "trade id"
    search = (f'<form method="get" action="/"><input name="trade_id" placeholder="{esc(hint)}" '
              f'value="{esc(trade_id or "")}"><button>Look up trade</button></form>')
    if trade_id:
        search += render_trade(conn, trade_id)
    return page(section("Trade lookup", search) + "".join(out))


def page(body: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TradeSync Dashboard</title><style>
:root {{ --bg:#f6f7f9; --card:#fff; --text:#1d2330; --muted:#677084; --line:#dde1e8; --accent:#2f5bd3; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#14171d; --card:#1d222b; --text:#e6e9ef; --muted:#9aa3b5; --line:#2e3542; --accent:#7ea1ff; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.45 system-ui, sans-serif; }}
main {{ max-width:1100px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:15px; margin:0 0 12px; }} h3 {{ font-size:13px; margin:16px 0 6px; }}
section {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin-top:16px; }}
.muted {{ color:var(--muted); }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:12px; margin-top:12px; }}
.tile {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px; }}
.tile .label {{ color:var(--muted); font-size:12px; }} .tile .value {{ font-size:24px; font-weight:600; }}
.bar-row {{ display:grid; grid-template-columns:190px 1fr 70px; gap:10px; align-items:center; margin:6px 0; }}
.bar {{ background:var(--line); border-radius:4px; height:12px; overflow:hidden; }}
.bar span {{ display:block; height:100%; background:var(--accent); }}
.bar-num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.scroll {{ overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }}
th, td {{ text-align:left; padding:6px 10px; border-bottom:1px solid var(--line); white-space:nowrap; }}
th {{ color:var(--muted); font-weight:500; font-size:12px; }}
form {{ display:flex; gap:8px; flex-wrap:wrap; }}
input {{ flex:1; min-width:180px; padding:8px 10px; border:1px solid var(--line); border-radius:6px;
        background:var(--bg); color:var(--text); }}
button {{ padding:8px 14px; border:0; border-radius:6px; background:var(--accent); color:#fff; cursor:pointer; }}
@media (max-width:600px) {{ .bar-row {{ grid-template-columns:1fr 60px; }} .bar-label {{ grid-column:1 / -1; }} }}
</style></head><body><main>
<h1>TradeSync</h1><p class="muted">Read-only view of the reconciliation tables. Reload after a pipeline run.</p>
{body}</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path != "/":
            self.send_error(404)
            return
        trade_id = (parse_qs(url.query).get("trade_id") or [""])[0].strip().upper() or None
        try:
            with self.server.connect_fn() as conn:
                # Belt and braces: the DB itself refuses writes from this connection.
                conn.execute("SET default_transaction_read_only = on")
                body, status = render_page(conn, trade_id), 200
        except Exception as exc:  # show the error instead of a blank page during a demo
            body, status = page(f'<p class="muted">Database error: {esc(exc)}</p>'), 500
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_server(host: str, port: int, connect_fn=connect) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.connect_fn = connect_fn   # tests pass a connection to their own schema
    return server


def main() -> None:
    ap = argparse.ArgumentParser(description="TradeSync read-only dashboard")
    # 127.0.0.1 by default: reachable only from this machine, not the whole network.
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    server = make_server(args.host, args.port)
    print(f"Dashboard on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
