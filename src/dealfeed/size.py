"""Size estimates from PPP data (scope 3.1 / 3.4). Always ranges, never point values.

PPP first-draw loans were ~2.5x average monthly payroll, so
    annual_payroll ~= loan / 2.5 * 12
Caveats baked into the ranges:
  * payroll above $100k per employee was excluded from the PPP formula, so the
    estimate understates high-wage businesses (insurance agencies especially)
  * 2020-21 data; headcount has moved since
  * revenue comes from an industry payroll/revenue ratio that is a placeholder
"""
from __future__ import annotations

import math
import uuid

import psycopg

from .industries import BY_NAICS

PAYROLL_SPREAD = (0.8, 1.25)
EMPLOYEE_SPREAD = (0.75, 1.3)
METHOD = "ppp_payroll"


def estimate_from_ppp(amount: float, jobs: int | None, naics: str | None) -> dict:
    payroll = amount / 2.5 * 12
    out = {
        "payroll_low": round(payroll * PAYROLL_SPREAD[0], -3),
        "payroll_high": round(payroll * PAYROLL_SPREAD[1], -3),
        "employees_low": None, "employees_high": None,
        "revenue_low": None, "revenue_high": None,
    }
    if jobs and jobs > 0:
        out["employees_low"] = max(1, math.floor(jobs * EMPLOYEE_SPREAD[0]))
        out["employees_high"] = max(out["employees_low"] + 1, math.ceil(jobs * EMPLOYEE_SPREAD[1]))
    ind = BY_NAICS.get(naics or "")
    if ind:
        lo_ratio, hi_ratio = ind.payroll_to_revenue
        out["revenue_low"] = round(out["payroll_low"] / hi_ratio, -4)
        out["revenue_high"] = round(out["payroll_high"] / lo_ratio, -4)
    return out


def build_size_estimates(conn: psycopg.Connection) -> dict:
    # Largest first-draw PPP loan per business (second draws were capped differently).
    rows = conn.execute(
        """
        SELECT DISTINCT ON (l.business_id) l.business_id, l.amount, l.jobs_reported, l.approval_date,
               b.naics_code
          FROM sba_loans l JOIN source_records sr ON sr.id = l.source_record_id
          JOIN businesses b ON b.id = l.business_id
         WHERE l.program = 'ppp' AND l.amount > 0 AND coalesce(sr.canon->>'draw', 'first') = 'first'
         ORDER BY l.business_id, l.amount DESC
        """
    ).fetchall()
    params = []
    for r in rows:
        e = estimate_from_ppp(float(r["amount"]), r["jobs_reported"], r["naics_code"])
        params.append({
            "id": uuid.uuid5(uuid.NAMESPACE_URL, f"size:{r['business_id']}:{METHOD}"),
            "business_id": r["business_id"], "as_of": r["approval_date"], **e,
        })
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO size_estimates (id, business_id, employees_low, employees_high, payroll_low, payroll_high,
                                        revenue_low, revenue_high, method, as_of, source)
            VALUES (%(id)s, %(business_id)s, %(employees_low)s, %(employees_high)s, %(payroll_low)s,
                    %(payroll_high)s, %(revenue_low)s, %(revenue_high)s, 'ppp_payroll', %(as_of)s, 'ppp')
            ON CONFLICT (business_id, method, source) DO UPDATE SET
                employees_low = EXCLUDED.employees_low, employees_high = EXCLUDED.employees_high,
                payroll_low = EXCLUDED.payroll_low, payroll_high = EXCLUDED.payroll_high,
                revenue_low = EXCLUDED.revenue_low, revenue_high = EXCLUDED.revenue_high, as_of = EXCLUDED.as_of
            """,
            params,
        )
    conn.commit()
    return {"size_estimates": len(params)}
