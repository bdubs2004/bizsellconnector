"""Live status check against a state registry (state APIs mocked)."""
import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import biz_id
from dealfeed.api.app import app
from dealfeed.ingest import states
from dealfeed.services import live


def mock_client(rows, seen=None, fail=False):
    def handler(req):
        if seen is not None:
            seen.append(dict(req.url.params))
        if fail:
            raise httpx.ConnectError("down", request=req)
        return httpx.Response(200, json=rows)
    return httpx.Client(transport=httpx.MockTransport(handler))


CO_ROW = {"entityid": "19911000001", "entityname": "ROCKY MOUNTAIN PEST PROS, INC.", "entitystatus": "Good Standing",
          "entitytype": "DPC", "entityformdate": "1991-04-02T00:00:00.000", "principaladdress1": "1600 Broadway",
          "principalcity": "Denver", "principalstate": "CO", "principalzipcode": "80202"}


def test_lookup_active_and_query_shape():
    seen = []
    out = states.live_lookup("co_sos", "19911000001", client=mock_client([CO_ROW], seen))
    assert out["found"] and out["active"] and out["raw_status"] == {"entitystatus": "Good Standing"}
    assert seen[0]["$where"] == "entityid = 19911000001", "numeric id unquoted"
    seen = []
    states.live_lookup("ny_dos", "50'0001", client=mock_client([], seen))
    assert seen[0]["$where"] == "dos_id = '50''0001'", "text id quoted and escaped"


def test_lookup_dissolved_missing_unsupported_and_down():
    gone = states.live_lookup("co_sos", "1", client=mock_client([CO_ROW | {"entitystatus": "Voluntarily Dissolved"}]))
    assert gone["found"] and not gone["active"] and gone["raw_status"] == {"entitystatus": "Voluntarily Dissolved"}
    assert states.live_lookup("tx_cpa", "1", client=mock_client([])) == {"ok": True, "supported": True, "found": False}
    assert states.live_lookup("ia_sos", "1")["supported"] is False
    down = states.live_lookup("co_sos", "1", client=mock_client([], fail=True))
    assert down["ok"] is False and "Couldn't reach" in down["error"]
    assert states.live_lookup("co_sos", "abc")["ok"] is False


def test_lookup_oregon_groups_rows():
    rows = [{"registry_number": "800001", "business_name": "CASCADE PEST CONTROL, INC.",
             "entity_type": "DOMESTIC BUSINESS CORPORATION", "registry_date": "1993-08-20T10:00:00.000",
             "associated_name_type": t, "address": a, "city": "Portland", "state": "OR", "zip": "97204"}
            for t, a in (("MAILING ADDRESS", "PO Box 1"), ("PRINCIPAL PLACE OF BUSINESS", "500 New St"))]
    out = states.live_lookup("or_sos", "800001", client=mock_client(rows))
    assert out["active"] and out["record"]["address_line1"].startswith("500 NEW")


@pytest.fixture(autouse=True)
def fresh_cache():
    live.clear_cache()
    yield
    live.clear_cache()


def fake_lookup(result, calls):
    def f(key, source_id, client=None):
        calls.append((key, source_id))
        return dict(result)
    return f


def test_service_reports_changes_and_caches(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(states, "live_lookup", fake_lookup({
        "ok": True, "supported": True, "found": True, "active": True, "status": "active",
        "raw_status": {"entitystatus": "Good Standing"},
        "record": {"legal_name": "ROCKY MOUNTAIN PEST PROS, INC.", "address_line1": "9 NEW LOCATION RD",
                   "city": "DENVER", "zip5": "80202", "status_since": None}}, calls))
    bid = biz_id(conn, "ROCKY MOUNTAIN PEST%")
    out = live.check_business(conn, bid)
    assert out["verdict"] == "active" and calls == [("co_sos", "19911000001")]
    assert [c["field"] for c in out["changes"]] == ["address"]
    assert "Still active with Colorado (Good Standing)" in out["message"]
    again = live.check_business(conn, bid)
    assert again["cached"] and len(calls) == 1, "second check within 10 minutes uses the cache"


def test_service_not_found_and_errors_not_cached(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(states, "live_lookup", fake_lookup({"ok": True, "supported": True, "found": False}, calls))
    out = live.check_business(conn, biz_id(conn, "LONE STAR PEST%"))
    assert out["verdict"] == "not_found" and "No longer on Texas" in out["message"]
    live.clear_cache()
    monkeypatch.setattr(states, "live_lookup", fake_lookup({"ok": False, "supported": True, "error": "down"}, calls))
    bid = biz_id(conn, "NUTMEG%")
    assert live.check_business(conn, bid)["verdict"] == "error"
    live.check_business(conn, bid)
    assert len(calls) == 3, "failures aren't cached, so the next click retries"


def test_service_unsupported_cases(conn):
    assert live.check_business(conn, biz_id(conn, "Hawkeye%"))["verdict"] == "unsupported"      # Iowa
    assert live.check_business(conn, biz_id(conn, "CORNBELT%"))["verdict"] == "unsupported"      # PPP-only


def test_web_and_api_and_mcp(conn, monkeypatch, built):
    monkeypatch.setattr(states, "live_lookup", fake_lookup({
        "ok": True, "supported": True, "found": True, "active": False, "status": "dissolved or withdrawn",
        "raw_status": {"entitystatus": "Voluntarily Dissolved"}, "record": None}, []))
    bid = biz_id(conn, "ROCKY MOUNTAIN PEST%")
    c = TestClient(app)
    c.post("/login", data={"email": "live@example.com"})
    page = c.get(f"/business/{bid}")
    assert "Check with the state now" in page.text
    part = c.get(f"/business/{bid}/live", headers={"HX-Request": "true"})
    assert 'id="live-box"' in part.text and "Colorado now lists it as: Voluntarily Dissolved" in part.text
    from dealfeed.services import accounts
    key = accounts.create_api_key(conn, accounts.get_user_by_email(conn, "live@example.com"))
    api = c.get(f"/v1/businesses/{bid}/live-status", headers={"Authorization": f"Bearer {key}"}).json()
    assert api["verdict"] == "inactive"

    import anyio
    from mcp import Client
    from dealfeed import mcp_server

    async def tools():
        async with Client(mcp_server.build_server(http=False)) as cl:
            return {t.name for t in (await cl.list_tools()).tools}
    assert "check_live_status" in anyio.run(tools)
