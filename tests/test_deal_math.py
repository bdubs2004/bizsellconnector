import pytest
from pydantic import ValidationError

from dealfeed.services.deal_math import DealInputs, _payment, run


def test_payment_formula():
    # $1M, 10%, 10 years -> ~$13,215/month (standard amortization table value)
    assert _payment(1_000_000, 10, 10) == pytest.approx(13215.07, abs=0.01)


def test_basic_scenario():
    out = run(DealInputs(purchase_price=1_200_000, cash_flow=400_000, interest_rate_pct=10.5))
    assert out["purchase_multiple"] == 3.0
    assert out["sba_loan"]["amount"] == 1_080_000
    assert out["buyer_cash_needed"]["equity_injection"] == 120_000
    # (400k - 80k salary) / annual debt
    assert out["dscr"] == pytest.approx(320_000 / out["total_annual_debt_service"], abs=0.01)
    assert out["verdict"].startswith("Comfortable")


def test_tight_deal_verdict_mentions_threshold():
    out = run(DealInputs(purchase_price=1_500_000, cash_flow=330_000, interest_rate_pct=11))
    assert 1.0 <= out["dscr"] < 1.25
    assert "Tight" in out["verdict"] and "1.25x" in out["verdict"]


def test_seller_note_on_standby_reduces_debt_service():
    base = dict(purchase_price=1_000_000, cash_flow=300_000, interest_rate_pct=10, seller_note_pct=10)
    paying = run(DealInputs(**base))
    standby = run(DealInputs(**base, seller_note_full_standby=True))
    assert standby["total_annual_debt_service"] < paying["total_annual_debt_service"]
    assert standby["sba_loan"]["amount"] == 800_000


def test_rate_is_required_and_mix_validated():
    with pytest.raises(ValidationError):
        DealInputs(purchase_price=1, cash_flow=1)
    with pytest.raises(ValidationError):
        DealInputs(purchase_price=1, cash_flow=1, interest_rate_pct=10, equity_injection_pct=60, seller_note_pct=50)
