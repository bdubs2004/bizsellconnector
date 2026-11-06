"""Industry classification (scope 3.2 step 4).

Priority, highest first (never overrides naics_source = 'manual'):
  1. NAICS from a linked SBA loan (7(a)/504 first, then PPP; most recent wins)
  2. Rules on the business name (industries.classify_by_name)
  3. Claude, if enabled (limited per run because there are ~335k entities)
"""
from __future__ import annotations

import psycopg

from . import llm
from .industries import classify_by_name


def classify(conn: psycopg.Connection, llm_limit: int = 0) -> dict:
    loan = conn.execute(
        """
        UPDATE businesses b
           SET naics_code = x.naics_code, naics_source = x.src,
               naics_confidence = CASE WHEN x.src = 'sba' THEN 0.9 ELSE 0.85 END
          FROM (
            SELECT DISTINCT ON (business_id) business_id, naics_code,
                   CASE WHEN program = 'ppp' THEN 'ppp' ELSE 'sba' END AS src
              FROM sba_loans
             WHERE business_id IS NOT NULL AND naics_code ~ '^\\d{6}$'
             ORDER BY business_id, (program = 'ppp'), approval_date DESC NULLS LAST
          ) x
         WHERE x.business_id = b.id AND b.naics_source IS DISTINCT FROM 'manual'
           AND (b.naics_code IS DISTINCT FROM x.naics_code OR b.naics_source IS DISTINCT FROM x.src)
        """
    ).rowcount

    # Rules: recompute for everything not classified by loan data, manual or LLM.
    rows = conn.execute(
        "SELECT id, normalized_name FROM businesses "
        "WHERE naics_source IS NULL OR naics_source = 'rules'"
    ).fetchall()
    rule_updates, cleared = [], []
    for r in rows:
        hit = classify_by_name(r["normalized_name"])
        if hit:
            ind, conf = hit
            rule_updates.append((ind.naics[0], conf, str(r["id"])))
        else:
            cleared.append(str(r["id"]))
    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE businesses SET naics_code = %s, naics_confidence = %s, naics_source = 'rules' WHERE id = %s",
            rule_updates,
        )
        cur.executemany(
            "UPDATE businesses SET naics_code = NULL, naics_confidence = NULL, naics_source = NULL "
            "WHERE id = %s AND naics_source = 'rules'",
            [(i,) for i in cleared],
        )

    llm_done = 0
    if llm_limit and llm.enabled():
        pending = conn.execute(
            "SELECT id, legal_name, city FROM businesses WHERE naics_code IS NULL AND status <> 'inactive' "
            "ORDER BY formation_date NULLS LAST LIMIT %s", (llm_limit,)
        ).fetchall()
        for p in pending:
            g = llm.classify_naics(p["legal_name"], f"City: {p['city']}, Iowa" if p["city"] else "")
            if g and g.naics_code and len(g.naics_code) == 6 and g.naics_code.isdigit():
                conn.execute(
                    "UPDATE businesses SET naics_code = %s, naics_confidence = %s, naics_source = 'llm' WHERE id = %s",
                    (g.naics_code, max(0.0, min(g.confidence, 0.8)), p["id"]),
                )
                llm_done += 1
    conn.commit()
    return {"from_loans": loan, "from_rules": len(rule_updates), "from_llm": llm_done}
