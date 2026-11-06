"""v1 "likely to sell" score (scope 3.3): transparent rules, every point has a reason.

This is a guess until outcome data proves it. Weights live in WEIGHTS so they
can be tuned without touching logic; bump MODEL_VERSION when you change them.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

import psycopg

from .industries import BY_NAICS
from .normalize import looks_like_ra_service

MODEL_VERSION = "rules-v1.0"

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
        fired.append(Fired("business_age", 0, "Formation date unknown (not an Iowa SOS entity)", {}))

    # Status
    if biz.get("status") == "inactive":
        fired.append(Fired("inactive", WEIGHTS["inactive"], "Not in the latest Iowa SOS active-entity list", {}))

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


def score_all(conn: psycopg.Connection, today: date | None = None) -> dict:
    today = today or date.today()
    now = datetime.now(timezone.utc)
    businesses = conn.execute(
        "SELECT id, formation_date, status, naics_code FROM businesses"
    ).fetchall()
    sizes = {r["business_id"]: r for r in conn.execute(
        "SELECT DISTINCT ON (business_id) * FROM size_estimates ORDER BY business_id, as_of DESC NULLS LAST"
    ).fetchall()}
    loans: dict = {}
    for r in conn.execute(
        "SELECT business_id, program, approval_date, business_age_desc FROM sba_loans WHERE business_id IS NOT NULL"
    ).fetchall():
        loans.setdefault(r["business_id"], []).append(r)
    people: dict = {}
    for r in conn.execute("SELECT business_id, name, role, first_seen, last_seen FROM business_people").fetchall():
        people.setdefault(r["business_id"], []).append(r)
    extra: dict = {}
    for r in conn.execute("SELECT business_id, signal_type, value FROM signals WHERE signal_type = ANY(%s)",
                          (list(ENRICHMENT_SIGNALS),)).fetchall():
        extra.setdefault(r["business_id"], {})[r["signal_type"]] = r["value"] or {}

    score_rows, signal_rows = [], []
    for b in businesses:
        bid = b["id"]
        total, fired = evaluate(b, sizes.get(bid), loans.get(bid, []), people.get(bid, []), extra.get(bid, {}), today)
        ordered = sorted(fired, key=lambda f: -abs(f.points))
        breakdown = [{"signal": f.signal, "points": f.points, "reason": f.reason} for f in ordered]
        score_rows.append((bid, total, [f.reason for f in ordered], json.dumps(breakdown), MODEL_VERSION, now))
        for f in fired:
            if f.signal in ENRICHMENT_SIGNALS:
                continue
            signal_rows.append((uuid.uuid5(uuid.NAMESPACE_URL, f"signal:{bid}:{f.signal}"), bid, f.signal,
                                json.dumps(f.value), f.points, now))
    with conn.cursor() as cur:
        cur.execute("DELETE FROM signals WHERE NOT (signal_type = ANY(%s))", (list(ENRICHMENT_SIGNALS),))
        cur.executemany(
            "INSERT INTO signals (id, business_id, signal_type, value, weight, observed_at) VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (business_id, signal_type) DO UPDATE SET value = EXCLUDED.value, weight = EXCLUDED.weight, "
            "observed_at = EXCLUDED.observed_at",
            signal_rows,
        )
        cur.executemany(
            "INSERT INTO scores (business_id, score, reasons, breakdown, model_version, scored_at) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (business_id) DO UPDATE SET score = EXCLUDED.score, "
            "reasons = EXCLUDED.reasons, breakdown = EXCLUDED.breakdown, model_version = EXCLUDED.model_version, "
            "scored_at = EXCLUDED.scored_at",
            score_rows,
        )
    conn.commit()
    return {"scored": len(score_rows), "model_version": MODEL_VERSION}
