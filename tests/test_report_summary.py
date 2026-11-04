"""The report summary: a short single-voice read of the whole report."""

from __future__ import annotations

import time
from datetime import date
from unittest.mock import MagicMock

import pytest

from api.jobs import JobManager
from api.schemas import CreateAnalysisRequest
from api.service import AnalysisResult
from tradingagents.agents.structured import NO_HANDLES, ONE_VOICE, SOURCED_NUMBERS
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.setup import GraphSetup
from tradingagents.report_summary import MAX_WORDS, create_summary_node, word_count, write_summary
from tradingagents.reporting import write_report_tree

STATE = {
    "company_of_interest": "ACME", "trade_date": "2026-11-02",
    "final_trade_decision": "Rating: Underweight. Trim into strength.",
    "market_report": "MARKET-TEXT", "sentiment_report": "SENTIMENT-TEXT",
    "news_report": "NEWS-TEXT", "fundamentals_report": "FUNDAMENTALS-TEXT",
    "investment_plan": "RESEARCH-PLAN", "trader_investment_plan": "TRADER-PLAN",
    "position_plan": None,
}


class ScriptedLLM:
    """Answers each call with the next scripted reply and records the prompts."""

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return MagicMock(content=reply)


def _words(n):
    return " ".join(["word"] * n)


@pytest.mark.unit
def test_a_summary_within_the_limit_is_returned_as_written():
    llm = ScriptedLLM(_words(580))
    assert write_summary(llm, "material") == _words(580)
    assert len(llm.prompts) == 1


@pytest.mark.unit
def test_a_summary_that_runs_long_is_asked_for_again_with_its_length():
    llm = ScriptedLLM(_words(MAX_WORDS + 50), _words(590))
    assert word_count(write_summary(llm, "material")) == 590
    assert str(MAX_WORDS + 50) in llm.prompts[1]


@pytest.mark.unit
def test_a_summary_that_never_fits_is_dropped_not_cut_off():
    llm = ScriptedLLM(*[_words(MAX_WORDS + 50)] * 3)
    assert write_summary(llm, "material") is None


@pytest.mark.unit
def test_an_empty_reply_is_not_a_summary():
    assert write_summary(ScriptedLLM("  ", "  ", "  "), "material") is None


@pytest.mark.unit
def test_a_failing_model_gives_no_summary_and_no_leaked_error_text():
    node = create_summary_node(ScriptedLLM(RuntimeError("sk-SECRET")))
    assert node(STATE) == {"report_summary": None}


@pytest.mark.unit
def test_the_prompt_carries_the_whole_report_and_the_writing_rules():
    llm = ScriptedLLM(_words(500))
    out = create_summary_node(llm)({**STATE, "position_plan": {"bogus": 1}})
    prompt = llm.prompts[0]
    for text in ("Underweight. Trim into strength.", "MARKET-TEXT", "SENTIMENT-TEXT",
                 "NEWS-TEXT", "FUNDAMENTALS-TEXT", "RESEARCH-PLAN", "TRADER-PLAN",
                 ONE_VOICE, SOURCED_NUMBERS, NO_HANDLES):
        assert text in prompt
    assert out["report_summary"] == _words(500)


@pytest.mark.unit
@pytest.mark.parametrize("with_plan", [True, False])
def test_the_summary_step_runs_last(with_plan):
    setup = GraphSetup(MagicMock(), MagicMock(), ConditionalLogic(), 3)
    workflow = setup.setup_graph(
        plan_node=(lambda s: {}) if with_plan else None, summary_node=lambda s: {})
    previous = "Plan" if with_plan else "Portfolio Manager"
    assert (previous, "Summary") in workflow.edges
    assert ("Summary", "__end__") in workflow.edges


@pytest.mark.unit
def test_the_summary_step_is_off_unless_asked_for(monkeypatch):
    from api.service import build_config
    from tradingagents.default_config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["report_summary"] is False
    request = CreateAnalysisRequest(ticker="ACME", trade_date=date(2026, 11, 2))
    monkeypatch.delenv("TRADINGAGENTS_REPORT_SUMMARY", raising=False)
    assert build_config(request)["report_summary"] is True
    monkeypatch.setenv("TRADINGAGENTS_REPORT_SUMMARY", "0")
    assert build_config(request)["report_summary"] is False


def _wait(manager, job_id):
    for _ in range(100):
        job = manager.get(job_id)
        if job.status in ("completed", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


@pytest.mark.unit
def test_the_api_response_carries_the_summary():
    request = CreateAnalysisRequest(ticker="ACME", trade_date=date(2026, 11, 2))
    manager = JobManager(runner=lambda r: AnalysisResult("Hold", {}, None, None, "A short read."))
    assert _wait(manager, manager.submit(request).id).to_response().summary == "A short read."
    manager = JobManager(runner=lambda r: ("Hold", {}))
    assert _wait(manager, manager.submit(request).id).to_response().summary is None


@pytest.mark.unit
def test_the_saved_report_leads_with_the_summary_and_the_rest_is_an_appendix(tmp_path):
    state = {**STATE, "report_summary": "THE-SUMMARY"}
    text = write_report_tree(state, "ACME", tmp_path, html=False).read_text()
    assert text.index("THE-SUMMARY") < text.index("MARKET-TEXT")
    assert "## Appendix I. Analyst Team Reports" in text
    assert (tmp_path / "0_summary" / "summary.md").read_text() == "THE-SUMMARY"


@pytest.mark.unit
def test_a_report_without_a_summary_is_unchanged(tmp_path):
    text = write_report_tree(STATE, "ACME", tmp_path, html=False).read_text()
    assert "## I. Analyst Team Reports" in text and "Appendix" not in text
    assert not (tmp_path / "0_summary" / "summary.md").exists()


@pytest.mark.unit
def test_the_html_report_numbers_the_appendix_sections_too(tmp_path):
    state = {**STATE, "report_summary": "THE-SUMMARY"}
    write_report_tree(state, "ACME", tmp_path, html=True)
    assert "THE-SUMMARY" in (tmp_path / "complete_report.html").read_text()
