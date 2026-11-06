"""SBA 7(a) acquisition scenario math (scope 6.1 `sba_deal_math`). Pure functions, no DB.

Simplifications, all stated in the output:
  * SBA loan is a fixed-rate, fully amortizing loan. Real 7(a) loans are often
    variable (Prime + spread); pass the rate you're quoted.
  * Closing costs are one percentage of price (SBA guaranty fee, packaging,
    legal, QoE). Override closing_cost_pct with your own estimate.
  * DSCR uses cash flow after paying the buyer a market salary, which is how
    most lenders underwrite SDE-based deals.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from ..inputs import number


class DealInputs(BaseModel):
    purchase_price: float = Field(gt=0, description="Total purchase price in dollars")
    cash_flow: float = Field(gt=0, description="Annual SDE (or EBITDA if cash_flow_type='ebitda')")
    cash_flow_type: str = Field("sde", pattern="^(sde|ebitda)$")
    interest_rate_pct: float = Field(gt=0, lt=30, description="Annual SBA loan rate, e.g. 10.5. Always an input.")
    term_years: int = Field(10, ge=1, le=25, description="SBA loan term; 10 is standard without real estate")
    equity_injection_pct: float = Field(10, ge=0, le=100, description="Buyer cash as % of price")
    seller_note_pct: float = Field(0, ge=0, le=100, description="Seller financing as % of price")
    seller_note_rate_pct: float = Field(6, ge=0, lt=30)
    seller_note_term_years: int = Field(10, ge=1, le=25)
    seller_note_full_standby: bool = Field(
        False, description="If true, the seller note has no payments during the scenario (full standby)"
    )
    buyer_salary: float | None = Field(
        None, ge=0,
        description="Market salary for the buyer/operator, subtracted from SDE before debt service. "
                    "Defaults to $80,000 for SDE, $0 for EBITDA (already net of a manager).",
    )
    closing_cost_pct: float = Field(3.5, ge=0, le=20, description="Closing costs as % of price, paid in cash")
    working_capital: float = Field(0, ge=0, description="Extra cash the buyer must bring for working capital")
    min_dscr: float = Field(1.25, gt=0, description="Lender DSCR threshold")

    @field_validator("purchase_price", "cash_flow", "interest_rate_pct", "equity_injection_pct", "seller_note_pct",
                     "seller_note_rate_pct", "buyer_salary", "closing_cost_pct", "working_capital", "min_dscr",
                     mode="before")
    @classmethod
    def _numbers(cls, v):
        return number(v)            # '1,050,000', '$1.05M', '350k', '10.5%'

    @field_validator("cash_flow_type", mode="before")
    @classmethod
    def _cf_type(cls, v):
        s = str(v).strip().lower() if v is not None else "sde"
        if s not in ("sde", "ebitda"):
            raise ValueError("cash_flow_type must be 'sde' (seller's discretionary earnings) or 'ebitda'. "
                             "Revenue isn't cash flow: estimate SDE first.")
        return s

    @model_validator(mode="after")
    def _check_mix(self):
        if self.equity_injection_pct + self.seller_note_pct > 100:
            raise ValueError("equity_injection_pct + seller_note_pct cannot exceed 100")
        return self


def _payment(principal: float, annual_rate_pct: float, years: int) -> float:
    if principal <= 0:
        return 0.0
    r = annual_rate_pct / 100 / 12
    n = years * 12
    if r == 0:
        return principal / n
    return principal * r / (1 - (1 + r) ** -n)


def run(inp: DealInputs) -> dict:
    price = inp.purchase_price
    equity = price * inp.equity_injection_pct / 100
    seller_note = price * inp.seller_note_pct / 100
    loan = price - equity - seller_note

    sba_monthly = _payment(loan, inp.interest_rate_pct, inp.term_years)
    seller_monthly = 0.0 if inp.seller_note_full_standby else _payment(
        seller_note, inp.seller_note_rate_pct, inp.seller_note_term_years)
    annual_debt = (sba_monthly + seller_monthly) * 12

    salary = inp.buyer_salary if inp.buyer_salary is not None else (80_000 if inp.cash_flow_type == "sde" else 0)
    available = inp.cash_flow - salary
    dscr = available / annual_debt if annual_debt > 0 else None
    after_debt = available - annual_debt

    closing = price * inp.closing_cost_pct / 100
    cash_needed = equity + closing + inp.working_capital
    payback = cash_needed / after_debt if after_debt > 0 else None
    multiple = price / inp.cash_flow

    if dscr is None:
        verdict = "No debt in this structure."
    elif dscr >= inp.min_dscr + 0.25:
        verdict = f"Comfortable: DSCR {dscr:.2f}x clears the {inp.min_dscr:.2f}x lenders typically want with room to spare."
    elif dscr >= inp.min_dscr:
        verdict = f"Workable: DSCR {dscr:.2f}x meets the {inp.min_dscr:.2f}x threshold, but there's little cushion."
    elif dscr >= 1.0:
        verdict = (f"Tight: DSCR {dscr:.2f}x is below the typical {inp.min_dscr:.2f}x lenders want. "
                   "Lower the price, add seller financing on standby, or extend the term.")
    else:
        verdict = f"Doesn't cash flow: DSCR {dscr:.2f}x means cash flow can't cover the debt."

    r0 = lambda v: round(v, 0)  # noqa: E731
    return {
        "inputs": inp.model_dump(),
        "purchase_multiple": round(multiple, 2),
        "sources_of_funds": {"sba_loan": r0(loan), "seller_note": r0(seller_note), "buyer_equity": r0(equity)},
        "sba_loan": {"amount": r0(loan), "monthly_payment": r0(sba_monthly), "annual_debt_service": r0(sba_monthly * 12)},
        "seller_note": {"amount": r0(seller_note), "monthly_payment": r0(seller_monthly),
                        "full_standby": inp.seller_note_full_standby},
        "total_annual_debt_service": r0(annual_debt),
        "buyer_salary_assumed": r0(salary),
        "cash_flow_available_for_debt": r0(available),
        "dscr": round(dscr, 2) if dscr is not None else None,
        "min_dscr": inp.min_dscr,
        "cash_flow_after_debt_service": r0(after_debt),
        "buyer_cash_needed": {"equity_injection": r0(equity), "closing_costs": r0(closing),
                              "working_capital": r0(inp.working_capital), "total": r0(cash_needed)},
        "payback_years": round(payback, 1) if payback is not None else None,
        "verdict": verdict,
        "assumptions": [
            "Fixed-rate, fully amortizing SBA loan at the rate you entered.",
            f"Buyer salary of ${salary:,.0f} is paid before debt service.",
            f"Closing costs estimated at {inp.closing_cost_pct}% of price.",
            "Not financial advice; confirm terms with an SBA lender.",
        ],
    }
