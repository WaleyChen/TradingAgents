"""Ask a model for a position plan, check it, and ask again with the problems.

The plan is derived from the final decision and the verified readings, not
from the debate. A plan that cannot pass the checks is not returned: the
outcome carries an error and no plan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from tradingagents.agents.context import get_language_instruction
from tradingagents.agents.structured import NO_EXTERNAL_TOOLS, bind_structured
from tradingagents.context import Events
from tradingagents.plan.check import Readings, check_plan
from tradingagents.plan.models import MAX_LEGS, MAX_PCT, Plan

logger = logging.getLogger(__name__)

DEFAULT_RETRIES = 2

# What a caller is told. Exception text stays in the log: a provider's error can
# echo a key or an endpoint, and the check failures below carry only numbers.
NOT_VALID = "the model did not return a valid plan"
FAILED = "plan generation failed"

_RULES = f"""\
Turn the decision into a position plan for a long-only model equity portfolio.

Write for retail traders in plain words: a percentage is a share of the reader's own
account, so say "your account", never "model portfolio" or "standard weight".

Percentages are the TOTAL position size, as a share of the portfolio, after the
trigger fires (not increments). Each is between 0 and {MAX_PCT:g}. `now` is the
size to hold today. Use at most {MAX_LEGS} entries and {MAX_LEGS} exits.

Rules the plan must satisfy:
1. Entries use price at_or_below and sit below the current price. Each entry
   holds more than `now`, and a deeper entry holds more than a shallower one.
2. A take_profit sits above the current price. A stop sits below the lowest
   entry. Every exit holds less than `now`; a full exit is 0.
3. Scenario probabilities sum to 1. Name one scenario "base", and keep its
   price between the stop and the take-profit price.
4. Each trigger must still be in the future: not already true at the readings.
5. The plan must agree with the rating: Sell means `now` is 0; Buy and
   Overweight mean `now` is above 0.
6. The portfolio is long-only, so a bearish decision is still a ladder of
   buy levels below the price, a small or zero `now`, and a stop below the
   lowest of those levels. Never put a stop above an entry. A price target
   below the current price becomes the base scenario or an entry level, not a stop.
7. `target` is the one price the plan stands behind. Its method is base_scenario
   (the price equals the base scenario) or valuation (its basis names the earnings
   or cash flow and the multiple behind it). A moving average or a support level is
   never a fair value: use technical_reference only for a level you are watching.
   A Buy or Overweight target is above the current price; Sell or Underweight, below.
8. Every scenario says what would have to happen (`driver`) and why that probability
   (`basis`): call a probability a judgment when it is one. Leave expected_return_pct
   null; the service computes it.
9. `if_you_own` and `if_you_dont` are one sentence each, so a trim is never mistaken for a short.
10. `watch` lists one to five measurable things that would change the view, each
    supporting or weakening it, with a date after today when it has one. A watch item
    about earnings may carry only the earnings date given below, or no date at all;
    never make up an earnings date.

Triggers come from this closed list; write nothing else:
- {{"price": {{"when": "at_or_below" | "at_or_above", "value": N}}}}
- {{"indicator": "rsi_14", "when": "below" | "above" | "crosses_above" | "crosses_below", "value": N}}
- {{"indicator": "sma_20" | "sma_50" | "sma_200", "when": "crosses_above" | "crosses_below"}}  (price against the average)
- {{"indicator": "close_vs_sma_20" | "close_vs_sma_50" | "close_vs_sma_200", "when": "weekly_close_below" | "weekly_close_above"}}
- {{"indicator": "macd_histogram", "when": "crosses_above" | "crosses_below"}}  (against zero)
- {{"all_of": [A, B]}} or {{"any_of": [A, B]}}, exactly two conditions, no nesting.
"""


@dataclass(frozen=True)
class PlanOutcome:
    """A plan that passed every check, or the reason there is none."""

    plan: Plan | None
    error: str | None
    attempts: int


def _readings_block(r: Readings) -> str:
    rows = [("date", r.date), ("price", r.price), ("rsi_14", r.rsi_14), ("sma_20", r.sma_20),
            ("sma_50", r.sma_50), ("sma_200", r.sma_200), ("macd_histogram", r.macd_histogram)]
    return "\n".join(f"- {name}: {'unavailable' if v is None else v}" for name, v in rows)


def _already_true(r: Readings) -> str:
    """The trigger forms the readings already satisfy: they would fire at once, so they fail the checks."""
    lines = []
    for days, average in (("20", r.sma_20), ("50", r.sma_50), ("200", r.sma_200)):
        if average is None:
            continue
        side, other = (("crosses_below", "weekly_close_below") if r.price < average
                       else ("crosses_above", "weekly_close_above"))
        if r.price != average:
            lines.append(f"- sma_{days} {side}; close_vs_sma_{days} {other}")
    if r.macd_histogram:
        lines.append(f"- macd_histogram {'crosses_below' if r.macd_histogram < 0 else 'crosses_above'}")
    if r.rsi_14 is not None:
        lines.append(f"- rsi_14 below or crosses_below any N above {r.rsi_14:g}; "
                     f"rsi_14 above or crosses_above any N below {r.rsi_14:g}")
    return "\n".join(lines) or "- none"


def _earnings_line(events: Events | None) -> str:
    nxt = events.next_earnings if events is not None else None
    if nxt is None:
        return "Earnings date: not supplied. Do not give a date to any watch item about earnings."
    state = "confirmed" if nxt.confirmed else "not confirmed"
    return (f"Earnings date: Next earnings are on {nxt.date} ({state}) for {nxt.fiscal_period}. "
            "A watch item about earnings carries exactly this date or none.")


def _prompt(decision: str, rating: str, readings: Readings, problems: list[str],
            events: Events | None = None) -> str:
    retry = ""
    if problems:
        listed = "\n".join(f"- {p}" for p in problems)
        retry = f"\nYour previous plan failed these checks; fix every one:\n{listed}\n"
    return (f"{_RULES}\nRating: {rating}\n\nVerified readings (the only source for prices and "
            f"indicator values; a trigger on an unavailable reading is not allowed):\n"
            f"{_readings_block(readings)}\n\nAlready true now (a trigger of this form would fire "
            f"at once, so never use it; pick the opposite form or a price level):\n"
            f"{_already_true(readings)}\n\n{_earnings_line(events)}\n\nFinal decision:\n{decision}\n{retry}\n"
            f"{NO_EXTERNAL_TOOLS}{get_language_instruction()}")


def _expected_return(plan: Plan, price: float) -> float | None:
    """Probability-weighted scenario price against the verified price, in percent; the model does not do this sum."""
    if abs(sum(s.probability for s in plan.scenarios) - 1) > 0.01:
        return None
    return round((sum(s.probability * s.price for s in plan.scenarios) / price - 1) * 100, 2)


def _anchor(plan: Plan, readings: Readings) -> Plan:
    """The price and date are facts the readings own, not the model's to restate."""
    update = {"as_of_price": readings.price, "expected_return_pct": _expected_return(plan, readings.price)}
    if readings.date:
        update["as_of_date"] = readings.date
    return Plan.model_validate({**plan.model_dump(mode="json"), **update})


def generate_plan(llm, *, decision: str, rating: str, readings: Readings,
                  events: Events | None = None, max_retries: int = DEFAULT_RETRIES) -> PlanOutcome:
    """Plan for ``decision``, retried with the failed checks, or an error and no plan."""
    structured = bind_structured(llm, Plan, "Plan")
    if structured is None:
        return PlanOutcome(None, "the model provider does not support structured output", 0)

    next_earnings = events.next_earnings.date if events is not None and events.next_earnings else None
    problems: list[str] = []          # what the model is told on the next try
    shown = NOT_VALID                 # what the caller is told if every try fails
    for attempt in range(1, max_retries + 2):
        try:
            plan = structured.invoke(_prompt(decision, rating, readings, problems, events))
            if plan is None:
                raise ValueError("the model returned no plan")
            plan = _anchor(plan, readings)
        except ValueError as exc:  # also pydantic and output-parser failures: the model's to fix
            logger.warning("Plan: attempt %d was not a valid plan (%s)", attempt, exc)
            problems, shown = [f"the reply was not a valid plan: {exc}"], NOT_VALID
            continue
        except Exception as exc:  # auth, quota, network: retrying will not help
            logger.warning("Plan: attempt %d failed (%s: %s)", attempt, type(exc).__name__, exc)
            return PlanOutcome(None, FAILED, attempt)
        problems = check_plan(plan, readings, rating, next_earnings)
        if not problems:
            return PlanOutcome(plan, None, attempt)
        shown = "; ".join(problems)
    return PlanOutcome(None, shown, max_retries + 1)
