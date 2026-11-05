"""Summaries a retail trader can read: plain words, rounded numbers, sources off the page, a glossary."""

from __future__ import annotations

from datetime import date

import pytest

from api.jobs import JobManager
from api.schemas import CreateAnalysisRequest
from api.service import AnalysisResult
from tests.test_job_store import _wait
from tests.test_report_summary import MAX_WORDS, STATE, StructuredLLM, _draft, _words
from tradingagents.agents.structured import SOURCED_NUMBERS, UNEXPLAINED
from tradingagents.glossary import GLOSSARY, glossary_for
from tradingagents.readability import readability_problems
from tradingagents.report_summary import TARGET_WORDS, create_summary_node, write_summary
from tradingagents.summary_check import Figure, SummaryDraft, check_summary


def _prompt():
    llm = StructuredLLM(_draft())
    create_summary_node(llm)({**STATE, "final_rating": "Underweight"})
    return " ".join(llm.prompts[0].split())


@pytest.mark.unit
def test_the_summary_keeps_sources_out_of_the_sentences_because_the_figures_list_carries_them():
    prompt = _prompt()
    assert "Do not put sources, filing names or dates in brackets" in prompt
    assert "figures list" in prompt and SOURCED_NUMBERS not in prompt


@pytest.mark.unit
def test_the_summary_is_told_to_write_plainly_round_and_keep_paragraphs_short():
    prompt = _prompt()
    for expected in ("plain words", "Round the numbers in the text", "$133 billion", "64%",
                     "two or three sentences", "no more than 20 words", "bold"):
        assert expected in prompt


@pytest.mark.unit
def test_the_summary_may_use_the_terms_the_page_defines_and_must_explain_no_others():
    prompt = _prompt()
    assert "The page defines these terms for the reader" in prompt
    for term in ("RSI", "Free cash flow", "P/E"):
        assert term in prompt
    assert "any other jargon" in prompt


@pytest.mark.unit
def test_the_summary_is_shorter_for_a_retail_reader():
    assert TARGET_WORDS == 450 and MAX_WORDS == 550
    assert "about 450 words" in _prompt()


@pytest.mark.unit
def test_only_a_real_error_earns_a_retry_never_a_warning():
    wrong = Figure(label="net margin", value=70.0, unit="pct", source="context")
    from tests.test_report_summary import KNOWN

    errors = StructuredLLM(_draft("Net margin 70.0%.", wrong), _draft("Net margin 70.0%.", wrong))
    assert len(write_summary(errors, "m", known=KNOWN, max_retries=1).problems) == 1 and len(errors.prompts) == 2
    long_sentences = StructuredLLM(_draft(" ".join(["This is a sentence " + "word " * 40 + "."] * 3)))
    out = write_summary(long_sentences, "m", known=KNOWN)
    assert len(long_sentences.prompts) == 1 and any(p["code"] == "long_sentences" for p in out.problems)


# --- rounding ----------------------------------------------------------------

@pytest.mark.unit
def test_a_figure_rounded_for_the_page_still_matches_the_exact_one_it_was_declared_with():
    exact = Figure(label="revenue", value=133.188, unit="usd_billions", source="analyst_report")
    from tradingagents.summary_check import known_values

    for text in ("Revenue was $133 billion.", "Revenue was about $133.2 billion."):
        assert check_summary(SummaryDraft(text=text, figures=[exact]), known_values(None, None, None)) == []
    thirty = Figure(label="capex", value=30.7, unit="usd_billions", source="analyst_report")
    assert check_summary(SummaryDraft(text="It spent $31 billion.", figures=[thirty]), []) == []   # 1% off
    assert check_summary(SummaryDraft(text="It spent $35 billion.", figures=[thirty]), [])[0].code == "undeclared_figure"


# --- glossary ----------------------------------------------------------------

@pytest.mark.unit
def test_every_glossary_entry_has_a_short_plain_meaning():
    assert len(GLOSSARY) >= 25
    for entry in GLOSSARY:
        assert entry.term and entry.meaning and len(entry.meaning) <= 170 and entry.meaning.endswith(".")


@pytest.mark.unit
def test_the_terms_in_a_summary_are_found_in_order_without_repeats():
    text = ("The RSI is low and the price is below its 50-day average. Free cash flow was strong, "
            "but the P/E is high. RSI could recover; EV/EBITDA is 8x.")
    assert [g["term"] for g in glossary_for(text)] == ["RSI", "Moving average", "Free cash flow", "P/E", "EV/EBITDA"]
    assert all(g["meaning"] for g in glossary_for(text))


@pytest.mark.unit
def test_aliases_and_case_are_matched_and_word_boundaries_respected():
    found = [g["term"] for g in glossary_for("capital spending rose; the 200-day moving average held; Insider buying")]
    assert found == ["Capital spending", "Moving average"]            # "insider" alone is not a defined term
    assert glossary_for("a stratrix of nothing; atrophy; carts") == []   # ATR, RSI not found inside other words
    assert glossary_for("Nothing technical here.") == []


@pytest.mark.unit
def test_the_node_returns_the_glossary_for_the_text_it_wrote():
    llm = StructuredLLM(_draft("The RSI is 33 and free cash flow is strong."))
    out = create_summary_node(llm)(STATE)
    assert [g["term"] for g in out["summary_glossary"]] == ["RSI", "Free cash flow"]
    dropped = StructuredLLM(*[_draft(_words(MAX_WORDS + 50))] * 3)
    assert create_summary_node(dropped)(STATE)["summary_glossary"] is None


@pytest.mark.unit
def test_the_api_returns_the_glossary_and_keeps_it_across_a_restart(tmp_path):
    from api.store import open_store

    def runner(request):
        return AnalysisResult("Hold", {}, None, None, "S", None, [], [{"term": "RSI", "meaning": "A gauge."}])

    db = tmp_path / "jobs" / "jobs.db"
    manager = JobManager(runner=runner, store=open_store(db))
    job = manager.submit(CreateAnalysisRequest(ticker="ACME", trade_date=date(2026, 11, 2)))
    _wait(manager, job.id)
    assert manager.get(job.id).to_response().glossary[0].term == "RSI"
    again = JobManager(runner=runner, store=open_store(db)).get(job.id).to_response()
    assert again.glossary[0].meaning == "A gauge."


# --- readability warnings ----------------------------------------------------

@pytest.mark.unit
def test_long_sentences_and_long_paragraphs_are_flagged_as_warnings_with_what_to_fix():
    long_sentence = "This is a sentence " + "that goes on and on and on " * 6 + "."
    found = readability_problems(" ".join([long_sentence] * 4))
    assert {p.code for p in found} >= {"long_sentences", "long_paragraph"}
    assert all(p.severity == "warning" for p in found)
    block = " ".join(["Short one."] * 7)
    assert [p.code for p in readability_problems(block)] == ["long_paragraph"]


@pytest.mark.unit
def test_plain_short_text_has_no_readability_problems():
    text = "Micron looks expensive. The stock has fallen 16% this month.\n\nIf you own it, sell some. If not, wait."
    assert readability_problems(text) == []
    assert readability_problems("") == []


# --- declaring the figures ---------------------------------------------------

STATED = "Margins are 81%, 75% and 64%, spending is 23% of sales and revenue was $54 billion."


@pytest.mark.unit
def test_a_summary_that_declares_few_of_its_figures_has_not_done_the_job():
    found = check_summary(SummaryDraft(text=STATED, figures=[]), [])
    assert [p.code for p in found if p.severity == "error"] == ["figures_not_declared"]
    assert sum(p.code == "undeclared_figure" for p in found) == 5          # each still named, as a warning
    half = [Figure(label="a", value=81, unit="pct", source="analyst_report")]
    assert any(p.code == "figures_not_declared" for p in check_summary(SummaryDraft(text=STATED, figures=half), []))


@pytest.mark.unit
def test_one_or_two_stray_figures_stay_a_warning_and_a_full_list_is_clean():
    figures = [Figure(label=str(v), value=v, unit="pct", source="analyst_report") for v in (81, 75, 64, 23)]
    figures.append(Figure(label="revenue", value=54, unit="usd_billions", source="analyst_report"))
    assert check_summary(SummaryDraft(text=STATED, figures=figures), []) == []
    found = check_summary(SummaryDraft(text=STATED, figures=figures[:3] + figures[4:]), [])   # the 23% missing
    assert [(p.code, p.severity) for p in found] == [("undeclared_figure", "warning")]
    few = "Sales rose 12% and margins held at 40%."
    assert [p.severity for p in check_summary(SummaryDraft(text=few, figures=[]), [])] == ["warning", "warning"]


@pytest.mark.unit
def test_a_draft_with_no_declared_figures_is_sent_back_until_it_lists_them():
    figures = [Figure(label=str(v), value=v, unit="pct", source="analyst_report") for v in (81, 75, 64, 23)]
    figures.append(Figure(label="revenue", value=54, unit="usd_billions", source="analyst_report"))
    llm = StructuredLLM(_draft(STATED), _draft(STATED, *figures))
    out = write_summary(llm, "m", known=[])
    assert len(llm.prompts) == 2 and out.problems == [] and len(out.figures) == 5
    assert "you declared" in llm.prompts[1] and "list every one" in llm.prompts[1]


@pytest.mark.unit
def test_the_prompt_asks_for_each_rounded_number_to_be_listed_with_its_exact_value():
    prompt = _prompt()
    assert "list each one in `figures` with its exact value" in prompt
    assert "not in the sentences" not in prompt          # that wording made a model return no figures at all


@pytest.mark.unit
def test_the_summary_is_told_an_unexplained_move_is_a_question_not_a_conclusion():
    assert " ".join(UNEXPLAINED.split()) in _prompt()
