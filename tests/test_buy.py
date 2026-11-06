"""Buy box, buy score and priority ranking."""
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conftest import biz_id, web_login
from dealfeed.api.app import app
from dealfeed.buy import BuyBox, evaluate_buy, priority
from dealfeed.services import accounts, buybox, search
from dealfeed.services.search import SearchParams

TODAY = date(2026, 11, 6)
PEST = {"naics_code": "561710", "naics_source": "ppp", "state": "IA", "status": "active",
        "formation_date": date(1990, 1, 1), "revenue_low": 2_000_000, "revenue_high": 3_000_000}


def reasons(fired):
    return {f.signal: f for f in fired}


def test_good_fit_scores_high_with_reasons():
    score, fired, deal = evaluate_buy(PEST, BuyBox(interest_rate_pct=10.5), TODAY)
    r = reasons(fired)
    assert r["industry_quality"].points == 17 and "recurring" in r["industry_quality"].reason
    assert r["price_fit"].points == 15 and "fits your" in r["price_fit"].reason
    assert r["dscr"].points > 0 and "10.5%" in r["dscr"].reason
    assert deal["price_range"][0] < deal["price_mid"] < deal["price_range"][1]
    assert score >= 80


def test_buy_box_drives_the_score():
    tight = BuyBox(interest_rate_pct=14, industries=["insurance_agency"], states=["CO"], max_price=600_000,
                   cash_available=50_000)
    score, fired, _ = evaluate_buy(PEST, tight, TODAY)
    r = reasons(fired)
    assert r["box_industry"].points == -30 and r["box_state"].points == -30
    assert r["price_fit"].points == -15 and "above" in r["price_fit"].reason
    assert r["cash"].points == -10
    assert score < 30


def test_unknown_size_and_industry_are_neutral_not_fake():
    score, fired, deal = evaluate_buy({"naics_code": None, "state": "IA", "status": "active",
                                       "formation_date": date(1980, 1, 1)}, BuyBox(interest_rate_pct=10), TODAY)
    assert deal is None
    r = reasons(fired)
    assert r["price_fit"].points == 0 and "can't be estimated" in r["price_fit"].reason
    assert r["industry_quality"].points == 0


def test_delinquent_and_sba_cap():
    big = PEST | {"revenue_low": 40_000_000, "revenue_high": 60_000_000, "status": "delinquent"}
    _, fired, _ = evaluate_buy(big, BuyBox(interest_rate_pct=10, max_price=100_000_000, cash_available=10**8), TODAY)
    r = reasons(fired)
    assert r["status"].points == -15 and r["sba_cap"].points == -10


def test_priority_needs_both():
    assert priority(90, 90) == 90 and priority(90, 10) == 30 and priority(None, 50) == 50


def test_buy_box_validation_and_storage(conn):
    u = accounts.create_user(conn, "box@example.com", "searcher")
    assert buybox.get_buy_box(conn, u.id).is_default
    box = buybox.set_buy_box(conn, u.id, buybox.BuyBoxInput(industries=["pest control", "janitorial"], states=["ia"],
                                                            interest_rate_pct=11.25, cash_available=400_000))
    assert box.industries == ["commercial_cleaning", "pest_control"] and box.states == ["IA"] and not box.is_default
    with pytest.raises(ValidationError):
        buybox.BuyBoxInput(states=["WY"], interest_rate_pct=10)
    with pytest.raises(ValidationError):
        buybox.BuyBoxInput(min_price=2e6, max_price=1e6, interest_rate_pct=10)


def test_priority_ranking_and_buy_box_filter(conn):
    p = SearchParams(industry="pest control", limit=50)
    pri = search.search_targets(conn, p, 50)["results"]
    assert all(t["priority"] is not None and t["buy_score"] is not None for t in pri)
    assert [t["priority"] for t in pri] == sorted([t["priority"] for t in pri], reverse=True)
    sell = search.search_targets(conn, p.model_copy(update={"sort": "sell"}), 50)["results"]
    assert [t["score"] for t in sell] == sorted([t["score"] for t in sell], reverse=True)
    box = BuyBox(interest_rate_pct=10, states=["TX"])
    only_tx = search.search_targets(conn, SearchParams(in_buy_box=True, limit=50), 50, box=box)["results"]
    assert only_tx and {t["state"] for t in only_tx} == {"TX"}


def test_api_and_web_buy_box(conn):
    c = TestClient(app)
    u = accounts.create_user(conn, "boxapi@example.com", "searcher")
    h = {"Authorization": f"Bearer {accounts.create_api_key(conn, u)}"}
    assert c.get("/v1/buy-box", headers=h).json()["is_default"] is True
    r = c.put("/v1/buy-box", headers=h, json={"industries": ["pest control"], "states": ["CO"], "interest_rate_pct": 10})
    assert r.status_code == 200 and r.json()["states"] == ["CO"]
    top = c.post("/v1/targets/search", headers=h, json={"industry": "pest control", "limit": 10}).json()["results"][0]
    assert top["state"] == "CO", "Rocky Mountain is the only pest control target in this buy box's state"
    bid = biz_id(conn, "Hawkeye%")
    ex = c.get(f"/v1/businesses/{bid}/score", headers=h).json()
    assert "Outside your target states" in ex["buy_explanation"]

    web_login(c, "boxweb@example.com")
    assert "default buy box" in c.get("/buy-box").text
    r = c.post("/buy-box", data={"industries": ["pest_control"], "states": ["IA"], "min_price": "500000",
                                 "max_price": "3000000", "cash_available": "250000", "buyer_salary": "90000",
                                 "interest_rate_pct": "11", "term_years": "10", "min_dscr": "1.25"},
               follow_redirects=False)
    assert r.status_code == 303
    page = c.get(f"/business/{bid}").text
    assert "Fit for your buy box" in page and "Using default assumptions" not in page and "Open in deal math" in page
    bad = c.post("/buy-box", data={"interest_rate_pct": "11", "min_price": "9", "max_price": "1"})
    assert bad.status_code == 400 and "Check your inputs" in bad.text
