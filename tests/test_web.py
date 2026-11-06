import re

import pytest
from fastapi.testclient import TestClient

from conftest import biz_id, page_csrf, web_login
from dealfeed.api.app import app


@pytest.fixture
def web(built):
    """A browser-like client signed in as a fresh searcher-plan user."""
    c = TestClient(app)
    r = web_login(c, "web@example.com")
    assert r.status_code == 303 and r.headers["location"] == "/search"
    return c


HX = {"HX-Request": "true"}


def test_landing_shows_free_sample_without_login(built):
    r = TestClient(app).get("/")
    assert r.status_code == 200
    assert "Hawkeye Pest Solutions" in r.text
    # Recently acquired business (score 40) is kept out of the showcase
    assert "Cedar Valley Janitorial" not in r.text


def test_protected_pages_redirect_to_login(built):
    c = TestClient(app)
    for path in ("/search", "/pipeline", "/connect", "/deal-math"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")


def test_login_rejects_open_redirect_and_bad_email(built):
    c = TestClient(app)
    r = web_login(c, "x@example.com", next_url="//evil.example")
    assert r.headers["location"] == "/search"
    c2 = TestClient(app)
    assert c2.post("/login", data={"email": "not-an-email", "csrf_token": page_csrf(c2)}).status_code == 400


def test_dev_login_disabled_in_production(built, monkeypatch):
    monkeypatch.setenv("DEALFEED_ENV", "production")
    c = TestClient(app)
    r = c.post("/login", data={"email": "prod@example.com", "csrf_token": page_csrf(c)}, follow_redirects=False)
    assert r.status_code == 403


def test_search_full_page_and_htmx_partial(web):
    full = web.get("/search", params={"industry": "pest_control", "min_years": "20"})
    assert full.status_code == 200 and "<form id=\"filters\"" in full.text and "Hawkeye" in full.text
    part = web.get("/search", params={"industry": "insurance_agency"}, headers=HX | {"HX-Target": "results"})
    assert "<form id=\"filters\"" not in part.text, "HTMX gets only the results fragment"
    assert "Heartland Insurance" in part.text and "Hawkeye" not in part.text


def test_sized_only_filter(web):
    r = web.get("/search", params={"industry": "pest_control", "min_employees": "1", "sized_only": "on"})
    assert "Hawkeye" in r.text and "Old Mill" not in r.text, "Old Mill has no PPP size estimate"
    r = web.get("/search", params={"industry": "pest_control", "min_employees": "1"})
    assert "Old Mill" in r.text, "unknown-size businesses are included by default"


def test_map_points_json_is_script_safe(web, conn):
    bid = biz_id(conn, "Hawkeye%")
    conn.execute("UPDATE businesses SET latitude = 41.6, longitude = -93.6 WHERE id = %s", (bid,))
    conn.commit()
    try:
        r = web.get("/search", params={"industry": "pest_control"})
        m = re.search(r'id="map-points">(.*?)</script>', r.text, re.S)
        assert m and "41.6" in m.group(1) and "<" not in m.group(1)
    finally:
        conn.execute("UPDATE businesses SET latitude = NULL, longitude = NULL WHERE id = %s", (bid,))
        conn.commit()


def test_business_page_save_and_pipeline_flow(web, conn):
    bid = biz_id(conn, "Old Mill%")
    page = web.get(f"/business/{bid}")
    assert page.status_code == 200 and "Likely to sell:" in page.text and "Fit for your buy box" in page.text
    r = web.post(f"/business/{bid}/save", data={"status": "researching", "notes": "Ask about succession"}, headers=HX)
    assert 'id="save-box"' in r.text and "Saved" in r.text and "Ask about succession" in r.text

    pipe = web.get("/pipeline")
    assert "Old Mill" in pipe.text
    row = web.post(f"/pipeline/{bid}", data={"status": "contacted", "notes": "Left voicemail"}, headers=HX)
    assert row.text.lstrip().startswith(f'<tr id="row-{bid}"') and 'value="contacted" selected' in row.text
    assert "Old Mill" in web.get("/pipeline", params={"status": "contacted"}).text

    csv = web.get("/pipeline/export.csv")
    assert csv.status_code == 200 and "Left voicemail" in csv.text

    gone = web.post(f"/business/{bid}/remove", headers=HX | {"HX-Target": f"row-{bid}"})
    assert gone.text == ""
    assert "Old Mill" not in web.get("/pipeline").text


def test_deal_math_partial_and_validation(web):
    r = web.post("/deal-math", data={"cash_flow": "350000", "purchase_price": "1050000", "interest_rate_pct": "10.5",
                                     "cash_flow_type": "sde"}, headers=HX)
    assert "DSCR" in r.text and "Comfortable" in r.text
    bad = web.post("/deal-math", data={"cash_flow": "350000", "purchase_price": "1050000"}, headers=HX)
    assert "Check your inputs" in bad.text and "interest_rate_pct" in bad.text


def test_connect_create_and_revoke_key(web, conn):
    r = web.post("/connect/keys", data={"label": "Claude Code"})
    key = re.search(r'id="new-key">(dfk_[\w-]+)<', r.text).group(1)
    assert web.get("/v1/me", headers={"Authorization": f"Bearer {key}"}).json()["email"] == "web@example.com"
    kid = conn.execute("SELECT id FROM api_keys WHERE key_prefix = %s", (key[:10],)).fetchone()["id"]
    web.post(f"/connect/keys/{kid}/revoke")
    assert web.get("/v1/me", headers={"Authorization": f"Bearer {key}"}).status_code == 401
    assert key not in web.get("/connect").text, "key is only shown once"


def test_free_plan_sees_upgrade_message(web):
    web.post("/connect/plan", data={"plan": "free"})
    assert "export.csv" not in web.get("/search").text
    r = web.get("/pipeline/export.csv")
    assert r.status_code == 402 and "Exports" in r.text
    web.post("/connect/plan", data={"plan": "searcher"})
