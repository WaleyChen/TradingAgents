"""Generation chatter does not belong in a report: the model talking to itself, or offering more."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from tradingagents.agents.analysts.turn import WRAP_UP, clean_report, take_turn

REPORT = "# AAPL fundamentals\n\nRevenue grew 8% and margins held."


@pytest.mark.unit
@pytest.mark.parametrize("lead", [
    "Perfect! Now I have comprehensive data. Let me write the report.",
    "Great, I now have all the data I need.",
    "Excellent! I have gathered everything.",
    "Okay, let me put the report together.",
    "Alright. Let me draft this now.",
    "I'll now write the final report.",
    "Here is the complete fundamental analysis report:",
])
def test_a_leading_line_of_chatter_is_removed(lead):
    assert clean_report(f"{lead}\n\n{REPORT}") == REPORT


@pytest.mark.unit
def test_several_chatter_paragraphs_in_a_row_are_removed():
    text = f"Perfect!\n\nNow I have the data. Let me build the report.\n\n{REPORT}"
    assert clean_report(text) == REPORT


@pytest.mark.unit
@pytest.mark.parametrize("trail", [
    "Let me know if you would like me to expand on any section.",
    "I hope this helps!", "Feel free to ask for more detail.", "Please let me know if you want a deeper dive.",
])
def test_a_closing_offer_is_removed(trail):
    assert clean_report(f"{REPORT}\n\n{trail}") == REPORT


@pytest.mark.unit
@pytest.mark.parametrize("text", [
    "Intuitive Surgical grew revenue 22% in the quarter.\n\nMargins fell.",
    "Now trading at $482, the stock sits near its average.\n\nMore below.",
    "Perfect execution of the plan lifted margins.\n\nMore below.",
    "Here is why the stock fell: a one-time charge.\n\nMore below.",
    "Let me be clear about the risks in this name: " + "word " * 45 + ".\n\nMore below.",   # too long to be chatter
    "",
])
def test_real_content_is_never_removed(text):
    assert clean_report(text) == text


@pytest.mark.unit
def test_a_report_that_is_only_chatter_is_left_alone_rather_than_emptied():
    assert clean_report("Perfect! Now I have the data.") == "Perfect! Now I have the data."


@pytest.mark.unit
def test_content_that_is_not_text_passes_through_unchanged():
    blocks = [{"type": "text", "text": "Perfect! Now I have it."}]
    assert clean_report(blocks) is blocks


class _Chain:
    def __init__(self, message):
        self.message = message

    def invoke(self, messages):
        return self.message


class _Prompt:
    def __or__(self, other):
        return other

    def partial(self, **kwargs):
        return self


class _LLM(_Chain):
    def bind_tools(self, tools):
        return self


@pytest.mark.unit
def test_a_finished_report_is_cleaned_but_the_message_is_not_rewritten():
    message = AIMessage(content=f"Perfect! Now I have comprehensive data.\n\n{REPORT}")
    result, report = take_turn(_Prompt(), _LLM(message), [], [HumanMessage(content="go")])
    assert report == REPORT and result is message


@pytest.mark.unit
def test_the_wrap_up_report_is_cleaned_too_and_a_tool_turn_still_has_no_report():
    final = AIMessage(content=f"Great, I have what I need.\n\n{REPORT}")
    _, report = take_turn(_Prompt(), _LLM(final), [], [HumanMessage(content=WRAP_UP)])
    assert report == REPORT
    calling = AIMessage(content="", tool_calls=[{"name": "get_x", "args": {}, "id": "1"}])
    assert take_turn(_Prompt(), _LLM(calling), [], [HumanMessage(content="go")])[1] == ""
