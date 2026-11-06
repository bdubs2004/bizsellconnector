"""Industry classification (scope 3.2 step 4), all in SQL so it scales to ~10M businesses.

Priority, highest first (never overrides naics_source = 'manual'):
  1. NAICS on a linked SBA loan (7(a)/504 first, then PPP; most recent wins)   source sba / ppp
  2. NAICS on the state filing (Connecticut, Texas self-report it)              source state
  3. Rules on the business name (industries.py), run as Postgres regexes        source rules
  4. Claude, if enabled (limited per run)                                       source llm
"""
from __future__ import annotations

import psycopg

from . import llm
from .industries import INDUSTRIES


def pg_regex(patterns: tuple[str, ...]) -> str:
    """Python \\b word boundaries -> Postgres ARE \\y."""
    return "|".join(f"(?:{p})" for p in patterns).replace(r"\b", r"\y")


def classify(conn: psycopg.Connection, llm_limit: int = 0) -> dict:
    out = {}
    out["from_loans"] = conn.execute(
        r"""
        UPDATE businesses b
           SET naics_code = x.naics_code, naics_source = x.src,
               naics_confidence = CASE WHEN x.src = 'sba' THEN 0.9 ELSE 0.85 END
          FROM (
            SELECT DISTINCT ON (business_id) business_id, naics_code,
                   CASE WHEN program = 'ppp' THEN 'ppp' ELSE 'sba' END AS src
              FROM sba_loans
             WHERE business_id IS NOT NULL AND naics_code ~ '^\d{6}$'
             ORDER BY business_id, (program = 'ppp'), approval_date DESC NULLS LAST
          ) x
         WHERE x.business_id = b.id AND b.naics_source IS DISTINCT FROM 'manual'
           AND (b.naics_code IS DISTINCT FROM x.naics_code OR b.naics_source IS DISTINCT FROM x.src)
        """
    ).rowcount

    out["from_state_filing"] = conn.execute(
        r"""
        UPDATE businesses SET naics_code = filing_naics, naics_source = 'state', naics_confidence = 0.8
         WHERE filing_naics ~ '^\d{6}$'
           AND (naics_source IS NULL OR naics_source IN ('rules', 'llm', 'state'))
           AND (naics_code IS DISTINCT FROM filing_naics OR naics_source IS DISTINCT FROM 'state')
        """
    ).rowcount

    # Name rules: clear and re-apply so rule changes take effect; first matching industry wins.
    conn.execute("UPDATE businesses SET naics_code = NULL, naics_confidence = NULL, naics_source = NULL "
                 "WHERE naics_source = 'rules'")
    rules = 0
    for ind in INDUSTRIES:
        sql = ("UPDATE businesses SET naics_code = %(code)s, naics_confidence = 0.6, naics_source = 'rules' "
               "WHERE naics_source IS NULL AND normalized_name ~ %(inc)s")
        params = {"code": ind.naics[0], "inc": pg_regex(ind.include)}
        if ind.exclude:
            sql += " AND normalized_name !~ %(exc)s"
            params["exc"] = pg_regex(ind.exclude)
        rules += conn.execute(sql, params).rowcount
    out["from_rules"] = rules

    llm_done = 0
    if llm_limit and llm.enabled():
        pending = conn.execute(
            "SELECT id, legal_name, city, state FROM businesses WHERE naics_code IS NULL AND status <> 'inactive' "
            "ORDER BY formation_date NULLS LAST LIMIT %s", (llm_limit,)
        ).fetchall()
        for p in pending:
            g = llm.classify_naics(p["legal_name"], f"Location: {p['city']}, {p['state']}" if p["city"] else "")
            if g and g.naics_code and len(g.naics_code) == 6 and g.naics_code.isdigit():
                conn.execute(
                    "UPDATE businesses SET naics_code = %s, naics_confidence = %s, naics_source = 'llm' WHERE id = %s",
                    (g.naics_code, max(0.0, min(g.confidence, 0.8)), p["id"]),
                )
                llm_done += 1
    out["from_llm"] = llm_done
    conn.commit()
    return out
