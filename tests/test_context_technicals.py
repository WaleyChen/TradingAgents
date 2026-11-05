"""The caller's technicals: schema, checks, derived distances and their use as the plan's readings."""

from __future__ import annotations

import copy
from datetime import date

import pytest
from pydantic import ValidationError

from tests.test_context_financials import GOOD as FINANCIALS
from tests.test_plan import READINGS
from tradingagents.context import Context, check_context
from tradingagents.context.derive import derive_technicals
from tradingagents.context.models import Price, Technicals
from tradingagents.context.render import render_context
from tradingagents.plan.check import Readings
from tradingagents.plan.readings import readings_from
from tradingagents.summary_check import known_values

TECH = {
    "ticker": "MU", "currency": "USD", "source": "getmarketalerts", "as_of": "2026-11-04", "close": 795.23,
    "sma_20": 914.9, "sma_50": 972.08, "sma_200": 753.73, "ema_10": 878.2, "rsi_14": 35.38,
    "macd": {"line": -38.89, "signal": -29.12, "histogram": -9.77},
    "volume": 35_400_000, "volume_avg_50d": 21_800_000, "high_52w": 1213.56, "low_52w": 201.37,
}
PRICE = {"ticker": "MU", "currency": "USD", "source": "getmarketalerts", "as_of": "2026-11-04", "close": 795.23}


def technicals(**changes):
    return Technicals.model_validate({**copy.deepcopy(TECH), **changes})


def codes(ctx, **kw):
    return [p.code for p in check_context(ctx, **kw)]


@pytest.mark.unit
def test_consistent_technicals_are_accepted_and_most_fields_may_be_left_out():
    t = technicals(ticker=" mu ")
    assert t.ticker == "MU" and t.macd.histogram == -9.77 and t.volume == 35_400_000
    minimal = {"ticker": "MU", "currency": "USD", "source": "getmarketalerts", "as_of": "2026-11-04", "close": 10.0}
    assert Technicals.model_validate(minimal).rsi_14 is None


@pytest.mark.unit
@pytest.mark.parametrize("bad", [
    {"surprise": 1}, {"currency": "EUR"}, {"source": "made_up"}, {"rsi_14": 101}, {"rsi_14": -1},
    {"sma_200": 0}, {"close": 0}, {"volume": -1}, {"volume": 1.5}, {"macd": {"line": 1, "extra": 2}},
])
def test_the_technicals_schema_is_closed_and_bounded(bad):
    with pytest.raises(ValidationError):
        technicals(**bad)


@pytest.mark.unit
def test_close_is_required():
    data = copy.deepcopy(TECH)
    del data["close"]
    with pytest.raises(ValidationError):
        Technicals.model_validate(data)


@pytest.mark.unit
def test_technicals_are_tied_to_the_company_and_the_run_window():
    assert codes(Context(technicals=technicals()), ticker="MU", trade_date=date(2026, 11, 4)) == []
    found = check_context(Context(technicals=technicals(ticker="AMZN")), ticker="MU")
    assert [(p.code, p.path) for p in found] == [("ticker_mismatch", "technicals.ticker")]
    assert codes(Context(technicals=technicals()), trade_date=date(2026, 11, 3)) == ["technicals_date"]
    assert codes(Context(technicals=technicals(as_of="2026-10-20")), trade_date=date(2026, 11, 4)) == ["technicals_date"]


@pytest.mark.unit
def test_the_close_must_tie_to_the_price_on_the_same_date():
    ok = Context(technicals=technicals(), price=Price.model_validate(PRICE))
    assert codes(ok) == []
    off = Context(technicals=technicals(close=830.0), price=Price.model_validate(PRICE))
    assert [(p.code, p.path) for p in check_context(off)] == [("technicals_price_mismatch", "technicals.close")]
    other_day = Context(technicals=technicals(close=830.0, as_of="2026-11-03"), price=Price.model_validate(PRICE))
    assert codes(other_day) == []                                  # different dates: nothing to tie


@pytest.mark.unit
def test_the_macd_histogram_must_be_the_line_less_the_signal():
    bad = technicals(macd={"line": -38.89, "signal": -29.12, "histogram": -38.89})
    assert [(p.code, p.path) for p in check_context(Context(technicals=bad))] == [
        ("macd_inconsistent", "technicals.macd")]
    partial = technicals(macd={"line": -38.89, "signal": None, "histogram": -9.77})
    assert codes(Context(technicals=partial)) == []


@pytest.mark.unit
def test_the_close_must_sit_inside_the_52_week_range():
    found = codes(Context(technicals=technicals(high_52w=700.0)))
    assert found == ["range_inconsistent"]
    assert codes(Context(technicals=technicals(low_52w=900.0))) == ["range_inconsistent"]


@pytest.mark.unit
def test_distances_and_the_volume_ratio_are_computed_in_code():
    m = derive_technicals(technicals())
    assert m["pct_vs_sma_20"] == pytest.approx((795.23 / 914.9 - 1) * 100)
    assert m["pct_vs_sma_50"] == pytest.approx(-18.19, abs=0.01)
    assert m["pct_vs_sma_200"] == pytest.approx(5.51, abs=0.01)
    assert m["pct_from_high_52w"] == pytest.approx(-34.47, abs=0.01)
    assert m["volume_vs_average"] == pytest.approx(35.4 / 21.8)
    assert derive_technicals(technicals(sma_50=None, volume_avg_50d=None, high_52w=None)).keys() == {
        "pct_vs_sma_20", "pct_vs_sma_200"}


@pytest.mark.unit
def test_the_agents_read_the_levels_and_the_computed_distances():
    text = render_context(Context(technicals=technicals()))
    for expected in ("Technicals for MU", "close $795.23", "50-day average $972.08", "RSI(14) 35.38",
                     "MACD line -38.89, signal -29.12, histogram -9.77", "10-day EMA $878.20",
                     "52-week high $1,213.56", "18.2% below the 50-day average",
                     "5.5% above the 200-day average", "1.62x"):
        assert expected in text
    assert "no other levels" in text


@pytest.mark.unit
def test_the_levels_and_distances_are_figures_the_summary_may_cite_as_verified():
    known = known_values(Context(technicals=technicals()), None, None)
    labels = {k.label for k in known}
    assert {"technicals close", "technicals sma_50", "technicals rsi_14", "technicals macd histogram",
            "technicals high_52w", "pct vs sma_50", "volume vs average"} <= labels
    assert any(k.label == "pct vs sma_50" and k.kind == "pct" for k in known)


def _fetch(**fields):
    base = {"price": 100.0, "rsi_14": 50.0, "sma_20": 101.0, "sma_50": 102.0, "sma_200": 103.0,
            "macd_histogram": 0.5, "date": "2026-11-03"}
    return lambda symbol, as_of: Readings(**{**base, **fields})


def _no_fetch(symbol, as_of):
    raise AssertionError("complete technicals must not trigger a fetch")


@pytest.mark.unit
def test_complete_technicals_are_the_readings_and_nothing_is_fetched():
    r = readings_from(technicals(), "MU", "2026-11-04", fetch=_no_fetch)
    assert r == Readings(price=795.23, rsi_14=35.38, sma_20=914.9, sma_50=972.08, sma_200=753.73,
                         macd_histogram=-9.77, date="2026-11-04")


@pytest.mark.unit
def test_what_the_caller_left_out_is_filled_from_our_own_readings():
    r = readings_from(technicals(sma_50=None, rsi_14=None, macd=None), "MU", "2026-11-04", fetch=_fetch())
    assert (r.price, r.sma_20, r.sma_200) == (795.23, 914.9, 753.73)        # supplied wins
    assert (r.sma_50, r.rsi_14, r.macd_histogram) == (102.0, 50.0, 0.5)    # ours fills the gaps
    assert r.date == "2026-11-04"


@pytest.mark.unit
def test_with_no_technicals_the_readings_are_ours():
    assert readings_from(None, "MU", "2026-11-04", fetch=_fetch()).price == 100.0


@pytest.mark.unit
def test_if_our_fetch_fails_the_supplied_close_still_gives_readings():
    def boom(symbol, as_of):
        raise RuntimeError("vendor down")

    r = readings_from(technicals(sma_50=None), "MU", "2026-11-04", fetch=boom)
    assert r.price == 795.23 and r.sma_50 is None and r.sma_200 == 753.73
    with pytest.raises(RuntimeError):
        readings_from(None, "MU", "2026-11-04", fetch=boom)


@pytest.mark.unit
def test_the_plan_step_checks_against_the_callers_technicals(monkeypatch):
    from tests.test_plan_generate import FakeLLM, _plan
    from tradingagents.plan import node as node_mod
    from tradingagents.plan.node import create_plan_node

    monkeypatch.setattr(node_mod, "compute_readings", _no_fetch)
    llm = FakeLLM(_plan())
    out = create_plan_node(llm, technicals=technicals())({
        "company_of_interest": "MU", "trade_date": "2026-11-04",
        "final_trade_decision": "Rating: Overweight", "final_rating": "Overweight"})
    assert out["verified_readings"]["sma_200"] == 753.73 and out["verified_readings"]["price"] == 795.23
    assert "753.73" in llm.prompts[0]


@pytest.mark.unit
def test_the_request_context_accepts_technicals():
    ctx = Context.model_validate({"financials": FINANCIALS, "technicals": TECH})
    assert ctx.technicals.rsi_14 == 35.38
    assert READINGS.price != ctx.technicals.close
