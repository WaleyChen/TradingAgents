"""valuation_history, market_backdrop and prior_reports: schema, checks, who reads them, derived comparisons."""

from __future__ import annotations

import copy
from datetime import date

import pytest
from pydantic import ValidationError

from tradingagents.context import Context, check_context
from tradingagents.context.derive import derive_prior_reports
from tradingagents.context.models import MarketBackdrop, PriorReport, ValuationHistory
from tradingagents.context.render import render_context, render_role_blocks
from tradingagents.summary_check import known_values

VALUATION = {
    "ticker": "MU", "currency": "USD", "source": "getmarketalerts", "as_of": "2026-11-04",
    "pe_ttm": {"latest": 21.5, "min_5y": 8.1, "median_5y": 14.2, "max_5y": 38.0, "percentile_5y": 0.88, "years": 5},
    "peg": 0.29,
}
BACKDROP = {
    "source": "getmarketalerts", "as_of": "2026-11-04",
    "vix": {"value": 22.4, "as_of": "2026-11-04"},
    "fear_and_greed": {"value": 31, "label": "Fear", "as_of": "2026-11-04"},
    "buffett_indicator_pct": {"value": 215, "as_of": "2026-09-30"},
    "credit_spread_hy_bps": {"value": 310, "as_of": "2026-11-03"},
}
PRIOR = [{"ticker": "MU", "as_of": "2026-10-12", "decision": "Overweight", "target": 1100.0,
          "price_then": 950.1, "horizon": "3-6 months"}]


def valuation(**changes):
    return ValuationHistory.model_validate({**copy.deepcopy(VALUATION), **changes})


def backdrop(**changes):
    return MarketBackdrop.model_validate({**copy.deepcopy(BACKDROP), **changes})


def codes(**blocks):
    kw = blocks.pop("kw", {})
    return [(p.code, p.path) for p in check_context(Context.model_validate(blocks), **kw)]


# --- valuation_history -------------------------------------------------------

@pytest.mark.unit
def test_valuation_history_is_accepted_and_its_parts_are_optional():
    v = valuation(ticker=" mu ")
    assert v.ticker == "MU" and v.pe_ttm.percentile_5y == 0.88
    minimal = {"ticker": "MU", "currency": "USD", "source": "getmarketalerts", "as_of": "2026-11-04"}
    assert ValuationHistory.model_validate(minimal).pe_ttm is None
    assert codes(valuation_history=VALUATION) == []


@pytest.mark.unit
@pytest.mark.parametrize("bad", [
    {"surprise": 1}, {"currency": "EUR"}, {"source": "made_up"},
    {"pe_ttm": {**VALUATION["pe_ttm"], "percentile_5y": 1.5}}, {"pe_ttm": {**VALUATION["pe_ttm"], "years": 0}},
    {"pe_ttm": {**VALUATION["pe_ttm"], "latest": 0}}, {"pe_ttm": {**VALUATION["pe_ttm"], "extra": 1}},
])
def test_the_valuation_schema_is_closed_and_bounded(bad):
    with pytest.raises(ValidationError):
        valuation(**bad)


@pytest.mark.unit
def test_the_valuation_range_and_percentile_must_agree():
    swapped = {**VALUATION["pe_ttm"], "min_5y": 40.0}
    assert ("valuation_range_inconsistent", "valuation_history.pe_ttm") in codes(
        valuation_history={**VALUATION, "pe_ttm": swapped})
    outside = {**VALUATION["pe_ttm"], "latest": 60.0}
    assert ("valuation_range_inconsistent", "valuation_history.pe_ttm") in codes(
        valuation_history={**VALUATION, "pe_ttm": outside})
    low_pct = {**VALUATION["pe_ttm"], "percentile_5y": 0.10}                # latest 21.5 is above the 14.2 median
    assert ("percentile_inconsistent", "valuation_history.pe_ttm") in codes(
        valuation_history={**VALUATION, "pe_ttm": low_pct})
    at_max = {**VALUATION["pe_ttm"], "latest": 38.0, "percentile_5y": 0.5}
    assert ("percentile_inconsistent", "valuation_history.pe_ttm") in codes(
        valuation_history={**VALUATION, "pe_ttm": at_max})


@pytest.mark.unit
def test_valuation_history_is_tied_to_the_company_and_the_run_date():
    assert codes(valuation_history={**VALUATION, "ticker": "AMZN"}, kw={"ticker": "MU"}) == [
        ("ticker_mismatch", "valuation_history.ticker")]
    assert codes(valuation_history=VALUATION, kw={"trade_date": date(2026, 11, 3)}) == [
        ("as_of_after_trade_date", "valuation_history.as_of")]


@pytest.mark.unit
def test_the_two_price_to_earnings_figures_are_named_apart_and_never_to_be_mixed():
    text = render_context(Context.model_validate({"valuation_history": VALUATION}))
    for expected in ("trailing-twelve-month EPS", "21.5x", "8.1x", "38.0x", "median of 14.2x",
                     "88th percentile", "PEG 0.29", "never mix"):
        assert expected in text
    from tests.test_context_company import PRICE
    from tests.test_context_financials import GOOD

    data = copy.deepcopy(GOOD)
    data["annual"][0]["income"]["eps_diluted"] = 74.91
    both = render_context(Context.model_validate({"financials": data, "price": PRICE, "valuation_history": VALUATION}))
    assert "latest fiscal year's diluted EPS" in both and "trailing-twelve-month EPS" in both


@pytest.mark.unit
def test_valuation_history_goes_to_the_fundamentals_analyst_and_the_digest():
    blocks = render_role_blocks(Context.model_validate({"valuation_history": VALUATION}))
    assert "Valuation history for MU" in blocks["fundamentals"]
    assert "88th percentile" in blocks["decision"]


# --- market_backdrop ---------------------------------------------------------

@pytest.mark.unit
def test_the_backdrop_is_accepted_and_every_indicator_is_optional():
    b = backdrop()
    assert b.vix.value == 22.4 and b.fear_and_greed.label == "Fear"
    assert MarketBackdrop.model_validate({"source": "getmarketalerts", "as_of": "2026-11-04"}).vix is None
    assert codes(market_backdrop=BACKDROP) == []


@pytest.mark.unit
@pytest.mark.parametrize("bad", [
    {"surprise": 1}, {"source": "made_up"}, {"vix": {"value": 22.4}}, {"vix": {"value": -1, "as_of": "2026-11-04"}},
    {"fear_and_greed": {"value": 101, "label": "Greed", "as_of": "2026-11-04"}},
    {"vix": {"value": 22.4, "as_of": "2026-11-04", "extra": 1}},
])
def test_the_backdrop_schema_is_closed_and_bounded(bad):
    with pytest.raises(ValidationError):
        backdrop(**bad)


@pytest.mark.unit
def test_an_indicator_cannot_be_dated_after_the_block_and_the_label_must_fit_the_value():
    later = {**BACKDROP, "vix": {"value": 22.4, "as_of": "2026-11-09"}}
    assert ("date_after_as_of", "market_backdrop.vix.as_of") in codes(market_backdrop=later)
    wrong = {**BACKDROP, "fear_and_greed": {"value": 31, "label": "Extreme Greed", "as_of": "2026-11-04"}}
    assert codes(market_backdrop=wrong) == [("label_inconsistent", "market_backdrop.fear_and_greed")]
    assert codes(market_backdrop=BACKDROP, kw={"trade_date": date(2026, 11, 3)}) == [
        ("as_of_after_trade_date", "market_backdrop.as_of")]


@pytest.mark.unit
def test_every_indicator_is_read_with_its_own_date():
    text = render_context(Context.model_validate({"market_backdrop": BACKDROP}))
    for expected in ("VIX 22.4 (as of 2026-11-04)", "Fear & Greed 31, Fear (as of 2026-11-04)",
                     "Buffett indicator 215% (as of 2026-09-30)", "high-yield credit spread 310 basis points (as of 2026-11-03)",
                     "state each date"):
        assert expected in text
    blocks = render_role_blocks(Context.model_validate({"market_backdrop": BACKDROP}))
    assert "VIX 22.4" in blocks["news"]


# --- prior_reports -----------------------------------------------------------

@pytest.mark.unit
def test_prior_reports_are_accepted_newest_first_and_target_may_be_null():
    ctx = Context.model_validate({"prior_reports": PRIOR + [
        {"ticker": "MU", "as_of": "2026-09-01", "decision": "Hold", "target": None, "price_then": 700.0}]})
    assert ctx.prior_reports[1].target is None and ctx.prior_reports[1].horizon is None


@pytest.mark.unit
@pytest.mark.parametrize("bad", [
    [{**PRIOR[0], "decision": "Moonshot"}], [{**PRIOR[0], "price_then": 0}], [{**PRIOR[0], "surprise": 1}],
    PRIOR * 4,
])
def test_the_prior_report_schema_is_closed_and_bounded(bad):
    with pytest.raises(ValidationError):
        Context.model_validate({"prior_reports": bad})


@pytest.mark.unit
def test_prior_reports_must_be_for_this_company_before_the_run_and_newest_first():
    assert codes(prior_reports=[{**PRIOR[0], "ticker": "AMZN"}], kw={"ticker": "MU"}) == [
        ("ticker_mismatch", "prior_reports[0].ticker")]
    assert codes(prior_reports=PRIOR, kw={"trade_date": date(2026, 10, 12)}) == [
        ("prior_after_run", "prior_reports[0].as_of")]
    older_first = [{**PRIOR[0], "as_of": "2026-09-01"}, PRIOR[0]]
    assert codes(prior_reports=older_first) == [("prior_order", "prior_reports")]


@pytest.mark.unit
def test_a_prior_buy_with_a_target_below_its_price_is_inconsistent():
    bad = [{**PRIOR[0], "target": 900.0}]                                   # Overweight, target under the 950.10 price
    assert codes(prior_reports=bad) == [("prior_target_inconsistent", "prior_reports[0].target")]
    sell = [{**PRIOR[0], "decision": "Sell", "target": 900.0}]
    assert codes(prior_reports=sell) == []


@pytest.mark.unit
def test_the_move_since_the_last_report_is_worked_out_in_code():
    row = derive_prior_reports([PriorReport.model_validate(PRIOR[0])], current_price=795.23)[0]
    assert row["price_change_pct"] == pytest.approx((795.23 / 950.1 - 1) * 100)
    assert row["direction_to_target"] == "away"                 # the target was above; the price fell
    up = derive_prior_reports([PriorReport.model_validate(PRIOR[0])], current_price=1020.0)[0]
    assert up["direction_to_target"] == "toward" and up["progress_pct"] == pytest.approx(46.6, abs=0.1)
    done = derive_prior_reports([PriorReport.model_validate(PRIOR[0])], current_price=1150.0)[0]
    assert done["direction_to_target"] == "reached"
    none = derive_prior_reports([PriorReport.model_validate({**PRIOR[0], "target": None})], 795.23)[0]
    assert none["direction_to_target"] is None and none["decision_label"] == "Weak Buy"


@pytest.mark.unit
def test_the_summary_reads_prior_reports_in_plain_labels_with_the_move_since():
    ctx = Context.model_validate({"prior_reports": PRIOR, "price": {
        "ticker": "MU", "currency": "USD", "source": "getmarketalerts", "as_of": "2026-11-04", "close": 795.23}})
    text = render_context(ctx)
    for expected in ("Previous reports", "2026-10-12", "Weak Buy", "$1,100.00", "$950.10", "3-6 months",
                     "-16.3%", "moving away from that target"):
        assert expected in text
    assert "Overweight" not in text.split("Previous reports")[1]       # our internal rating word is not shown
    blocks = render_role_blocks(ctx)
    assert all("Previous reports" not in b for b in blocks.values())   # the analysts never see the old rating


@pytest.mark.unit
def test_valuation_backdrop_and_prior_figures_can_be_cited_as_verified():
    ctx = Context.model_validate({"valuation_history": VALUATION, "market_backdrop": BACKDROP,
                                  "prior_reports": PRIOR})
    labels = {k.label for k in known_values(ctx, None, None)}
    assert {"pe ttm", "pe ttm median 5y", "peg", "vix", "fear and greed", "prior target", "prior price"} <= labels
