"""Checks a plan must pass before it is returned.

``check_plan`` compares the plan with verified readings and with itself, and
lists every contradiction. An empty list means the plan may be shown.

Scope and meaning, so a plan's reader and its checker agree:

* Long-only. Percentages are the total position size, as a share of an equity
  portfolio, that a model portfolio should hold after the trigger fires;
  ``now`` is the recommended size today, not a customer's holding.
* A trigger must be about the future. One already true at the verified
  readings would fire at once, so it is an error. A crossing is judged by
  which side of the level the reading sits on today, and a weekly close by the
  latest price against the average; neither uses bar history.
* A trigger that needs a reading which is unavailable cannot be checked, so
  it is an error rather than a pass.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields

from tradingagents.plan.models import Leaf, Plan, Trigger

PRICE_TOLERANCE = 0.005
PROBABILITY_TOLERANCE = 0.01


@dataclass(frozen=True)
class Readings:
    """Verified market readings as of the plan date."""

    price: float
    rsi_14: float | None = None
    sma_20: float | None = None
    sma_50: float | None = None
    sma_200: float | None = None
    macd_histogram: float | None = None
    date: str | None = None

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name != "date" and value is not None and not math.isfinite(value):
                raise ValueError(f"{f.name} must be a finite number, got {value}")
        if self.price <= 0:
            raise ValueError(f"price must be positive, got {self.price}")


def _level(leaf: Leaf, r: Readings) -> tuple[float | None, float | None]:
    """The (reading, level) an indicator test compares, or Nones."""
    name = leaf.indicator
    if name == "rsi_14":
        return r.rsi_14, leaf.value
    if name == "macd_histogram":
        return r.macd_histogram, 0.0
    if name.startswith("close_vs_"):
        return r.price, getattr(r, name.removeprefix("close_vs_"))
    return r.price, getattr(r, name)


def _is_true_now(leaf: Leaf, r: Readings) -> bool | None:
    if leaf.price is not None:
        at_most = leaf.price.when == "at_or_below"
        return r.price <= leaf.price.value if at_most else r.price >= leaf.price.value
    reading, level = _level(leaf, r)
    if reading is None or level is None:
        return None
    return reading < level if leaf.when.endswith("below") else reading > level


def _trigger_true_now(trigger: Trigger, r: Readings) -> bool | None:
    states = [_is_true_now(leaf, r) for leaf in trigger.leaves()]
    combine = any if trigger.any_of is not None else all
    if combine is any and True in states:
        return True
    if combine is all and False in states:
        return False
    return None if None in states else combine(states)


def _prices(trigger: Trigger) -> list[tuple[str, float]]:
    return [(leaf.price.when, leaf.price.value) for leaf in trigger.leaves() if leaf.price]


def _entry_price(trigger: Trigger) -> float | None:
    """The price at which an entry first fires, when only a price can fire it."""
    prices = [leaf.price.value if leaf.price else None for leaf in trigger.leaves()]
    if trigger.any_of is not None:
        return None if None in prices else max(prices)   # an indicator can fire it at any price
    known = [p for p in prices if p is not None]
    return min(known) if known else None


def _check_price(plan: Plan, readings: Readings) -> list[str]:
    gap = abs(plan.as_of_price - readings.price) / readings.price
    if gap <= PRICE_TOLERANCE:
        return []
    return [f"as_of_price {plan.as_of_price} differs from the verified price {readings.price}"]


def _check_entries(plan: Plan) -> list[str]:
    out: list[str] = []
    ref, now = plan.as_of_price, plan.now.portfolio_pct
    for i, e in enumerate(plan.entries, 1):
        for when, value in _prices(e.trigger):
            if when != "at_or_below":
                out.append(f"entry {i}: entries must use at_or_below, not {when}")
            elif value >= ref:
                out.append(f"entry {i}: price {value} must sit below as_of_price {ref}")
        if e.portfolio_pct <= now:
            out.append(f"entry {i}: {e.portfolio_pct}% must exceed the current {now}%")
    ladder = sorted(((p, i, e) for i, e in enumerate(plan.entries, 1)
                     if (p := _entry_price(e.trigger)) is not None), key=lambda t: -t[0])
    for (_, _, shallow), (_, i, deeper) in zip(ladder, ladder[1:], strict=False):
        if deeper.portfolio_pct <= shallow.portfolio_pct:
            out.append(f"entry {i}: a deeper entry must hold more than a shallower one")
    return out


def _exit_price(trigger: Trigger) -> float | None:
    prices = _prices(trigger)
    return prices[0][1] if len(prices) == 1 and len(trigger.leaves()) == 1 else None


def _check_exits(plan: Plan) -> list[str]:
    out: list[str] = []
    ref, now = plan.as_of_price, plan.now.portfolio_pct
    entry_prices = [v for e in plan.entries for _, v in _prices(e.trigger)]
    floor = min(entry_prices, default=ref)
    # A stop sits below the entries, so it fires after they fill; a take-profit
    # sits above the price, so it reduces what is held now (or, from flat, what
    # the entries built).
    peak = max([now, *(e.portfolio_pct for e in plan.entries)])
    held = {"stop": peak, "take_profit": now if now > 0 else peak}
    for i, x in enumerate(plan.exits, 1):
        label = f"{x.kind} {i}"
        for when, value in _prices(x.trigger):
            if x.kind == "take_profit" and (when != "at_or_above" or value <= ref):
                out.append(f"{label}: a take_profit price must be at_or_above and above as_of_price {ref}")
            if x.kind == "stop" and (when != "at_or_below" or value >= floor):
                out.append(f"{label}: a stop price must be at_or_below and below the lowest entry {floor}")
        if x.portfolio_pct >= held[x.kind]:
            out.append(f"{label}: {x.portfolio_pct}% must reduce the {held[x.kind]}% held when it fires")
    return out + _check_exit_steps(plan)


def _check_exit_steps(plan: Plan) -> list[str]:
    """Each exit reached after another must leave no more than it did."""
    out: list[str] = []
    placed = [(i, x, _exit_price(x.trigger)) for i, x in enumerate(plan.exits, 1)]
    profits = sorted(((p, i, x) for i, x, p in placed if x.kind == "take_profit" and p is not None))
    stops = sorted(((p, i, x) for i, x, p in placed if x.kind == "stop" and p is not None), reverse=True)
    for (_, _, first), (_, i, later) in zip(profits, profits[1:], strict=False):
        if later.portfolio_pct > first.portfolio_pct:
            out.append(f"take_profit {i}: a higher take_profit must not hold more than a lower one")
    for (_, _, first), (_, i, later) in zip(stops, stops[1:], strict=False):
        if later.portfolio_pct > first.portfolio_pct:
            out.append(f"stop {i}: a lower stop must not hold more than a higher one")
    return out


def _check_scenarios(plan: Plan) -> list[str]:
    out: list[str] = []
    total = sum(s.probability for s in plan.scenarios)
    if abs(total - 1) > PROBABILITY_TOLERANCE:
        out.append(f"scenario probabilities sum to {total:.2f}, not 1")
    bases = [s for s in plan.scenarios if s.name.strip().lower() == "base"]
    if len(bases) != 1:
        found = f"{len(bases)} base scenarios; it must appear once" if bases else "no base scenario"
        return [*out, found]
    base = bases[0]
    stops = [v for x in plan.exits if x.kind == "stop" for _, v in _prices(x.trigger)]
    targets = [v for x in plan.exits if x.kind == "take_profit" for _, v in _prices(x.trigger)]
    if stops and base.price < max(stops):
        out.append(f"base scenario {base.price} sits below the stop {max(stops)}")
    if targets and base.price > max(targets):
        out.append(f"base scenario {base.price} sits above the target {max(targets)}")
    return out


def _never_fires(trigger: Trigger) -> bool:
    """An all_of whose conditions cannot hold together."""
    if trigger.all_of is None:
        return False
    a, b = trigger.all_of
    if a.indicator and a.indicator == b.indicator != "rsi_14":
        return {a.when, b.when} in ({"crosses_above", "crosses_below"},
                                    {"weekly_close_above", "weekly_close_below"})
    if a.indicator == b.indicator == "rsi_14":
        lows = [x.value for x in (a, b) if x.when in ("below", "crosses_below")]
        highs = [x.value for x in (a, b) if x.when in ("above", "crosses_above")]
        return bool(lows and highs and min(lows) <= max(highs))
    if a.price and b.price:
        below = [x.price.value for x in (a, b) if x.price.when == "at_or_below"]
        above = [x.price.value for x in (a, b) if x.price.when == "at_or_above"]
        return bool(below and above and min(below) < max(above))
    return False


def _unavailable(trigger: Trigger, r: Readings) -> list[str]:
    return [leaf.indicator for leaf in trigger.leaves()
            if leaf.indicator and None in _level(leaf, r)]


def _check_triggers(plan: Plan, readings: Readings) -> list[str]:
    out: list[str] = []
    legs = [(f"entry {i}", e.trigger) for i, e in enumerate(plan.entries, 1)]
    legs += [(f"{x.kind} {i}", x.trigger) for i, x in enumerate(plan.exits, 1)]
    for label, trigger in legs:
        for name in _unavailable(trigger, readings):
            out.append(f"{label}: the {name} reading is unavailable, so the trigger cannot be checked")
        if _never_fires(trigger):
            out.append(f"{label}: the combined conditions can never be true together")
        elif _trigger_true_now(trigger, readings) is True:
            out.append(f"{label}: trigger is already true at the verified readings and would fire immediately")
    return out


def _check_rating(plan: Plan, rating: str | None) -> list[str]:
    """Coarse on purpose: what counts as a 'standard weight' is a product choice."""
    if rating is None:
        return []
    now = plan.now.portfolio_pct
    if rating == "Sell" and now != 0:
        return [f"rating Sell requires a recommended size of 0%, not {now}%"]
    if rating in ("Buy", "Overweight") and now <= 0:
        return [f"rating {rating} requires a recommended size above 0%"]
    return []


def check_plan(plan: Plan, readings: Readings, rating: str | None = None) -> list[str]:
    """Every contradiction in the plan; an empty list means it may be shown."""
    return [*_check_price(plan, readings), *_check_entries(plan), *_check_exits(plan),
            *_check_scenarios(plan), *_check_triggers(plan, readings), *_check_rating(plan, rating)]
