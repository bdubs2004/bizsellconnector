"""Command line: `dealfeed <command>`. Run `dealfeed -h` for the list."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from . import db, geocode, match, pipeline
from .config import get_settings
from .ingest import common, sba, states
from .services import accounts
from .services.search import SearchParams
from .services.targets import export_targets

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _stash(path: Path, source: str) -> Path:
    return common.stash_raw_file(path, source, get_settings().raw_dir, date.today())


def cmd_migrate(a, conn):
    _print({"applied": db.migrate(conn)})


def _state_list(arg: str | None) -> list[str]:
    if not arg or arg.lower() == "all":
        return list(states.SUPPORTED_STATES)
    return [s.strip().upper() for s in arg.split(",") if s.strip()]


def cmd_sources(a, conn):
    last = {r["source"]: r for r in conn.execute(
        "SELECT DISTINCT ON (source) source, finished_at, rows_loaded, notes FROM pipeline_runs "
        "WHERE status = 'ok' ORDER BY source, id DESC").fetchall()}
    for src in states.SOURCES.values():
        r = last.get(src.key)
        print(f"{src.state}  {src.key:8s} {src.name}")
        print(f"    {src.homepage}  ({src.license})")
        print(f"    last load: {r['finished_at']:%Y-%m-%d} · {r['rows_loaded']:,} rows" if r else "    last load: never")
        if src.notes:
            print(f"    note: {src.notes}")


def cmd_fetch(a, conn):
    for st in _state_list(a.state):
        src = states.get_source(st)
        dest_dir = get_settings().raw_dir / src.key / date.today().isoformat()
        print(f"Fetching {src.state} ({src.homepage}) -> {dest_dir}", file=sys.stderr)
        path = states.fetch(src.key, dest_dir, min_age_years=a.min_age, max_rows=a.max_rows)
        print(path)


def cmd_refresh(a, conn):
    """fetch + ingest for one or more states (then run `dealfeed build`)."""
    for st in _state_list(a.state):
        src = states.get_source(st)
        dest_dir = get_settings().raw_dir / src.key / date.today().isoformat()
        print(f"Fetching {src.state} -> {dest_dir}", file=sys.stderr)
        path = states.fetch(src.key, dest_dir, min_age_years=a.min_age, max_rows=a.max_rows)
        _print(states.load(conn, src.key, path, min_age_years=a.min_age))
    print("Now run: dealfeed build", file=sys.stderr)


def cmd_ingest(a, conn):
    if a.source == "state":
        if not a.state or not a.file:
            sys.exit("usage: dealfeed ingest state --state CO --file path/to/file.csv")
        src = states.get_source(a.state)
        _print(states.load(conn, src.key, _stash(Path(a.file), src.key), min_age_years=a.min_age))
    elif a.source == "ppp":
        for f in a.file_list:
            _print(sba.load_ppp(conn, _stash(Path(f), "ppp"), _state_list(a.states)))
    elif a.source in ("7a", "504"):
        for f in a.file_list:
            _print(sba.load_sba(conn, _stash(Path(f), f"sba_{a.source}"), a.source, _state_list(a.states)))


def cmd_inspect(a, conn):
    p = Path(a.file)
    _print(sba.inspect(p, a.source) if a.source in ("ppp", "7a", "504") else states.inspect(a.source, p))


def cmd_build(a, conn):
    _print(pipeline.build(conn, rematch=a.rematch, llm_classify_limit=a.llm_classify))


def cmd_compact(a, conn):
    conn.close()
    _print(db.compact())


def cmd_geocode(a, conn):
    _print(geocode.geocode_missing(conn, limit=a.limit))


def cmd_stats(a, conn):
    _print(pipeline.stats(conn))


def cmd_review(a, conn):
    if a.action == "list":
        rows = conn.execute(
            """SELECT r.id, r.score, r.llm_verdict, sr.source, sr.canon->>'legal_name' AS record_name,
                      sr.canon->>'address_line1' AS record_addr, sr.canon->>'city' AS record_city,
                      b.legal_name AS candidate_name, b.address_line1 AS candidate_addr, b.city AS candidate_city
                 FROM match_review r JOIN source_records sr ON sr.id = r.source_record_id
                 JOIN businesses b ON b.id = r.candidate_business
                WHERE r.status = 'pending' ORDER BY r.score DESC LIMIT %s""", (a.limit,)).fetchall()
        _print(rows)
    else:
        _print(match.resolve_review(conn, a.id, accept=(a.action == "accept")))


def cmd_export_top(a, conn):
    """Phase 1 deliverable: top N targets per industry to a CSV spreadsheet."""
    params = SearchParams(industry=a.industry, state=a.state, min_years_in_business=a.min_years,
                          counties=a.counties)
    out = export_targets(conn, user_id=None, max_rows=a.limit, search=params)
    Path(a.out).write_text(out["csv"])
    print(f"Wrote {out['rows']} rows to {a.out}", file=sys.stderr)


def cmd_user(a, conn):
    if a.action == "create":
        u = accounts.create_user(conn, a.email, a.plan)
        _print({"id": u.id, "email": u.email, "plan": u.plan.name})
    elif a.action == "key":
        u = accounts.get_user_by_email(conn, a.email)
        if not u:
            sys.exit(f"No user {a.email}; run `dealfeed user create {a.email}` first")
        key = accounts.create_api_key(conn, u, a.label)
        print(f"API key for {u.email} (shown once, store it now):\n{key}")
    elif a.action == "usage":
        u = accounts.get_user_by_email(conn, a.email)
        _print(accounts.usage_summary(conn, u) if u else {"error": "no such user"})


def cmd_demo(a, conn):
    """Load the fictional fixture data and build, so you can try the API/MCP without real files."""
    db.migrate(conn)
    pulled = datetime.now(timezone.utc)
    for key in states.STATE_SOURCE_KEYS:
        _print(states.load(conn, key, FIXTURES / f"{key}_sample.csv", pulled, min_age_years=0))
    _print(sba.load_ppp(conn, FIXTURES / "ppp_sample.csv", None, pulled))
    _print(sba.load_sba(conn, FIXTURES / "sba_7a_sample.csv", "7a", None, pulled))
    _print(pipeline.build(conn))
    _demo_coordinates(conn)
    u = accounts.get_user_by_email(conn, "demo@example.com") or accounts.create_user(conn, "demo@example.com", "fund")
    print(f"\nDemo user demo@example.com (fund plan). API key:\n{accounts.create_api_key(conn, u, 'demo')}")


# Approximate city centers, used ONLY by `dealfeed demo` so the map has something to show.
# Real data gets coordinates from `dealfeed geocode`.
_DEMO_CITY_CENTERS = {
    "DES MOINES": (41.59, -93.62), "AMES": (42.03, -93.62), "CEDAR RAPIDS": (41.98, -91.67),
    "DAVENPORT": (41.52, -90.58), "ANKENY": (41.73, -93.60), "MASON CITY": (43.15, -93.20),
    "IOWA CITY": (41.66, -91.53), "BETTENDORF": (41.55, -90.50), "DUBUQUE": (42.50, -90.66),
    "WATERLOO": (42.49, -92.34), "FORT DODGE": (42.50, -94.17),
    "DENVER": (39.74, -104.99), "COLORADO SPRINGS": (38.83, -104.82), "ALBANY": (42.65, -73.76),
    "BUFFALO": (42.89, -78.88), "HARTFORD": (41.76, -72.68), "NEW HAVEN": (41.31, -72.92),
    "PORTLAND": (45.52, -122.68), "EUGENE": (44.05, -123.09), "AUSTIN": (30.27, -97.74),
    "SAN ANTONIO": (29.42, -98.49),
}


def _demo_coordinates(conn):
    rows = conn.execute("SELECT id, city FROM businesses WHERE latitude IS NULL").fetchall()
    for i, r in enumerate(rows):
        c = _DEMO_CITY_CENTERS.get((r["city"] or "").upper())
        if c:
            conn.execute("UPDATE businesses SET latitude = %s, longitude = %s WHERE id = %s",
                         (c[0] + (i % 5 - 2) * 0.01, c[1] + (i % 3 - 1) * 0.012, r["id"]))
    conn.commit()


def cmd_serve_api(a, conn):
    import uvicorn
    conn.close()
    uvicorn.run("dealfeed.api.app:app", host=a.host, port=a.port, reload=a.reload)


def cmd_serve_mcp(a, conn):
    from . import mcp_server
    conn.close()
    mcp_server.main(a.transport, a.host, a.port)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="dealfeed", description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("migrate", help="create/upgrade database tables").set_defaults(fn=cmd_migrate)

    sub.add_parser("sources", help="list the state registries we load").set_defaults(fn=cmd_sources)

    for name, fn, help_ in (("fetch", cmd_fetch, "download state registry files"),
                            ("refresh", cmd_refresh, "download + load state registries")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("state", help=f"state code ({', '.join(states.SUPPORTED_STATES)}) or 'all'")
        p.add_argument("--min-age", type=int, default=None,
                       help="skip entities younger than N years (default DEALFEED_MIN_AGE_YEARS, 5)")
        p.add_argument("--max-rows", type=int, default=None, help="stop after N rows (for trying it out)")
        p.set_defaults(fn=fn)

    p = sub.add_parser("ingest", help="load a file you already downloaded")
    p.add_argument("source", choices=["state", "ppp", "7a", "504"])
    p.add_argument("file_list", nargs="*", metavar="FILE", help="CSV file(s) for ppp/7a/504")
    p.add_argument("--state", help="state code for `ingest state`")
    p.add_argument("--file", help="CSV/zip file for `ingest state`")
    p.add_argument("--min-age", type=int, default=None)
    p.add_argument("--states", default="all", help="SBA files: comma-separated states to keep (default all covered)")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("inspect", help="show a file's columns and how they map")
    p.add_argument("file")
    p.add_argument("--source", required=True,
                   help=f"ppp, 7a, 504, or a state source ({', '.join(states.STATE_SOURCE_KEYS)})")
    p.set_defaults(fn=cmd_inspect)

    p = sub.add_parser("build", help="match, classify, size and score")
    p.add_argument("--rematch", action="store_true", help="redo automatic matches (keeps manual decisions)")
    p.add_argument("--llm-classify", type=int, default=0, metavar="N",
                   help="classify up to N unclassified businesses with Claude (needs DEALFEED_LLM=on)")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("geocode", help="add lat/lon/county via the Census geocoder")
    p.add_argument("--limit", type=int)
    p.set_defaults(fn=cmd_geocode)

    sub.add_parser("stats", help="data metrics").set_defaults(fn=cmd_stats)
    sub.add_parser("compact", help="return free space to the OS (run after the first full load)").set_defaults(
        fn=cmd_compact)

    p = sub.add_parser("review", help="manual match review queue")
    p.add_argument("action", choices=["list", "accept", "reject"])
    p.add_argument("id", nargs="?", type=int)
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(fn=cmd_review)

    p = sub.add_parser("export-top", help="write top targets for an industry to CSV")
    p.add_argument("--industry", required=True)
    p.add_argument("--state", help="limit to one state")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--min-years", type=int)
    p.add_argument("--counties", nargs="*")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_export_top)

    p = sub.add_parser("user", help="manage customers and API keys")
    p.add_argument("action", choices=["create", "key", "usage"])
    p.add_argument("email")
    p.add_argument("--plan", default="free", choices=list(accounts.PLANS))
    p.add_argument("--label")
    p.set_defaults(fn=cmd_user)

    sub.add_parser("demo", help="load fictional sample data and build").set_defaults(fn=cmd_demo)

    p = sub.add_parser("serve", aliases=["serve-api"], help="run the web app + REST API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(fn=cmd_serve_api)

    p = sub.add_parser("serve-mcp", help="run the MCP server")
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8001)
    p.set_defaults(fn=cmd_serve_mcp)

    a = ap.parse_args(argv)
    # stdout is the MCP channel in stdio mode, so logs always go to stderr.
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, stream=sys.stderr)
    conn = db.connect()
    try:
        a.fn(a, conn)
    finally:
        if not conn.closed:
            conn.close()


if __name__ == "__main__":
    main()
