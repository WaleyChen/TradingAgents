"""The company block and the debt, share and market-cap figures it unlocks."""

from __future__ import annotations

import copy
from datetime import date

import pytest
from pydantic import ValidationError

from tests.test_context_financials import GOOD as FINANCIALS, B, d
from tradingagents.context import Context, Financials, check_context, derive_metrics
from tradingagents.context.models import Company, Price
from tradingagents.context.render import render_context
from tradingagents.summary_check import known_values

COMPANY = {
    "ticker": "MU", "currency": "USD", "source": "financial_datasets", "as_of": "2026-11-04",
    "name": "Micron Technology, Inc.", "sector": "Technology", "industry": "Semiconductors",
    "exchange": "NASDAQ", "market_cap": 898 * B, "shares_outstanding": 1_128_000_000,
}
PRICE = {"ticker": "MU", "currency": "USD", "source": "getmarketalerts", "as_of": "2026-11-04", "close": 795.23}


DEBT, DA = d(6.0), d(8.0)


def financials(debt=DEBT, da=DA):
    data = copy.deepcopy(FINANCIALS)
    data["balance"][0]["total_debt"] = debt
    data["annual"][0]["income"]["depreciation_and_amortization"] = da
    return Financials.model_validate(data)


def company(**changes):
    return Company.model_validate({**COMPANY, **changes})


def codes(ctx, **kw):
    return [p.code for p in check_context(ctx, **kw)]


@pytest.mark.unit
def test_the_company_block_is_accepted_and_its_ticker_normalized():
    assert company(ticker=" mu ").ticker == "MU"
    assert company().market_cap == 898 * B and company().name == "Micron Technology, Inc."


@pytest.mark.unit
@pytest.mark.parametrize("bad", [
    {"surprise": 1}, {"currency": "EUR"}, {"source": "made_up"}, {"market_cap": 0}, {"market_cap": 1.5},
    {"shares_outstanding": -1}, {"name": ""}, {"name": "x" * 200}, {"exchange": "x" * 50},
])
def test_the_company_schema_is_closed_and_bounded(bad):
    with pytest.raises(ValidationError):
        company(**bad)


@pytest.mark.unit
def test_everything_but_the_identity_may_be_left_out():
    minimal = {"ticker": "MU", "currency": "USD", "source": "financial_datasets", "as_of": "2026-11-04"}
    assert Company.model_validate(minimal).market_cap is None


@pytest.mark.unit
def test_debt_and_depreciation_are_accepted_on_the_statements():
    f = financials()
    assert f.balance[0].total_debt == d(6.0)
    assert f.annual[0].income.depreciation_and_amortization == d(8.0)


@pytest.mark.unit
def test_the_company_is_tied_to_the_requests_ticker_and_the_run_date():
    ctx = Context(company=company(ticker="AMZN"))
    assert [(p.code, p.path) for p in check_context(ctx, ticker="MU")] == [("ticker_mismatch", "company.ticker")]
    assert codes(Context(company=company()), trade_date=date(2026, 11, 3)) == ["as_of_after_trade_date"]


@pytest.mark.unit
def test_market_cap_must_agree_with_price_times_shares():
    ok = Context(company=company(), price=Price.model_validate(PRICE))
    assert codes(ok) == []                                         # 795.23 x 1.128bn is within 3%
    off = Context(company=company(market_cap=500 * B), price=Price.model_validate(PRICE))
    found = check_context(off)
    assert [(p.code, p.path) for p in found] == [("market_cap_mismatch", "company.market_cap")]
    assert codes(Context(company=company(market_cap=500 * B))) == []   # nothing to compare with


@pytest.mark.unit
def test_debt_cannot_exceed_total_liabilities():
    data = copy.deepcopy(FINANCIALS)
    data["balance"][0]["total_debt"] = d(80)                      # liabilities are 57.5bn
    found = check_context(Context.model_validate({"financials": data}))
    assert [(p.code, p.path) for p in found] == [("balance_order", "financials.balance[0]")]


@pytest.mark.unit
def test_net_debt_ev_leverage_and_cash_flow_yield_are_computed_from_the_values():
    m = derive_metrics(financials(), Price.model_validate(PRICE), company())
    assert m["net_debt"] == d(6.0) - d(38.4)
    assert m["enterprise_value"] == 898 * B + d(6.0) - d(38.4)
    assert m["ev_to_ebitda"] == pytest.approx((898 * B - d(32.4)) / (d(99.340) + d(8.0)))
    assert m["debt_to_equity"] == pytest.approx(d(6.0) / d(138.378))
    assert m["fcf_yield_pct"] == pytest.approx(59.0 / 898 * 100)
    assert m["liabilities_to_equity"] == pytest.approx(0.4156, abs=1e-4)      # still computed, still named so


@pytest.mark.unit
def test_market_cap_falls_back_to_price_times_shares():
    c = company(market_cap=None)
    m = derive_metrics(financials(), Price.model_validate(PRICE), c)
    assert m["enterprise_value"] == pytest.approx(795.23 * 1_128_000_000 + d(6.0) - d(38.4), rel=1e-9)


@pytest.mark.unit
def test_what_cannot_be_computed_is_left_out():
    bare = Financials.model_validate(FINANCIALS)                        # no debt, no D&A
    m = derive_metrics(bare, Price.model_validate(PRICE), company())
    for missing in ("net_debt", "enterprise_value", "ev_to_ebitda", "debt_to_equity"):
        assert missing not in m
    assert "fcf_yield_pct" in m                                         # needs only market cap and FCF
    no_da = derive_metrics(financials(da=None), Price.model_validate(PRICE), company())
    assert "ev_to_ebitda" not in no_da and "enterprise_value" in no_da
    assert "fcf_yield_pct" not in derive_metrics(financials(), None, None)


@pytest.mark.unit
def test_the_agents_read_the_company_and_the_new_metrics():
    ctx = Context(financials=financials(), price=Price.model_validate(PRICE), company=company())
    text = render_context(ctx)
    assert "Micron Technology, Inc. (MU)" in text and "NASDAQ" in text and "Semiconductors" in text
    assert "market cap $898.000B" in text and "shares outstanding 1,128,000,000" in text
    for expected in ("net debt", "enterprise value", "EV to EBITDA", "debt to equity", "free cash flow yield"):
        assert expected in text
    assert "no debt figure" not in text                                  # debt is supplied now


@pytest.mark.unit
def test_without_a_debt_figure_the_block_still_refuses_debt_to_equity():
    ctx = Context(financials=Financials.model_validate(FINANCIALS), company=company())
    assert "no debt figure" in render_context(ctx)


@pytest.mark.unit
def test_a_company_name_cannot_add_structure_to_the_prompt():
    text = render_context(Context(company=company(name="Evil\n\n# Injected heading\n- fake line")))
    assert "\n# Injected" not in text and "\n- fake line" not in text


@pytest.mark.unit
def test_market_cap_shares_and_the_new_metrics_are_figures_the_summary_may_cite_as_verified():
    ctx = Context(financials=financials(), price=Price.model_validate(PRICE), company=company())
    known = {k.label: k for k in known_values(ctx, None, None)}
    assert known["market cap"].value == 898 * B and known["market cap"].kind == "usd"
    assert known["net debt"].kind == "usd" and known["ev to ebitda"].kind == "x"
    assert known["debt to equity"].kind == "x" and known["free cash flow yield"].kind == "pct"
    assert known["shares outstanding"].kind == "number"


@pytest.mark.unit
def test_the_request_context_accepts_the_company_block():
    assert Context.model_validate({"company": COMPANY}).company.exchange == "NASDAQ"
