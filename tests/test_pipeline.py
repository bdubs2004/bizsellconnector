"""End-to-end pipeline on the fictional fixtures. Uses its own database reset
because it mutates data (second pull, review decisions)."""
from datetime import datetime, timezone

import polars as pl
import pytest

from conftest import FIX, biz_id, load_fixtures, reset_db
from dealfeed import match, pipeline, score
from dealfeed.ingest import states


@pytest.fixture(scope="module")
def fresh(built):
    built[0].rollback()            # release any locks the shared session connection holds
    conn = reset_db()
    result = load_fixtures(conn)
    yield conn, result
    conn.close()
    # These tests mutate data (second pull, review decisions). Put the shared DB back
    # to the plain fixture state so later modules see what they expect.
    built[0].rollback()
    clean = reset_db()
    load_fixtures(clean)
    clean.close()


def test_build_counts(fresh):
    _, r = fresh
    assert r["spine"]["by_state"] == {
        "CO": {"active": 1, "delinquent": 1, "inactive": 0},
        "CT": {"active": 2, "delinquent": 0, "inactive": 0},
        "IA": {"active": 12, "delinquent": 0, "inactive": 0},
        "NY": {"active": 2, "delinquent": 0, "inactive": 0},
        "OR": {"active": 2, "delinquent": 0, "inactive": 0},
        "TX": {"active": 1, "delinquent": 1, "inactive": 0},
    }
    # 10 exact (IA, CO and TX PPP loans, a 2nd draw and a 7(a)), Sunrise "PEST MGMT" at the same street address,
    # Old Mill "EXTERMINATORS" (gray zone, pest control) -> review, Cornbelt (no registry record) -> new,
    # Iowa Pest Pros 7(a) -> unmatched
    assert r["match"] == {"exact": 10, "address": 1, "review": 1, "new_from_ppp": 1, "unmatched": 1}
    assert r["score"]["scored"] == 23   # 22 registry businesses + Cornbelt


def test_privacy_rules(fresh):
    conn, _ = fresh
    rows = conn.execute("SELECT source, raw::text AS raw, canon::text AS canon FROM source_records").fetchall()
    for r in rows:
        assert "999 Home Street" not in r["raw"] and "999 HOME" not in r["canon"], "RA address must never be stored"
        assert '"Race"' not in r["raw"] and '"Gender"' not in r["raw"], "PPP demographics must be dropped"
    names = {r["n"] for r in conn.execute("SELECT canon->>'legal_name' AS n FROM source_records")}
    assert "JOHN Q SAMPLE" not in names, "sole-proprietor PPP loans are skipped"
    assert "CORNHUSKER PEST CO" not in names, "out-of-state rows are filtered"


def test_ranking_makes_sense(fresh):
    conn, _ = fresh
    s = {r["legal_name"]: r["score"] for r in conn.execute(
        "SELECT legal_name, score FROM businesses")}
    assert s["Hawkeye Pest Solutions, Inc."] >= 80
    assert s["Cedar Valley Janitorial Services Co"] < 50, "recent change-of-ownership 7(a) must sink the score"
    assert s["River City Cleaning Services LLC"] < 20, "formed 2023"
    assert s["Hawkeye Pest Solutions, Inc."] > s["Golden Plains Auto Repair LLC"], "too-small business ranks lower"


def test_size_estimate_uses_first_draw_and_ranges(fresh):
    conn, _ = fresh
    se = conn.execute("SELECT * FROM size_estimates WHERE business_id = %s",
                      (biz_id(conn, "Cedar Valley%"),)).fetchall()
    assert len(se) == 1
    se = se[0]
    # first draw $245k -> payroll ~ $1.176M; range 0.8x-1.25x
    assert float(se["payroll_low"]) == pytest.approx(941_000, abs=1_000)
    assert float(se["payroll_high"]) == pytest.approx(1_470_000, abs=1_000)
    assert se["employees_low"] < 31 < se["employees_high"]
    assert se["revenue_low"] is not None and se["revenue_low"] < se["revenue_high"]


def test_review_accept_links_record(fresh):
    conn, _ = fresh
    rev = conn.execute("SELECT r.id FROM match_review r JOIN businesses b ON b.id = r.candidate_business "
                       "WHERE b.legal_name LIKE 'Old Mill%%' AND r.status = 'pending'").fetchone()
    assert rev
    match.resolve_review(conn, rev["id"], accept=True)
    pipeline.build(conn)
    old_mill = biz_id(conn, "Old Mill%")
    assert conn.execute("SELECT count(*) AS n FROM size_estimates WHERE business_id = %s", (old_mill,)).fetchone()["n"] == 1
    assert conn.execute("SELECT naics_source FROM businesses WHERE id = %s", (old_mill,)).fetchone()["naics_source"] == "ppp"


def test_second_pull_marks_missing_inactive_and_keeps_ids(fresh, tmp_path):
    conn, _ = fresh
    hawkeye_before = biz_id(conn, "Hawkeye%")
    conn.execute("INSERT INTO users (id, email) VALUES (gen_random_uuid(), 'keep@example.com')")
    uid = conn.execute("SELECT id FROM users WHERE email = 'keep@example.com'").fetchone()["id"]
    conn.execute("INSERT INTO saved_targets (user_id, business_id) VALUES (%s, %s)", (uid, hawkeye_before))
    conn.commit()

    df = pl.read_csv(FIX / "ia_sos_sample.csv", infer_schema=False)
    df.filter(~pl.col("legal_name").str.starts_with("Old Mill")).write_csv(tmp_path / "pull2.csv")
    states.load(conn, "ia_sos", tmp_path / "pull2.csv", datetime(2026, 11, 1, tzinfo=timezone.utc))
    pipeline.build(conn)

    assert biz_id(conn, "Hawkeye%") == hawkeye_before
    assert conn.execute("SELECT count(*) AS n FROM saved_targets WHERE user_id = %s", (uid,)).fetchone()["n"] == 1
    row = conn.execute("SELECT id, status FROM businesses WHERE legal_name LIKE 'Old Mill%%'").fetchone()
    assert row["status"] == "inactive"
    reasons = [b["reason"] for b in score.explain(conn, [row["id"]])[row["id"]]["breakdown"]]
    assert any("latest Iowa registry" in r for r in reasons)
    # RA tenure widens across pulls
    ppl = conn.execute("SELECT first_seen, last_seen FROM business_people WHERE business_id = %s",
                       (hawkeye_before,)).fetchone()
    assert str(ppl["first_seen"]) == "2026-10-01" and str(ppl["last_seen"]) == "2026-11-01"


def test_storage_is_slim(fresh):
    conn, _ = fresh
    assert conn.execute("SELECT count(*) AS n FROM registry_stage").fetchone()["n"] == 0, "build empties the stage"
    fat = conn.execute("SELECT count(*) AS n FROM source_records WHERE raw <> '{}'::jsonb OR "
                       "(source NOT IN ('ppp', 'sba_7a', 'sba_504') AND canon <> '{}'::jsonb)").fetchone()["n"]
    assert fat == 0, "no JSON copies of registry rows and no raw rows in the database"
    # A rebuild without a new pull must not lose anything (stage is empty, businesses stay as they are)
    before = conn.execute("SELECT count(*) AS n, sum(score) AS s FROM businesses WHERE status <> 'inactive'").fetchone()
    pipeline.build(conn)
    after = conn.execute("SELECT count(*) AS n, sum(score) AS s FROM businesses WHERE status <> 'inactive'").fetchone()
    assert before == after


def test_explanations_match_stored_scores(fresh):
    conn, _ = fresh
    rows = conn.execute("SELECT id, score FROM businesses").fetchall()
    ex = score.explain(conn, [r["id"] for r in rows])
    assert all(ex[r["id"]]["score"] == r["score"] for r in rows)


def test_stats_runs(fresh):
    conn, _ = fresh
    st = pipeline.stats(conn)
    assert st["pending_reviews"] == 0
    assert st["pct_with_size"] is not None
