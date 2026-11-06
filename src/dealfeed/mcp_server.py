"""MCP server (scope 6.1): lets Claude, ChatGPT and other MCP clients use the data.

Two ways to run it:

  * stdio, for your own Claude Desktop / Claude Code. The caller is the user
    named by DEALFEED_LOCAL_USER (an email in the users table).
        dealfeed serve-mcp --transport stdio

  * Streamable HTTP, hosted. Callers authenticate with a bearer token. In v1 the
    token is a Deal Feed API key (works with clients that let you set a header,
    e.g. Claude Code `--header`, Cursor, custom agents). Phase 3 swaps
    ApiKeyVerifier for a JWT verifier backed by Clerk/Supabase so claude.ai and
    ChatGPT connectors can do the OAuth login flow.
        dealfeed serve-mcp --transport http --port 8001
"""
from __future__ import annotations

import logging
from typing import Any, Callable

import anyio
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import db
from .config import get_settings
from .services import accounts
from .services.accounts import AuthError, PlanError, User
from .services.business import NotFound
from .services.buybox import BuyBoxInput
from .services.deal_math import DealInputs
from .services.ops import Ops
from .services.search import SearchParams
from .states_meta import SUPPORTED_STATES, covered_states_text

log = logging.getLogger(__name__)

INSTRUCTIONS = f"""\
Deal Feed: off-market small businesses in {covered_states_text()} that are NOT listed for sale but score as
likely to sell in the next few years. Built for searchers doing entrepreneurship-through-acquisition (ETA).
Coverage: established domestic for-profit entities from each state's public business registry, matched to
SBA PPP/7(a)/504 loan records. State codes: {", ".join(SUPPORTED_STATES)}.

Typical flow: get_buy_box (set_buy_box if it's still the default) -> market_summary to size an industry ->
search_targets (ranked by priority: likely to sell x fit for the buy box) -> get_business / explain_score /
check_live_status on the interesting ones -> sba_deal_math for financing scenarios -> save_target.

Always tell the user that scores are directional estimates, that size figures are ranges from 2020-21 PPP
data, and show the data dates included in responses. Never present stale data as current. Industries with
best coverage: pest control, commercial cleaning/janitorial, independent insurance agencies.
"""


class ApiKeyVerifier:
    """TokenVerifier that accepts Deal Feed API keys as bearer tokens."""

    async def verify_token(self, token: str) -> AccessToken | None:
        def check():
            conn = db.connect()
            try:
                return accounts.user_for_api_key(conn, token)
            except AuthError:
                return None
            finally:
                conn.close()
        user = await anyio.to_thread.run_sync(check)
        if not user:
            return None
        if not user.plan.mcp:
            # Let them in so the tool call returns a clear upgrade message instead of a bare 401.
            log.info("user %s on plan %s has no MCP access", user.email, user.plan.name)
        return AccessToken(token=token, client_id=str(user.id), subject=str(user.id), scopes=["dealfeed"])


def _current_user_id() -> str:
    tok = get_access_token()
    if tok:
        return tok.subject or tok.client_id
    email = get_settings().local_user_email
    if not email:
        raise ToolError("Server not configured: set DEALFEED_LOCAL_USER for stdio mode.")
    return f"email:{email}"


async def _run(fn: Callable[[Ops], Any]) -> Any:
    """Resolve the caller (contextvar, so before leaving the event loop), then do DB work in a thread."""
    who = _current_user_id()

    def work():
        conn = db.connect()
        try:
            user: User | None = (accounts.get_user_by_email(conn, who[6:]) if who.startswith("email:")
                                 else accounts.get_user(conn, who))
            if not user:
                raise ToolError("Unknown user. Create one with `dealfeed user create`.")
            return fn(Ops(conn, user, surface="mcp"))
        except (PlanError, NotFound, ValueError) as e:
            raise ToolError(str(e)) from e
        finally:
            conn.close()

    return await anyio.to_thread.run_sync(work)


READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def register_tools(server: MCPServer) -> None:
    @server.tool(annotations=READ)
    async def search_targets(
        industry: str | None = None,
        naics_codes: list[str] | None = None,
        state: str | None = None,
        counties: list[str] | None = None,
        city: str | None = None,
        name_contains: str | None = None,
        min_years_in_business: int | None = None,
        min_employees: int | None = None,
        max_employees: int | None = None,
        min_payroll: float | None = None,
        max_payroll: float | None = None,
        min_score: int | None = None,
        include_unknown_size: bool = True,
        in_buy_box: bool = False,
        sort: str = "priority",
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Find off-market businesses matching criteria, ranked by likelihood-to-sell score (0-100).

        industry: plain words like "pest control", "janitorial", "insurance agency", "auto repair",
        "landscaping", "bookkeeping", or a NAICS code. state: IA, CO, NY, CT, OR or TX (omit for all).
        counties: county names (populated for NY and PPP-matched businesses; sparse elsewhere). Size filters use
        estimated employees / annual payroll from PPP data; include_unknown_size keeps businesses with no
        estimate. limit is capped at 50 per call; page with offset/next_offset.

        Each result has three scores: score (likely to sell), buy_score (fit for the user's buy box: industry
        quality, est. price vs. their range, debt coverage at their rate, cash to close) and priority (both
        combined). sort: "priority" (default), "sell" or "buy". in_buy_box=true limits results to the buy
        box's industries and states.
        """
        p = SearchParams(industry=industry, naics_codes=naics_codes, state=state, counties=counties, city=city,
                         name_contains=name_contains, min_years_in_business=min_years_in_business,
                         min_employees=min_employees, max_employees=max_employees, min_payroll=min_payroll,
                         max_payroll=max_payroll, min_score=min_score, include_unknown_size=include_unknown_size,
                         in_buy_box=in_buy_box, sort=sort if sort in ("priority", "sell", "buy") else "priority",
                         limit=max(1, min(limit, 50)), offset=offset)
        return await _run(lambda ops: ops.search_targets(p))

    @server.tool(annotations=READ)
    async def get_business(business_id: str) -> dict[str, Any]:
        """Full profile for one business: address, industry, score breakdown, size estimates, SBA loan
        history, people named in public state filings, and every source record with its date."""
        return await _run(lambda ops: ops.get_business(business_id))

    @server.tool(annotations=READ)
    async def explain_score(business_id: str) -> dict[str, Any]:
        """Plain-English breakdown of both scores, point by point: why the owner is (or isn't) likely to sell,
        and why it is (or isn't) a fit for the user's buy box, with the estimated price, cash flow and debt
        coverage behind the buy score."""
        return await _run(lambda ops: ops.explain_score(business_id))

    @server.tool(annotations=READ)
    async def get_buy_box() -> dict[str, Any]:
        """The user's buy box (what they want to buy): industries, states, price range, cash available,
        loan rate and term, target multiple, minimum DSCR, salary. Buy scores are measured against it."""
        return await _run(lambda ops: ops.get_buy_box())

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                             open_world_hint=False))
    async def set_buy_box(
        interest_rate_pct: float,
        industries: list[str] | None = None,
        states: list[str] | None = None,
        min_price: float = 500_000,
        max_price: float = 5_000_000,
        cash_available: float = 300_000,
        term_years: int = 10,
        target_multiple: float | None = None,
        min_dscr: float = 1.25,
        buyer_salary: float = 80_000,
    ) -> dict[str, Any]:
        """Save the user's buy box. Replaces the whole box, so read it first with get_buy_box and pass
        every field. Ask the user for anything you don't know, especially interest_rate_pct (their quoted SBA
        rate) and cash_available (down payment + closing costs they can bring)."""
        inp = BuyBoxInput(industries=industries or [], states=states or [], min_price=min_price, max_price=max_price,
                          cash_available=cash_available, interest_rate_pct=interest_rate_pct, term_years=term_years,
                          target_multiple=target_multiple, min_dscr=min_dscr, buyer_salary=buyer_salary)
        return await _run(lambda ops: ops.set_buy_box(inp))

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def check_live_status(business_id: str) -> dict[str, Any]:
        """Check a business's status with its state registry right now (one live lookup; CO, NY, CT, OR, TX;
        not available for Iowa). Reports whether it's still active and whether its name, address or status
        changed since our last monthly pull. Use before recommending outreach to a specific business."""
        return await _run(lambda ops: ops.live_status(business_id))

    @server.tool(annotations=READ)
    async def sba_deal_math(
        purchase_price: float,
        cash_flow: float,
        interest_rate_pct: float,
        cash_flow_type: str = "sde",
        term_years: int = 10,
        equity_injection_pct: float = 10,
        seller_note_pct: float = 0,
        seller_note_rate_pct: float = 6,
        seller_note_term_years: int = 10,
        seller_note_full_standby: bool = False,
        buyer_salary: float | None = None,
        closing_cost_pct: float = 3.5,
        working_capital: float = 0,
        min_dscr: float = 1.25,
    ) -> dict[str, Any]:
        """Run an SBA 7(a) acquisition scenario: loan size, monthly/annual debt service, DSCR vs the lender
        threshold, buyer cash needed, cash flow after debt, payback, and a plain-English verdict.

        cash_flow is annual SDE (or EBITDA with cash_flow_type="ebitda"). interest_rate_pct is required:
        ask the user or use a current quote; never assume a rate. To price at a multiple, compute
        purchase_price = multiple x SDE first.
        """
        inp = DealInputs(purchase_price=purchase_price, cash_flow=cash_flow, cash_flow_type=cash_flow_type,
                         interest_rate_pct=interest_rate_pct, term_years=term_years,
                         equity_injection_pct=equity_injection_pct, seller_note_pct=seller_note_pct,
                         seller_note_rate_pct=seller_note_rate_pct, seller_note_term_years=seller_note_term_years,
                         seller_note_full_standby=seller_note_full_standby, buyer_salary=buyer_salary,
                         closing_cost_pct=closing_cost_pct, working_capital=working_capital, min_dscr=min_dscr)
        return await _run(lambda ops: ops.sba_deal_math(inp))

    @server.tool(annotations=READ)
    async def market_summary(industry: str | None = None, naics_code: str | None = None,
                             state: str | None = None) -> dict[str, Any]:
        """Market overview for an industry in one state (or all covered states if omitted): business counts by
        state, age and size distribution, score distribution, top counties, and SBA 7(a)/504 loan benchmarks
        and active lenders (last 5 years)."""
        return await _run(lambda ops: ops.market_summary(state, industry, naics_code))

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                             open_world_hint=False))
    async def save_target(business_id: str, status: str = "new", notes: str | None = None) -> dict[str, Any]:
        """Add or update a business in the user's pipeline. status: new, researching, contacted, passed."""
        return await _run(lambda ops: ops.save_target(business_id, status, notes))

    @server.tool(annotations=READ)
    async def list_saved_targets(status: str | None = None) -> dict[str, Any]:
        """Show the user's saved pipeline, optionally filtered by status (new, researching, contacted, passed)."""
        return await _run(lambda ops: ops.list_saved_targets(status))

    @server.tool(annotations=READ)
    async def export_targets(
        saved: bool = False,
        saved_status: str | None = None,
        industry: str | None = None,
        state: str | None = None,
        counties: list[str] | None = None,
        min_years_in_business: int | None = None,
        min_score: int | None = None,
    ) -> dict[str, Any]:
        """CSV export of either the user's saved pipeline (saved=true) or a search. Returns the CSV text in
        `csv`; offer to save it as a file for the user. Search exports count toward the monthly row limit."""
        sp = None if saved else SearchParams(industry=industry, state=state, counties=counties,
                                             min_years_in_business=min_years_in_business, min_score=min_score)
        return await _run(lambda ops: ops.export_targets(sp, saved, saved_status))


def build_server(http: bool = False) -> MCPServer:
    kwargs: dict[str, Any] = {}
    if http:
        url = get_settings().public_url.rstrip("/")
        kwargs = {
            "token_verifier": ApiKeyVerifier(),
            "auth": AuthSettings(issuer_url=url, resource_server_url=f"{url}/mcp",
                                 validate_token_resource=False),
        }
    server = MCPServer(name="dealfeed", title="Deal Feed", version="0.1.0", instructions=INSTRUCTIONS, **kwargs)
    register_tools(server)
    return server


def main(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8001) -> None:
    if transport == "stdio":
        build_server(http=False).run("stdio")
    else:
        build_server(http=True).run("streamable-http", host=host, port=port)
