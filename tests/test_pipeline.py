"""End-to-end pipeline on the fictional fixtures. Uses its own database reset
because it mutates data (second pull, review decisions)."""
from datetime import datetime, timezone

import polars as pl
import pytest

from conftest import FIX, biz_id, load_fixtures, reset_db
from dealfeed import match, pipeline
from dealfeed.ingest import ia_sos


@pytest.fixture(scope="module")
def fresh():
    conn = reset_db()
    result = load_fixtures(conn)
    yield conn, result
    conn.close()


def test_build_counts(fresh):
    _, r = fresh
    assert r["spine"] == {"active": 12, "inactive": 0}
    # 8 exact (incl. a 2nd-draw loan and the 7(a)), Sunrise "PEST MGMT" -> review,
    # Cornbelt (no SOS record) -> new business, Iowa Pest Pros 7(a) -> unmatched
    assert r["match"] == {"exact": 8, "review": 1, "new_from_ppp": 1, "unmatched": 1}
    assert r["score"]["scored"] == 13


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
        "SELECT b.legal_name, s.score FROM businesses b JOIN scores s ON s.business_id = b.id")}
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
                       "WHERE b.legal_name LIKE 'Sunrise%%' AND r.status = 'pending'").fetchone()
    assert rev
    match.resolve_review(conn, rev["id"], accept=True)
    pipeline.build(conn)
    sunrise = biz_id(conn, "Sunrise%")
    assert conn.execute("SELECT count(*) AS n FROM size_estimates WHERE business_id = %s", (sunrise,)).fetchone()["n"] == 1
    assert conn.execute("SELECT naics_source FROM businesses WHERE id = %s", (sunrise,)).fetchone()["naics_source"] == "ppp"


def test_second_pull_marks_missing_inactive_and_keeps_ids(fresh, tmp_path):
    conn, _ = fresh
    hawkeye_before = biz_id(conn, "Hawkeye%")
    conn.execute("INSERT INTO users (id, email) VALUES (gen_random_uuid(), 'keep@example.com')")
    uid = conn.execute("SELECT id FROM users WHERE email = 'keep@example.com'").fetchone()["id"]
    conn.execute("INSERT INTO saved_targets (user_id, business_id) VALUES (%s, %s)", (uid, hawkeye_before))
    conn.commit()

    df = pl.read_csv(FIX / "ia_sos_sample.csv", infer_schema=False)
    df.filter(~pl.col("Legal Name").str.starts_with("Old Mill")).write_csv(tmp_path / "pull2.csv")
    ia_sos.load(conn, tmp_path / "pull2.csv", datetime(2026, 11, 1, tzinfo=timezone.utc))
    pipeline.build(conn)

    assert biz_id(conn, "Hawkeye%") == hawkeye_before
    assert conn.execute("SELECT count(*) AS n FROM saved_targets WHERE user_id = %s", (uid,)).fetchone()["n"] == 1
    row = conn.execute("SELECT b.status, s.score, s.reasons FROM businesses b JOIN scores s ON s.business_id = b.id "
                       "WHERE legal_name LIKE 'Old Mill%%'").fetchone()
    assert row["status"] == "inactive"
    assert any("latest Iowa SOS" in r for r in row["reasons"])
    # RA tenure widens across pulls
    ppl = conn.execute("SELECT first_seen, last_seen FROM business_people WHERE business_id = %s",
                       (hawkeye_before,)).fetchone()
    assert str(ppl["first_seen"]) == "2026-10-01" and str(ppl["last_seen"]) == "2026-11-01"


def test_stats_runs(fresh):
    conn, _ = fresh
    st = pipeline.stats(conn)
    assert st["pending_reviews"] == 0
    assert st["pct_with_size"] is not None
