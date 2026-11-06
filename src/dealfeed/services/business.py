"""get_business and explain_score (scope 6.1). Every field carries its source and date."""
from __future__ import annotations

import uuid
from datetime import date

import psycopg

from ..industries import BY_NAICS
from ..score import MODEL_VERSION, WEIGHTS
from ..ingest.states import SOURCES as STATE_SOURCES
from .search import BASE_SELECT, DATA_CAVEAT, size_summary, target_summary

SOURCE_LABELS = {
    **{k: src.attribution for k, src in STATE_SOURCES.items()},
    "ppp": "SBA Paycheck Protection Program loan data (data.sba.gov)",
    "sba_7a": "SBA 7(a) loan data (data.sba.gov)",
    "sba_504": "SBA 504 loan data (data.sba.gov)",
}


class NotFound(Exception):
    pass


def _uuid(business_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(business_id))
    except ValueError:
        raise NotFound(f"No business with id {business_id!r}")


def get_business(conn: psycopg.Connection, business_id: str, user_id=None, today: date | None = None) -> dict:
    today = today or date.today()
    bid = _uuid(business_id)
    row = conn.execute(f"{BASE_SELECT} WHERE b.id = %s", (bid,)).fetchone()
    if not row:
        raise NotFound(f"No business with id {business_id!r}")
    summary = target_summary(row, today)

    sources = conn.execute(
        "SELECT source, source_id, pulled_at, first_seen_at, match_method, match_score FROM source_records "
        "WHERE business_id = %s ORDER BY source, pulled_at DESC", (bid,)
    ).fetchall()
    loans = conn.execute(
        "SELECT program, amount, approval_date, lender, status, jobs_reported, business_age_desc "
        "FROM sba_loans WHERE business_id = %s ORDER BY approval_date DESC NULLS LAST", (bid,)
    ).fetchall()
    people = conn.execute(
        "SELECT name, role, first_seen, last_seen, source FROM business_people WHERE business_id = %s "
        "ORDER BY last_seen DESC NULLS LAST", (bid,)
    ).fetchall()
    sizes = conn.execute("SELECT * FROM size_estimates WHERE business_id = %s ORDER BY as_of DESC NULLS LAST",
                         (bid,)).fetchall()
    saved = None
    if user_id:
        saved = conn.execute("SELECT status, notes, saved_at FROM saved_targets WHERE user_id = %s AND business_id = %s",
                             (user_id, bid)).fetchone()

    ind = BY_NAICS.get(row.get("naics_code") or "")
    return {
        **summary,
        "entity_type": row.get("entity_type"),
        "registry": SOURCE_LABELS.get(row.get("primary_source"), row.get("primary_source")),
        "dba_names": row.get("dba_names") or [],
        "address": {
            "line1": row.get("address_line1"), "city": row.get("city"), "state": row.get("state"),
            "zip": row.get("zip"), "county": row.get("county"),
            "kind": {"service_of_process": "Address for service of process (no business location on file)",
                     "location": "Business location", "principal": "Principal office"}.get(row.get("address_kind") or "",
                                                                                          "Address on file"),
            "source": SOURCE_LABELS.get(row.get("primary_source"), row.get("primary_source")),
        },
        "industry_detail": {
            "naics_code": row.get("naics_code"), "label": ind.label if ind else None,
            "source": row.get("naics_source"), "confidence": row.get("naics_confidence"),
        },
        "score_breakdown": row.get("breakdown"),
        "size_estimates": [
            size_summary({**s, "se_method": s["method"], "se_as_of": s["as_of"]}) for s in sizes
        ],
        "sba_loans": [
            {"program": l["program"], "amount": float(l["amount"]) if l["amount"] is not None else None,
             "approval_date": l["approval_date"].isoformat() if l["approval_date"] else None,
             "lender": l["lender"], "status": l["status"], "jobs_reported": l["jobs_reported"],
             "business_age_at_loan": l["business_age_desc"]}
            for l in loans
        ],
        "people_from_public_filings": [
            {"name": p["name"], "role": p["role"], "source": SOURCE_LABELS.get(p["source"], p["source"]),
             "first_seen": p["first_seen"].isoformat() if p["first_seen"] else None,
             "last_seen": p["last_seen"].isoformat() if p["last_seen"] else None}
            for p in people
        ],
        "sources": [
            {"source": SOURCE_LABELS.get(s["source"], s["source"]), "record_id": s["source_id"],
             "pulled_at": s["pulled_at"].date().isoformat(), "first_seen": s["first_seen_at"].date().isoformat(),
             "match_method": s["match_method"], "match_score": s["match_score"]}
            for s in sources
        ],
        "your_pipeline": dict(saved) if saved else None,
        "notes": [DATA_CAVEAT],
    }


def explain_score(conn: psycopg.Connection, business_id: str) -> dict:
    bid = _uuid(business_id)
    row = conn.execute(
        "SELECT b.legal_name, s.score, s.breakdown, s.model_version, s.scored_at FROM businesses b "
        "LEFT JOIN scores s ON s.business_id = b.id WHERE b.id = %s", (bid,)
    ).fetchone()
    if not row:
        raise NotFound(f"No business with id {business_id!r}")
    if row["score"] is None:
        return {"business_id": str(bid), "name": row["legal_name"], "score": None,
                "explanation": "This business hasn't been scored yet."}
    bd = row["breakdown"]
    plus = [b for b in bd if b["points"] > 0]
    minus = [b for b in bd if b["points"] < 0]
    info = [b for b in bd if b["points"] == 0]
    lines = [f"{row['legal_name']} scores {row['score']}/100 (model {row['model_version']}). "
             f"Every business starts at {WEIGHTS['base']}."]
    lines += [f"+{b['points']}: {b['reason']}" for b in plus]
    lines += [f"{b['points']}: {b['reason']}" for b in minus]
    lines += [f"(no effect) {b['reason']}" for b in info]
    return {
        "business_id": str(bid),
        "name": row["legal_name"],
        "score": row["score"],
        "base_points": WEIGHTS["base"],
        "breakdown": bd,
        "explanation": "\n".join(lines),
        "model_version": row["model_version"],
        "scored_at": row["scored_at"].isoformat(),
        "how_scoring_works": (
            "A transparent rules-based score. Older businesses, owner-operated signals, and a size that fits "
            "SBA-financed buyers raise it; recent SBA acquisition loans, very young businesses, and inactive "
            "filings lower it. It's a starting guess that will be retrained on real outcomes."
        ),
        "current_model": MODEL_VERSION,
    }
