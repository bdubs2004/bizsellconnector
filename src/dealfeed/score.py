"""v1 "likely to sell" score (scope 3.3): transparent rules, every point has a reason.

This is a guess until outcome data proves it. Weights live in WEIGHTS so they
can be tuned without touching logic; bump MODEL_VERSION when you change them.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

import psycopg

from .industries import BY_NAICS
from .states_meta import STATE_NAMES
from .normalize import looks_like_ra_service

MODEL_VERSION = "rules-v1.1"

WEIGHTS = {
    "base": 30,
    "age_30_plus": 35,
    "age_20_29": 25,
    "age_10_19": 10,
    "age_under_5": -30,
    "individual_agent": 5,
    "long_tenure_agent": 10,        # same RA seen for 15+ years (needs pull history)
    "size_sweet_spot": 10,
    "size_too_small": -10,
    "size_too_large": -10,
    "eta_demand_high": 5,
    "recent_sba_loan": -40,         # 7(a)/504 within 3 years
    "change_of_ownership": -45,     # 7(a)/504 flagged "Change of Ownership" within 6 years
    "older_sba_loan": -15,          # 7(a)/504 3-6 years ago
    "inactive": -40,
    "delinquent": -20,              # behind on state filings / franchise tax
    "stale_website": 10,            # set by website enrichment (Phase 4)
    "no_website": 8,
}

SWEET_SPOT_EMPLOYEES = (5, 50)
SWEET_SPOT_PAYROLL = (250_000, 3_000_000)


@dataclass
class Fired:
    signal: str
    points: int
    reason: str
    value: dict


def _years_between(a: date, b: date) -> int:
    return b.year - a.year - ((b.month, b.day) < (a.month, a.day))


def _fmt_range(lo, hi, money=False) -> str:
    if lo is None or hi is None:
        return "?"
    if money:
        f = lambda v: f"${float(v) / 1e6:.1f}M" if v >= 1e6 else f"${float(v) / 1e3:.0f}k"  # noqa: E731
        return f"{f(lo)}-{f(hi)}"
    return f"{lo}-{hi}"


def evaluate(biz: dict, size: dict | None, loans: list[dict], people: list[dict], extra_signals: dict,
             today: date) -> tuple[int, list[Fired]]:
    fired: list[Fired] = []

    # Age
    fd = biz.get("formation_date")
    if fd:
        age = _years_between(fd, today)
        v = {"formation_date": fd.isoformat(), "years": age}
        if age >= 30:
            fired.append(Fired("business_age", WEIGHTS["age_30_plus"], f"Operating since {fd.year} ({age} yrs)", v))
        elif age >= 20:
            fired.append(Fired("business_age", WEIGHTS["age_20_29"], f"Operating since {fd.year} ({age} yrs)", v))
        elif age >= 10:
            fired.append(Fired("business_age", WEIGHTS["age_10_19"], f"Operating since {fd.year} ({age} yrs)", v))
        elif age < 5:
            fired.append(Fired("recent_formation", WEIGHTS["age_under_5"], f"Formed {fd.year}: likely too young to sell", v))
        else:
            fired.append(Fired("business_age", 0, f"Operating since {fd.year} ({age} yrs)", v))
    else:
        fired.append(Fired("business_age", 0, "Formation date unknown (not in a state registry we load)", {}))

    # Status
    registry = STATE_NAMES.get(biz.get("state") or "", "state")
    if biz.get("status") == "inactive":
        fired.append(Fired("inactive", WEIGHTS["inactive"], f"No longer in the latest {registry} registry pull", {}))
    elif biz.get("status") == "delinquent":
        since = biz.get("status_since")
        fired.append(Fired("delinquent", WEIGHTS["delinquent"],
                           f"Behind on {registry} filings" + (f" since {since.year}" if since else ""),
                           {"since": since.isoformat() if since else None}))

    # Registered agent
    ra = [p for p in people if p["role"] == "registered agent"]
    if ra:
        current = max(ra, key=lambda p: p["last_seen"] or date.min)
        if not looks_like_ra_service(current["name"]):
            fired.append(Fired("individual_agent", WEIGHTS["individual_agent"],
                               "Registered agent is an individual (likely owner-run)", {"name": current["name"]}))
        if current["first_seen"] and current["last_seen"]:
            span = _years_between(current["first_seen"], current["last_seen"])
            if span >= 15:
                fired.append(Fired("long_tenure_agent", WEIGHTS["long_tenure_agent"],
                                   f"Same registered agent for {span}+ years", {"years": span}))

    # Size
    if size:
        el, eh = size.get("employees_low"), size.get("employees_high")
        pl_, ph = size.get("payroll_low"), size.get("payroll_high")
        as_of = size.get("as_of")
        yr = f"PPP {as_of.year}" if as_of else "PPP"
        v = {"employees": [el, eh], "payroll": [float(pl_) if pl_ else None, float(ph) if ph else None], "as_of": str(as_of)}
        emp_mid = (el + eh) / 2 if el is not None and eh is not None else None
        pay_mid = (float(pl_) + float(ph)) / 2 if pl_ is not None and ph is not None else None
        label = f"Est. {_fmt_range(el, eh)} employees ({yr})" if emp_mid is not None else \
            f"Est. payroll {_fmt_range(pl_, ph, money=True)} ({yr})"
        if (emp_mid is not None and emp_mid < 3) or (emp_mid is None and pay_mid is not None and pay_mid < 120_000):
            fired.append(Fired("size", WEIGHTS["size_too_small"], f"{label}: likely too small for SBA debt", v))
        elif (emp_mid is not None and emp_mid > 100) or (pay_mid is not None and pay_mid > 8_000_000):
            fired.append(Fired("size", WEIGHTS["size_too_large"], f"{label}: likely above typical ETA range", v))
        elif (emp_mid is not None and SWEET_SPOT_EMPLOYEES[0] <= emp_mid <= SWEET_SPOT_EMPLOYEES[1]) or \
             (pay_mid is not None and SWEET_SPOT_PAYROLL[0] <= pay_mid <= SWEET_SPOT_PAYROLL[1]):
            fired.append(Fired("size", WEIGHTS["size_sweet_spot"], f"{label}: in the ETA sweet spot", v))
        else:
            fired.append(Fired("size", 0, label, v))
    else:
        fired.append(Fired("size", 0, "No size data (no PPP loan on record)", {}))

    # Industry demand
    ind = BY_NAICS.get(biz.get("naics_code") or "")
    if ind and ind.eta_demand == "high":
        fired.append(Fired("eta_demand", WEIGHTS["eta_demand_high"], f"{ind.label}: strong searcher demand",
                           {"naics": biz["naics_code"]}))

    # SBA acquisition loans
    acq = [l for l in loans if l["program"] in ("7a", "504") and l["approval_date"]]
    if acq:
        latest = max(acq, key=lambda l: l["approval_date"])
        yrs = _years_between(latest["approval_date"], today)
        label = "7(a)" if latest["program"] == "7a" else "504"
        v = {"program": latest["program"], "approval_date": latest["approval_date"].isoformat(),
             "business_age_desc": latest.get("business_age_desc")}
        cho = "change of ownership" in (latest.get("business_age_desc") or "").lower()
        if cho and yrs < 6:
            fired.append(Fired("recent_acquisition", WEIGHTS["change_of_ownership"],
                               f"SBA {label} loan {latest['approval_date'].year} marked 'Change of Ownership': recently acquired", v))
        elif yrs < 3:
            fired.append(Fired("recent_sba_loan", WEIGHTS["recent_sba_loan"],
                               f"SBA {label} loan {latest['approval_date'].year}: may have recently changed hands", v))
        elif yrs < 6:
            fired.append(Fired("recent_sba_loan", WEIGHTS["older_sba_loan"],
                               f"SBA {label} loan {latest['approval_date'].year}", v))
        else:
            fired.append(Fired("sba_loan_history", 0, f"Last SBA {label} loan {latest['approval_date'].year}", v))
    else:
        fired.append(Fired("sba_loan_history", 0, "No SBA 7(a)/504 loan on record", {}))

    # Website enrichment (Phase 4 writes these into `signals`)
    if "stale_website" in extra_signals:
        fired.append(Fired("stale_website", WEIGHTS["stale_website"],
                           extra_signals["stale_website"].get("reason", "Website looks dated"), extra_signals["stale_website"]))
    elif "no_website" in extra_signals:
        fired.append(Fired("no_website", WEIGHTS["no_website"], "No website found", extra_signals["no_website"]))

    total = WEIGHTS["base"] + sum(f.points for f in fired)
    return max(0, min(100, total)), fired


ENRICHMENT_SIGNALS = ("stale_website", "no_website")
CHUNK = 20_000

_SELECT = """
SELECT b.id, b.formation_date, b.status, b.status_since, b.naics_code, b.state,
       se.employees_low, se.employees_high, se.payroll_low, se.payroll_high, se.as_of,
       acq.program, acq.approval_date, acq.business_age_desc,
       ra.name AS ra_name, ra.first_seen AS ra_first_seen, ra.last_seen AS ra_last_seen
  FROM businesses b
  LEFT JOIN LATERAL (SELECT employees_low, employees_high, payroll_low, payroll_high, as_of FROM size_estimates x
                      WHERE x.business_id = b.id ORDER BY as_of DESC NULLS LAST LIMIT 1) se ON true
  LEFT JOIN LATERAL (SELECT program, approval_date, business_age_desc FROM sba_loans l
                      WHERE l.business_id = b.id AND l.program IN ('7a', '504') AND l.approval_date IS NOT NULL
                      ORDER BY approval_date DESC LIMIT 1) acq ON true
  LEFT JOIN LATERAL (SELECT name, first_seen, last_seen FROM business_people p
                      WHERE p.business_id = b.id AND p.role = 'registered agent'
                      ORDER BY last_seen DESC NULLS LAST LIMIT 1) ra ON true
"""
_CHUNK_SQL = _SELECT + " WHERE b.id > %(after)s ORDER BY b.id LIMIT %(limit)s"
_IDS_SQL = _SELECT + " WHERE b.id = ANY(%(ids)s)"


def _evaluate_rows(conn: psycopg.Connection, rows: list[dict], today: date) -> dict:
    """{business_id: (score, [Fired, ...] sorted by impact)} for rows from _SELECT."""
    ids = [r["id"] for r in rows]
    extra: dict = {}
    if ids:
        for e in conn.execute("SELECT business_id, signal_type, value FROM signals "
                              "WHERE business_id = ANY(%s) AND signal_type = ANY(%s)",
                              (ids, list(ENRICHMENT_SIGNALS))).fetchall():
            extra.setdefault(e["business_id"], {})[e["signal_type"]] = e["value"] or {}
    out = {}
    for r in rows:
        size = r if r["employees_low"] is not None or r["payroll_low"] is not None else None
        loans = [{"program": r["program"], "approval_date": r["approval_date"],
                  "business_age_desc": r["business_age_desc"]}] if r["program"] else []
        people = [{"name": r["ra_name"], "role": "registered agent", "first_seen": r["ra_first_seen"],
                   "last_seen": r["ra_last_seen"]}] if r["ra_name"] else []
        score, fired = evaluate(r, size, loans, people, extra.get(r["id"], {}), today)
        out[r["id"]] = (score, sorted(fired, key=lambda f: -abs(f.points)))
    return out


def explain(conn: psycopg.Connection, ids: list, today: date | None = None) -> dict:
    """Recompute score + point-by-point breakdown for these businesses. The breakdown isn't stored
    (that was ~40% of the database); it's the same rules over the same data, so it matches the stored
    score as of the last build."""
    if not ids:
        return {}
    rows = conn.execute(_IDS_SQL, {"ids": list(ids)}).fetchall()
    return {bid: {"score": sc, "breakdown": [{"signal": f.signal, "points": f.points, "reason": f.reason}
                                             for f in fired]}
            for bid, (sc, fired) in _evaluate_rows(conn, rows, today or date.today()).items()}


def last_scored(conn: psycopg.Connection) -> dict | None:
    return conn.execute("SELECT finished_at AS scored_at, notes AS model_version FROM pipeline_runs "
                        "WHERE source = 'score' AND status = 'ok' ORDER BY id DESC LIMIT 1").fetchone()


def attach_breakdowns(conn: psycopg.Connection, rows: list[dict], today: date | None = None) -> list[dict]:
    """Add `breakdown`, `scored_at`, `model_version` to business rows (search results, profiles)."""
    ex = explain(conn, [r["id"] for r in rows], today)
    meta = last_scored(conn) or {}
    for r in rows:
        r["breakdown"] = ex.get(r["id"], {}).get("breakdown", [])
        r["scored_at"] = meta.get("scored_at")
        r["model_version"] = meta.get("model_version", MODEL_VERSION)
    return rows


def score_all(conn: psycopg.Connection, today: date | None = None) -> dict:
    """Score every business in keyset-paged chunks so memory stays flat at millions of rows. Only the
    number is stored (businesses.score, which search sorts on); explanations are recomputed on view."""
    today = today or date.today()
    run = conn.execute("INSERT INTO pipeline_runs (source, notes) VALUES ('score', %s) RETURNING id",
                       (MODEL_VERSION,)).fetchone()["id"]
    conn.commit()
    after = uuid.UUID(int=0)
    total = 0
    while True:
        rows = conn.execute(_CHUNK_SQL, {"after": after, "limit": CHUNK}).fetchall()
        if not rows:
            break
        scored = _evaluate_rows(conn, rows, today)
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _scores (business_id UUID, score INT) ON COMMIT DROP")
        with conn.cursor().copy("COPY _scores (business_id, score) FROM STDIN") as cp:
            for bid, (sc, _) in scored.items():
                cp.write_row((bid, sc))
        conn.execute("UPDATE businesses b SET score = t.score FROM _scores t "
                     "WHERE t.business_id = b.id AND b.score IS DISTINCT FROM t.score")
        conn.commit()
        total += len(rows)
        after = rows[-1]["id"]
    conn.execute("UPDATE pipeline_runs SET status = 'ok', finished_at = now(), rows_loaded = %s WHERE id = %s",
                 (total, run))
    conn.commit()
    return {"scored": total, "model_version": MODEL_VERSION}
