"""Plain-language ratings for retail readers: fixed labels, never model-written."""

from __future__ import annotations

from datetime import date

import pytest

from api.jobs import JobManager
from api.schemas import CreateAnalysisRequest
from tests.test_report_summary import STATE, StructuredLLM, _draft, _wait
from tradingagents.rating_label import plain_rating
from tradingagents.report_summary import create_summary_node


@pytest.mark.unit
@pytest.mark.parametrize("rating, label", [
    ("Buy", "Strong Buy"), ("Overweight", "Weak Buy"), ("Hold", "Hold"),
    ("Underweight", "Weak Sell"), ("Sell", "Strong Sell"),
])
def test_each_rating_has_a_plain_label_and_a_meaning_for_owners_and_non_owners(rating, label):
    plain = plain_rating(rating)
    assert plain.label == label and plain.meaning.endswith(".")


@pytest.mark.unit
def test_a_sell_side_meaning_says_what_to_do_whether_or_not_you_own_it():
    assert "If you own it" in plain_rating("Underweight").meaning
    assert "If you don't" in plain_rating("Underweight").meaning
    assert "don't" in plain_rating("Underweight").meaning.lower()


@pytest.mark.unit
@pytest.mark.parametrize("value", [None, "", "REVIEW", "Moonshot"])
def test_a_rating_that_is_not_one_of_the_five_has_no_label(value):
    assert plain_rating(value) is None


@pytest.mark.unit
def test_the_match_ignores_case_and_spacing():
    assert plain_rating("  underWEIGHT ").label == "Weak Sell"


@pytest.mark.unit
def test_the_api_response_carries_the_label_and_meaning_beside_the_unchanged_decision():
    request = CreateAnalysisRequest(ticker="ACME", trade_date=date(2026, 11, 2))
    manager = JobManager(runner=lambda r: ("Underweight", {}))
    got = _wait(manager, manager.submit(request).id).to_response()
    assert got.decision == "Underweight"
    assert got.rating_label == "Weak Sell" and "If you own it" in got.rating_meaning
    manager = JobManager(runner=lambda r: ("REVIEW", {}))
    got = _wait(manager, manager.submit(request).id).to_response()
    assert got.rating_label is None and got.rating_meaning is None


@pytest.mark.unit
def test_the_summary_is_written_for_retail_readers_in_the_plain_label():
    llm = StructuredLLM(_draft())
    create_summary_node(llm)({**STATE, "final_rating": "Underweight"})
    prompt = llm.prompts[0]
    assert "retail" in prompt
    assert "Weak Sell" in prompt and "never the internal term" in prompt
    for banned in ("standard weight", "model portfolio"):
        assert banned in prompt                      # named so the writer avoids them


@pytest.mark.unit
def test_a_summary_without_a_known_rating_still_runs():
    llm = StructuredLLM(_draft())
    assert create_summary_node(llm)({**STATE})["report_summary"] == "A short summary."


@pytest.mark.unit
def test_the_plan_is_written_in_plain_words_about_the_readers_own_account():
    from tests.test_plan_generate import FakeLLM, _plan, _run

    llm = FakeLLM(_plan())
    _run(llm)
    assert "retail" in llm.prompts[0] and "your account" in llm.prompts[0]
