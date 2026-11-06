"""Entity resolution (scope 3.2 step 3).

The Iowa SOS file is the spine: each SOS entity becomes one business, and its
business id equals its source_record id so it is stable across rebuilds.
SBA records (PPP, 7(a), 504) are then linked to those businesses:

  1. exact match on core name + ZIP
  2. fuzzy match (rapidfuzz) within the same ZIP, falling back to the same city
  3. gray-zone scores go to Claude (if enabled), then to the manual review queue
  4. unmatched PPP borrowers become their own business (they were operating in
     2020-21 even if they aren't an SOS entity, e.g. DBAs filed by county);
     unmatched 7(a)/504 records stay unlinked
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

import psycopg
from rapidfuzz import fuzz, process

from . import llm
from .config import get_settings

log = logging.getLogger(__name__)

LOAN_SOURCES = ("ppp", "sba_7a", "sba_504")


def build_spine(conn: psycopg.Connection) -> dict:
    """Upsert businesses from IA SOS records and mark ones missing from the latest pull inactive."""
    latest = conn.execute(
        "SELECT max(id) AS id FROM pipeline_runs WHERE source = 'ia_sos' AND status = 'ok'"
    ).fetchone()["id"]
    if latest is None:
        return {"businesses": 0, "note": "no successful ia_sos run yet"}
    conn.execute(
        """
        INSERT INTO businesses (id, legal_name, normalized_name, entity_type, state_of_formation, formation_date,
                                status, address_line1, city, state, zip, primary_source, updated_at)
        SELECT sr.id, sr.canon->>'legal_name', sr.canon->>'normalized_name', sr.canon->>'entity_type',
               sr.canon->>'state_of_formation', (sr.canon->>'formation_date')::date,
               CASE WHEN sr.run_id = %(latest)s THEN 'active' ELSE 'inactive' END,
               sr.canon->>'address_line1', sr.canon->>'city', coalesce(sr.canon->>'state', 'IA'),
               sr.canon->>'zip5', 'ia_sos', now()
          FROM source_records sr
         WHERE sr.source = 'ia_sos'
        ON CONFLICT (id) DO UPDATE SET
            legal_name = EXCLUDED.legal_name, normalized_name = EXCLUDED.normalized_name,
            entity_type = EXCLUDED.entity_type, state_of_formation = EXCLUDED.state_of_formation,
            formation_date = EXCLUDED.formation_date, status = EXCLUDED.status,
            address_line1 = EXCLUDED.address_line1, city = EXCLUDED.city, state = EXCLUDED.state,
            zip = EXCLUDED.zip, updated_at = now()
        """,
        {"latest": latest},
    )
    conn.execute(
        "UPDATE source_records SET business_id = id, match_method = 'spine', match_score = 100 "
        "WHERE source = 'ia_sos' AND business_id IS DISTINCT FROM id"
    )
    # Registered agent as a public-filing person. first_seen/last_seen widen over monthly pulls,
    # which is what the "same agent for 15+ years" signal will eventually rely on.
    conn.execute(
        """
        INSERT INTO business_people (id, business_id, name, role, first_seen, last_seen, source)
        SELECT md5(sr.id::text || upper(sr.canon->>'ra_name') || 'registered agent')::uuid, sr.id,
               upper(sr.canon->>'ra_name'), 'registered agent',
               sr.first_seen_at::date, sr.pulled_at::date, 'ia_sos'
          FROM source_records sr
         WHERE sr.source = 'ia_sos' AND coalesce(sr.canon->>'ra_name', '') <> ''
        ON CONFLICT (business_id, name, role, source) DO UPDATE
           SET last_seen = greatest(business_people.last_seen, EXCLUDED.last_seen),
               first_seen = least(business_people.first_seen, EXCLUDED.first_seen)
        """
    )
    counts = conn.execute(
        "SELECT count(*) FILTER (WHERE status = 'active') AS active, count(*) FILTER (WHERE status = 'inactive') AS inactive "
        "FROM businesses WHERE primary_source = 'ia_sos'"
    ).fetchone()
    conn.commit()
    return dict(counts)


@dataclass
class _Cand:
    id: str
    core: str
    zip: str | None
    city: str | None
    address: str | None
    rec: dict


class _Index:
    def __init__(self, rows: list[dict]):
        self.exact: dict[tuple[str, str | None], list[_Cand]] = defaultdict(list)
        self.by_zip: dict[str, list[_Cand]] = defaultdict(list)
        self.by_city: dict[str, list[_Cand]] = defaultdict(list)
        for r in rows:
            c = _Cand(str(r["id"]), r["core_name"] or "", r["zip"], r["city"], r["address_line1"], r)
            if not c.core:
                continue
            self.exact[(c.core, c.zip)].append(c)
            if c.zip:
                self.by_zip[c.zip].append(c)
            if c.city:
                self.by_city[c.city].append(c)

    def add(self, c: _Cand) -> None:
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
    scored = [(c, s) for c, s in scored if s >= gray]
    return sorted(scored, key=lambda x: -x[1])


def link_loan_records(conn: psycopg.Connection, rematch: bool = False) -> dict:
    s = get_settings()
    if rematch:
        conn.execute(
            "UPDATE source_records SET business_id = NULL, match_method = NULL, match_score = NULL "
            "WHERE source = ANY(%s) AND match_method IN ('exact', 'fuzzy', 'llm', 'unmatched', 'review')",
            (list(LOAN_SOURCES),),
        )
        conn.execute("DELETE FROM match_review WHERE status = 'pending'")

    biz = conn.execute(
        "SELECT b.id, b.legal_name, b.address_line1, b.city, b.zip, "
        "       coalesce(sr.canon->>'core_name', '') AS core_name "
        "  FROM businesses b LEFT JOIN source_records sr ON sr.id = b.id"
    ).fetchall()
    # Businesses created from PPP-only records don't have an SOS record; compute their core name.
    from .normalize import core_name
    for b in biz:
        if not b["core_name"]:
            b["core_name"] = core_name(b["legal_name"])
    idx = _Index(biz)

    todo = conn.execute(
        "SELECT id, source, canon FROM source_records WHERE source = ANY(%s) AND match_method IS NULL "
        "ORDER BY source = 'ppp' DESC, (canon->>'approval_date') NULLS LAST",
        (list(LOAN_SOURCES),),
    ).fetchall()

    rejected = {(str(r["source_record_id"]), str(r["candidate_business"])) for r in conn.execute(
        "SELECT source_record_id, candidate_business FROM match_review WHERE status = 'rejected'").fetchall()}

    stats = defaultdict(int)
    updates: list[tuple] = []         # (business_id, method, score, record_id)
    reviews: list[tuple] = []         # (record_id, business_id, score, verdict)
    new_biz: list[dict] = []

    for row in todo:
        rec = row["canon"]
        rid = str(row["id"])
        if not rec.get("core_name"):
            updates.append((None, "unmatched", None, rid))
            stats["unmatched"] += 1
            continue
        exact = [c for c in idx.exact.get((rec["core_name"], rec.get("zip5")), []) if (rid, c.id) not in rejected]
        if len(exact) == 1:
            updates.append((exact[0].id, "exact", 100.0, rid))
            stats["exact"] += 1
            continue
        cands = exact and [(c, 100.0) for c in exact] or _best(rec, idx, s.match_gray)
        cands = [(c, sc) for c, sc in cands if (rid, c.id) not in rejected]
        top = cands[0] if cands else None
        ambiguous = len(cands) > 1 and cands[1][1] >= top[1] - 2 and top[1] >= s.match_auto
        if top and top[1] >= s.match_auto and not ambiguous:
            updates.append((top[0].id, "fuzzy", top[1], rid))
            stats["fuzzy"] += 1
            continue
        if top and (ambiguous or top[1] >= s.match_gray):
            verdict = None
            if llm.enabled() and not ambiguous:
                v = llm.same_business(top[0].rec | {"zip5": top[0].zip}, rec)
                verdict = v.verdict if v else None
                if verdict == "yes":
                    updates.append((top[0].id, "llm", top[1], rid))
                    stats["llm_yes"] += 1
                    continue
                if verdict == "no":
                    stats["llm_no"] += 1
                    top = None   # fall through to "no match"
            if top:
                for c, sc in cands[:3]:
                    reviews.append((rid, c.id, sc, verdict))
                updates.append((None, "review", top[1], rid))
                stats["review"] += 1
                continue
        # No match
        if row["source"] == "ppp":
            new_biz.append({"id": rid, **rec})
            updates.append((rid, "new", None, rid))
            idx.add(_Cand(rid, rec["core_name"], rec.get("zip5"), rec.get("city"), rec.get("address_line1"), rec))
            stats["new_from_ppp"] += 1
        else:
            updates.append((None, "unmatched", None, rid))
            stats["unmatched"] += 1

    with conn.cursor() as cur:
        if new_biz:
            cur.executemany(
                """
                INSERT INTO businesses (id, legal_name, normalized_name, status, address_line1, city, state, zip,
                                        county, primary_source)
                VALUES (%(id)s, %(legal_name)s, %(normalized_name)s, 'unknown', %(address_line1)s, %(city)s,
                        %(state)s, %(zip5)s, %(county)s, 'ppp')
                ON CONFLICT (id) DO NOTHING
                """,
                new_biz,
            )
        cur.executemany(
            "UPDATE source_records SET business_id = %s, match_method = %s, match_score = %s WHERE id = %s",
            updates,
        )
        cur.executemany(
            "INSERT INTO match_review (source_record_id, candidate_business, score, llm_verdict) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            reviews,
        )
    sync_loan_links(conn)
    conn.commit()
    return dict(stats)


def sync_loan_links(conn: psycopg.Connection) -> None:
    conn.execute(
        "UPDATE sba_loans l SET business_id = sr.business_id FROM source_records sr "
        "WHERE sr.id = l.source_record_id AND l.business_id IS DISTINCT FROM sr.business_id"
    )
    # Fill county from PPP project county where the business has none yet.
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
            conn.execute("UPDATE match_review SET status = 'rejected' WHERE source_record_id = %s",
                         (r["source_record_id"],))
    sync_loan_links(conn)
    conn.commit()
    return {"id": review_id, "accepted": accept}
