"""Earnings events from the caller: schema, checks, the prompt block and the watch-date rule."""

from __future__ import annotations

import copy
from datetime import date

import pytest
from pydantic import ValidationError

from tests.test_context_financials import GOOD as FINANCIALS
from tests.test_plan import GOOD as PLAN, READINGS, problems
from tests.test_plan_generate import FakeLLM, _plan
from tradingagents.context import Context, check_context
from tradingagents.context.models import Events
from tradingagents.context.render import render_context
from tradingagents.plan.generate import generate_plan

EVENTS = {
    "ticker": "MU", "currency": "USD", "as_of": "2026-11-05", "source": "financial_datasets",
    "next_earnings": {"date": "2026-12-17", "fiscal_period": "Q1 FY2027", "confirmed": False},
    "recent_earnings": [{
        "fiscal_period": "Q4 FY2026", "period_end": "2026-09-03", "reported_on": "2026-10-09",
        "eps_actual": 12.5, "eps_estimate": None, "revenue_actual": 54229000000, "revenue_estimate": None,
    }],
}


def events(**changes):
    data = copy.deepcopy(EVENTS)
    data.update(changes)
    return Events.model_validate(data)


def codes(ev, **kw):
    return [p.code for p in check_context(Context(events=ev), **kw)]


@pytest.mark.unit
def test_consistent_events_are_accepted_and_estimates_may_be_null():
    ev = events()
    assert ev.next_earnings.date == date(2026, 12, 17) and ev.recent_earnings[0].eps_estimate is None
    assert codes(ev, ticker="MU", trade_date=date(2026, 11, 5)) == []
    assert codes(events(next_earnings=None, recent_earnings=[])) == []


@pytest.mark.unit
@pytest.mark.parametrize("bad", [
    {"surprise": 1}, {"currency": "EUR"}, {"source": "made_up"}, {"ticker": ""},
    {"next_earnings": {"date": "2026-12-17", "fiscal_period": "Q1", "confirmed": False, "extra": 1}},
    {"next_earnings": {"date": "2026-12-17", "fiscal_period": "Q1"}},                     # confirmed is required
    {"recent_earnings": EVENTS["recent_earnings"] * 5},
])
def test_the_events_schema_is_closed_and_bounded(bad):
    with pytest.raises(ValidationError):
        events(**bad)


@pytest.mark.unit
def test_events_are_tied_to_the_requests_company():
    found = check_context(Context(events=events(ticker="AMZN")), ticker="MU")
    assert [(p.code, p.path) for p in found] == [("ticker_mismatch", "events.ticker")]


@pytest.mark.unit
def test_an_earnings_date_that_has_already_passed_is_stale():
    ev = events(next_earnings={"date": "2026-11-05", "fiscal_period": "Q1 FY2027", "confirmed": True})
    found = check_context(Context(events=ev))
    assert [(p.code, p.path) for p in found] == [("earnings_date_passed", "events.next_earnings.date")]


@pytest.mark.unit
def test_a_report_date_must_fall_after_its_period_and_by_the_block_date():
    early = copy.deepcopy(EVENTS["recent_earnings"][0])
    early["reported_on"] = "2026-09-01"
    late = {**EVENTS["recent_earnings"][0], "fiscal_period": "Q3 FY2026", "period_end": "2026-05-28",
            "reported_on": "2026-12-01"}
    found = check_context(Context(events=events(recent_earnings=[early, late])))
    assert {p.code for p in found} == {"reported_before_period_end", "reported_after_as_of"}


@pytest.mark.unit
def test_the_same_quarter_twice_and_a_block_dated_after_the_run_are_refused():
    twice = events(recent_earnings=EVENTS["recent_earnings"] * 2)
    assert codes(twice) == ["duplicate_period"]
    assert codes(events(), trade_date=date(2026, 11, 4)) == ["as_of_after_trade_date"]


@pytest.mark.unit
def test_the_request_context_accepts_events_alongside_the_other_blocks():
    ctx = Context.model_validate({"financials": FINANCIALS, "events": EVENTS})
    assert ctx.events.ticker == "MU"


@pytest.mark.unit
def test_the_prompt_block_gives_the_next_date_and_the_only_rule_for_dating_earnings():
    text = render_context(Context(events=events()))
    assert "Next earnings: 2026-12-17 (Q1 FY2027, not confirmed)" in text
    assert "Q4 FY2026" in text and "reported 2026-10-09" in text and "EPS $12.50" in text
    assert "$54.229B" in text and "estimates not supplied" in text
    assert "only date" in text
    confirmed = events(next_earnings={"date": "2026-12-17", "fiscal_period": "Q1 FY2027", "confirmed": True})
    assert "Q1 FY2027, confirmed" in render_context(Context(events=confirmed))


@pytest.mark.unit
def test_with_no_upcoming_earnings_the_block_says_not_to_invent_one():
    assert "not supplied" in render_context(Context(events=events(next_earnings=None)))


def _watch(what, when):
    data = copy.deepcopy(PLAN)
    data["watch"] = [{"what": what, "when": when, "effect": "supports"}]
    return data


@pytest.mark.unit
@pytest.mark.parametrize("what", [
    "Next quarterly report: growth and capex", "Q1 earnings call guidance", "Earnings print next month",
    "Quarterly results show margin recovery",
])
def test_a_watch_item_about_earnings_may_only_carry_the_supplied_date(what):
    ok = date(2026, 12, 17)
    assert problems(_watch(what, "2026-12-17"), next_earnings=ok) == []
    assert problems(_watch(what, None), next_earnings=ok) == []
    wrong = problems(_watch(what, "2026-12-10"), next_earnings=ok)
    assert any("earnings" in p and "2026-12-17" in p for p in wrong)
    undated = problems(_watch(what, "2026-12-10"), next_earnings=None)
    assert any("no earnings date was supplied" in p for p in undated)


@pytest.mark.unit
def test_a_watch_item_that_is_not_about_earnings_keeps_its_own_date():
    data = _watch("Gross margin holds above 75% in the next filing", "2026-12-10")
    assert problems(data, next_earnings=date(2026, 12, 17)) == []
    assert problems(data, next_earnings=None) == []


@pytest.mark.unit
def test_the_generator_tells_the_model_the_date_and_retries_an_invented_one():
    invented = _plan(watch=[{"what": "Q1 earnings print", "when": "2026-12-10", "effect": "supports"}])
    fixed = _plan(watch=[{"what": "Q1 earnings print", "when": "2026-12-17", "effect": "supports"}])
    llm = FakeLLM(invented, fixed)
    out = generate_plan(llm, decision="Rating: Overweight", rating="Overweight", readings=READINGS,
                        events=events())
    assert out.plan is not None and out.attempts == 2
    assert "Next earnings are on 2026-12-17 (not confirmed)" in llm.prompts[0]
    assert "2026-12-17" in llm.prompts[1] and "earnings" in llm.prompts[1]


@pytest.mark.unit
def test_without_events_the_model_is_told_to_date_no_earnings_item():
    llm = FakeLLM(_plan())
    generate_plan(llm, decision="Rating: Overweight", rating="Overweight", readings=READINGS)
    assert "Earnings date: not supplied" in llm.prompts[0]


@pytest.mark.unit
def test_the_plan_step_hands_the_supplied_events_to_the_generator(monkeypatch):
    from tests.test_plan_node import STATE
    from tradingagents.plan import node as node_mod
    from tradingagents.plan.node import create_plan_node

    monkeypatch.setattr(node_mod, "compute_readings", lambda symbol, as_of: READINGS)
    llm = FakeLLM(_plan())
    create_plan_node(llm, events())(STATE)
    assert "2026-12-17" in llm.prompts[0]


@pytest.mark.unit
def test_reported_actuals_are_figures_the_summary_may_cite_as_verified():
    from tradingagents.summary_check import known_values

    known = known_values(Context(events=events()), None, None)
    assert any(k.label == "reported eps" and k.value == 12.5 for k in known)
    assert any(k.label == "reported revenue" and k.value == 54229000000 for k in known)
