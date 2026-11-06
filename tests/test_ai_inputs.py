"""How AIs actually call the tools: loose formats, aliases, mistakes. Every bad input must come back as a
precise, fixable message (never a generic failure or a stack trace), and every reasonable variant must work.
Found by scripts/ai_harness.py."""
import anyio
import pytest
from mcp import Client

from conftest import biz_id
from dealfeed import inputs, mcp_server
from dealfeed.services import accounts


@pytest.fixture
def ai(built, monkeypatch):
    conn = built[0]
    u = accounts.get_user_by_email(conn, "ai-inputs@example.com") or accounts.create_user(conn, "ai-inputs@example.com", "fund")
    conn.execute("DELETE FROM row_views WHERE user_id = %s", (u.id,))
    conn.execute("DELETE FROM api_usage WHERE user_id = %s", (u.id,))
    conn.commit()
    monkeypatch.setenv("DEALFEED_LOCAL_USER", "ai-inputs@example.com")

    def call(tool, args):
        async def go():
            async with Client(mcp_server.build_server(http=False)) as c:
                r = await c.call_tool(tool, args)
                return r.is_error, (" ".join(getattr(x, "text", "") for x in r.content) if r.is_error
                                    else r.structured_content)
        return anyio.run(go)
    call.user = u
    return call


@pytest.mark.parametrize("args", [
    {"state": "Iowa"}, {"state": "ia"}, {"state": " iowa "}, {"state": "Iowa State"},
    {"industry": "Pest Control Companies"}, {"industry": "exterminators"},
    {"min_years_in_business": "20+"}, {"min_years_in_business": "20 years"}, {"limit": "10"},
    {"naics_codes": [561710]}, {"counties": ["Polk County, IA"]}, {"counties": ["polk county iowa"]},
    {"min_payroll": "$250k", "max_payroll": "3M"}, {"sort": "score"}, {"sort": "likely_to_sell"},
    {"sort": "fit"}, {"sort": "SELL"}, {"limit": 500}, {"limit": 0},
])
def test_loose_inputs_work(ai, args):
    err, out = ai("search_targets", args)
    assert not err, out


def test_aliases_resolve_correctly(ai):
    assert ai("search_targets", {"state": "Iowa"})[1]["filters_applied"]["state"] == "IA"
    polk = ai("search_targets", {"counties": ["Polk County, IA"], "limit": 50})[1]
    assert polk["returned"] and {r["county"] for r in polk["results"]} == {"POLK"}
    sell = ai("search_targets", {"sort": "likely_to_sell", "limit": 50})[1]["results"]
    assert [r["score"] for r in sell] == sorted([r["score"] for r in sell], reverse=True)


@pytest.mark.parametrize("tool,args,expect", [
    ("search_targets", {"state": "WY"}, "Covered states"),
    ("search_targets", {"state": "Narnia"}, "two-letter code"),
    ("search_targets", {"sort": "revenue"}, "Use 'priority'"),
    ("search_targets", {"min_employees": 50, "max_employees": 5}, "greater than max_employees"),
    ("search_targets", {"offset": 5000}, "offset"),
    ("search_targets", {"min_score": 101}, "min_score"),
    ("sba_deal_math", {"purchase_price": 1e6, "cash_flow": 3e5, "interest_rate_pct": 10, "cash_flow_type": "revenue"},
     "Revenue isn't cash flow"),
    ("sba_deal_math", {"purchase_price": 0, "cash_flow": 3e5, "interest_rate_pct": 10}, "purchase_price"),
    ("sba_deal_math", {"purchase_price": "a lot", "cash_flow": 3e5, "interest_rate_pct": 10}, "Couldn't read"),
    ("set_buy_box", {"interest_rate_pct": 10, "states": ["Narnia"]}, "Covered states"),
    ("set_buy_box", {"interest_rate_pct": 10, "industries": ["HVAC"]}, "Unknown industry"),
    ("market_summary", {"industry": "HVAC"}, "naics_code"),
    ("save_target", {"business_id": "x", "status": "won"}, "Use one of"),
])
def test_bad_inputs_get_precise_messages(ai, tool, args, expect):
    err, msg = ai(tool, args)
    assert err and expect in msg, msg
    assert "Traceback" not in msg and "validation error for" not in msg.lower().replace("invalid input", "")


def test_money_strings_and_case_in_deal_math_and_buy_box(ai):
    err, out = ai("sba_deal_math", {"purchase_price": "$1.05M", "cash_flow": "350k", "interest_rate_pct": "10.5%",
                                    "cash_flow_type": "SDE"})
    assert not err and out["purchase_multiple"] == 3.0
    err, box = ai("set_buy_box", {"interest_rate_pct": "11%", "states": ["Iowa", "texas"], "cash_available": "250k",
                                  "industries": "pest control"})
    assert not err and box["states"] == ["IA", "TX"] and box["cash_available"] == 250_000


def test_ids_statuses_and_unknown_industry_note(ai, conn):
    bid = biz_id(conn, "Hawkeye%")
    for variant in (f"  {bid}  ", bid.upper(), "{" + bid + "}"):
        assert not ai("get_business", {"business_id": variant})[0]
    assert not ai("save_target", {"business_id": bid, "status": "Researching "})[0]
    lst = ai("list_saved_targets", {"status": "all"})[1]
    assert lst["targets"][0]["pipeline_status"] == "researching"
    out = ai("search_targets", {"industry": "HVAC"})[1]
    assert any("isn't one of the classified industries" in n for n in out["notes"])


def test_repeat_views_dont_burn_quota(ai, conn):
    for _ in range(4):
        assert not ai("search_targets", {"industry": "pest control", "limit": 10})[0]
    once = ai("search_targets", {"industry": "pest control", "limit": 10})[1]["returned"]
    used = accounts.rows_used_today(conn, ai.user)
    assert used == once, f"5 identical searches charged {used} rows, expected {once}"
    bid = biz_id(conn, "Hawkeye%")
    ai("get_business", {"business_id": bid})
    assert accounts.rows_used_today(conn, ai.user) == used, "profile of an already-seen business is free"


def test_ai_defaults_keep_responses_small(ai):
    out = ai("search_targets", {})[1]
    assert out["returned"] <= 10
    err, exp = ai("export_targets", {"saved": False, "max_rows": 5})
    assert not err and len(exp["csv"].strip().splitlines()) <= 6 and "business_ids" not in exp


def test_number_parser():
    assert inputs.number("$1.05M") == 1_050_000 and inputs.number("350k") == 350_000
    assert inputs.number("2.5 million") == 2_500_000 and inputs.number("10.5%") == 10.5
    assert inputs.number("1,050,000") == 1_050_000 and inputs.number(None) is None
    with pytest.raises(ValueError):
        inputs.number("lots")
