"""Generating a plan: ask, check, and ask again with the problems, or give up
with an error and no plan."""

from __future__ import annotations

import copy

import pytest

from tests.test_plan import GOOD, PRICE, READINGS
from tradingagents.plan import Plan
from tradingagents.plan.generate import generate_plan


class _Structured:
    def __init__(self, replies, prompts):
        self.replies, self.prompts = list(replies), prompts

    def invoke(self, prompt):
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeLLM:
    def __init__(self, *replies):
        self.replies, self.prompts = replies, []

    def with_structured_output(self, schema):
        assert schema is Plan
        return _Structured(self.replies, self.prompts)


class NoStructuredLLM:
    def with_structured_output(self, schema):
        raise NotImplementedError("no tool calling")


def _plan(**changes) -> Plan:
    data = copy.deepcopy(GOOD)
    data.update(changes)
    return Plan.model_validate(data)


def _run(llm, **kw):
    return generate_plan(llm, decision="Rating: Overweight. Add on dips.", rating="Overweight",
                         readings=READINGS, **kw)


def _bad() -> Plan:
    data = copy.deepcopy(GOOD)
    data["entries"][0]["trigger"] = {"price": {"when": "at_or_below", "value": 260.0}}
    return Plan.model_validate(data)


@pytest.mark.unit
def test_a_plan_that_passes_is_returned_after_one_call():
    llm = FakeLLM(_plan())
    out = _run(llm)
    assert out.error is None and out.plan is not None
    assert out.attempts == 1 and len(llm.prompts) == 1


@pytest.mark.unit
def test_the_prompt_carries_the_decision_and_the_verified_numbers():
    llm = FakeLLM(_plan())
    _run(llm)
    prompt = llm.prompts[0]
    assert "Add on dips" in prompt and str(PRICE) in prompt and "rsi_14" in prompt


@pytest.mark.unit
def test_a_failed_check_is_retried_with_the_problems_listed():
    llm = FakeLLM(_bad(), _plan())
    out = _run(llm)
    assert out.plan is not None and out.attempts == 2
    assert "must sit below as_of_price" in llm.prompts[1]


@pytest.mark.unit
def test_a_plan_that_never_passes_is_an_error_and_no_plan():
    llm = FakeLLM(_bad(), _bad(), _bad())
    out = _run(llm, max_retries=2)
    assert out.plan is None and out.attempts == 3
    assert "must sit below as_of_price" in out.error


@pytest.mark.unit
def test_a_malformed_reply_counts_as_an_attempt_and_is_retried():
    llm = FakeLLM(ValueError("trigger: not on the list"), _plan())
    out = _run(llm)
    assert out.plan is not None and out.attempts == 2
    assert "not on the list" in llm.prompts[1]


@pytest.mark.unit
def test_a_provider_without_structured_output_fails_closed():
    out = _run(NoStructuredLLM())
    assert out.plan is None and "structured output" in out.error


@pytest.mark.unit
def test_the_price_and_date_are_the_verified_ones_whatever_the_model_wrote():
    from datetime import date

    stale = _plan(as_of_price=PRICE * 1.002, as_of_date=date(2020, 1, 1))
    from tradingagents.plan import Readings
    readings = Readings(**{**READINGS.__dict__, "date": "2026-11-02"})
    out = generate_plan(FakeLLM(stale), decision="x", rating="Overweight", readings=readings)
    assert out.plan.as_of_price == PRICE and out.plan.as_of_date.isoformat() == "2026-11-02"


@pytest.mark.unit
def test_an_infrastructure_error_is_not_retried_and_its_text_is_not_returned():
    """A provider's error body can echo a key; it belongs in the log only."""
    llm = FakeLLM(RuntimeError("Incorrect API key provided: sk-SECRET123"), _plan())
    out = _run(llm)
    assert out.plan is None and out.attempts == 1 and len(llm.prompts) == 1
    assert "sk-SECRET123" not in out.error and out.error


@pytest.mark.unit
def test_a_malformed_reply_is_retried_but_its_text_is_not_returned():
    llm = FakeLLM(ValueError("bad trigger sk-LEAK"), ValueError("bad trigger sk-LEAK"),
                  ValueError("bad trigger sk-LEAK"))
    out = _run(llm)
    assert out.plan is None and "sk-LEAK" not in out.error


@pytest.mark.unit
def test_the_prompt_lists_the_triggers_that_are_already_true():
    from tradingagents.plan.generate import _prompt

    # price 247.19: below the 20 and 50 day averages, above the 200; RSI 39; MACD below zero
    text = _prompt("decision", "Hold", READINGS, [])
    assert "Already true now" in text
    for already in ("sma_20 crosses_below", "close_vs_sma_20 weekly_close_below",
                    "sma_50 crosses_below", "close_vs_sma_50 weekly_close_below",
                    "sma_200 crosses_above", "close_vs_sma_200 weekly_close_above",
                    "macd_histogram crosses_below", "N above 39", "N below 39"):
        assert already in text
    assert "sma_200 crosses_below" not in text and "close_vs_sma_200 weekly_close_below" not in text


@pytest.mark.unit
def test_readings_that_are_unavailable_add_nothing_to_the_already_true_list():
    from tradingagents.plan.check import Readings
    from tradingagents.plan.generate import _prompt

    text = _prompt("decision", "Hold", Readings(price=10.0), [])
    assert "sma_" not in text.split("Already true now")[1].split("Final decision")[0]
