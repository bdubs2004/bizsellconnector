"""The one place product operations live. REST, MCP and the web app all call these, so plan
limits, metering and response shapes are identical on every surface.

Metering (the ZoomInfo / Apollo model):
- Viewing is free on every surface, under a daily fair-use cap on *new* businesses viewed.
- Credits are spent when data leaves Deal Feed: CSV exports, and business data returned by the REST API
  (searches, profiles, saved lists). One credit per business per month; re-pulling it that month is free.
- Inside the product (web app, AI chat over MCP) nothing costs credits."""
from __future__ import annotations

from datetime import date

import psycopg

from .. import security
from . import accounts, business, buybox, deal_math, live, search, targets
from .accounts import InsufficientCredits, PlanError, User


class Ops:
    def __init__(self, conn: psycopg.Connection, user: User, surface: str, today: date | None = None):
        self.conn, self.user, self.surface, self.today = conn, user, surface, today
        if surface == "mcp" and not user.plan.mcp:
            raise PlanError(f"The {user.plan.name} plan doesn't include AI-assistant (MCP) access. "
                            "Upgrade to Searcher or above.")

    @property
    def billable(self) -> bool:
        """Data returned on this surface leaves the platform, so it costs credits."""
        return self.surface == "api"

    def _meter(self, tool: str, credits: int = 0) -> None:
        accounts.record_usage(self.conn, self.user, tool, credits, self.surface)

    def _deliver(self, tool: str, ids: list[str]) -> tuple[list[str], int]:
        """Fair use + credits for business data about to be returned. Returns (ids the caller may receive,
        credits charged). Non-billable surfaces get everything; the API gets what credits cover."""
        allowed = ids
        charged = 0
        if self.billable and ids:
            allowed = accounts.affordable(self.conn, self.user, ids)
            if not allowed:
                raise InsufficientCredits(len(ids), 0)
            charged = accounts.charge_credits(self.conn, self.user, allowed, "api")
        accounts.record_views(self.conn, self.user, allowed)
        self._meter(tool, charged)
        return allowed, charged

    def buy_box(self):
        return buybox.get_buy_box(self.conn, self.user.id)

    def get_buy_box(self) -> dict:
        return buybox.describe(self.buy_box())

    def set_buy_box(self, inp: buybox.BuyBoxInput) -> dict:
        out = buybox.describe(buybox.set_buy_box(self.conn, self.user.id, inp))
        self._meter("set_buy_box", 0)
        return out

    def search_targets(self, params: search.SearchParams) -> dict:
        accounts.check_views(self.conn, self.user)
        limit = min(params.limit, self.user.plan.max_per_call)
        out = search.search_targets(self.conn, params, limit, self.today, box=self.buy_box())
        ids = [r["business_id"] for r in out["results"]]
        allowed, charged = self._deliver("search_targets", ids)
        if len(allowed) < len(ids):
            keep = set(allowed)
            out["results"] = [r for r in out["results"] if r["business_id"] in keep]
            out["returned"] = len(out["results"])
            out.setdefault("notes", []).append(
                f"Returned {len(allowed)} of {len(ids)} results: you're out of credits for the rest this month.")
        if self.billable:
            out["credits_charged"] = charged
        return out

    def get_business(self, business_id: str) -> dict:
        out = business.get_business(self.conn, business_id, self.user.id, self.today)
        if not accounts.viewed_this_month(self.conn, self.user, out["business_id"]):
            accounts.check_views(self.conn, self.user)
        _, charged = self._deliver("get_business", [out["business_id"]])
        if self.billable:
            out["credits_charged"] = charged
        return out

    def explain_score(self, business_id: str) -> dict:
        out = business.explain_score(self.conn, business_id, self.user.id)
        self._meter("explain_score", 0)
        return out

    def live_status(self, business_id: str) -> dict:
        # Each check hits a state's public API: throttle per user and overall so we stay a polite client.
        security.limiter.hit("live_user", str(self.user.id))
        security.limiter.hit("live_global", "all")
        out = live.check_business(self.conn, business_id)
        self._meter("live_status", 0)
        return out

    def sba_deal_math(self, inputs: deal_math.DealInputs) -> dict:
        out = deal_math.run(inputs)
        self._meter("sba_deal_math", 0)
        return out

    def market_summary(self, state: str | None = None, industry: str | None = None,
                       naics_code: str | None = None) -> dict:
        out = search.market_summary(self.conn, state, industry, naics_code)
        self._meter("market_summary", 0)
        return out

    def save_target(self, business_id: str, status: str = "new", notes: str | None = None) -> dict:
        out = targets.save_target(self.conn, self.user.id, business_id, status, notes)
        self._meter("save_target", 0)
        return out

    def remove_target(self, business_id: str) -> dict:
        ok = targets.remove_target(self.conn, self.user.id, business_id)
        self._meter("remove_target", 0)
        return {"removed": ok, "business_id": business_id}

    def list_saved_targets(self, status: str | None = None) -> dict:
        out = targets.list_saved_targets(self.conn, self.user.id, status, self.today)
        if self.billable:   # your list, but the data still leaves via the API: charged like any other pull
            ids = [t["business_id"] for t in out["targets"]]
            charged = accounts.charge_credits(self.conn, self.user, ids, "api")
            out["credits_charged"] = charged
            self._meter("list_saved_targets", charged)
        else:
            self._meter("list_saved_targets", 0)
        return out

    def export_targets(self, search_params: search.SearchParams | None = None, saved: bool = False,
                       saved_status: str | None = None, max_rows: int | None = None) -> dict:
        """Exports cost credits: 1 per business not already exported or pulled this month, saved list or
        search alike. If credits don't cover every row, the export keeps the top rows they do cover."""
        plan = self.user.plan
        if not plan.export:
            raise PlanError(f"Exports aren't included in the {plan.name} plan.")
        max_rows = min(plan.max_export_rows, max_rows) if max_rows else plan.max_export_rows
        out = targets.export_targets(self.conn, self.user.id, max_rows, search_params, saved_status, saved, self.today,
                                     allow=lambda ids: accounts.affordable(self.conn, self.user, ids))
        ids = out.pop("business_ids")
        if out["matched"] and not ids:
            raise InsufficientCredits(out["matched"], 0)
        charged = accounts.charge_credits(self.conn, self.user, ids, "export")
        out["credits_charged"] = charged
        out["credits_left"] = accounts.credits(self.conn, self.user)["left"]
        if out["rows"] < out["matched"]:
            out["note"] = (f"Exported {out['rows']} of {out['matched']} rows: that's all your credits cover. "
                           "Add credits or wait for next month's allowance for the rest.")
        self._meter("export_targets", charged)
        return out

    def me(self) -> dict:
        return accounts.usage_summary(self.conn, self.user)
