"""The one place product operations live. REST and MCP both call these, so plan
limits, metering and response shapes are identical on every surface."""
from __future__ import annotations

from datetime import date

import psycopg

from . import accounts, business, buybox, deal_math, live, search, targets
from .accounts import PlanError, User


class Ops:
    def __init__(self, conn: psycopg.Connection, user: User, surface: str, today: date | None = None):
        self.conn, self.user, self.surface, self.today = conn, user, surface, today
        if surface == "mcp" and not user.plan.mcp:
            raise PlanError(f"The {user.plan.name} plan doesn't include AI-assistant (MCP) access. "
                            "Upgrade to Searcher or above.")

    def _meter(self, tool: str, rows: int) -> None:
        accounts.record_usage(self.conn, self.user, tool, rows, self.surface)

    def buy_box(self):
        return buybox.get_buy_box(self.conn, self.user.id)

    def get_buy_box(self) -> dict:
        return buybox.describe(self.buy_box())

    def set_buy_box(self, inp: buybox.BuyBoxInput) -> dict:
        out = buybox.describe(buybox.set_buy_box(self.conn, self.user.id, inp))
        self._meter("set_buy_box", 0)
        return out

    def search_targets(self, params: search.SearchParams) -> dict:
        limit = accounts.allowed_rows(self.conn, self.user, params.limit)
        out = search.search_targets(self.conn, params, limit, self.today, box=self.buy_box())
        self._meter("search_targets", out["returned"])
        return out

    def get_business(self, business_id: str) -> dict:
        accounts.allowed_rows(self.conn, self.user, 1)
        out = business.get_business(self.conn, business_id, self.user.id, self.today)
        self._meter("get_business", 1)
        return out

    def explain_score(self, business_id: str) -> dict:
        out = business.explain_score(self.conn, business_id, self.user.id)
        self._meter("explain_score", 0)
        return out

    def live_status(self, business_id: str) -> dict:
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
        self._meter("list_saved_targets", 0)
        return out

    def export_targets(self, search_params: search.SearchParams | None = None, saved: bool = False,
                       saved_status: str | None = None) -> dict:
        plan = self.user.plan
        if not plan.export:
            raise PlanError(f"Exports aren't included in the {plan.name} plan.")
        max_rows = plan.max_export_rows
        if plan.rows_per_month is not None and not saved:
            remaining = plan.rows_per_month - accounts.rows_used_this_month(self.conn, self.user)
            if remaining <= 0:
                raise PlanError("Monthly row limit reached.")
            max_rows = min(max_rows, remaining)
        out = targets.export_targets(self.conn, self.user.id, max_rows, search_params, saved_status, saved, self.today)
        # Exporting your own saved list doesn't count against the quota; exporting search results does.
        self._meter("export_targets", 0 if saved else out["rows"])
        return out

    def me(self) -> dict:
        return accounts.usage_summary(self.conn, self.user)
