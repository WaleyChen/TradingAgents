"""The report summary: a short, single-voice read of the whole report.

The full report keeps every analyst's work as an appendix. This step writes the
part a customer reads first, from the finished decision and reports. A summary
that does not fit the word limit after a retry is dropped rather than cut off;
the report stands without it.
"""

from __future__ import annotations

import logging

from tradingagents.agents.context import get_language_instruction
from tradingagents.agents.structured import (
    NO_EXTERNAL_TOOLS,
    NO_HANDLES,
    ONE_VOICE,
    SOURCED_NUMBERS,
)
from tradingagents.plan.render import render_plan

logger = logging.getLogger(__name__)

TARGET_WORDS = 600
MAX_WORDS = 700
DEFAULT_RETRIES = 2

_SECTIONS = (
    ("Final decision", "final_trade_decision"),
    ("Market report", "market_report"),
    ("Sentiment report", "sentiment_report"),
    ("News report", "news_report"),
    ("Fundamentals report", "fundamentals_report"),
    ("Investment plan", "investment_plan"),
    ("Trader proposal", "trader_investment_plan"),
)


def word_count(text: str) -> int:
    return len(text.split())


def _material(state: dict) -> str:
    parts = [f"## {title}\n{state[key]}" for title, key in _SECTIONS if state.get(key)]
    plan = state.get("position_plan")
    if plan:
        try:
            parts.append(f"## Position plan\n{render_plan(plan)}")
        except ValueError:  # an unrenderable plan must not lose the summary
            logger.warning("Summary: the position plan could not be rendered")
    return "\n\n".join(parts)


def _prompt(ticker: str, trade_date: str, material: str) -> str:
    return f"""Write the summary of a research report on {ticker} as of {trade_date}.

Length: about {TARGET_WORDS} words, never more than {MAX_WORDS}.

Cover, in this order: the rating and what to do now; the three or four points that
decided it, each with its figures; the main risks and what would change the view;
the entry and exit levels from the position plan, if there is one. Keep to what the
material below supports.

If there is a position plan, give the position plan's target and expected return and
no other price target, even if another section states one; never call a moving average
or a support level a target or a fair value.

{ONE_VOICE} {SOURCED_NUMBERS} {NO_HANDLES}
{NO_EXTERNAL_TOOLS}

{material}
{get_language_instruction()}"""


def write_summary(llm, material: str, *, ticker: str = "", trade_date: str = "",
                  max_retries: int = DEFAULT_RETRIES) -> str | None:
    """A summary within the word limit, or None when none is produced."""
    prompt = _prompt(ticker, trade_date, material)
    for _ in range(max_retries + 1):
        text = str(llm.invoke(prompt).content).strip()
        if text and word_count(text) <= MAX_WORDS:
            return text
        count = word_count(text)
        prompt = (f"{prompt}\n\nYour last attempt had {count} words. "
                  f"Write it again in at most {MAX_WORDS}, aiming for {TARGET_WORDS}.")
    return None


def create_summary_node(llm):
    def summary_node(state) -> dict:
        try:
            text = write_summary(
                llm, _material(state), ticker=state["company_of_interest"],
                trade_date=state["trade_date"])
        except Exception:  # the report is done; a failure here must not discard it
            logger.exception("Summary: unexpected failure for %s", state.get("company_of_interest"))
            text = None
        return {"report_summary": text}

    return summary_node
