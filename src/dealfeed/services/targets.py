"""Saved-target pipeline and CSV export (scope 6.1)."""
from __future__ import annotations

import csv
import io
from datetime import date

import psycopg

from ..buy import annotate
from ..score import attach_breakdowns
from .buybox import get_buy_box
from .business import NotFound, _uuid
from .search import BASE_SELECT, SearchParams, search_rows, target_summary

STATUSES = ("new", "researching", "contacted", "passed")


def save_target(conn: psycopg.Connection, user_id, business_id: str, status: str = "new",
                notes: str | None = None) -> dict:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    bid = _uuid(business_id)
    if not conn.execute("SELECT 1 FROM businesses WHERE id = %s", (bid,)).fetchone():
        raise NotFound(f"No business with id {business_id!r}")
    row = conn.execute(
        """
        INSERT INTO saved_targets (user_id, business_id, status, notes) VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id, business_id) DO UPDATE
           SET status = EXCLUDED.status, notes = coalesce(EXCLUDED.notes, saved_targets.notes), updated_at = now()
        RETURNING status, notes, saved_at, updated_at
        """,
        (user_id, bid, status, notes),
    ).fetchone()
    conn.commit()
    return {"business_id": str(bid), **{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}}


def remove_target(conn: psycopg.Connection, user_id, business_id: str) -> bool:
    n = conn.execute("DELETE FROM saved_targets WHERE user_id = %s AND business_id = %s",
                     (user_id, _uuid(business_id))).rowcount
    conn.commit()
    return n > 0


def _saved_rows(conn: psycopg.Connection, user_id, status: str | None, limit: int | None = None) -> list[dict]:
    select = BASE_SELECT.replace(
        "SELECT b.*,",
        "SELECT b.*, t.status AS pipeline_status, t.notes AS pipeline_notes, t.saved_at, "
        "t.updated_at AS pipeline_updated,", 1)
    sql = f"{select} JOIN saved_targets t ON t.business_id = b.id WHERE t.user_id = %(user)s"
    args: dict = {"user": user_id}
    if status:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        sql += " AND t.status = %(status)s"
        args["status"] = status
    sql += " ORDER BY t.updated_at DESC"
    if limit:
        sql += " LIMIT %(limit)s"
        args["limit"] = limit
    rows = attach_breakdowns(conn, conn.execute(sql, args).fetchall())
    return annotate(rows, get_buy_box(conn, user_id))


def list_saved_targets(conn: psycopg.Connection, user_id, status: str | None = None, today: date | None = None) -> dict:
    today = today or date.today()
    rows = _saved_rows(conn, user_id, status)
    counts = conn.execute("SELECT status, count(*) AS n FROM saved_targets WHERE user_id = %s GROUP BY 1",
                          (user_id,)).fetchall()
    return {
        "targets": [
            {**target_summary(r, today), "pipeline_status": r["pipeline_status"], "notes": r["pipeline_notes"],
             "saved_at": r["saved_at"].isoformat(), "updated_at": r["pipeline_updated"].isoformat()}
            for r in rows
        ],
        "counts_by_status": {c["status"]: c["n"] for c in counts},
    }


EXPORT_COLUMNS = ["business_id", "name", "priority", "score", "buy_score", "est_price", "est_dscr", "industry", "naics_code", "city", "county", "state",
                  "years_in_business", "formation_date", "status", "employees_est", "payroll_est", "size_as_of",
                  "top_reasons", "pipeline_status", "notes", "state_registry_as_of"]


def _csv(rows: list[dict], today: date) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        t = target_summary(r, today)
        se = t["size_estimate"] or {}
        w.writerow({
            **t,
            "est_price": ("-".join(f"{v:,.0f}" for v in t["deal_estimate"]["price_range"])
                          if t["deal_estimate"] else None),
            "est_dscr": t["deal_estimate"]["dscr"] if t["deal_estimate"] else None,
            "employees_est": se.get("employees"), "payroll_est": se.get("annual_payroll"), "size_as_of": se.get("as_of"),
            "top_reasons": " · ".join(t["top_reasons"]),
            "pipeline_status": r.get("pipeline_status"), "notes": r.get("pipeline_notes"),
            "state_registry_as_of": t["data_as_of"]["state_registry"],
        })
    return buf.getvalue()


def export_targets(conn: psycopg.Connection, user_id, max_rows: int, search: SearchParams | None = None,
                   saved_status: str | None = None, saved: bool = False, today: date | None = None) -> dict:
    today = today or date.today()
    if saved:
        rows = _saved_rows(conn, user_id, saved_status, limit=max_rows)
    else:
        params = (search or SearchParams()).model_copy(update={"sort": "sell"} if max_rows > 1000 else {})
        rows, _, _ = search_rows(conn, params, limit=max_rows, offset=0, box=get_buy_box(conn, user_id))
    return {"rows": len(rows), "csv": _csv(rows, today)}
