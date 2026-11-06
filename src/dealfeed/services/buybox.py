"""Per-user buy box storage."""
from __future__ import annotations

import psycopg
from pydantic import BaseModel, Field, field_validator, model_validator

from ..buy import BuyBox
from ..industries import BY_SLUG, resolve_industry
from ..states_meta import SUPPORTED_STATES


class BuyBoxInput(BaseModel):
    industries: list[str] = Field(default_factory=list, description="Industry names or slugs, e.g. ['pest control']. "
                                                                    "Empty = any industry.")
    states: list[str] = Field(default_factory=list, description="State codes, e.g. ['IA', 'CO']. Empty = any.")
    min_price: float = Field(500_000, ge=0)
    max_price: float = Field(5_000_000, gt=0)
    cash_available: float = Field(300_000, ge=0, description="Cash you can put in: down payment + closing costs")
    interest_rate_pct: float = Field(gt=0, lt=30, description="SBA loan rate you're being quoted")
    term_years: int = Field(10, ge=1, le=25)
    target_multiple: float | None = Field(None, gt=0, le=15, description="Price / SDE you'd pay. Empty = industry typical")
    min_dscr: float = Field(1.25, gt=0, le=5)
    buyer_salary: float = Field(80_000, ge=0, description="What you'd pay yourself, before debt service")

    @field_validator("industries")
    @classmethod
    def _industries(cls, v):
        out = []
        for x in v:
            ind = resolve_industry(x)
            if not ind:
                raise ValueError(f"Unknown industry {x!r}. Options: {', '.join(BY_SLUG)}")
            out.append(ind.slug)
        return sorted(set(out))

    @field_validator("states")
    @classmethod
    def _states(cls, v):
        out = sorted({s.strip().upper() for s in v if s.strip()})
        bad = [s for s in out if s not in SUPPORTED_STATES]
        if bad:
            raise ValueError(f"Not covered yet: {bad}. Covered: {', '.join(SUPPORTED_STATES)}")
        return out

    @model_validator(mode="after")
    def _range(self):
        if self.min_price > self.max_price:
            raise ValueError("min_price can't be above max_price")
        return self


def get_buy_box(conn: psycopg.Connection, user_id) -> BuyBox:
    if user_id is None:
        return BuyBox(is_default=True)
    r = conn.execute("SELECT * FROM buy_boxes WHERE user_id = %s", (user_id,)).fetchone()
    if not r:
        return BuyBox(is_default=True)
    return BuyBox(
        industries=list(r["industries"]), states=list(r["states"]), min_price=float(r["min_price"]),
        max_price=float(r["max_price"]), cash_available=float(r["cash_available"]),
        interest_rate_pct=float(r["interest_rate_pct"]), term_years=r["term_years"],
        target_multiple=float(r["target_multiple"]) if r["target_multiple"] is not None else None,
        min_dscr=float(r["min_dscr"]), buyer_salary=float(r["buyer_salary"]), is_default=False,
    )


def set_buy_box(conn: psycopg.Connection, user_id, box: BuyBoxInput) -> BuyBox:
    d = box.model_dump()
    conn.execute(
        """
        INSERT INTO buy_boxes (user_id, industries, states, min_price, max_price, cash_available, interest_rate_pct,
                               term_years, target_multiple, min_dscr, buyer_salary, updated_at)
        VALUES (%(user_id)s, %(industries)s, %(states)s, %(min_price)s, %(max_price)s, %(cash_available)s,
                %(interest_rate_pct)s, %(term_years)s, %(target_multiple)s, %(min_dscr)s, %(buyer_salary)s, now())
        ON CONFLICT (user_id) DO UPDATE SET industries = EXCLUDED.industries, states = EXCLUDED.states,
            min_price = EXCLUDED.min_price, max_price = EXCLUDED.max_price, cash_available = EXCLUDED.cash_available,
            interest_rate_pct = EXCLUDED.interest_rate_pct, term_years = EXCLUDED.term_years,
            target_multiple = EXCLUDED.target_multiple, min_dscr = EXCLUDED.min_dscr,
            buyer_salary = EXCLUDED.buyer_salary, updated_at = now()
        """,
        d | {"user_id": user_id},
    )
    conn.commit()
    return get_buy_box(conn, user_id)


def describe(box: BuyBox) -> dict:
    d = box.to_dict()
    d["industries_labels"] = [BY_SLUG[s].label for s in box.industries if s in BY_SLUG]
    if box.is_default:
        d["note"] = (f"Default buy box (no saved preferences): any industry or state, $0.5M-$5M price, $300k cash, "
                     f"{box.interest_rate_pct}% rate. Set yours to personalize buy scores.")
    return d
