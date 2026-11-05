"""The report summary: a short single-voice read of the whole report."""

from __future__ import annotations

import time
from datetime import date
from unittest.mock import MagicMock

import pytest

from api.jobs import JobManager
from api.schemas import CreateAnalysisRequest
from api.service import AnalysisResult, build_config
from tests.test_context_financials import GOOD as FINANCIALS
from tests.test_plan import GOOD as PLAN, READINGS
from tradingagents.agents.structured import NO_HANDLES, ONE_VOICE
from tradingagents.context import Context
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.setup import GraphSetup
from tradingagents.report_summary import MAX_WORDS, create_summary_node, word_count, write_summary
from tradingagents.reporting import write_report_tree
from tradingagents.summary_check import Figure, SummaryDraft, known_values

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
    """n words of readable text: ten-word sentences, three to a paragraph, so it raises no readability warning."""
    out = []
    for i in range(1, n + 1):
        end = "." if i % 10 == 0 else ""
        out.append("word" + end + ("\n\n" if i % 30 == 0 and i < n else ""))
    return " ".join(out).replace("\n\n ", "\n\n")


CONTEXT = Context.model_validate({"financials": FINANCIALS})
KNOWN = known_values(CONTEXT, READINGS, PLAN)


class StructuredLLM:
    """Binds structured output; answers each call with the next SummaryDraft."""

    def __init__(self, *drafts):
        self.drafts, self.prompts = list(drafts), []

    def with_structured_output(self, schema):
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        reply = self.drafts.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _draft(text="A short summary.", *figures):
    return SummaryDraft(text=text, figures=list(figures))


NET_MARGIN = Figure(label="net margin", value=63.8, unit="pct", source="context")


@pytest.mark.unit
def test_a_summary_within_the_limit_is_returned_as_written():
    llm = ScriptedLLM(_words(430))
    assert write_summary(llm, "material").text == _words(430)
    assert len(llm.prompts) == 1


@pytest.mark.unit
def test_a_summary_that_runs_long_is_asked_for_again_with_its_length():
    llm = ScriptedLLM(_words(MAX_WORDS + 50), _words(440))
    assert word_count(write_summary(llm, "material").text) == 440
    assert str(MAX_WORDS + 50) in llm.prompts[1]


@pytest.mark.unit
def test_a_summary_that_never_fits_is_dropped_not_cut_off():
    llm = ScriptedLLM(*[_words(MAX_WORDS + 50)] * 3)
    outcome = write_summary(llm, "material")
    assert outcome.text is None and [p["code"] for p in outcome.problems] == ["too_long"]


@pytest.mark.unit
def test_an_empty_reply_is_not_a_summary():
    assert write_summary(ScriptedLLM("  ", "  ", "  "), "material").text is None


@pytest.mark.unit
def test_a_failing_model_gives_no_summary_and_no_leaked_error_text():
    node = create_summary_node(ScriptedLLM(RuntimeError("sk-SECRET")))
    assert node(STATE) == {"report_summary": None, "summary_figures": None, "summary_problems": [],
                            "summary_glossary": None}


@pytest.mark.unit
def test_the_prompt_carries_the_whole_report_and_the_writing_rules():
    llm = ScriptedLLM(_words(500))
    out = create_summary_node(llm)({**STATE, "position_plan": {"bogus": 1}})
    prompt = llm.prompts[0]
    for text in ("Underweight. Trim into strength.", "MARKET-TEXT", "SENTIMENT-TEXT",
                 "NEWS-TEXT", "FUNDAMENTALS-TEXT", "RESEARCH-PLAN", "TRADER-PLAN",
                 ONE_VOICE, NO_HANDLES):
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


@pytest.mark.unit
def test_the_summary_takes_its_target_from_the_plan_only():
    llm = ScriptedLLM(_words(500))
    create_summary_node(llm)(STATE)
    prompt = llm.prompts[0]
    assert "the position plan's target" in prompt and "never call a moving average" in prompt


@pytest.mark.unit
def test_free_text_when_structured_output_is_unavailable_has_nothing_to_check():
    outcome = write_summary(ScriptedLLM(_words(500)), "material", known=KNOWN)
    assert outcome.text == _words(500) and outcome.figures is None and outcome.problems == []


@pytest.mark.unit
def test_a_summary_with_declared_and_matching_figures_comes_back_with_them_marked_verified():
    llm = StructuredLLM(_draft("Net margin 63.8%.", NET_MARGIN))
    outcome = write_summary(llm, "material", known=KNOWN)
    assert outcome.text == "Net margin 63.8%." and outcome.problems == []
    assert outcome.figures[0]["verified"] is True and outcome.figures[0]["label"] == "net margin"


@pytest.mark.unit
def test_an_undeclared_figure_is_a_warning_and_is_not_worth_a_retry():
    llm = StructuredLLM(_draft("Net margin 63.8% and a $500 target.", NET_MARGIN))
    outcome = write_summary(llm, "material", known=KNOWN)
    assert len(llm.prompts) == 1                                  # no second call for something harmless
    assert outcome.text == "Net margin 63.8% and a $500 target."
    assert [(p["code"], p["severity"]) for p in outcome.problems] == [("undeclared_figure", "warning")]


@pytest.mark.unit
def test_a_figure_that_is_not_in_its_source_is_sent_back_naming_it():
    wrong = Figure(label="net margin", value=70.0, unit="pct", source="context")
    llm = StructuredLLM(_draft("Net margin 70.0%.", wrong), _draft("Net margin 63.8%.", NET_MARGIN))
    assert write_summary(llm, "material", known=KNOWN).text == "Net margin 63.8%."
    assert "net margin: 70 (pct) is not in the context data" in llm.prompts[1]


@pytest.mark.unit
def test_figures_that_still_fail_after_the_retries_are_returned_with_the_problems():
    wrong = Figure(label="net margin", value=70.0, unit="pct", source="context")
    llm = StructuredLLM(*[_draft("Net margin 70.0%.", wrong)] * 3)
    outcome = write_summary(llm, "material", known=KNOWN)
    assert outcome.text == "Net margin 70.0%." and len(llm.prompts) == 3
    assert [p["code"] for p in outcome.problems] == ["figure_not_in_source"]
    assert outcome.figures[0]["verified"] is False


@pytest.mark.unit
def test_a_structured_summary_that_never_fits_the_length_is_dropped():
    llm = StructuredLLM(*[_draft(_words(MAX_WORDS + 50))] * 3)
    outcome = write_summary(llm, "material", known=KNOWN)
    assert outcome.text is None and outcome.problems[0]["code"] == "too_long"


@pytest.mark.unit
def test_a_structured_miss_falls_back_to_free_text():
    class Missing(StructuredLLM):
        def invoke(self, prompt):
            if prompt not in self.prompts:
                self.prompts.append(prompt)
                raise ValueError("no parse")
            return MagicMock(content="Plain summary.")

    outcome = write_summary(Missing(), "material", known=KNOWN)
    assert outcome.text == "Plain summary." and outcome.figures is None


@pytest.mark.unit
def test_the_prompt_asks_for_every_figure_with_its_source():
    llm = StructuredLLM(_draft())
    write_summary(llm, "material", known=KNOWN)
    prompt = llm.prompts[0]
    for expected in ("figures", "usd_billions", "context", "readings", "plan", "analyst_report", "derived"):
        assert expected in prompt


@pytest.mark.unit
def test_the_node_checks_against_the_context_the_readings_and_the_plan():
    from dataclasses import asdict

    state = {**STATE, "position_plan": PLAN, "verified_readings": asdict(READINGS)}
    ok = Figure(label="target", value=300, unit="usd", source="plan")
    node = create_summary_node(StructuredLLM(_draft("Net margin 63.8%, target $300.00.", NET_MARGIN, ok)), CONTEXT)
    out = node(state)
    assert out["report_summary"].startswith("Net margin") and out["summary_problems"] == []
    assert [f["verified"] for f in out["summary_figures"]] == [True, True]


@pytest.mark.unit
def test_the_node_shows_the_model_the_supplied_data_and_the_readings():
    from dataclasses import asdict

    llm = StructuredLLM(_draft())
    create_summary_node(llm, CONTEXT)({**STATE, "verified_readings": asdict(READINGS)})
    prompt = llm.prompts[0]
    assert "Verified data supplied by the caller" in prompt and "sec_xbrl" in prompt
    assert "Verified market readings" in prompt and "sma_200" in prompt


@pytest.mark.unit
def test_a_plan_or_readings_that_do_not_parse_cannot_crash_the_node():
    llm = StructuredLLM(_draft())
    out = create_summary_node(llm, CONTEXT)({**STATE, "position_plan": {"bogus": 1},
                                             "verified_readings": {"nonsense": 1}})
    assert out["report_summary"] == "A short summary." and out["summary_problems"] == []


@pytest.mark.unit
def test_the_api_response_carries_the_figures_and_the_problems():
    request = CreateAnalysisRequest(ticker="ACME", trade_date=date(2026, 11, 2))
    figures = [{"label": "net margin", "value": 63.8, "unit": "pct", "source": "context",
                "period": "FY2026", "verified": True}]
    problems = [{"code": "undeclared_figure", "subject": "$5", "message": "$5 is not in the figures list"}]
    manager = JobManager(runner=lambda r: AnalysisResult("Hold", {}, None, None, "S", figures, problems))
    got = _wait(manager, manager.submit(request).id).to_response()
    assert got.summary_figures[0].verified is True and got.summary_problems[0].code == "undeclared_figure"
    manager = JobManager(runner=lambda r: ("Hold", {}))
    got = _wait(manager, manager.submit(request).id).to_response()
    assert got.summary_figures is None and got.summary_problems is None


@pytest.mark.unit
def test_the_api_hands_the_graph_the_parsed_context_for_the_checks():
    request = CreateAnalysisRequest(ticker="MU", trade_date=date(2026, 11, 4),
                                    context={"financials": FINANCIALS})
    data = build_config(request)["context_data"]
    assert Context.model_validate(data) == request.context


@pytest.mark.unit
def test_the_plan_step_passes_its_verified_readings_on_to_the_summary(monkeypatch):
    from dataclasses import asdict

    from tests.test_plan_generate import FakeLLM, _plan
    from tradingagents.plan import node as plan_node_module
    from tradingagents.plan.node import create_plan_node

    monkeypatch.setattr(plan_node_module, "compute_readings", lambda symbol, as_of: READINGS)
    out = create_plan_node(FakeLLM(_plan()))({"company_of_interest": "ACME", "trade_date": "2026-11-02",
                                              "final_trade_decision": "Rating: Overweight", "final_rating": "Overweight"})
    assert out["verified_readings"] == asdict(READINGS)
    monkeypatch.setattr(plan_node_module, "compute_readings", lambda *a: (_ for _ in ()).throw(RuntimeError()))
    assert create_plan_node(FakeLLM(_plan()))({"company_of_interest": "ACME", "trade_date": "d"
                                               })["verified_readings"] is None


@pytest.mark.unit
def test_the_outcome_marks_which_problems_are_warnings():
    wrong = Figure(label="net margin", value=70.0, unit="pct", source="context")
    llm = StructuredLLM(*[_draft("Net margin 70.0% and $500.", wrong)] * 3)
    problems = write_summary(llm, "material", known=KNOWN).problems
    assert {(p["code"], p["severity"]) for p in problems} == {
        ("figure_not_in_source", "error"), ("undeclared_figure", "warning")}


@pytest.mark.unit
def test_a_summary_that_is_too_long_is_an_error():
    llm = StructuredLLM(*[_draft(_words(MAX_WORDS + 50))] * 3)
    assert write_summary(llm, "material", known=KNOWN).problems[0]["severity"] == "error"


@pytest.mark.unit
def test_the_summary_can_skip_the_full_analyst_reports_and_read_the_digested_decision():
    from tradingagents.report_summary import _material

    full = _material(STATE, None, None)
    lean = _material(STATE, None, None, include_reports=False)
    for report in ("MARKET-TEXT", "SENTIMENT-TEXT", "NEWS-TEXT", "FUNDAMENTALS-TEXT"):
        assert report in full and report not in lean
    for kept in ("Underweight. Trim into strength.", "RESEARCH-PLAN", "TRADER-PLAN"):
        assert kept in lean
    assert len(lean) < len(full)


@pytest.mark.unit
def test_the_node_follows_the_include_reports_switch():
    full_llm, lean_llm = StructuredLLM(_draft()), StructuredLLM(_draft())
    create_summary_node(full_llm)(STATE)
    create_summary_node(lean_llm, include_reports=False)(STATE)
    assert "MARKET-TEXT" in full_llm.prompts[0] and "MARKET-TEXT" not in lean_llm.prompts[0]


@pytest.mark.unit
def test_the_summary_step_can_run_on_the_quick_model(monkeypatch):
    from tradingagents.default_config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["summary_reads_reports"] is True and DEFAULT_CONFIG["summary_tier"] == "deep"


@pytest.mark.unit
def test_the_api_summary_reads_the_digests_unless_the_environment_says_otherwise(monkeypatch):
    request = CreateAnalysisRequest(ticker="ACME", trade_date=date(2026, 11, 2))
    monkeypatch.delenv("TRADINGAGENTS_SUMMARY_READS_REPORTS", raising=False)
    assert build_config(request)["summary_reads_reports"] is False
    monkeypatch.setenv("TRADINGAGENTS_SUMMARY_READS_REPORTS", "1")
    assert build_config(request)["summary_reads_reports"] is True


@pytest.mark.unit
def test_the_summary_switches_are_settable_from_the_environment(monkeypatch):
    from tradingagents.default_config import build_default_config

    monkeypatch.setenv("TRADINGAGENTS_SUMMARY_READS_REPORTS", "false")
    monkeypatch.setenv("TRADINGAGENTS_SUMMARY_TIER", "quick")
    config = build_default_config()
    assert config["summary_reads_reports"] is False and config["summary_tier"] == "quick"


@pytest.mark.unit
def test_the_summary_is_asked_what_changed_since_the_last_report_without_being_anchored_on_it():
    llm = StructuredLLM(_draft())
    create_summary_node(llm)(STATE)
    prompt = " ".join(llm.prompts[0].split())
    assert "how the view has changed" in prompt and "toward or away from the old target" in prompt
    assert "Do not let the previous rating decide the new one" in prompt
