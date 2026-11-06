"""Entity resolution (scope 3.2 step 3), built for ~10M businesses across states.

State registries are the spine: each registry entity becomes one business whose id
equals its source_record id (stable across rebuilds). SBA records (PPP, 7(a), 504)
are then linked to those businesses:

  1. exact match on core name + ZIP, in SQL
  2. fuzzy match (rapidfuzz) for the rest, processed one ZIP3 area at a time so
     memory stays bounded; same ZIP first, then same city
  3. gray-zone scores: same street address -> match; else Claude (if enabled); else
     the manual review queue, but only for records in an industry we sell (otherwise
     the queue would grow to hundreds of thousands of rows nobody will read)
  4. unmatched PPP borrowers become their own business; unmatched 7(a)/504 stay unlinked
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

import psycopg
from rapidfuzz import fuzz, process

from . import llm
from .config import get_settings
from .industries import BY_NAICS
from .ingest.states import STATE_SOURCE_KEYS

log = logging.getLogger(__name__)

LOAN_SOURCES = ("ppp", "sba_7a", "sba_504")


def _latest_runs(conn: psycopg.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT source, max(id) AS run_id FROM pipeline_runs WHERE status = 'ok' AND source = ANY(%s) GROUP BY source",
        (list(STATE_SOURCE_KEYS),),
    ).fetchall()
    return {r["source"]: r["run_id"] for r in rows}


def build_spine(conn: psycopg.Connection) -> dict:
    """Apply staged registry rows (from the latest pull of each state) to `businesses`, mark entities
    missing from their state's latest pull inactive, then empty the stage."""
    latest = _latest_runs(conn)
    if not latest:
        return {"note": "no state registry loaded yet"}
    runs = list(latest.values())
    applied = conn.execute(
        """
        INSERT INTO businesses (id, legal_name, normalized_name, core_name, dba_names, entity_type, state_of_formation,
                                formation_date, status, status_since, address_line1, address_kind, city, state, zip,
                                county, latitude, longitude, filing_naics, primary_source, updated_at)
        SELECT st.id, c->>'legal_name', c->>'normalized_name', c->>'core_name',
               ARRAY(SELECT jsonb_array_elements_text(coalesce(c->'dba_names', '[]'))),
               c->>'entity_type', c->>'registry_state', (c->>'formation_date')::date,
               coalesce(c->>'status', 'active'), (c->>'status_since')::date,
               c->>'address_line1', c->>'address_kind', c->>'city', c->>'registry_state', c->>'zip5',
               c->>'county', (c->>'latitude')::real, (c->>'longitude')::real, c->>'naics', sr.source, now()
          FROM registry_stage st JOIN source_records sr ON sr.id = st.id,
               LATERAL (SELECT st.canon AS c) x
         WHERE st.run_id = ANY(%(runs)s)
        ON CONFLICT (id) DO UPDATE SET
            legal_name = EXCLUDED.legal_name, normalized_name = EXCLUDED.normalized_name,
            core_name = EXCLUDED.core_name, dba_names = EXCLUDED.dba_names, entity_type = EXCLUDED.entity_type,
            state_of_formation = EXCLUDED.state_of_formation, formation_date = EXCLUDED.formation_date,
            status = EXCLUDED.status, status_since = EXCLUDED.status_since,
            address_line1 = EXCLUDED.address_line1, address_kind = EXCLUDED.address_kind, city = EXCLUDED.city,
            state = EXCLUDED.state, zip = EXCLUDED.zip,
            -- keep values filled in later (geocoder, PPP county) when the registry has none
            county = coalesce(EXCLUDED.county, businesses.county),
            latitude = coalesce(EXCLUDED.latitude, businesses.latitude),
            longitude = coalesce(EXCLUDED.longitude, businesses.longitude),
            filing_naics = EXCLUDED.filing_naics,
            updated_at = now()
        -- Skip rows that didn't change: a monthly pull is mostly identical, and rewriting millions of
        -- unchanged rows would leave that much dead space behind until vacuum.
        WHERE (businesses.legal_name, businesses.dba_names, businesses.entity_type, businesses.formation_date,
               businesses.status, businesses.status_since, businesses.address_line1, businesses.address_kind,
               businesses.city, businesses.state, businesses.zip, businesses.filing_naics)
              IS DISTINCT FROM
              (EXCLUDED.legal_name, EXCLUDED.dba_names, EXCLUDED.entity_type, EXCLUDED.formation_date,
               EXCLUDED.status, EXCLUDED.status_since, EXCLUDED.address_line1, EXCLUDED.address_kind,
               EXCLUDED.city, EXCLUDED.state, EXCLUDED.zip, EXCLUDED.filing_naics)
           OR (EXCLUDED.county IS NOT NULL AND businesses.county IS DISTINCT FROM EXCLUDED.county)
           OR (EXCLUDED.latitude IS NOT NULL AND businesses.latitude IS DISTINCT FROM EXCLUDED.latitude)
        """,
        {"runs": runs},
    ).rowcount
    gone = conn.execute(
        """
        UPDATE businesses b SET status = 'inactive', updated_at = now()
          FROM source_records sr
         WHERE sr.id = b.id AND sr.source = ANY(%(srcs)s) AND NOT (sr.run_id = ANY(%(runs)s))
           AND b.status <> 'inactive'
        """,
        {"srcs": list(latest), "runs": runs},
    ).rowcount
    conn.execute(
        "UPDATE source_records SET business_id = id, match_method = 'spine', match_score = 100 "
        "WHERE source = ANY(%s) AND business_id IS NULL",
        (list(STATE_SOURCE_KEYS),),
    )
    # People named on the filing. first_seen/last_seen widen with each monthly pull, which is what
    # the "same agent for 15+ years" signal will eventually rely on.
    conn.execute(
        """
        INSERT INTO business_people (business_id, name, role, first_seen, last_seen, source)
        SELECT sr.id, p->>'name', p->>'role', sr.first_seen_at::date, sr.pulled_at::date, sr.source
          FROM registry_stage st JOIN source_records sr ON sr.id = st.id,
               jsonb_array_elements(coalesce(st.canon->'people', '[]')) p
         WHERE st.run_id = ANY(%(runs)s)
        ON CONFLICT (business_id, name, role, source) DO UPDATE
           SET last_seen = greatest(business_people.last_seen, EXCLUDED.last_seen),
               first_seen = least(business_people.first_seen, EXCLUDED.first_seen)
         WHERE business_people.last_seen IS DISTINCT FROM greatest(business_people.last_seen, EXCLUDED.last_seen)
            OR business_people.first_seen IS DISTINCT FROM least(business_people.first_seen, EXCLUDED.first_seen)
        """,
        {"runs": runs},
    )
    # Applied (or superseded by a newer pull of the same state): the stage is emptied either way.
    # TRUNCATE, not DELETE, so the space goes back to the OS instead of sitting in the table.
    conn.execute("TRUNCATE registry_stage")
    counts = conn.execute(
        "SELECT state, count(*) FILTER (WHERE status = 'active') AS active, "
        "count(*) FILTER (WHERE status = 'delinquent') AS delinquent, "
        "count(*) FILTER (WHERE status = 'inactive') AS inactive "
        "FROM businesses WHERE primary_source = ANY(%s) GROUP BY state ORDER BY state",
        (list(STATE_SOURCE_KEYS),),
    ).fetchall()
    conn.commit()
    return {"by_state": {r["state"]: {k: r[k] for k in ("active", "delinquent", "inactive")} for r in counts},
            "marked_inactive_this_build": gone, "applied_from_stage": applied}


# --- loan linking ------------------------------------------------------------------------------

def _exact_sql(conn: psycopg.Connection) -> int:
    """Link loan records whose core name + ZIP match exactly one business and wasn't rejected."""
    return conn.execute(
        """
        WITH cand AS (
            SELECT sr.id AS rid, b.id AS bid, count(*) OVER (PARTITION BY sr.id) AS n
              FROM source_records sr
              JOIN businesses b ON b.core_name = sr.canon->>'core_name' AND b.zip = sr.canon->>'zip5'
             WHERE sr.source = ANY(%(srcs)s) AND sr.match_method IS NULL AND coalesce(sr.canon->>'core_name', '') <> ''
               AND NOT EXISTS (SELECT 1 FROM match_review r WHERE r.source_record_id = sr.id
                                AND r.candidate_business = b.id AND r.status = 'rejected')
        )
        UPDATE source_records s SET business_id = c.bid, match_method = 'exact', match_score = 100
          FROM cand c WHERE c.rid = s.id AND c.n = 1
        """,
        {"srcs": list(LOAN_SOURCES)},
    ).rowcount


@dataclass
class _Cand:
    id: str
    core: str
    zip: str | None
    city: str | None
    address: str | None
    name: str


class _Index:
    def __init__(self):
        self.exact: dict[tuple[str, str | None], list[_Cand]] = defaultdict(list)
        self.by_zip: dict[str, list[_Cand]] = defaultdict(list)
        self.by_city: dict[str, list[_Cand]] = defaultdict(list)

    def add(self, c: _Cand) -> None:
        if not c.core:
            return
        self.exact[(c.core, c.zip)].append(c)
        if c.zip:
            self.by_zip[c.zip].append(c)
        if c.city:
            self.by_city[c.city].append(c)


def _score(rec: dict, cand: _Cand) -> float:
    s = fuzz.token_sort_ratio(rec["core_name"], cand.core)
    if rec.get("address_line1") and rec.get("address_line1") == cand.address:
        s = min(100.0, s + 5)
    return s


def _best(rec: dict, idx: _Index, gray: float) -> list[tuple[_Cand, float]]:
    pool = idx.by_zip.get(rec.get("zip5") or "", []) or idx.by_city.get(rec.get("city") or "", [])
    if not pool:
        return []
    hits = process.extract(rec["core_name"], [c.core for c in pool], scorer=fuzz.token_sort_ratio,
                           score_cutoff=gray - 5, limit=5)
    scored = [(pool[i], _score(rec, pool[i])) for _, _, i in hits]
    return sorted([(c, s) for c, s in scored if s >= gray], key=lambda x: -x[1])


def link_loan_records(conn: psycopg.Connection, rematch: bool = False) -> dict:
    s = get_settings()
    stats: dict[str, int] = defaultdict(int)
    if rematch:
        conn.execute(
            "UPDATE source_records SET business_id = NULL, match_method = NULL, match_score = NULL "
            "WHERE source = ANY(%s) AND match_method IN ('exact', 'fuzzy', 'address', 'llm', 'unmatched', 'review')",
            (list(LOAN_SOURCES),),
        )
        conn.execute("DELETE FROM match_review WHERE status = 'pending'")

    stats["exact"] = _exact_sql(conn)
    conn.commit()

    pending = conn.execute(
        "SELECT id, source, canon FROM source_records WHERE source = ANY(%s) AND match_method IS NULL "
        "ORDER BY source = 'ppp' DESC, (canon->>'approval_date') NULLS LAST",
        (list(LOAN_SOURCES),),
    ).fetchall()
    rejected = {(str(r["source_record_id"]), str(r["candidate_business"])) for r in conn.execute(
        "SELECT source_record_id, candidate_business FROM match_review WHERE status = 'rejected'").fetchall()}

    # Group by ZIP3 so each chunk only loads nearby businesses.
    by_area: dict[str, list[dict]] = defaultdict(list)
    for r in pending:
        z = r["canon"].get("zip5") or ""
        by_area[z[:3] if len(z) == 5 else f"city:{r['canon'].get('state')}:{r['canon'].get('city')}"].append(r)

    for area, recs in by_area.items():
        if area.startswith("city:"):
            _, st, city = area.split(":", 2)
            rows = conn.execute("SELECT id, legal_name, core_name, zip, city, address_line1 FROM businesses "
                                "WHERE state = %s AND city = %s", (st, city)).fetchall()
        else:
            rows = conn.execute("SELECT id, legal_name, core_name, zip, city, address_line1 FROM businesses "
                                "WHERE left(zip, 3) = %s", (area,)).fetchall()
        idx = _Index()
        for b in rows:
            idx.add(_Cand(str(b["id"]), b["core_name"] or "", b["zip"], b["city"], b["address_line1"], b["legal_name"]))
        _link_area(conn, recs, idx, rejected, s, stats)
        conn.commit()

    sync_loan_links(conn)
    conn.commit()
    return dict(stats)


def _link_area(conn, recs, idx: _Index, rejected, s, stats) -> None:
    updates: list[tuple] = []
    reviews: list[tuple] = []
    new_biz: list[dict] = []
    for row in recs:
        rec, rid = row["canon"], str(row["id"])
        if not rec.get("core_name"):
            updates.append((None, "unmatched", None, rid))
            stats["unmatched"] += 1
            continue
        exact = [c for c in idx.exact.get((rec["core_name"], rec.get("zip5")), []) if (rid, c.id) not in rejected]
        if len(exact) == 1:          # e.g. a business created earlier in this same build
            updates.append((exact[0].id, "exact", 100.0, rid))
            stats["exact"] += 1
            continue
        cands = [(c, 100.0) for c in exact] if exact else _best(rec, idx, s.match_gray)
        cands = [(c, sc) for c, sc in cands if (rid, c.id) not in rejected]
        top = cands[0] if cands else None
        ambiguous = bool(top) and len(cands) > 1 and cands[1][1] >= top[1] - 2 and top[1] >= s.match_auto
        if top and top[1] >= s.match_auto and not ambiguous:
            updates.append((top[0].id, "fuzzy", top[1], rid))
            stats["fuzzy"] += 1
            continue
        if top and not ambiguous and rec.get("address_line1") and rec["address_line1"] == top[0].address:
            updates.append((top[0].id, "address", top[1], rid))
            stats["address"] += 1
            continue
        if top:
            verdict = None
            if llm.enabled() and not ambiguous:
                v = llm.same_business({"legal_name": top[0].name, "address_line1": top[0].address,
                                       "city": top[0].city, "zip5": top[0].zip}, rec)
                verdict = v.verdict if v else None
                if verdict == "yes":
                    updates.append((top[0].id, "llm", top[1], rid))
                    stats["llm_yes"] += 1
                    continue
                if verdict == "no":
                    stats["llm_no"] += 1
                    top = None
            if top and (rec.get("naics") or "") in BY_NAICS:
                for c, sc in cands[:3]:
                    reviews.append((rid, c.id, sc, verdict))
                updates.append((None, "review", top[1], rid))
                stats["review"] += 1
                continue
            if top:
                # Gray zone outside our industries: not worth a human's time. Don't link, don't create.
                updates.append((None, "unmatched", top[1], rid))
                stats["unmatched_gray"] += 1
                continue
        if row["source"] == "ppp":
            new_biz.append({"id": rid, **rec})
            updates.append((rid, "new", None, rid))
            idx.add(_Cand(rid, rec["core_name"], rec.get("zip5"), rec.get("city"), rec.get("address_line1"),
                          rec.get("legal_name") or ""))
            stats["new_from_ppp"] += 1
        else:
            updates.append((None, "unmatched", None, rid))
            stats["unmatched"] += 1

    with conn.cursor() as cur:
        if new_biz:
            cur.executemany(
                """
                INSERT INTO businesses (id, legal_name, normalized_name, core_name, status, address_line1, city,
                                        state, zip, county, primary_source)
                VALUES (%(id)s, %(legal_name)s, %(normalized_name)s, %(core_name)s, 'unknown', %(address_line1)s,
                        %(city)s, %(state)s, %(zip5)s, %(county)s, 'ppp')
                ON CONFLICT (id) DO NOTHING
                """,
                new_biz,
            )
        cur.executemany(
            "UPDATE source_records SET business_id = %s, match_method = %s, match_score = %s WHERE id = %s", updates)
        cur.executemany(
            "INSERT INTO match_review (source_record_id, candidate_business, score, llm_verdict) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", reviews)


def sync_loan_links(conn: psycopg.Connection) -> None:
    conn.execute(
        "UPDATE sba_loans l SET business_id = sr.business_id FROM source_records sr "
        "WHERE sr.id = l.source_record_id AND l.business_id IS DISTINCT FROM sr.business_id"
    )
    # Fill county from the PPP project county where the business has none yet.
    conn.execute(
        """
        UPDATE businesses b SET county = x.county FROM (
            SELECT DISTINCT ON (business_id) business_id, county FROM sba_loans
             WHERE business_id IS NOT NULL AND county IS NOT NULL ORDER BY business_id, approval_date DESC
        ) x WHERE x.business_id = b.id AND b.county IS NULL
        """
    )


def resolve_review(conn: psycopg.Connection, review_id: int, accept: bool) -> dict:
    r = conn.execute("SELECT * FROM match_review WHERE id = %s", (review_id,)).fetchone()
    if not r:
        raise KeyError(review_id)
    if accept:
        conn.execute("UPDATE source_records SET business_id = %s, match_method = 'manual', match_score = %s "
                     "WHERE id = %s", (r["candidate_business"], r["score"], r["source_record_id"]))
        conn.execute("UPDATE match_review SET status = 'rejected', resolved_at = now() "
                     "WHERE source_record_id = %s AND id <> %s AND status = 'pending'", (r["source_record_id"], r["id"]))
        conn.execute("UPDATE match_review SET status = 'matched', resolved_at = now() WHERE id = %s", (review_id,))
    else:
        conn.execute("UPDATE match_review SET status = 'rejected', resolved_at = now() WHERE id = %s", (review_id,))
        left = conn.execute("SELECT count(*) AS n FROM match_review WHERE source_record_id = %s AND status = 'pending'",
                            (r["source_record_id"],)).fetchone()["n"]
        if left == 0:
            # Every candidate rejected: let the next build treat it as a non-match.
            conn.execute("UPDATE source_records SET match_method = NULL WHERE id = %s AND match_method = 'review'",
                         (r["source_record_id"],))
    sync_loan_links(conn)
    conn.commit()
    return {"id": review_id, "accepted": accept}
