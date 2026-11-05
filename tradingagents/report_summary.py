"""The report summary: a short, single-voice read of the whole report.

The full report keeps every analyst's work as an appendix. This step writes the
part a customer reads first, from the finished decision and reports. The model
also declares every dollar, percent and multiple it states, with a source, and
the figures that claim verified data (the caller's context, the market readings,
the position plan) are checked against it in code (``summary_check``).

A summary that does not fit the word limit after the retries is dropped rather
than cut off. One whose figures still fail the checks is returned with the
problems listed, so the caller decides whether to show it; the report stands
either way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from tradingagents.agents.context import get_language_instruction
from tradingagents.agents.structured import (
    NO_EXTERNAL_TOOLS,
    NO_HANDLES,
    ONE_VOICE,
    SOURCED_NUMBERS,
    bind_structured,
    invoke_structured,
)
from tradingagents.context import Context
from tradingagents.context.render import render_context
from tradingagents.plan.check import Readings
from tradingagents.plan.generate import _readings_block
from tradingagents.plan.render import render_plan
from tradingagents.rating_label import plain_rating
from tradingagents.summary_check import SummaryDraft, annotate, check_summary, known_values

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

# What the decision-makers already digested from the analysts' full reports.
_DECISION_SECTIONS = tuple(s for s in _SECTIONS if s[1] in
                           ("final_trade_decision", "investment_plan", "trader_investment_plan"))

_FIGURES = """\
Also fill `figures`: one entry for every dollar amount, percentage and multiple (like 9.1x)
that you state in the text, with its label, value, unit (usd, usd_millions, usd_billions,
pct, x or number), source and period. The source is `context` for the supplied-data block,
`readings` for the verified market readings, `plan` for the position plan, `analyst_report`
for a figure taken from the reports below, or `derived` for your own calculation. Figures
sourced to context, readings or plan are checked against that data, so use its exact
values. Do not write a dollar amount, percentage or multiple that is not in `figures`."""


@dataclass(frozen=True)
class SummaryOutcome:
    text: str | None
    figures: list[dict[str, Any]] | None      # None: nothing was declared, so nothing was checked
    problems: list[dict[str, str]]


def word_count(text: str) -> int:
    return len(text.split())


def _material(state: dict, context: Context | None, readings: Readings | None,
              include_reports: bool = True) -> str:
    plain = plain_rating(state.get("final_rating"))
    sections = _SECTIONS if include_reports else _DECISION_SECTIONS
    parts = ([f"## Rating for the reader\n{plain.label}: {plain.meaning} The internal rating is "
              f"{state['final_rating']}; use the label, never the internal term."] if plain else []) + [f"## {title}\n{state[key]}" for title, key in sections if state.get(key)]
    if context is not None:
        parts.append(f"## Supplied verified data\n{render_context(context)}")
    if readings is not None:
        parts.append(f"## Verified market readings\n{_readings_block(readings)}")
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

Your readers are retail traders, not professionals. Lead with the rating label given below,
in plain words. Say what to do for someone who owns the stock and for someone who doesn't.
Write "your position" and "your account"; do not write "standard weight", "model portfolio",
"overweight" or "underweight". Explain any indicator you mention in a few words.

If there is a position plan, give the position plan's target and expected return and
no other price target, even if another section states one; never call a moving average
or a support level a target or a fair value.

{ONE_VOICE} {SOURCED_NUMBERS} {NO_HANDLES}
{NO_EXTERNAL_TOOLS}

{_FIGURES}

{material}
{get_language_instruction()}"""


def _problems(draft: SummaryDraft, known, *, figures: bool = True) -> list[dict[str, str]]:
    """Figure problems (when the model declared figures to check) and the length limit."""
    found = ([{"code": p.code, "subject": p.subject, "message": p.message, "severity": p.severity}
              for p in check_summary(draft, known)] if figures else [])
    count = word_count(draft.text)
    if count > MAX_WORDS:
        found.insert(0, {"code": "too_long", "subject": "summary", "severity": "error",
                         "message": f"the summary has {count} words; write it again in at most {MAX_WORDS}, "
                                    f"aiming for {TARGET_WORDS}"})
    return found


def _free_text(llm, prompt: str) -> SummaryDraft | None:
    text = str(llm.invoke(prompt).content).strip()
    return SummaryDraft(text=text) if text else None


def write_summary(llm, material: str, *, ticker: str = "", trade_date: str = "", known=(),
                  max_retries: int = DEFAULT_RETRIES) -> SummaryOutcome:
    """A summary within the word limit and, when it declares figures, checked; else no text."""
    structured = bind_structured(llm, SummaryDraft, "Summary")
    prompt = _prompt(ticker, trade_date, material)
    draft, problems, checked = None, [], False
    for _ in range(max_retries + 1):
        draft = invoke_structured(structured, prompt, "Summary") if structured is not None else None
        checked = draft is not None
        if draft is None:
            draft = _free_text(llm, prompt)
        if draft is None:
            problems = [{"code": "empty", "subject": "summary", "severity": "error",
                         "message": "the reply was empty"}]
        else:
            problems = _problems(draft, known, figures=checked)
            if not problems:
                break
        prompt = (f"{prompt}\n\nYour last attempt failed these checks; fix every one:\n"
                  + "\n".join(f"- {p['message']}" for p in problems))
    if draft is None or any(p["code"] == "too_long" for p in problems):
        return SummaryOutcome(None, None, problems)
    return SummaryOutcome(draft.text, annotate(draft.figures, known) if checked else None, problems)


def _readings(state: dict) -> Readings | None:
    try:
        return Readings(**state["verified_readings"]) if state.get("verified_readings") else None
    except (TypeError, ValueError):
        return None


def _known(context: Context | None, readings: Readings | None, plan: Any):
    try:
        return known_values(context, readings, plan if isinstance(plan, dict) else None)
    except (KeyError, TypeError, ValueError):  # a plan that does not parse adds nothing to check against
        return known_values(context, readings, None)


def create_summary_node(llm, context: Context | None = None, include_reports: bool = True):
    def summary_node(state) -> dict:
        try:
            readings = _readings(state)
            outcome = write_summary(
                llm, _material(state, context, readings, include_reports), ticker=state["company_of_interest"],
                trade_date=state["trade_date"], known=_known(context, readings, state.get("position_plan")))
        except Exception:  # the report is done; a failure here must not discard it
            logger.exception("Summary: unexpected failure for %s", state.get("company_of_interest"))
            return {"report_summary": None, "summary_figures": None, "summary_problems": []}
        if outcome.problems:
            logger.warning("Summary for %s has problems: %s", state.get("company_of_interest"),
                           [(p["code"], p["subject"]) for p in outcome.problems])
        return {"report_summary": outcome.text, "summary_figures": outcome.figures,
                "summary_problems": outcome.problems}

    return summary_node
