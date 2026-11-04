"""A position plan as readable markdown for the saved report."""

from __future__ import annotations

from tradingagents.plan.models import Leaf, Plan, Trigger

_AVERAGES = {"20": "20-day", "50": "50-day", "200": "200-day"}
_WEEKLY = {"weekly_close_below": "weekly close below", "weekly_close_above": "weekly close above"}


def _line(text: str) -> str:
    """One line of text: a model-written field must not add structure to the report."""
    return " ".join(text.split())


def _leaf(leaf: Leaf) -> str:
    if leaf.price:
        return f"price {leaf.price.when.replace('_', ' ')} ${leaf.price.value:,.2f}"
    name, when = leaf.indicator, leaf.when
    if name == "rsi_14":
        return f"RSI(14) {when.replace('_', ' ')} {leaf.value:g}"
    if name == "macd_histogram":
        return f"MACD histogram {when.replace('_', ' ')} zero"
    days = name.rsplit("_", 1)[1]
    if name.startswith("close_vs_"):
        return f"{_WEEKLY[when]} the {_AVERAGES[days]} average"
    return f"price {when.replace('_', ' ')} the {_AVERAGES[days]} average"


def _trigger(trigger: Trigger) -> str:
    if trigger.all_of or trigger.any_of:
        word = "all of" if trigger.all_of else "any of"
        return f"{word}: " + "; ".join(_leaf(leaf) for leaf in trigger.leaves())
    return _leaf(trigger)


def render_plan(plan: Plan | dict) -> str:
    """Markdown for a plan, or for the plain dict the graph state carries."""
    p = plan if isinstance(plan, Plan) else Plan.model_validate(plan)
    lines = [
        f"As of {p.as_of_date} at ${p.as_of_price:,.2f}; horizon {_line(p.horizon)}. "
        f"Sizes are the total position as a share of the portfolio after the trigger fires.",
        "",
        f"**Now: {p.now.portfolio_pct:g}%.** {_line(p.now.summary)}",
        "",
    ]
    if p.entries:
        lines += ["**Entries**", *(f"- {e.portfolio_pct:g}% when {_trigger(e.trigger)}: {_line(e.reason)}"
                                   for e in p.entries), ""]
    if p.exits:
        lines += ["**Exits**", *(f"- {x.kind.replace('_', ' ')} to {x.portfolio_pct:g}% when "
                                  f"{_trigger(x.trigger)}: {_line(x.reason)}" for x in p.exits), ""]
    lines += ["**Scenarios**", *(f"- {_line(s.name)}: ${s.price:,.2f}, {s.probability:.0%}" for s in p.scenarios), "",
              f"**Confidence: {p.confidence}.** {_line(p.confidence_reason)}"]
    return "\n".join(lines)
