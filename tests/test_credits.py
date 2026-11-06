"""Credits model (ZoomInfo / Apollo style): viewing is free under a fair-use cap; data that leaves the
platform (CSV exports, REST API results) costs 1 credit per business per month."""
import uuid

import pytest
from fastapi.testclient import TestClient

from conftest import biz_id, web_login
from dealfeed.api.app import app
from dealfeed.services import accounts


def fresh(conn, email, plan="searcher"):
    u = accounts.get_user_by_email(conn, email) or accounts.create_user(conn, email, plan)
    for t in ("credit_charges", "credit_grants", "row_views"):
        conn.execute(f"DELETE FROM {t} WHERE user_id = %s", (u.id,))
    conn.commit()
    return accounts.set_plan(conn, u, plan)


def burn(conn, u, leave=0):
    n = accounts.credits(conn, u)["left"] - leave
    accounts.charge_credits(conn, u, [uuid.uuid4() for _ in range(n)], "test")


def api(conn, email, plan="searcher"):
    u = fresh(conn, email, plan)
    return u, {"Authorization": f"Bearer {accounts.create_api_key(conn, u)}"}


def test_monthly_allowance_then_top_ups(built):
    conn = built[0]
    u = fresh(conn, "credits-math@example.com")
    assert accounts.credits(conn, u)["left"] == 500
    burn(conn, u)
    with pytest.raises(accounts.InsufficientCredits, match="Out of credits"):
        accounts.charge_credits(conn, u, [uuid.uuid4()], "export")
    c = accounts.grant_credits(conn, u, 100, "top-up")
    assert c["left"] == c["topup_left"] == 100 and c["monthly_left"] == 0
    assert isinstance(c["left"], int), "JSON clients need numbers"
    ids = [uuid.uuid4() for _ in range(40)]
    assert accounts.charge_credits(conn, u, ids, "export") == 40
    assert accounts.charge_credits(conn, u, ids, "export") == 0, "same businesses again this month: free"
    assert accounts.credits(conn, u)["topup_left"] == 60


def test_api_pulls_cost_credits_once(built):
    conn = built[0]
    u, h = api(conn, "credits-api@example.com")
    c = TestClient(app)
    first = c.post("/v1/targets/search", json={"industry": "pest control", "limit": 5}, headers=h).json()
    assert first["credits_charged"] == first["returned"] > 0
    again = c.post("/v1/targets/search", json={"industry": "pest control", "limit": 5}, headers=h).json()
    assert again["credits_charged"] == 0
    bid = first["results"][0]["business_id"]
    assert c.get(f"/v1/businesses/{bid}", headers=h).json()["credits_charged"] == 0
    assert accounts.credits(conn, u)["used_this_month"] == first["returned"]


def test_api_search_trims_to_credits_left(built):
    conn = built[0]
    u, h = api(conn, "credits-trim@example.com")
    burn(conn, u, leave=2)
    out = TestClient(app).post("/v1/targets/search", json={"limit": 10}, headers=h).json()
    assert out["returned"] == 2 and out["credits_charged"] == 2
    assert any("out of credits" in n for n in out["notes"])


def test_saved_list_via_api_is_charged(built):
    conn = built[0]
    u, h = api(conn, "credits-saved@example.com")
    c = TestClient(app)
    bid = biz_id(conn, "Hawkeye%")
    c.post("/v1/saved-targets", json={"business_id": bid}, headers=h)
    assert c.get("/v1/saved-targets", headers=h).json()["credits_charged"] == 1
    assert c.get("/v1/saved-targets", headers=h).json()["credits_charged"] == 0


def test_export_trims_to_credits_and_reports(built):
    conn = built[0]
    u, h = api(conn, "credits-export@example.com")
    burn(conn, u, leave=3)
    r = TestClient(app).post("/v1/exports", json={"search": {"industry": "pest control"}}, headers=h)
    assert r.status_code == 200
    assert r.headers["X-Rows"] == "3" and r.headers["X-Credits-Charged"] == "3" and r.headers["X-Credits-Left"] == "0"
    assert int(r.headers["X-Rows-Matched"]) > 3 and len(r.text.strip().splitlines()) == 4
    r = TestClient(app).post("/v1/exports", json={"search": {"industry": "janitorial"}}, headers=h)
    assert r.status_code == 402 and "Out of credits" in r.json()["detail"]


def test_web_viewing_is_free_and_export_charges(built):
    conn = built[0]
    u = fresh(conn, "credits-web@example.com")
    c = TestClient(app)
    web_login(c, "credits-web@example.com")
    assert c.get("/search?industry=pest+control").status_code == 200
    assert c.get(f"/business/{biz_id(conn, 'Hawkeye%')}").status_code == 200
    assert accounts.credits(conn, u)["used_this_month"] == 0 and accounts.views_today(conn, u) > 0
    r = c.get("/search/export.csv?industry=pest+control")
    assert r.status_code == 200 and accounts.credits(conn, u)["used_this_month"] == len(r.text.strip().splitlines()) - 1
    page = c.get("/connect").text
    assert "export credits left" in page and "new businesses viewed today" in page


def test_fair_use_cap_blocks_new_views_not_repeat_ones(built):
    conn = built[0]
    u, h = api(conn, "credits-fairuse@example.com")
    c = TestClient(app)
    seen = c.post("/v1/targets/search", json={"limit": 1}, headers=h).json()["results"][0]["business_id"]
    accounts.record_views(conn, u, [uuid.uuid4() for _ in range(u.plan.views_per_day)])
    r = c.post("/v1/targets/search", json={}, headers=h)
    assert r.status_code == 402 and "fair-use" in r.json()["detail"]
    assert c.get(f"/v1/businesses/{seen}", headers=h).status_code == 200, "already-seen business still opens"
