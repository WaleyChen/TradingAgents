"""A plan read as text, and its place in the saved report."""

from __future__ import annotations

import pytest

from tests.test_plan import GOOD
from tradingagents.plan.render import render_plan
from tradingagents.reporting import write_report_tree

STATE = {"company_of_interest": "ACME", "trade_date": "2026-11-02",
         "final_trade_decision": "Rating: Overweight", "final_rating": "Overweight"}


@pytest.mark.unit
def test_the_plan_reads_as_a_ladder_of_prices_and_sizes():
    text = render_plan(GOOD)
    assert "2.5%" in text and "3-6 months" in text
    assert "- 3% when price at or below $245.00" in text
    assert "RSI(14) below 30" in text and "all of" in text.lower()
    assert "weekly close below the 200-day average" in text
    assert "base" in text and "50%" in text


@pytest.mark.unit
def test_report_files_include_the_plan_or_say_why_there_is_none(tmp_path):
    path = write_report_tree({**STATE, "position_plan": GOOD}, "ACME", tmp_path)
    assert "Position Plan" in path.read_text()
    assert "price at or below $245.00" in (tmp_path / "6_plan" / "plan.md").read_text()

    write_report_tree({**STATE, "position_plan": None, "plan_error": "did not pass"}, "ACME", tmp_path)
    assert "did not pass" in (tmp_path / "6_plan" / "plan.md").read_text()


@pytest.mark.unit
def test_a_run_without_the_plan_step_has_no_plan_section(tmp_path):
    path = write_report_tree(STATE, "ACME", tmp_path)
    assert "Position Plan" not in path.read_text()
    assert not (tmp_path / "6_plan" / "plan.md").exists()


@pytest.mark.unit
def test_a_free_text_field_cannot_add_structure_to_the_report():
    import copy

    data = copy.deepcopy(GOOD)
    data["entries"][0]["reason"] = "dip\n\n# Injected heading\n- fake entry"
    text = render_plan(data)
    assert "\n# Injected" not in text and "\n- fake entry" not in text


@pytest.mark.unit
def test_the_decision_fields_are_shown_with_their_method_and_basis():
    import copy

    data = copy.deepcopy(GOOD)
    data["expected_return_pct"] = 15.3
    text = render_plan(data)
    assert "Target: $300.00" in text and "base scenario" in text and "Base-case value in 6 months." in text
    assert "Expected return: +15.3%" in text and "computed from the scenarios" in text
    assert "**If you own it:** Hold; add only on the entry levels below." in text
    assert "**If you don't:** Start small now and build on the entry levels." in text
    assert "- bull: $330.00, 25%: Capex pays back faster than feared (Judgment: needs two clean quarters.)" in text
    assert "- supports: Gross margin holds above 75% in the next filing (by 2026-12-10)" in text


@pytest.mark.unit
def test_a_negative_expected_return_and_an_undated_watch_item_read_correctly():
    import copy

    data = copy.deepcopy(GOOD)
    data["expected_return_pct"] = -4.2
    data["watch"][0]["when"] = None
    data["watch"][0]["effect"] = "weakens"
    text = render_plan(data)
    assert "Expected return: -4.2%" in text
    assert "- weakens: Gross margin holds above 75% in the next filing" in text and "(by" not in text


@pytest.mark.unit
def test_the_new_free_text_fields_cannot_add_structure_either():
    import copy

    data = copy.deepcopy(GOOD)
    data["if_you_own"] = "hold\n\n# Injected"
    data["watch"][0]["what"] = "x\n- fake"
    data["scenarios"][0]["driver"] = "y\n## Injected two"
    data["target"]["basis"] = "z\n- fake target"
    text = render_plan(data)
    for injected in ("\n# Injected", "\n- fake", "\n## Injected two"):
        assert injected not in text
