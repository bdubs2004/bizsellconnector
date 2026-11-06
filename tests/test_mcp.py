import anyio
import pytest
from mcp import Client

from conftest import biz_id
from dealfeed import mcp_server
from dealfeed.services import accounts

TOOLS = {"search_targets", "get_business", "explain_score", "sba_deal_math", "market_summary",
         "save_target", "list_saved_targets", "export_targets"}


@pytest.fixture(scope="module")
def users(built):
    conn = built[0]
    accounts.create_user(conn, "mcp@example.com", "searcher")
    accounts.create_user(conn, "mcpfree@example.com", "free")


def call(tool, args, email):
    async def go():
        async with Client(mcp_server.build_server(http=False)) as c:
            return await c.call_tool(tool, args)
    import os
    os.environ["DEALFEED_LOCAL_USER"] = email
    return anyio.run(go)


def test_tools_listed(built):
    async def go():
        async with Client(mcp_server.build_server(http=False)) as c:
            return await c.list_tools()
    names = {t.name for t in anyio.run(go).tools}
    assert TOOLS <= names


def test_search_and_profile_via_mcp(users, conn):
    r = call("search_targets", {"industry": "pest control", "limit": 3}, "mcp@example.com")
    assert not r.is_error, r.content
    data = r.structured_content
    assert data["returned"] <= 3 and data["results"][0]["score"] >= 80
    bid = data["results"][0]["business_id"]
    p = call("get_business", {"business_id": bid}, "mcp@example.com").structured_content
    assert p["business_id"] == bid
    s = call("save_target", {"business_id": biz_id(conn, "Heartland%"), "status": "contacted"}, "mcp@example.com")
    assert s.structured_content["status"] == "contacted"


def test_deal_math_via_mcp(users):
    r = call("sba_deal_math", {"purchase_price": 1_050_000, "cash_flow": 350_000, "interest_rate_pct": 10.25},
             "mcp@example.com")
    assert not r.is_error and r.structured_content["purchase_multiple"] == 3.0


def test_free_plan_blocked_from_mcp(users):
    r = call("search_targets", {}, "mcpfree@example.com")
    assert r.is_error and "Upgrade" in r.content[0].text


def test_bad_input_is_tool_error(users):
    r = call("get_business", {"business_id": "nope"}, "mcp@example.com")
    assert r.is_error


def test_api_key_verifier(built):
    conn = built[0]
    u = accounts.get_user_by_email(conn, "mcp@example.com") or accounts.create_user(conn, "mcp@example.com", "searcher")
    key = accounts.create_api_key(conn, u)
    v = mcp_server.ApiKeyVerifier()
    tok = anyio.run(v.verify_token, key)
    assert tok and tok.subject == str(u.id)
    assert anyio.run(v.verify_token, "dfk_wrong") is None
