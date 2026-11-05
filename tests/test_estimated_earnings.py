"""An estimated next-earnings date from Yahoo, used only when the caller sends none."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from tests.test_context_events import EVENTS
from tests.test_graph_end_to_end import TRADE_DATE, ScriptedModel, _graph, offline  # noqa: F401
from tradingagents.context import Context
from tradingagents.context.estimate import (
    estimate_next_earnings,
    fetch_yahoo_earnings_dates,
    with_estimated_earnings,
)
from tradingagents.context.models import Events
from tradingagents.context.render import render_context

AS_OF = date(2026, 11, 4)
NEXT = date(2026, 12, 17)


def _fetch(*dates):
    return lambda ticker: list(dates)


def _boom(ticker):
    raise AssertionError("must not fetch")


@pytest.mark.unit
def test_the_first_future_date_is_the_estimate_and_it_is_never_confirmed():
    est = estimate_next_earnings("MU", AS_OF, _fetch(date(2026, 10, 9), NEXT, date(2026, 12, 20)))
    assert est.date == NEXT and est.confirmed is False and est.fiscal_period


@pytest.mark.unit
@pytest.mark.parametrize("dates", [[], [date(2026, 10, 9)], [AS_OF]])
def test_no_future_date_means_no_estimate(dates):
    assert estimate_next_earnings("MU", AS_OF, _fetch(*dates)) is None


@pytest.mark.unit
def test_a_failing_lookup_means_no_estimate_and_never_an_error():
    def down(ticker):
        raise RuntimeError("yahoo down")

    assert estimate_next_earnings("MU", AS_OF, down) is None


@pytest.mark.unit
def test_with_no_context_an_estimate_makes_an_events_block_of_its_own():
    ctx = with_estimated_earnings(None, "MU", AS_OF, _fetch(NEXT))
    assert ctx.events.next_earnings.date == NEXT and ctx.events.source == "yahoo_finance"
    assert ctx.events.ticker == "MU" and ctx.events.recent_earnings == []
    assert with_estimated_earnings(None, "MU", AS_OF, _fetch()) is None


@pytest.mark.unit
def test_events_without_a_next_date_get_the_estimate_and_keep_what_they_had():
    data = {**EVENTS, "next_earnings": None}
    original = Context(events=Events.model_validate(data))
    ctx = with_estimated_earnings(original, "MU", AS_OF, _fetch(NEXT))
    assert ctx.events.next_earnings.date == NEXT
    assert ctx.events.recent_earnings == original.events.recent_earnings and ctx.events.source == "financial_datasets"
    assert original.events.next_earnings is None                       # the caller's context is untouched


@pytest.mark.unit
def test_a_context_without_events_gains_them_beside_its_other_blocks():
    from tests.test_context_financials import GOOD

    original = Context.model_validate({"financials": GOOD})
    ctx = with_estimated_earnings(original, "MU", AS_OF, _fetch(NEXT))
    assert ctx.financials == original.financials and ctx.events.next_earnings.date == NEXT


@pytest.mark.unit
def test_a_date_the_caller_sent_is_never_replaced_and_nothing_is_fetched():
    original = Context(events=Events.model_validate(EVENTS))
    assert with_estimated_earnings(original, "MU", AS_OF, _boom) is original


@pytest.mark.unit
def test_the_agents_are_told_the_date_is_yahoos_estimate():
    ctx = with_estimated_earnings(None, "MU", AS_OF, _fetch(NEXT))
    text = render_context(ctx)
    assert "Next earnings: 2026-12-17" in text and "estimated by Yahoo Finance" in text and "not confirmed" in text


@pytest.mark.unit
def test_the_yahoo_reader_accepts_the_shapes_yahoo_returns():
    import yfinance as yf

    class Ticker:
        def __init__(self, calendar):
            self.calendar = calendar

    shapes = [{"Earnings Date": [NEXT, date(2026, 12, 21)]}, {"Earnings Date": [datetime(2026, 12, 17, 4, 0)]},
              {"Earnings Date": NEXT}, {}, None]
    expected = [[NEXT, date(2026, 12, 21)], [NEXT], [NEXT], [], []]
    got = []
    original = yf.Ticker
    try:
        for shape in shapes:
            yf.Ticker = lambda symbol, _s=shape: Ticker(_s)
            got.append(fetch_yahoo_earnings_dates("MU"))
    finally:
        yf.Ticker = original
    assert got == expected


@pytest.mark.unit
def test_the_run_state_carries_the_estimate_to_the_agents_and_the_plan(tmp_path, monkeypatch, offline):  # noqa: F811
    from tradingagents.graph import trading_graph

    monkeypatch.setattr(trading_graph, "estimate_next_earnings", lambda t, a: estimate_next_earnings(t, a, _fetch(NEXT)))
    graph = _graph(tmp_path, monkeypatch, ScriptedModel(), estimate_next_earnings=True)
    state = graph.create_run_state("MU", "2026-11-04")
    assert state["run_context"]["events"]["next_earnings"]["date"] == "2026-12-17"
    assert "2026-12-17" in state["verified_blocks"]["news"]
    off = _graph(tmp_path, monkeypatch, ScriptedModel())
    assert off.create_run_state("MU", TRADE_DATE)["run_context"] is None


@pytest.mark.unit
def test_the_api_estimates_unless_the_environment_says_otherwise(monkeypatch):
    from api.schemas import CreateAnalysisRequest
    from api.service import build_config

    request = CreateAnalysisRequest(ticker="MU", trade_date=AS_OF)
    monkeypatch.delenv("TRADINGAGENTS_ESTIMATE_NEXT_EARNINGS", raising=False)
    assert build_config(request)["estimate_next_earnings"] is True
    monkeypatch.setenv("TRADINGAGENTS_ESTIMATE_NEXT_EARNINGS", "0")
    assert build_config(request)["estimate_next_earnings"] is False
