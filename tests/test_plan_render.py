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
