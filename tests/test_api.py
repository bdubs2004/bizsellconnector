import pytest
from fastapi.testclient import TestClient

from conftest import biz_id
from dealfeed.api.app import app
from dealfeed.services import accounts


@pytest.fixture(scope="module")
def keys(built):
    conn = built[0]
    fund = accounts.create_user(conn, "fund@example.com", "fund")
    free = accounts.create_user(conn, "free@example.com", "free")
    return {"fund": accounts.create_api_key(conn, fund), "free": accounts.create_api_key(conn, free)}


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def h(key):
    return {"Authorization": f"Bearer {key}"}


def test_requires_key(client, built):
    assert client.post("/v1/targets/search", json={}).status_code == 401
    assert client.post("/v1/targets/search", json={}, headers=h("dfk_nope")).status_code == 401


def test_search_ranked_with_reasons_and_dates(client, keys):
    r = client.post("/v1/targets/search", json={"industry": "pest control", "min_years_in_business": 20},
                    headers=h(keys["fund"]))
    assert r.status_code == 200, r.text
    body = r.json()
    names = [t["name"] for t in body["results"]]
    assert names[0].startswith(("Hawkeye", "Prairie"))
    assert all(t["years_in_business"] >= 20 for t in body["results"])
    top = body["results"][0]
    assert top["top_reasons"] and top["data_as_of"]["iowa_sos"]
    assert top["size_estimate"]["source"] == "SBA PPP loan data"
    assert any("directional" in n for n in body["notes"])


def test_search_filters(client, keys):
    # Size filters match on range overlap: Hawkeye (10-19) is out at 20+, Cornbelt (18-33) is in.
    r = client.post("/v1/targets/search", json={"counties": ["Polk County"], "min_employees": 20,
                                                "include_unknown_size": False}, headers=h(keys["fund"]))
    names = {t["name"] for t in r.json()["results"]}
    assert names == {"CORNBELT CLEANING CREW INC"}


def test_business_profile_and_score(client, keys, conn):
    bid = biz_id(conn, "Hawkeye%")
    r = client.get(f"/v1/businesses/{bid}", headers=h(keys["fund"]))
    assert r.status_code == 200
    p = r.json()
    assert {s["source"].split(",")[0] for s in p["sources"]} >= {"Iowa Secretary of State"}
    assert p["sba_loans"][0]["program"] == "ppp"
    assert p["people_from_public_filings"][0]["role"] == "registered agent"
    assert "raw" not in str(p.keys())
    e = client.get(f"/v1/businesses/{bid}/score", headers=h(keys["fund"])).json()
    assert e["score"] == p["score"] and "+" in e["explanation"]
    assert client.get("/v1/businesses/not-a-uuid", headers=h(keys["fund"])).status_code == 404


def test_market_summary(client, keys):
    r = client.get("/v1/markets/summary", params={"industry": "pest control"}, headers=h(keys["fund"]))
    m = r.json()
    assert m["businesses"]["total"] >= 4
    # Iowa Pest Pros 7(a) (unmatched to any business) still counts toward the benchmark
    assert m["sba_7a_504_last_5_years"]["loans"] == 1
    assert m["sba_7a_504_last_5_years"]["change_of_ownership_loans"] == 1


def test_saved_targets_and_export(client, keys, conn):
    bid = biz_id(conn, "Prairie%")
    r = client.post("/v1/saved-targets", json={"business_id": bid, "status": "researching", "notes": "call Tue"},
                    headers=h(keys["fund"]))
    assert r.status_code == 200, r.text
    lst = client.get("/v1/saved-targets", headers=h(keys["fund"])).json()
    assert lst["targets"][0]["pipeline_status"] == "researching"
    assert client.post("/v1/saved-targets", json={"business_id": bid, "status": "bogus"},
                       headers=h(keys["fund"])).status_code == 400
    csv = client.post("/v1/exports", json={"saved": True}, headers=h(keys["fund"]))
    assert csv.status_code == 200 and "Prairie" in csv.text and "call Tue" in csv.text


def test_deal_math(client, keys):
    r = client.post("/v1/deal-math", json={"purchase_price": 900000, "cash_flow": 300000, "interest_rate_pct": 10.5},
                    headers=h(keys["fund"]))
    assert r.status_code == 200 and r.json()["dscr"] > 1
    assert client.post("/v1/deal-math", json={"purchase_price": 1, "cash_flow": 1},
                       headers=h(keys["fund"])).status_code == 422


def test_free_plan_limits(client, keys, conn):
    r = client.post("/v1/targets/search", json={"limit": 50}, headers=h(keys["free"]))
    assert r.json()["returned"] <= 10
    assert client.post("/v1/exports", json={"saved": True}, headers=h(keys["free"])).status_code == 402
    # Burn the rest of the monthly quota, then expect a 402
    free = accounts.get_user_by_email(conn, "free@example.com")
    accounts.record_usage(conn, free, "test", 1000, "api")
    assert client.post("/v1/targets/search", json={}, headers=h(keys["free"])).status_code == 402
    me = client.get("/v1/me", headers=h(keys["free"])).json()
    assert me["rows_used_this_month"] >= 1000
