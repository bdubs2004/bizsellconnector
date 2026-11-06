"""Buy score: how good an acquisition a business is *for this buyer*, measured against their buy box.

The sell score (score.py) asks "will the owner sell soon?". This asks "should I want to buy it?". Like
the sell score, it's a transparent rules-based estimate where every point has a reason. It's computed on
the fly per user (it depends on their buy box), never stored.

Inputs: the business row (industry, PPP size estimate, age, status), the industry's acquisition profile
(industries.py, placeholder benchmarks), and the buyer's buy box (price range, cash, rate...). The deal
estimate reuses the SBA deal math engine.

Priority = geometric mean of sell and buy scores: high only when BOTH are high.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date

from .config import get_settings
from .industries import BY_NAICS, BY_SLUG, Industry
from .services.deal_math import DealInputs, run

BUY_MODEL_VERSION = "buy-v1.0"
BASE = 30
SBA_7A_MAX_LOAN = 5_000_000


@dataclass
class BuyBox:
    industries: list[str] = field(default_factory=list)     # industry slugs; empty = any
    states: list[str] = field(default_factory=list)         # state codes; empty = any
    min_price: float = 500_000
    max_price: float = 5_000_000
    cash_available: float = 300_000
    interest_rate_pct: float = field(default_factory=lambda: get_settings().reference_rate_pct)
    term_years: int = 10
    target_multiple: float | None = None
    min_dscr: float = 1.25
    buyer_salary: float = 80_000
    is_default: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BuyFired:
    signal: str
    points: int
    reason: str


def _k(v: float) -> str:
    return f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.0f}k"


def estimate_deal(row: dict, ind: Industry | None, box: BuyBox) -> dict | None:
    """Rough deal from the PPP-based revenue range and the industry's SDE margins and multiple.
    Ranges for display; the midpoint drives the scoring."""
    if not ind or row.get("revenue_low") is None or row.get("revenue_high") is None:
        return None
    rev_lo, rev_hi = float(row["revenue_low"]), float(row["revenue_high"])
    m_lo, m_hi = ind.sde_margin
    sde_lo, sde_hi = rev_lo * m_lo, rev_hi * m_hi
    sde_mid = math.sqrt(sde_lo * sde_hi)
    multiple = box.target_multiple or sum(ind.typical_multiple) / 2
    price_mid = sde_mid * multiple
    math_out = run(DealInputs(purchase_price=price_mid, cash_flow=sde_mid, interest_rate_pct=box.interest_rate_pct,
                              term_years=box.term_years, buyer_salary=box.buyer_salary, min_dscr=box.min_dscr))
    return {
        "sde_range": [round(sde_lo, -3), round(sde_hi, -3)],
        "sde_mid": round(sde_mid, -3),
        "multiple": round(multiple, 2),
        "price_range": [round(sde_lo * multiple, -4), round(sde_hi * multiple, -4)],
        "price_mid": round(price_mid, -4),
        "dscr": math_out["dscr"],
        "cash_needed": math_out["buyer_cash_needed"]["total"],
        "sba_loan": math_out["sba_loan"]["amount"],
        "interest_rate_pct": box.interest_rate_pct,
        "assumptions": "PPP-based revenue range x industry SDE margins (placeholder benchmarks) x "
                       f"{multiple:.1f}x multiple; 10% down, {box.term_years}-yr SBA loan at {box.interest_rate_pct}%.",
    }


def evaluate_buy(row: dict, box: BuyBox, today: date | None = None) -> tuple[int, list[BuyFired], dict | None]:
    today = today or date.today()
    fired: list[BuyFired] = []
    ind = BY_NAICS.get(row.get("naics_code") or "")

    # Fit to the buy box
    if box.industries and (not ind or ind.slug not in box.industries):
        fired.append(BuyFired("box_industry", -30, "Outside your target industries"))
    if box.states and (row.get("state") or "") not in box.states:
        fired.append(BuyFired("box_state", -30, "Outside your target states"))

    # Industry quality
    if ind:
        rec = "recurring" if ind.recurring >= 0.8 else "partly recurring" if ind.recurring >= 0.5 else "mostly one-off"
        m_mid = sum(ind.sde_margin) / 2
        pts = round(ind.recurring * 12) + (8 if m_mid >= 0.25 else 5 if m_mid >= 0.15 else 2)
        fired.append(BuyFired("industry_quality", pts,
                              f"{ind.label}: {rec} revenue, ~{ind.sde_margin[0]:.0%}-{ind.sde_margin[1]:.0%} SDE margins"))
    else:
        fired.append(BuyFired("industry_quality", 0, "Industry unknown, so business quality can't be judged"))

    # Deal size, price fit, financeability
    deal = estimate_deal(row, ind, box)
    if deal:
        p = deal["price_mid"]
        lo_hi = f"{_k(deal['price_range'][0])}-{_k(deal['price_range'][1])}"
        if box.min_price <= p <= box.max_price:
            fired.append(BuyFired("price_fit", 15, f"Est. price {lo_hi} fits your {_k(box.min_price)}-{_k(box.max_price)} range"))
        elif p < box.min_price * 0.75 or p > box.max_price * 1.25:
            side = "below" if p < box.min_price else "above"
            fired.append(BuyFired("price_fit", -15, f"Est. price ~{_k(p)} is well {side} your range"))
        else:
            fired.append(BuyFired("price_fit", 5, f"Est. price ~{_k(p)} is near the edge of your range"))
        d = deal["dscr"]
        rate = f"at {box.interest_rate_pct}%"
        if d is None:
            pass
        elif d >= box.min_dscr + 0.25:
            fired.append(BuyFired("dscr", 15, f"Est. debt coverage {d:.2f}x {rate}: comfortable"))
        elif d >= box.min_dscr:
            fired.append(BuyFired("dscr", 8, f"Est. debt coverage {d:.2f}x {rate}: meets the {box.min_dscr:.2f}x bar"))
        elif d >= 1.0:
            fired.append(BuyFired("dscr", 0, f"Est. debt coverage {d:.2f}x {rate}: tight"))
        else:
            fired.append(BuyFired("dscr", -15, f"Est. debt coverage {d:.2f}x {rate}: doesn't cover the debt"))
        if deal["cash_needed"] > box.cash_available:
            fired.append(BuyFired("cash", -10, f"Needs ~{_k(deal['cash_needed'])} cash to close; your buy box has "
                                               f"{_k(box.cash_available)}"))
        if deal["sba_loan"] > SBA_7A_MAX_LOAN:
            fired.append(BuyFired("sba_cap", -10, "Loan would exceed the $5M SBA 7(a) limit"))
    else:
        fired.append(BuyFired("price_fit", 0, "No size data, so price and debt coverage can't be estimated"))

    # Stability
    fd = row.get("formation_date")
    if fd:
        yrs = today.year - fd.year - ((today.month, today.day) < (fd.month, fd.day))
        if yrs >= 20:
            fired.append(BuyFired("stability", 10, f"{yrs} years operating: proven"))
        elif yrs >= 10:
            fired.append(BuyFired("stability", 6, f"{yrs} years operating"))
        elif yrs < 5:
            fired.append(BuyFired("stability", -5, f"Only {yrs} years operating"))
    status = row.get("status")
    if status == "delinquent":
        fired.append(BuyFired("status", -15, "Behind on state filings: diligence flag"))
    elif status == "inactive":
        fired.append(BuyFired("status", -30, "No longer in the state's active registry"))

    # How much we actually know
    src = row.get("naics_source")
    if deal and src in ("ppp", "sba", "state"):
        fired.append(BuyFired("confidence", 5, "Size and industry from SBA/state records"))
    elif src == "rules":
        fired.append(BuyFired("confidence", 0, "Industry guessed from the business name"))

    score = max(0, min(100, BASE + sum(f.points for f in fired)))
    return score, sorted(fired, key=lambda f: -abs(f.points)), deal


def priority(sell: int | None, buy: int | None) -> int | None:
    """High only when both are high (geometric mean)."""
    if sell is None or buy is None:
        return sell if buy is None else buy
    return round(math.sqrt(max(sell, 0) * max(buy, 0)))


def annotate(rows: list[dict], box: BuyBox, today: date | None = None) -> list[dict]:
    """Add buy_score, buy_breakdown, deal_estimate and priority to business rows."""
    for r in rows:
        b, fired, deal = evaluate_buy(r, box, today)
        r["buy_score"] = b
        r["buy_breakdown"] = [{"signal": f.signal, "points": f.points, "reason": f.reason} for f in fired]
        r["deal_estimate"] = deal
        r["priority"] = priority(r.get("score"), b)
    return rows


def industry_options() -> list[tuple[str, str]]:
    return [(slug, ind.label) for slug, ind in BY_SLUG.items()]
