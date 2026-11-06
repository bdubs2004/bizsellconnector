"""Build step: everything after ingest. Safe to re-run; it's idempotent."""
from __future__ import annotations

import logging
from datetime import date

import psycopg

from . import classify, match, score, size

log = logging.getLogger(__name__)


def build(conn: psycopg.Connection, rematch: bool = False, llm_classify_limit: int = 0,
          today: date | None = None) -> dict:
    out = {}
    out["spine"] = match.build_spine(conn)
    log.info("spine: %s", out["spine"])
    out["match"] = match.link_loan_records(conn, rematch=rematch)
    log.info("match: %s", out["match"])
    out["classify"] = classify.classify(conn, llm_limit=llm_classify_limit)
    log.info("classify: %s", out["classify"])
    out["size"] = size.build_size_estimates(conn)
    out["score"] = score.score_all(conn, today=today)
    log.info("score: %s", out["score"])
    return out


def stats(conn: psycopg.Connection) -> dict:
    """Data metrics from scope Section 11."""
    q = lambda sql: conn.execute(sql).fetchall()  # noqa: E731
    return {
        "runs": q("SELECT source, max(finished_at) AS last_ok, sum(rows_loaded) FILTER (WHERE id IN "
                  "(SELECT max(id) FROM pipeline_runs WHERE status='ok' GROUP BY source)) AS last_rows "
                  "FROM pipeline_runs WHERE status = 'ok' GROUP BY source ORDER BY source"),
        "businesses": q("SELECT state, status, count(*) AS n FROM businesses GROUP BY 1, 2 ORDER BY 1, 2"),
        "by_industry": q("SELECT state, naics_code, naics_source, count(*) AS n FROM businesses "
                         "WHERE naics_code IN ('561710','561720','524210','811111','541213','541219','561730') "
                         "GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"),
        "match_methods": q("SELECT source, coalesce(match_method, 'pending') AS method, count(*) AS n "
                           "FROM source_records GROUP BY 1, 2 ORDER BY 1, 2"),
        "pending_reviews": q("SELECT count(*) AS n FROM match_review WHERE status = 'pending'")[0]["n"],
        "pct_with_size": q("SELECT round(100.0 * count(s.business_id) / nullif(count(*), 0), 1) AS pct "
                           "FROM businesses b LEFT JOIN (SELECT DISTINCT business_id FROM size_estimates) s "
                           "ON s.business_id = b.id WHERE b.status <> 'inactive'")[0]["pct"],
    }
