"""The summary's figures: declared, sourced and checked against verified data."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from tests.test_context_financials import GOOD as FINANCIALS
from tests.test_plan import GOOD as PLAN, READINGS
from tradingagents.context import Context
from tradingagents.summary_check import (
    Figure,
    SummaryDraft,
    annotate,
    check_summary,
    figures_in_text,
    known_values,
)

CONTEXT = Context.model_validate({
    "financials": FINANCIALS,
    "price": {"ticker": "MU", "currency": "USD", "source": "getmarketalerts",
              "as_of": "2026-11-04", "close": 795.23},
})
KNOWN = known_values(CONTEXT, READINGS, PLAN)


def fig(value, unit="usd", source="context", label="x", period=None):
    return Figure(label=label, value=value, unit=unit, source=source, period=period)


def draft(text, *figures):
    return SummaryDraft(text=text, figures=list(figures))


def codes(d):
    return [p.code for p in check_summary(d, KNOWN)]


@pytest.mark.unit
def test_the_text_is_read_for_dollar_percent_and_multiple_figures():
    text = ("Revenue was $133.2B and net income $85 million; net margin 63.8%, a drop of -13%; "
            "P/E 9.1x; the close was $1,062.50 on 2026-11-02 in FY2026 (Q3 2026), above the 200-day average.")
    found = {(kind, round(value, 4)) for kind, value, _ in figures_in_text(text)}
    assert found == {("usd", 133.2e9), ("usd", 85e6), ("pct", 63.8), ("pct", -13.0), ("x", 9.1),
                     ("usd", 1062.5)}


@pytest.mark.unit
def test_a_summary_whose_figures_are_all_declared_and_in_the_data_passes():
    d = draft("Revenue $133.188B, net margin 63.8%, price $795.23, target $300.00, 25% bull odds, 9.1x.",
              fig(133.188, "usd_billions", "context"), fig(63.8, "pct", "context"),
              fig(795.23, "usd", "context"), fig(300.0, "usd", "plan"),
              fig(25, "pct", "plan"), fig(9.1, "x", "analyst_report"))
    assert codes(d) == []


@pytest.mark.unit
def test_a_figure_in_the_text_that_was_not_declared_is_flagged():
    found = check_summary(draft("Net margin 63.8% and a $500 target.", fig(63.8, "pct", "context")), KNOWN)
    assert [(p.code, p.subject) for p in found] == [("undeclared_figure", "$500")]


@pytest.mark.unit
def test_a_figure_claimed_from_verified_data_must_be_in_it():
    d = draft("Net margin 70.0%.", fig(70.0, "pct", "context", label="net margin"))
    found = check_summary(d, KNOWN)
    assert [p.code for p in found] == ["figure_not_in_source"] and "net margin" in found[0].message
    assert codes(draft("RSI is 33.", fig(33, "number", "readings", label="rsi"))) == ["figure_not_in_source"]
    assert codes(draft("Entry at $240.", fig(240, "usd", "plan"))) == ["figure_not_in_source"]


@pytest.mark.unit
def test_figures_that_claim_no_verified_source_are_not_checked_and_are_not_marked_verified():
    d = draft("The company raised $634M.", fig(634, "usd_millions", "analyst_report", label="raise"))
    assert codes(d) == []
    assert annotate(d.figures, KNOWN)[0]["verified"] is False


@pytest.mark.unit
def test_matched_figures_are_marked_verified():
    figures = [fig(133.188, "usd_billions", "context"), fig(300, "usd", "plan"),
               fig(795.23, "usd", "context"), fig(0.5, "pct", "context")]
    marks = [a["verified"] for a in annotate(figures, KNOWN)]
    assert marks == [True, True, True, False]            # 0.5% is nowhere in the data


@pytest.mark.unit
def test_units_must_agree_not_just_numbers():
    assert codes(draft("Margin 300%.", fig(300, "pct", "plan"))) == ["figure_not_in_source"]


@pytest.mark.unit
def test_a_declared_figure_the_text_never_uses_is_harmless():
    assert codes(draft("No numbers here.", fig(63.8, "pct", "context"))) == []


@pytest.mark.unit
def test_rounding_is_tolerated_but_not_a_different_number():
    assert codes(draft("Net margin 63.8%.", fig(63.8, "pct", "context"))) == []
    assert codes(draft("Price about $795.", fig(795, "usd", "context"))) == []
    assert codes(draft("Price about $760.", fig(760, "usd", "context"))) == ["figure_not_in_source"]


@pytest.mark.unit
def test_the_known_values_cover_the_context_the_readings_and_the_plan():
    labels = {k.label for k in KNOWN}
    assert {"price close", "revenue", "net margin", "plan target", "plan scenario probability",
            "plan trigger price", "reading rsi_14", "reading sma_200"} <= labels
    assert any(k.kind == "pct" and k.value == 25.0 for k in KNOWN)          # 0.25 probability
    assert any(k.kind == "usd" and k.value == 245.0 for k in KNOWN)         # entry trigger price


@pytest.mark.unit
def test_with_no_context_or_plan_only_what_exists_is_known():
    known = known_values(None, READINGS, None)
    assert {k.label for k in known} == {"reading price", "reading rsi_14", "reading sma_20",
                                        "reading sma_50", "reading sma_200", "reading macd_histogram"}
    assert known_values(None, None, None) == []


@pytest.mark.unit
def test_the_draft_schema_is_closed_and_bounded():
    for bad in ({"text": "", "figures": []}, {"text": "x", "figures": [], "extra": 1},
                {"text": "x", "figures": [{"label": "a", "value": 1, "unit": "bananas", "source": "plan"}]},
                {"text": "x", "figures": [{"label": "a", "value": 1, "unit": "usd", "source": "gut"}]},
                {"text": "x", "figures": [{"label": "a", "value": float("nan"), "unit": "usd", "source": "plan"}]}):
        with pytest.raises(ValidationError):
            SummaryDraft.model_validate(copy.deepcopy(bad))


@pytest.mark.unit
def test_an_undeclared_figure_is_a_warning_and_a_false_claim_of_verification_is_an_error():
    stray = check_summary(draft("A $500 target.", fig(63.8, "pct", "context")), KNOWN)
    assert [(p.code, p.severity) for p in stray if p.code == "undeclared_figure"] == [("undeclared_figure", "warning")]
    false = check_summary(draft("Net margin 70.0%.", fig(70.0, "pct", "context")), KNOWN)
    assert [(p.code, p.severity) for p in false] == [("figure_not_in_source", "error")]


@pytest.mark.unit
def test_scale_words_are_read_in_any_case():
    for text in ("a $450 Million charge", "a $450 MILLION charge", "a $450 million charge", "a $0.45 billion charge",
                 "a $450M charge"):
        found = [v for kind, v, _ in figures_in_text(text) if kind == "usd"]
        assert found and abs(found[0] - 450e6) < 1e6, text


@pytest.mark.unit
def test_a_distance_declared_without_its_sign_still_matches_the_signed_one():
    from tests.test_context_technicals import TECH
    from tradingagents.context import Context

    known = known_values(Context.model_validate({"technicals": TECH}), None, None)
    below = [Figure(label="vs 50-day", value=v, unit="pct", source="context") for v in (18.2, -18.2)]
    for figure in below:
        assert check_summary(SummaryDraft(text="18.2% below.", figures=[figure]), known) == []
    wrong = Figure(label="vs 50-day", value=25.0, unit="pct", source="context")
    assert [p.code for p in check_summary(SummaryDraft(text="25% below.", figures=[wrong]), known)
            if p.severity == "error"] == ["figure_not_in_source"]


@pytest.mark.unit
def test_supplied_technicals_can_be_cited_as_readings_or_as_context():
    from tests.test_context_technicals import TECH
    from tradingagents.context import Context

    known = known_values(Context.model_validate({"technicals": TECH}), None, None)
    for source in ("context", "readings"):
        close = Figure(label="close", value=795.23, unit="usd", source=source)
        distance = Figure(label="vs 20-day", value=13.1, unit="pct", source=source)
        assert check_summary(SummaryDraft(text="$795.23, 13.1% below.", figures=[close, distance]), known) == []
