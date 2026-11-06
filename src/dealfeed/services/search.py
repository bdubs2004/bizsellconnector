"""search_targets and market_summary (scope 6.1)."""
from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Literal

import psycopg
from pydantic import BaseModel, Field

from ..config import get_settings
from ..industries import BY_NAICS, resolve_industry
from ..normalize import normalize_name
from ..buy import BuyBox, annotate
from ..industries import BY_SLUG
from ..score import attach_breakdowns
from ..security import escape_like
from ..states_meta import SUPPORTED_STATES, covered_states_text

COUNT_CAP = 10_000
# Priority / buy ranking re-ranks this many top candidates by sell score (buy scores are per user, so
# they can't be indexed; scoring 1,000 rows on the fly takes milliseconds).
CANDIDATES = 1_000

DATA_CAVEAT = ("Scores are directional, rules-based estimates of likelihood to sell, not predictions. "
               "Size figures are ranges derived from 2020-21 PPP data. Verify before outreach.")


class SearchParams(BaseModel):
    state: str | None = Field(None, max_length=2,
                              description="Two-letter state: IA, CO, NY, CT, OR or TX. Empty = all covered states.")
    industry: str | None = Field(None, max_length=80, description="Industry name or keyword, e.g. 'pest control', "
                                                                  "'janitorial', 'insurance agency', or a NAICS code")
    naics_codes: list[Annotated[str, Field(max_length=6)]] | None = Field(
        None, max_length=20, description="NAICS codes or prefixes, e.g. ['561710'] or ['5617']")
    name_contains: str | None = Field(None, max_length=80, description="Substring of the business name")
    counties: list[Annotated[str, Field(max_length=40)]] | None = Field(
        None, max_length=20, description="County names, e.g. ['Polk', 'Story']")
    city: str | None = Field(None, max_length=60)
    min_years_in_business: int | None = Field(None, ge=0)
    min_employees: int | None = Field(None, ge=0)
    max_employees: int | None = Field(None, ge=0)
    min_payroll: float | None = Field(None, ge=0)
    max_payroll: float | None = Field(None, ge=0)
    min_score: int | None = Field(None, ge=0, le=100)
    include_inactive: bool = False
    include_unknown_size: bool = Field(True, description="Include businesses with no size estimate when a size "
                                                         "filter is set")
    in_buy_box: bool = Field(False, description="Only businesses in your buy box's industries and states")
    sort: Literal["priority", "sell", "buy"] = Field(
        "priority", description="priority = likely to sell AND a fit for your buy box (default); sell = likely-to-"
                                "sell score; buy = fit for your buy box")
    limit: int = Field(25, ge=1, le=50)
    offset: int = Field(0, ge=0, le=1_000, description="Paging depth is capped at 1,000: narrow the filters instead")


def _years(fd: date | None, today: date) -> int | None:
    if not fd:
        return None
    return today.year - fd.year - ((today.month, today.day) < (fd.month, fd.day))


def _money(v) -> str:
    v = float(v)
    return f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.0f}k"


def size_summary(row: dict) -> dict | None:
    if row.get("se_method") is None:
        return None
    out = {"source": "SBA PPP loan data", "as_of": row["se_as_of"].isoformat() if row.get("se_as_of") else None,
           "method": row["se_method"]}
    if row.get("employees_low") is not None:
        out["employees"] = f"{row['employees_low']}-{row['employees_high']}"
    if row.get("payroll_low") is not None:
        out["annual_payroll"] = f"{_money(row['payroll_low'])}-{_money(row['payroll_high'])}"
    if row.get("revenue_low") is not None:
        out["revenue_rough"] = f"{_money(row['revenue_low'])}-{_money(row['revenue_high'])}"
    return out


def target_summary(row: dict, today: date) -> dict:
    ind = BY_NAICS.get(row.get("naics_code") or "")
    breakdown = row.get("breakdown") or []
    return {
        "business_id": str(row["id"]),
        "name": row["legal_name"],
        "city": row.get("city"),
        "county": row.get("county"),
        "state": row.get("state"),
        "industry": ind.label if ind else None,
        "naics_code": row.get("naics_code"),
        "naics_source": row.get("naics_source"),
        "formation_date": row["formation_date"].isoformat() if row.get("formation_date") else None,
        "years_in_business": _years(row.get("formation_date"), today),
        "status": row.get("status"),
        "score": row.get("score"),
        "top_reasons": [b["reason"] for b in breakdown if b["points"] != 0][:4],
        "size_estimate": size_summary(row),
        "buy_score": row.get("buy_score"),
        "priority": row.get("priority"),
        "top_buy_reasons": [b["reason"] for b in row.get("buy_breakdown") or [] if b["points"] != 0][:3],
        "deal_estimate": ({k: row["deal_estimate"][k] for k in ("price_range", "sde_range", "dscr", "cash_needed")}
                          if row.get("deal_estimate") else None),
        "location": ({"lat": row["latitude"], "lon": row["longitude"]}
                     if row.get("latitude") is not None and row.get("longitude") is not None else None),
        "data_as_of": {
            "state_registry": row["sos_pulled_at"].date().isoformat() if row.get("sos_pulled_at") else None,
            "scored_at": row["scored_at"].date().isoformat() if row.get("scored_at") else None,
        },
    }


BASE_SELECT = """
SELECT b.*,
       se.employees_low, se.employees_high, se.payroll_low, se.payroll_high, se.revenue_low, se.revenue_high,
       se.method AS se_method, se.as_of AS se_as_of,
       sos.pulled_at AS sos_pulled_at, sos.source AS registry_source
  FROM businesses b
  LEFT JOIN LATERAL (SELECT * FROM size_estimates x WHERE x.business_id = b.id
                     ORDER BY x.as_of DESC NULLS LAST LIMIT 1) se ON true
  LEFT JOIN source_records sos ON sos.id = b.id
"""

SIZE_JOIN = """
  LEFT JOIN LATERAL (SELECT * FROM size_estimates x WHERE x.business_id = b.id
                     ORDER BY x.as_of DESC NULLS LAST LIMIT 1) se ON true
"""


def _where(p: SearchParams, box: BuyBox | None = None) -> tuple[str, dict, dict]:
    clauses: list[str] = ["NOT b.suppressed"]
    args: dict = {}
    resolved: dict = {}
    if p.state:
        clauses.append("b.state = %(state)s")
        args["state"] = p.state.strip().upper()
        resolved["state"] = args["state"]
    else:
        resolved["states"] = list(SUPPORTED_STATES)

    naics = list(p.naics_codes or [])
    if p.industry:
        ind = resolve_industry(p.industry)
        if ind:
            naics += list(ind.naics)
            resolved["industry"] = ind.label
        else:
            # Unknown industry text: fall back to a name search so the caller still gets something.
            clauses.append("b.normalized_name LIKE %(industry_kw)s ESCAPE '\\'")
            args["industry_kw"] = f"%{escape_like(normalize_name(p.industry))}%"
            resolved["industry_keyword_name_match"] = p.industry
    if naics:
        clean = [re.sub(r"\D", "", n) for n in naics if re.sub(r"\D", "", n)]
        clauses.append("b.naics_code LIKE ANY(%(naics_like)s)")
        args["naics_like"] = [c + "%" for c in clean]
        resolved["naics_codes"] = sorted(set(clean))
    if p.name_contains:
        clauses.append("b.normalized_name LIKE %(name_kw)s ESCAPE '\\'")
        args["name_kw"] = f"%{escape_like(normalize_name(p.name_contains))}%"
    if p.counties:
        clauses.append("b.county = ANY(%(counties)s)")
        args["counties"] = [re.sub(r"\s+COUNTY$", "", c.strip().upper()) for c in p.counties]
        resolved["counties"] = args["counties"]
    if p.city:
        clauses.append("b.city = %(city)s")
        args["city"] = p.city.strip().upper()
    if p.min_years_in_business:
        clauses.append("b.formation_date <= current_date - make_interval(years => %(min_years)s)")
        args["min_years"] = p.min_years_in_business
    size_clauses = []
    if p.min_employees is not None:
        size_clauses.append("se.employees_high >= %(min_emp)s")
        args["min_emp"] = p.min_employees
    if p.max_employees is not None:
        size_clauses.append("se.employees_low <= %(max_emp)s")
        args["max_emp"] = p.max_employees
    if p.min_payroll is not None:
        size_clauses.append("se.payroll_high >= %(min_pay)s")
        args["min_pay"] = p.min_payroll
    if p.max_payroll is not None:
        size_clauses.append("se.payroll_low <= %(max_pay)s")
        args["max_pay"] = p.max_payroll
    if size_clauses:
        sc = " AND ".join(size_clauses)
        clauses.append(f"(({sc}) OR se.method IS NULL)" if p.include_unknown_size else f"({sc})")
    if p.min_score is not None:
        clauses.append("b.score >= %(min_score)s")
        args["min_score"] = p.min_score
    if p.in_buy_box and box:
        if box.industries:
            clauses.append("b.naics_code LIKE ANY(%(box_naics)s)")
            args["box_naics"] = [c + "%" for s in box.industries for c in BY_SLUG[s].naics]
        if box.states:
            clauses.append("b.state = ANY(%(box_states)s)")
            args["box_states"] = box.states
        resolved["in_buy_box"] = True
    if not p.include_inactive:
        clauses.append("b.status <> 'inactive'")
    return " AND ".join(clauses) or "true", args, resolved


def search_rows(conn: psycopg.Connection, p: SearchParams, limit: int, offset: int = 0,
                count: bool = True, box: BuyBox | None = None) -> tuple[list[dict], int, dict]:
    """Ranked rows plus a match count capped at COUNT_CAP + 1 (counting millions exactly is slow
    and nobody pages that far; callers show "10,000+").

    sort="sell" pages straight from the index. "priority" and "buy" take the top CANDIDATES by sell score,
    score them against the buyer's buy box, re-rank, then page."""
    box = box or BuyBox(is_default=True)
    where, args, resolved = _where(p, box)
    resolved["ranking"] = p.sort
    sql = (f"{BASE_SELECT} WHERE {where} "
           "ORDER BY b.score DESC NULLS LAST, b.formation_date ASC NULLS LAST, b.id "
           "LIMIT %(limit)s OFFSET %(offset)s")
    if p.sort == "sell":
        rows = annotate(conn.execute(sql, args | {"limit": limit, "offset": offset}).fetchall(), box)
    else:
        pool = annotate(conn.execute(sql, args | {"limit": CANDIDATES, "offset": 0}).fetchall(), box)
        key = "priority" if p.sort == "priority" else "buy_score"
        pool.sort(key=lambda r: (-(r[key] or 0), -(r["score"] or 0)))
        rows = pool[offset:offset + limit]
    rows = attach_breakdowns(conn, rows)
    total = 0
    if count:
        total = conn.execute(f"SELECT count(*) AS n FROM (SELECT 1 FROM businesses b {SIZE_JOIN} WHERE {where} "
                             f"LIMIT {COUNT_CAP + 1}) x", args).fetchone()["n"]
    return rows, total, resolved


def search_targets(conn: psycopg.Connection, p: SearchParams, limit: int, today: date | None = None,
                   box: BuyBox | None = None) -> dict:
    today = today or date.today()
    box = box or BuyBox(is_default=True)
    rows, total, resolved = search_rows(conn, p, limit, p.offset, box=box)
    results = [target_summary(r, today) for r in rows]
    notes = [DATA_CAVEAT]
    if p.sort != "sell":
        notes.append(f"Ranked by {'priority (sell x buy)' if p.sort == 'priority' else 'buy score'} among the top "
                     f"{CANDIDATES:,} matches by sell score. Buy scores use "
                     + ("default assumptions; set your buy box to personalize them." if box.is_default
                        else "your buy box."))
    if p.state and p.state.strip().upper() not in SUPPORTED_STATES:
        notes.append(f"No data for {p.state.upper()} yet. Covered states: {covered_states_text()}.")
    if limit < p.limit:
        notes.append(f"Results capped at {limit} by your plan's per-call or monthly limit.")
    return {
        "results": results,
        "returned": len(results),
        "total_matching": min(total, COUNT_CAP),
        "total_is_lower_bound": total > COUNT_CAP,
        "offset": p.offset,
        "next_offset": (p.offset + len(results)
                        if p.offset + len(results) < (total if p.sort == "sell" else min(total, CANDIDATES)) else None),
        "filters_applied": resolved,
        "notes": notes,
    }


def market_summary(conn: psycopg.Connection, state: str | None = None, industry: str | None = None,
                   naics_code: str | None = None) -> dict:
    """Counts and distributions for an industry in one state, or across all covered states."""
    codes: list[str] = []
    label = None
    if industry:
        ind = resolve_industry(industry)
        if not ind:
            raise ValueError(f"Unknown industry {industry!r}. Try a NAICS code instead.")
        codes, label = list(ind.naics), ind.label
    if naics_code:
        codes.append(re.sub(r"\D", "", naics_code))
        label = label or (BY_NAICS[naics_code].label if naics_code in BY_NAICS else f"NAICS {naics_code}")
    if not codes:
        raise ValueError("Provide an industry or a naics_code")
    st = state.strip().upper() if state else None
    args = {"state": st, "like": [c + "%" for c in codes]}
    in_state = "(%(state)s::text IS NULL OR b.state = %(state)s)"
    base = f"FROM businesses b WHERE {in_state} AND NOT b.suppressed AND b.naics_code LIKE ANY(%(like)s)"
    q = lambda sql: conn.execute(sql, args).fetchall()  # noqa: E731

    totals = q(f"SELECT count(*) AS total, count(*) FILTER (WHERE status = 'active') AS active, "
               f"count(*) FILTER (WHERE status = 'delinquent') AS delinquent, "
               f"count(*) FILTER (WHERE status = 'inactive') AS inactive, "
               f"count(*) FILTER (WHERE status = 'unknown') AS status_unknown {base}")[0]
    age = q(f"""SELECT CASE WHEN formation_date IS NULL THEN 'unknown'
                        WHEN age(formation_date) < interval '5 years' THEN '<5'
                        WHEN age(formation_date) < interval '10 years' THEN '5-9'
                        WHEN age(formation_date) < interval '20 years' THEN '10-19'
                        WHEN age(formation_date) < interval '30 years' THEN '20-29'
                        ELSE '30+' END AS bucket, count(*) AS n {base} AND b.status <> 'inactive' GROUP BY 1""")
    size = q("""SELECT CASE WHEN se.employees_low IS NULL THEN 'unknown'
                         WHEN (se.employees_low + se.employees_high) / 2.0 < 5 THEN '1-4'
                         WHEN (se.employees_low + se.employees_high) / 2.0 < 10 THEN '5-9'
                         WHEN (se.employees_low + se.employees_high) / 2.0 < 20 THEN '10-19'
                         WHEN (se.employees_low + se.employees_high) / 2.0 < 50 THEN '20-49'
                         WHEN (se.employees_low + se.employees_high) / 2.0 < 100 THEN '50-99'
                         ELSE '100+' END AS bucket, count(*) AS n
                  FROM businesses b LEFT JOIN LATERAL (SELECT * FROM size_estimates x WHERE x.business_id = b.id
                       ORDER BY as_of DESC NULLS LAST LIMIT 1) se ON true
                 WHERE (%(state)s::text IS NULL OR b.state = %(state)s) AND b.naics_code LIKE ANY(%(like)s)
                   AND b.status <> 'inactive' AND NOT b.suppressed
                 GROUP BY 1""")
    scores = q(f"""SELECT CASE WHEN b.score >= 80 THEN '80-100' WHEN b.score >= 60 THEN '60-79'
                            WHEN b.score >= 40 THEN '40-59' ELSE '0-39' END AS bucket, count(*) AS n
                     {base} AND b.status <> 'inactive' AND b.score IS NOT NULL GROUP BY 1""")
    counties = q(f"SELECT b.state, coalesce(b.county, 'UNKNOWN') AS county, count(*) AS n {base} "
                 "AND b.status <> 'inactive' GROUP BY 1, 2 ORDER BY 3 DESC, 1, 2 LIMIT 10")
    by_state = q(f"SELECT b.state, count(*) AS n {base} AND b.status <> 'inactive' GROUP BY 1 ORDER BY 2 DESC")
    sba = q("""SELECT count(*) AS loans_5y,
                      percentile_cont(0.25) WITHIN GROUP (ORDER BY amount) AS p25,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY amount) AS median,
                      percentile_cont(0.75) WITHIN GROUP (ORDER BY amount) AS p75,
                      count(*) FILTER (WHERE business_age_desc ILIKE '%%change of ownership%%') AS change_of_ownership
                 FROM sba_loans WHERE program IN ('7a', '504') AND (%(state)s::text IS NULL OR state = %(state)s)
                  AND naics_code LIKE ANY(%(like)s) AND approval_date >= current_date - interval '5 years'""")[0]
    lenders = q("""SELECT lender, count(*) AS loans FROM sba_loans
                    WHERE program IN ('7a', '504') AND (%(state)s::text IS NULL OR state = %(state)s)
                      AND naics_code LIKE ANY(%(like)s)
                      AND approval_date >= current_date - interval '5 years' AND lender IS NOT NULL
                    GROUP BY 1 ORDER BY 2 DESC LIMIT 5""")

    order = lambda rows, keys: {k: next((r["n"] for r in rows if r["bucket"] == k), 0) for k in keys}  # noqa: E731
    r0 = lambda v: round(float(v)) if v is not None else None  # noqa: E731
    return {
        "industry": label,
        "naics_codes": codes,
        "state": st or "all covered states",
        "by_state": {r["state"]: r["n"] for r in by_state},
        "businesses": dict(totals),
        "years_in_business": order(age, ["<5", "5-9", "10-19", "20-29", "30+", "unknown"]),
        "employees_estimate": order(size, ["1-4", "5-9", "10-19", "20-49", "50-99", "100+", "unknown"]),
        "score_distribution": order(scores, ["80-100", "60-79", "40-59", "0-39"]),
        "top_counties": [dict(r) for r in counties],
        "sba_7a_504_last_5_years": {
            "loans": sba["loans_5y"], "change_of_ownership_loans": sba["change_of_ownership"],
            "amount_p25": r0(sba["p25"]), "amount_median": r0(sba["median"]), "amount_p75": r0(sba["p75"]),
            "most_active_lenders": [dict(r) for r in lenders],
        },
        "notes": [
            "Counts cover businesses we could classify into this industry; unclassified entities are not included.",
            "Coverage vs. Census County Business Patterns is not computed yet.",
            f"Covered states: {covered_states_text()}. Domestic for-profit entities"
            + (f" at least {get_settings().min_age_years} years old." if get_settings().min_age_years else "."),
            DATA_CAVEAT,
        ],
    }
