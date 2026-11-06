"""search_targets and market_summary (scope 6.1)."""
from __future__ import annotations

import re
from datetime import date

import psycopg
from pydantic import BaseModel, Field

from ..industries import BY_NAICS, resolve_industry
from ..normalize import normalize_name

DATA_CAVEAT = ("Scores are directional, rules-based estimates of likelihood to sell, not predictions. "
               "Size figures are ranges derived from 2020-21 PPP data. Verify before outreach.")


class SearchParams(BaseModel):
    state: str = Field("IA", description="Two-letter state. v1 covers Iowa only.")
    industry: str | None = Field(None, description="Industry name or keyword, e.g. 'pest control', 'janitorial', "
                                                   "'insurance agency', or a NAICS code")
    naics_codes: list[str] | None = Field(None, description="NAICS codes or prefixes, e.g. ['561710'] or ['5617']")
    name_contains: str | None = Field(None, description="Substring of the business name")
    counties: list[str] | None = Field(None, description="County names, e.g. ['Polk', 'Story']")
    city: str | None = None
    min_years_in_business: int | None = Field(None, ge=0)
    min_employees: int | None = Field(None, ge=0)
    max_employees: int | None = Field(None, ge=0)
    min_payroll: float | None = Field(None, ge=0)
    max_payroll: float | None = Field(None, ge=0)
    min_score: int | None = Field(None, ge=0, le=100)
    include_inactive: bool = False
    include_unknown_size: bool = Field(True, description="Include businesses with no size estimate when a size "
                                                         "filter is set")
    limit: int = Field(25, ge=1, le=50)
    offset: int = Field(0, ge=0)


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
        "location": ({"lat": row["latitude"], "lon": row["longitude"]}
                     if row.get("latitude") is not None and row.get("longitude") is not None else None),
        "data_as_of": {
            "iowa_sos": row["sos_pulled_at"].date().isoformat() if row.get("sos_pulled_at") else None,
            "scored_at": row["scored_at"].date().isoformat() if row.get("scored_at") else None,
        },
    }


BASE_SELECT = """
SELECT b.*, s.score, s.reasons, s.breakdown, s.scored_at, s.model_version,
       se.employees_low, se.employees_high, se.payroll_low, se.payroll_high, se.revenue_low, se.revenue_high,
       se.method AS se_method, se.as_of AS se_as_of,
       sos.pulled_at AS sos_pulled_at
  FROM businesses b
  LEFT JOIN scores s ON s.business_id = b.id
  LEFT JOIN LATERAL (SELECT * FROM size_estimates x WHERE x.business_id = b.id
                     ORDER BY x.as_of DESC NULLS LAST LIMIT 1) se ON true
  LEFT JOIN source_records sos ON sos.id = b.id AND sos.source = 'ia_sos'
"""


def _where(p: SearchParams) -> tuple[str, dict, dict]:
    clauses = ["b.state = %(state)s"]
    args: dict = {"state": p.state.upper()}
    resolved: dict = {"state": p.state.upper()}

    naics = list(p.naics_codes or [])
    if p.industry:
        ind = resolve_industry(p.industry)
        if ind:
            naics += list(ind.naics)
            resolved["industry"] = ind.label
        else:
            # Unknown industry text: fall back to a name search so the caller still gets something.
            clauses.append("b.normalized_name LIKE %(industry_kw)s")
            args["industry_kw"] = f"%{normalize_name(p.industry)}%"
            resolved["industry_keyword_name_match"] = p.industry
    if naics:
        clean = [re.sub(r"\D", "", n) for n in naics if re.sub(r"\D", "", n)]
        clauses.append("b.naics_code LIKE ANY(%(naics_like)s)")
        args["naics_like"] = [c + "%" for c in clean]
        resolved["naics_codes"] = sorted(set(clean))
    if p.name_contains:
        clauses.append("b.normalized_name LIKE %(name_kw)s")
        args["name_kw"] = f"%{normalize_name(p.name_contains)}%"
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
        clauses.append("s.score >= %(min_score)s")
        args["min_score"] = p.min_score
    if not p.include_inactive:
        clauses.append("b.status <> 'inactive'")
    return " AND ".join(clauses), args, resolved


def search_rows(conn: psycopg.Connection, p: SearchParams, limit: int, offset: int = 0) -> tuple[list[dict], int, dict]:
    where, args, resolved = _where(p)
    sql = (f"SELECT q.*, count(*) OVER () AS total_matching FROM ({BASE_SELECT} WHERE {where}) q "
           "ORDER BY q.score DESC NULLS LAST, q.formation_date ASC NULLS LAST, q.legal_name "
           "LIMIT %(limit)s OFFSET %(offset)s")
    rows = conn.execute(sql, args | {"limit": limit, "offset": offset}).fetchall()
    total = rows[0]["total_matching"] if rows else 0
    return rows, total, resolved


def search_targets(conn: psycopg.Connection, p: SearchParams, limit: int, today: date | None = None) -> dict:
    today = today or date.today()
    rows, total, resolved = search_rows(conn, p, limit, p.offset)
    results = [target_summary(r, today) for r in rows]
    notes = [DATA_CAVEAT]
    if p.state.upper() != "IA":
        notes.append("v1 data covers Iowa only; other states return no results yet.")
    if limit < p.limit:
        notes.append(f"Results capped at {limit} by your plan's per-call or monthly limit.")
    return {
        "results": results,
        "returned": len(results),
        "total_matching": total,
        "offset": p.offset,
        "next_offset": p.offset + len(results) if p.offset + len(results) < total else None,
        "filters_applied": resolved,
        "notes": notes,
    }


def market_summary(conn: psycopg.Connection, state: str = "IA", industry: str | None = None,
                   naics_code: str | None = None) -> dict:
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
    args = {"state": state.upper(), "like": [c + "%" for c in codes]}
    base = "FROM businesses b WHERE b.state = %(state)s AND b.naics_code LIKE ANY(%(like)s)"
    q = lambda sql: conn.execute(sql, args).fetchall()  # noqa: E731

    totals = q(f"SELECT count(*) AS total, count(*) FILTER (WHERE status = 'active') AS active, "
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
                 WHERE b.state = %(state)s AND b.naics_code LIKE ANY(%(like)s) AND b.status <> 'inactive'
                 GROUP BY 1""")
    scores = q("""SELECT CASE WHEN s.score >= 80 THEN '80-100' WHEN s.score >= 60 THEN '60-79'
                           WHEN s.score >= 40 THEN '40-59' ELSE '0-39' END AS bucket, count(*) AS n
                    FROM businesses b JOIN scores s ON s.business_id = b.id
                   WHERE b.state = %(state)s AND b.naics_code LIKE ANY(%(like)s) AND b.status <> 'inactive'
                   GROUP BY 1""")
    counties = q(f"SELECT coalesce(county, 'UNKNOWN') AS county, count(*) AS n {base} AND b.status <> 'inactive' "
                 "GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 10")
    sba = q("""SELECT count(*) AS loans_5y,
                      percentile_cont(0.25) WITHIN GROUP (ORDER BY amount) AS p25,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY amount) AS median,
                      percentile_cont(0.75) WITHIN GROUP (ORDER BY amount) AS p75,
                      count(*) FILTER (WHERE business_age_desc ILIKE '%%change of ownership%%') AS change_of_ownership
                 FROM sba_loans WHERE program IN ('7a', '504') AND state = %(state)s
                  AND naics_code LIKE ANY(%(like)s) AND approval_date >= current_date - interval '5 years'""")[0]
    lenders = q("""SELECT lender, count(*) AS loans FROM sba_loans
                    WHERE program IN ('7a', '504') AND state = %(state)s AND naics_code LIKE ANY(%(like)s)
                      AND approval_date >= current_date - interval '5 years' AND lender IS NOT NULL
                    GROUP BY 1 ORDER BY 2 DESC LIMIT 5""")

    order = lambda rows, keys: {k: next((r["n"] for r in rows if r["bucket"] == k), 0) for k in keys}  # noqa: E731
    r0 = lambda v: round(float(v)) if v is not None else None  # noqa: E731
    return {
        "industry": label,
        "naics_codes": codes,
        "state": state.upper(),
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
            DATA_CAVEAT,
        ],
    }
