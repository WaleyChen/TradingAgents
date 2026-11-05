"""The earnings release the caller sends: schema, checks, the verified notes read from it, and where they go."""

from __future__ import annotations

import copy
from datetime import date

import pytest
from pydantic import ValidationError

from tests.test_graph_end_to_end import ScriptedModel, _graph, offline  # noqa: F401
from tradingagents.context import Context, check_context, context_digest
from tradingagents.context.models import EarningsRelease
from tradingagents.context.render import render_context, render_role_blocks
from tradingagents.release_notes import (
    ReleaseItem,
    ReleaseNotes,
    extract_release_notes,
    verify_items,
)
from tradingagents.summary_check import Figure, SummaryDraft, check_summary, known_values

CHARGE = ("Third quarter 2026 GAAP net income attributable to Intuitive Surgical, Inc. included a pre-tax charge "
          "of $450 million in connection with the settlement of previously disclosed antitrust class action "
          "litigation. The third quarter 2026 expense was $347 million, net of tax, or $0.99 per diluted share.")
TEXT = ("Intuitive Announces Third Quarter Earnings. Third quarter revenue was $2.9 billion, up 22% from the "
        "third quarter of 2025. " + CHARGE + " Third quarter non-GAAP net income was $1.05 billion, or $3.00 "
        "per diluted share, compared with $867 million, or $2.40 per diluted share. Full-year da Vinci procedure "
        "growth is now expected to be 14% to 15%. " + "Additional detail follows in the tables below. " * 10)
RELEASE = {
    "ticker": "ISRG", "source": "sec_edgar", "form": "8-K", "filed_on": "2026-10-20", "period_end": "2026-09-30",
    "accession": "0001035267-26-000067",
    "url": "https://www.sec.gov/Archives/edgar/data/1035267/000103526726000067/q326ex-991earningsrelease.htm",
    "text": TEXT,
}


def release(**changes):
    return EarningsRelease.model_validate({**copy.deepcopy(RELEASE), **changes})


def codes(r, **kw):
    return [(p.code, p.path) for p in check_context(Context(earnings_release=r), **kw)]


# --- the block ---------------------------------------------------------------

@pytest.mark.unit
def test_a_release_is_accepted_and_period_end_is_optional():
    r = release(ticker=" isrg ")
    assert r.ticker == "ISRG" and r.filed_on == date(2026, 10, 20) and r.form == "8-K"
    data = copy.deepcopy(RELEASE)
    del data["period_end"]
    assert EarningsRelease.model_validate(data).period_end is None
    assert codes(release(), ticker="ISRG", trade_date=date(2026, 11, 4)) == []


@pytest.mark.unit
@pytest.mark.parametrize("bad", [
    {"surprise": 1}, {"source": "getmarketalerts"}, {"form": "10-K"}, {"accession": "1035267-26-67"},
    {"url": "http://www.sec.gov/Archives/x.htm"}, {"url": "https://evil.example/Archives/x.htm"},
    {"url": "https://www.sec.gov/Archives/" + "x" * 400}, {"text": "too short"}, {"text": "x" * 120_001},
    {"ticker": ""},
])
def test_the_release_schema_is_closed_and_bounded(bad):
    with pytest.raises(ValidationError):
        release(**bad)


@pytest.mark.unit
def test_a_six_k_from_a_foreign_filer_is_accepted():
    assert release(form="6-K").form == "6-K"


@pytest.mark.unit
def test_the_release_is_tied_to_the_company_and_the_run_date():
    found = check_context(Context(earnings_release=release(ticker="AMZN")), ticker="ISRG")
    assert [(p.code, p.path) for p in found] == [("ticker_mismatch", "earnings_release.ticker")]
    assert codes(release(), trade_date=date(2026, 10, 19)) == [("as_of_after_trade_date", "earnings_release.filed_on")]


@pytest.mark.unit
def test_a_release_cannot_be_filed_before_its_period_ended():
    assert codes(release(period_end="2026-10-25")) == [("filed_before_period_end", "earnings_release.period_end")]


@pytest.mark.unit
def test_a_release_more_than_130_days_old_means_a_newer_one_was_missed():
    assert codes(release(), trade_date=date(2027, 2, 27)) == []                           # 130 days exactly
    assert codes(release(), trade_date=date(2027, 2, 28)) == [("release_stale", "earnings_release.filed_on")]


@pytest.mark.unit
def test_text_that_is_still_html_is_refused():
    html = "<html><body>" + "<p><b>Revenue</b> rose.</p>" * 40 + "</body></html>" + "x" * 500
    assert codes(release(text=html)) == [("text_not_plain", "earnings_release.text")]
    assert codes(release(text=TEXT + " revenue < costs and margin > 30% are fine")) == []


@pytest.mark.unit
def test_the_text_is_part_of_the_digest_so_it_proves_what_a_report_saw():
    a = context_digest(Context(earnings_release=release()))
    assert a == context_digest(Context(earnings_release=release()))
    assert a != context_digest(Context(earnings_release=release(text=TEXT + " changed")))


# --- reading it: every item must be in the text word for word -------------------

QUOTE = "a pre-tax charge of $450 million in connection with the settlement of previously disclosed antitrust class action litigation"
GOOD = ReleaseItem(label="Antitrust settlement charge", kind="one_time_charge", amount_usd=450_000_000, quote=QUOTE,
                   explanation="A charge for settling antitrust class action litigation.")


class FakeLLM:
    """Binds structured output and answers with the next ReleaseNotes."""

    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def with_structured_output(self, schema):
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _notes(*items):
    return ReleaseNotes(items=list(items))


@pytest.mark.unit
def test_an_item_whose_quote_and_amount_are_in_the_text_is_kept():
    kept, dropped = verify_items(_notes(GOOD), TEXT)
    assert [i.label for i in kept] == ["Antitrust settlement charge"] and dropped == []


@pytest.mark.unit
def test_quotes_match_across_whitespace_and_case_but_not_across_invented_words():
    spaced = GOOD.model_copy(update={"quote": QUOTE.upper().replace(" ", "  ")})
    assert verify_items(_notes(spaced), TEXT)[0]
    invented = GOOD.model_copy(update={"quote": "a charge of $450 million to settle a competition lawsuit"})
    kept, dropped = verify_items(_notes(invented), TEXT)
    assert kept == [] and "not in the release" in dropped[0]


@pytest.mark.unit
def test_an_amount_the_quote_does_not_state_is_dropped():
    wrong = GOOD.model_copy(update={"amount_usd": 900_000_000})
    kept, dropped = verify_items(_notes(wrong), TEXT)
    assert kept == [] and "amount" in dropped[0]
    rounded = GOOD.model_copy(update={"amount_usd": 449_500_000})                      # within 1%
    assert verify_items(_notes(rounded), TEXT)[0]


@pytest.mark.unit
def test_an_item_with_no_amount_is_kept_when_its_quote_is_real():
    guidance = ReleaseItem(label="Procedure growth outlook", kind="guidance", amount_usd=None,
                           quote="Full-year da Vinci procedure growth is now expected to be 14% to 15%.",
                           explanation="The company expects 14% to 15% procedure growth.")
    assert verify_items(_notes(guidance), TEXT)[0]


@pytest.mark.unit
def test_extraction_keeps_the_verified_items_and_retries_once_for_the_rest():
    fake = GOOD.model_copy(update={"label": "Invented", "quote": "a gain on the sale of a division of $9 billion"})
    llm = FakeLLM(_notes(GOOD, fake), _notes(GOOD))
    out = extract_release_notes(llm, release())
    assert [i["label"] for i in out["items"]] == ["Antitrust settlement charge"] and len(llm.prompts) == 2
    assert "not in the release" in llm.prompts[1]
    assert out["filed_on"] == "2026-10-20" and out["accession"] == RELEASE["accession"]


@pytest.mark.unit
def test_items_that_never_verify_are_not_shown_and_a_failure_means_no_notes_not_an_error():
    fake = GOOD.model_copy(update={"quote": "something the company never wrote at all, honestly"})
    assert extract_release_notes(FakeLLM(_notes(fake), _notes(fake)), release())["items"] == []
    assert extract_release_notes(FakeLLM(RuntimeError("sk-SECRET")), release()) is None
    assert extract_release_notes(object(), release()) is None                   # no structured output at all


@pytest.mark.unit
def test_the_prompt_asks_for_word_for_word_quotes_and_carries_the_release():
    llm = FakeLLM(_notes(GOOD))
    extract_release_notes(llm, release())
    prompt = " ".join(llm.prompts[0].split())
    assert "word for word" in prompt and "$450 million" in prompt and "one-time" in prompt


@pytest.mark.unit
def test_the_schema_caps_items_and_their_text():
    with pytest.raises(ValidationError):
        ReleaseNotes(items=[GOOD] * 9)
    with pytest.raises(ValidationError):
        ReleaseItem.model_validate({**GOOD.model_dump(), "quote": "x" * 500})


# --- where the notes go --------------------------------------------------------

NOTES = {"filed_on": "2026-10-20", "accession": RELEASE["accession"], "url": RELEASE["url"], "form": "8-K",
         "items": [GOOD.model_dump(mode="json")]}


@pytest.mark.unit
def test_the_fundamentals_analyst_reads_the_items_with_the_companys_own_words():
    ctx = Context(earnings_release=release())
    block = render_role_blocks(ctx, NOTES)["fundamentals"]
    for expected in ("Latest earnings release (SEC 8-K filed 2026-10-20", "Antitrust settlement charge",
                     "one-time charge", "$450.0M", QUOTE):
        assert expected in block
    assert "Third quarter revenue was $2.9 billion" not in block        # the raw text itself is never passed on


@pytest.mark.unit
def test_the_deciders_get_a_one_line_digest_and_other_roles_get_nothing():
    blocks = render_role_blocks(Context(earnings_release=release()), NOTES)
    assert "Antitrust settlement charge" in blocks["decision"] and QUOTE not in blocks["decision"]
    for role in ("market", "news", "sentiment"):
        assert role not in blocks or "earnings release" not in blocks[role].lower()
    assert render_role_blocks(Context(earnings_release=release()), None) == {}


@pytest.mark.unit
def test_the_summary_reads_the_notes_and_may_cite_their_amounts_as_figures_from_the_filing():
    text = render_context(Context(earnings_release=release()), NOTES)
    assert "Antitrust settlement charge" in text and "Third quarter revenue was $2.9 billion" not in text
    known = known_values(Context(earnings_release=release()), None, None, NOTES)
    assert any(k.source == "filing" and k.kind == "usd" and k.value == 450_000_000 for k in known)
    ok = Figure(label="settlement charge", value=450, unit="usd_millions", source="filing")
    bad = Figure(label="settlement charge", value=900, unit="usd_millions", source="filing")
    assert check_summary(SummaryDraft(text="A $450 million charge.", figures=[ok]), known) == []
    assert [p.code for p in check_summary(SummaryDraft(text="A $900 million charge.", figures=[bad]), known)
            if p.severity == "error"] == ["figure_not_in_source"]


@pytest.mark.unit
def test_the_run_carries_the_notes_but_not_the_text(tmp_path, monkeypatch, offline):  # noqa: F811
    from tradingagents.graph import trading_graph

    monkeypatch.setattr(trading_graph, "extract_release_notes", lambda llm, rel: NOTES)
    data = Context(earnings_release=release()).model_dump(mode="json")
    graph = _graph(tmp_path, monkeypatch, ScriptedModel(), release_notes=True, context_data=data)
    state = graph.create_run_state("ISRG", "2026-11-04")
    assert state["release_notes"]["items"][0]["label"] == "Antitrust settlement charge"
    assert "Antitrust settlement charge" in state["verified_blocks"]["fundamentals"]
    assert state["run_context"]["earnings_release"] is None             # the 35 KB text stays out of the run state


@pytest.mark.unit
def test_without_the_flag_or_the_block_or_after_a_failure_the_run_goes_on_with_no_notes(tmp_path, monkeypatch, offline):  # noqa: F811
    from tradingagents.graph import trading_graph

    data = Context(earnings_release=release()).model_dump(mode="json")
    off = _graph(tmp_path, monkeypatch, ScriptedModel(), context_data=data)
    assert off.create_run_state("ISRG", "2026-11-04")["release_notes"] is None
    monkeypatch.setattr(trading_graph, "extract_release_notes", lambda llm, rel: (_ for _ in ()).throw(RuntimeError("x")))
    failing = _graph(tmp_path, monkeypatch, ScriptedModel(), release_notes=True, context_data=data)
    assert failing.create_run_state("ISRG", "2026-11-04")["release_notes"] is None
    no_block = _graph(tmp_path, monkeypatch, ScriptedModel(), release_notes=True,
                      context_data=Context().model_dump(mode="json"))
    assert no_block.create_run_state("ISRG", "2026-11-04")["release_notes"] is None


@pytest.mark.unit
def test_the_api_reads_releases_by_default_and_the_environment_can_turn_it_off(monkeypatch):
    from api.schemas import CreateAnalysisRequest
    from api.service import build_config

    request = CreateAnalysisRequest(ticker="ISRG", trade_date=date(2026, 11, 4))
    monkeypatch.delenv("TRADINGAGENTS_RELEASE_NOTES", raising=False)
    assert build_config(request)["release_notes"] is True
    monkeypatch.setenv("TRADINGAGENTS_RELEASE_NOTES", "0")
    assert build_config(request)["release_notes"] is False


@pytest.mark.unit
def test_the_request_and_the_dry_run_accept_the_block():
    from fastapi.testclient import TestClient

    from api.app import create_app
    from api.jobs import JobManager

    ctx = Context.model_validate({"earnings_release": RELEASE})
    assert ctx.earnings_release.accession == RELEASE["accession"]
    with TestClient(create_app(job_manager=JobManager(runner=lambda r: ("Hold", {})))) as client:
        ok = client.post("/api/v1/context/check?ticker=ISRG&trade_date=2026-11-04", json={"earnings_release": RELEASE}).json()
        assert ok["ok"] is True
        stale = client.post("/api/v1/context/check?ticker=ISRG&trade_date=2027-06-01", json={"earnings_release": RELEASE}).json()
        assert [p["code"] for p in stale["problems"]] == ["release_stale"]


@pytest.mark.unit
def test_quote_marks_inside_a_quote_do_not_break_the_one_around_it():
    item = {**NOTES["items"][0], "quote": 'following a mediation in the "In re: da Vinci" class action, it agreed'}
    block = render_role_blocks(Context(earnings_release=release()), {**NOTES, "items": [item]})["fundamentals"]
    line = next(line for line in block.splitlines() if line.startswith("- Antitrust"))
    assert line.count('"') == 2 and "'In re: da Vinci'" in line
