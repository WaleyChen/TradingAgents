"""The position plan: entries and exits as data, with a closed trigger list.

A trigger is one of a fixed set of conditions, never free text, so a page can
show it, a validator can check it and an alert can be built from it.
Percentages are the total position size, as a share of an equity portfolio,
after the trigger fires.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_LEGS = 5
MAX_PCT = 10.0
MAX_PRICE = 1e7        # no real quote is near this; it only bounds a runaway value
MAX_TEXT = 500
MAX_LABEL = 60

# What each indicator may be asked, and whether it needs a threshold value.
_CROSSES = ("crosses_above", "crosses_below")
_WEEKLY = ("weekly_close_below", "weekly_close_above")
INDICATOR_RULES: dict[str, tuple[tuple[str, ...], bool]] = {
    "rsi_14": (("below", "above", *_CROSSES), True),
    "sma_20": (_CROSSES, False),
    "sma_50": (_CROSSES, False),
    "sma_200": (_CROSSES, False),
    "close_vs_sma_20": (_WEEKLY, False),
    "close_vs_sma_50": (_WEEKLY, False),
    "close_vs_sma_200": (_WEEKLY, False),
    "macd_histogram": (_CROSSES, False),
}
IndicatorName = Literal[
    "rsi_14", "sma_20", "sma_50", "sma_200",
    "close_vs_sma_20", "close_vs_sma_50", "close_vs_sma_200", "macd_histogram",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class PriceTest(_Strict):
    when: Literal["at_or_below", "at_or_above"]
    value: float = Field(gt=0, lt=MAX_PRICE)


class Leaf(_Strict):
    """One condition: a price level or a single indicator test."""

    price: PriceTest | None = None
    indicator: IndicatorName | None = None
    when: str | None = None
    value: float | None = None

    def _is_leaf(self) -> bool:
        return True

    @model_validator(mode="after")
    def _one_closed_condition(self):
        if not self._is_leaf():
            return self
        if (self.price is None) == (self.indicator is None):
            raise ValueError("a condition is either a price or an indicator")
        if self.price is not None:
            if (self.when, self.value) != (None, None):
                raise ValueError("a price condition carries its own when and value")
            return self
        allowed, needs_value = INDICATOR_RULES[self.indicator]
        if self.when not in allowed:
            raise ValueError(f"{self.indicator} allows only {', '.join(allowed)}")
        if needs_value and self.value is None:
            raise ValueError(f"{self.indicator} needs a value")
        if not needs_value and self.value is not None:
            raise ValueError(f"{self.indicator} takes no value")
        if self.indicator == "rsi_14" and not 0 <= self.value <= 100:
            raise ValueError("rsi_14 value must be between 0 and 100")
        return self


class Trigger(Leaf):
    """A condition, or at most two of them joined by all_of / any_of."""

    all_of: list[Leaf] | None = Field(default=None, min_length=2, max_length=2)
    any_of: list[Leaf] | None = Field(default=None, min_length=2, max_length=2)

    def _is_leaf(self) -> bool:
        return self.all_of is None and self.any_of is None

    @model_validator(mode="after")
    def _combiner_stands_alone(self):
        if self._is_leaf():
            return self
        if self.all_of is not None and self.any_of is not None:
            raise ValueError("use all_of or any_of, not both")
        if (self.price, self.indicator, self.when, self.value) != (None,) * 4:
            raise ValueError("a combined trigger holds only its conditions")
        return self

    def leaves(self) -> tuple[Leaf, ...]:
        if self.all_of is not None:
            return tuple(self.all_of)
        if self.any_of is not None:
            return tuple(self.any_of)
        return (self,)


Pct = Field(ge=0, le=MAX_PCT)


class Now(_Strict):
    portfolio_pct: float = Pct
    summary: str = Field(min_length=1, max_length=MAX_TEXT)


class Entry(_Strict):
    portfolio_pct: float = Pct
    trigger: Trigger
    reason: str = Field(min_length=1, max_length=MAX_TEXT)


class Exit(_Strict):
    kind: Literal["take_profit", "stop"]
    portfolio_pct: float = Pct
    trigger: Trigger
    reason: str = Field(min_length=1, max_length=MAX_TEXT)


class Scenario(_Strict):
    name: str = Field(min_length=1, max_length=MAX_LABEL)
    price: float = Field(gt=0, lt=MAX_PRICE)
    probability: float = Field(ge=0, le=1)
    driver: str = Field(min_length=1, max_length=MAX_TEXT, description="What would have to happen.")
    basis: str = Field(min_length=1, max_length=MAX_TEXT,
                       description="Why this probability: a judgment, say so, or the evidence.")


class Target(_Strict):
    """A price the plan stands behind, and where it comes from."""

    price: float = Field(gt=0, lt=MAX_PRICE)
    method: Literal["base_scenario", "valuation", "technical_reference"]
    basis: str = Field(min_length=1, max_length=MAX_TEXT)


class Watch(_Strict):
    """Something measurable that would change the view, with a date when it has one."""

    what: str = Field(min_length=1, max_length=MAX_TEXT)
    when: date | None = None
    effect: Literal["supports", "weakens"]


class Plan(_Strict):
    as_of_price: float = Field(gt=0, lt=MAX_PRICE)
    as_of_date: date
    horizon: str = Field(min_length=1, max_length=MAX_LABEL)
    now: Now
    entries: list[Entry] = Field(default_factory=list, max_length=MAX_LEGS)
    exits: list[Exit] = Field(default_factory=list, max_length=MAX_LEGS)
    scenarios: list[Scenario] = Field(min_length=1, max_length=5)
    expected_return_pct: float | None = Field(
        default=None, description="Computed by the service from the scenarios; leave it null.")
    target: Target
    if_you_own: str = Field(min_length=1, max_length=MAX_TEXT)
    if_you_dont: str = Field(min_length=1, max_length=MAX_TEXT)
    watch: list[Watch] = Field(min_length=1, max_length=MAX_LEGS)
    confidence: Literal["low", "medium", "high"]
    confidence_reason: str = Field(min_length=1, max_length=MAX_TEXT)
