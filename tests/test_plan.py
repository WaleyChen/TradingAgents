"""The position plan: a closed trigger vocabulary and checks that make a
contradictory plan impossible to return."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from tradingagents.plan import Plan, Readings, check_plan

PRICE = 247.19
READINGS = Readings(price=PRICE, rsi_14=39.0, sma_20=250.0, sma_50=255.0,
                    sma_200=240.0, macd_histogram=-0.8)

GOOD = {
    "as_of_price": PRICE,
    "as_of_date": "2026-11-02",
    "horizon": "3-6 months",
    "now": {"portfolio_pct": 2.5, "summary": "Hold a moderate position; add on dips."},
    "entries": [
        {"portfolio_pct": 3.0, "reason": "200-day support",
         "trigger": {"price": {"when": "at_or_below", "value": 245.0}}},
        {"portfolio_pct": 4.0, "reason": "Oversold washout",
         "trigger": {"all_of": [
             {"indicator": "rsi_14", "when": "below", "value": 30},
             {"price": {"when": "at_or_below", "value": 237.0}}]}},
    ],
    "exits": [
        {"kind": "take_profit", "portfolio_pct": 1.5, "reason": "Capex-payoff target",
         "trigger": {"price": {"when": "at_or_above", "value": 300.0}}},
        {"kind": "stop", "portfolio_pct": 0.0, "reason": "Uptrend support fails",
         "trigger": {"indicator": "close_vs_sma_200", "when": "weekly_close_below"}},
    ],
    "scenarios": [
        {"name": "bull", "price": 330, "probability": 0.25,
         "driver": "Capex pays back faster than feared", "basis": "Judgment: needs two clean quarters."},
        {"name": "base", "price": 300, "probability": 0.5,
         "driver": "Growth holds, margins stay near today's level", "basis": "Judgment: the current run-rate."},
        {"name": "bear", "price": 210, "probability": 0.25,
         "driver": "Demand slows while spending continues", "basis": "Judgment: a repeat of the last downturn."},
    ],
    "target": {"price": 300.0, "method": "base_scenario", "basis": "Base-case value in 6 months."},
    "if_you_own": "Hold; add only on the entry levels below.",
    "if_you_dont": "Start small now and build on the entry levels.",
    "watch": [{"what": "Stock reclaims its 50-day average", "when": "2026-12-10", "effect": "supports"}],
    "confidence": "medium",
    "confidence_reason": "Strong fundamentals, but capex return is unproven.",
}


def plan_with(**changes) -> dict:
    data = copy.deepcopy(GOOD)
    data.update(changes)
    return data


def problems(data: dict, readings: Readings = READINGS, rating: str | None = None, **kw) -> list[str]:
    return check_plan(Plan.model_validate(data), readings, rating, **kw)


@pytest.mark.unit
def test_the_example_plan_passes_every_check():
    assert problems(GOOD) == []


@pytest.mark.unit
@pytest.mark.parametrize("trigger", [
    {"indicator": "stochastic", "when": "below", "value": 20},          # not on the list
    {"indicator": "rsi_14", "when": "weekly_close_below", "value": 30},  # wrong verb
    {"indicator": "rsi_14", "when": "below"},                            # missing value
    {"indicator": "sma_50", "when": "crosses_above", "value": 5},        # value not allowed
    {"price": {"when": "crosses_above", "value": 10}},
    {"price": {"when": "at_or_below", "value": 10}, "indicator": "rsi_14"},  # two shapes
    {"all_of": [{"price": {"when": "at_or_below", "value": 1}}] * 3},    # more than 2
    {"all_of": [{"all_of": [{"price": {"when": "at_or_below", "value": 1}}]}]},  # nesting
    {"reason": "free text instead of a trigger"},
])
def test_triggers_outside_the_closed_list_are_rejected(trigger):
    data = plan_with()
    data["entries"][0]["trigger"] = trigger
    with pytest.raises(ValidationError):
        Plan.model_validate(data)


@pytest.mark.unit
@pytest.mark.parametrize("field, value", [("entries", 6), ("exits", 6)])
def test_a_plan_has_at_most_five_entries_and_five_exits(field, value):
    data = plan_with()
    data[field] = [copy.deepcopy(GOOD[field][0])] * value
    with pytest.raises(ValidationError):
        Plan.model_validate(data)


@pytest.mark.unit
@pytest.mark.parametrize("pct", [-1, 10.5])
def test_percentages_stay_between_zero_and_ten(pct):
    data = plan_with()
    data["entries"][0]["portfolio_pct"] = pct
    with pytest.raises(ValidationError):
        Plan.model_validate(data)


@pytest.mark.unit
def test_an_entry_above_the_current_price_is_flagged():
    data = plan_with()
    data["entries"][0]["trigger"] = {"price": {"when": "at_or_below", "value": 260.0}}
    assert any("entry" in p and "below" in p for p in problems(data))


@pytest.mark.unit
def test_an_entry_must_use_at_or_below():
    data = plan_with()
    data["entries"][0]["trigger"] = {"price": {"when": "at_or_above", "value": 240.0}}
    assert any("at_or_below" in p for p in problems(data))


@pytest.mark.unit
def test_a_take_profit_below_the_price_is_flagged():
    data = plan_with()
    data["exits"][0]["trigger"] = {"price": {"when": "at_or_above", "value": 200.0}}
    assert any("take_profit" in p for p in problems(data))


@pytest.mark.unit
def test_a_stop_must_sit_below_the_lowest_entry():
    data = plan_with()
    data["exits"][1]["trigger"] = {"price": {"when": "at_or_below", "value": 240.0}}
    assert any("stop" in p and "lowest entry" in p for p in problems(data))


@pytest.mark.unit
def test_entries_must_add_and_exits_must_reduce_from_the_current_size():
    data = plan_with()
    data["entries"][0]["portfolio_pct"] = 2.0   # "entry" that shrinks the position
    data["exits"][0]["portfolio_pct"] = 3.5     # "take profit" that grows it
    found = " ".join(problems(data))
    assert "entry" in found and "take_profit" in found


@pytest.mark.unit
def test_scenario_probabilities_must_sum_to_one():
    data = plan_with()
    data["scenarios"][0]["probability"] = 0.4
    assert any("sum" in p for p in problems(data))


@pytest.mark.unit
def test_the_base_case_must_sit_between_the_stop_and_the_target():
    data = plan_with()
    data["exits"][1]["trigger"] = {"price": {"when": "at_or_below", "value": 230.0}}
    data["scenarios"][1]["price"] = 350    # above the 300 target
    assert any("base" in p for p in problems(data))


@pytest.mark.unit
def test_the_iren_contradiction_is_caught():
    """Underweight, a sell at $26.60, a stop at $28.50 and a $33.62 target on a
    stock near $24: the plan the structured output exists to prevent."""
    data = plan_with(as_of_price=24.0)
    data["now"] = {"portfolio_pct": 2.0, "summary": "Hold a small position."}
    data["entries"] = []
    data["exits"] = [
        {"kind": "take_profit", "portfolio_pct": 1.0, "reason": "sell",
         "trigger": {"price": {"when": "at_or_above", "value": 26.6}}},
        {"kind": "stop", "portfolio_pct": 0.0, "reason": "stop",
         "trigger": {"price": {"when": "at_or_below", "value": 28.5}}},
    ]
    data["scenarios"] = [{"name": "base", "price": 33.62, "probability": 1.0, "driver": "d", "basis": "b"}]
    data["target"] = {"price": 33.62, "method": "base_scenario", "basis": "b"}
    found = " ".join(problems(data, Readings(price=24.0), rating="Underweight"))
    assert "stop" in found and "base" in found


@pytest.mark.unit
def test_an_indicator_entry_that_already_fired_is_flagged():
    """'RSI below 30' is a fine entry at RSI 39 and meaningless at RSI 22."""
    data = plan_with()
    data["entries"][1]["trigger"] = {"indicator": "rsi_14", "when": "below", "value": 30}
    assert problems(data) == []
    oversold = Readings(**{**READINGS.__dict__, "rsi_14": 22.0})
    assert any("already" in p for p in problems(data, oversold))


@pytest.mark.unit
def test_a_crossing_that_has_already_happened_is_flagged():
    data = plan_with()
    data["entries"][1]["trigger"] = {"indicator": "sma_20", "when": "crosses_above"}
    # price 247.19 sits below the 250 sma_20, so a cross above is still ahead
    assert problems(data) == []
    above = Readings(**{**READINGS.__dict__, "sma_20": 240.0})
    assert any("already" in p for p in problems(data, above))


@pytest.mark.unit
def test_the_plan_price_must_match_the_verified_price():
    assert any("as_of_price" in p for p in problems(plan_with(as_of_price=300.0)))


@pytest.mark.unit
def test_a_trigger_on_a_missing_reading_is_an_error():
    """A plan that cannot be checked is not a plan that passed."""
    data = plan_with()
    data["entries"][1]["trigger"] = {"indicator": "rsi_14", "when": "below", "value": 30}
    assert any("rsi_14" in p and "unavailable" in p for p in problems(data, Readings(price=PRICE)))


@pytest.mark.unit
def test_a_price_only_plan_needs_no_indicator_readings():
    data = plan_with()
    data["entries"][1]["trigger"] = {"price": {"when": "at_or_below", "value": 237.0}}
    data["exits"][1]["trigger"] = {"price": {"when": "at_or_below", "value": 220.0}}
    assert problems(data, Readings(price=PRICE)) == []


@pytest.mark.unit
def test_entries_must_grow_the_position_as_price_falls():
    data = plan_with()
    data["entries"][1]["trigger"] = {"price": {"when": "at_or_below", "value": 237.0}}
    data["entries"][1]["portfolio_pct"] = 2.8    # a deeper entry that sells down
    assert any("deeper" in p for p in problems(data))


@pytest.mark.unit
@pytest.mark.parametrize("kind, pct, trigger", [
    ("stop", 4.0, {"price": {"when": "at_or_below", "value": 230.0}}),         # the full ladder is 4%
    ("take_profit", 2.5, {"price": {"when": "at_or_above", "value": 300.0}}),  # now is 2.5%
])
def test_an_exit_must_reduce_the_position(kind, pct, trigger):
    data = plan_with()
    data["exits"] = [{"kind": kind, "portfolio_pct": pct, "reason": "x", "trigger": trigger}]
    assert any(kind in p and "reduce" in p for p in problems(data))


@pytest.mark.unit
def test_a_stop_reduces_the_position_the_entries_built():
    """Entries fill before a stop below them fires, so a stop is measured from
    the largest entry, not from `now`."""
    data = plan_with()
    data["now"] = {"portfolio_pct": 2.0, "summary": "x"}
    data["exits"][1]["portfolio_pct"] = 3.0     # down from the 4% ladder
    assert problems(data) == []


@pytest.mark.unit
def test_a_bearish_plan_can_buy_lower_and_stop_out_to_zero():
    data = plan_with()
    data["now"] = {"portfolio_pct": 0.0, "summary": "Stay out."}
    data["exits"] = [data["exits"][1]]
    data["target"] = {"price": 220.0, "method": "valuation", "basis": "Earnings fall back to the cycle average."}
    assert problems(data, rating="Sell") == []


@pytest.mark.unit
def test_with_nothing_held_and_no_entries_an_exit_has_nothing_to_reduce():
    data = plan_with()
    data["now"] = {"portfolio_pct": 0.0, "summary": "Stay out."}
    data["entries"] = []
    assert any("reduce" in p for p in problems(data))


@pytest.mark.unit
@pytest.mark.parametrize("rating, now, ok", [
    ("Sell", 0.0, True), ("Sell", 1.0, False),
    ("Buy", 2.5, True), ("Overweight", 0.0, False),
])
def test_the_rating_and_the_recommended_size_agree(rating, now, ok):
    data = plan_with()
    data["now"] = {"portfolio_pct": now, "summary": "x"}
    data["exits"] = [] if now == 0 else data["exits"]
    data["entries"] = [] if rating == "Sell" else data["entries"]
    if now == 1.0:
        data["exits"] = []
    got = [p for p in problems(data, rating=rating) if "recommended size" in p]
    assert (got == []) is ok


@pytest.mark.unit
def test_two_base_scenarios_are_an_error():
    data = plan_with()
    why = {"driver": "d", "basis": "b"}
    data["scenarios"] = [{"name": "base", "price": 300, "probability": 0.5, **why},
                         {"name": "Base", "price": 280, "probability": 0.5, **why}]
    assert any("base" in p and "once" in p for p in problems(data))


@pytest.mark.unit
def test_a_combined_trigger_that_can_never_fire_is_flagged():
    data = plan_with()
    data["entries"][1]["trigger"] = {"all_of": [
        {"indicator": "rsi_14", "when": "below", "value": 20},
        {"indicator": "rsi_14", "when": "above", "value": 80}]}
    assert any("never" in p for p in problems(data))


@pytest.mark.unit
@pytest.mark.parametrize("bad", [0.0, -5.0, float("nan"), float("inf")])
def test_the_verified_price_must_be_finite_and_positive(bad):
    with pytest.raises(ValueError):
        Readings(price=bad)


@pytest.mark.unit
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_an_indicator_reading_must_be_finite(bad):
    with pytest.raises(ValueError):
        Readings(price=10.0, rsi_14=bad)


@pytest.mark.unit
def test_an_infinite_value_is_not_a_valid_trigger_level():
    data = plan_with()
    data["entries"][0]["trigger"] = {"price": {"when": "at_or_below", "value": float("inf")}}
    with pytest.raises(ValidationError):
        Plan.model_validate(data)


@pytest.mark.unit
@pytest.mark.parametrize("indicator, pair", [
    ("sma_50", ("crosses_above", "crosses_below")),
    ("macd_histogram", ("crosses_above", "crosses_below")),
    ("close_vs_sma_50", ("weekly_close_below", "weekly_close_above")),
])
def test_opposite_conditions_on_one_indicator_can_never_fire_together(indicator, pair):
    data = plan_with()
    data["entries"][1]["trigger"] = {"all_of": [
        {"indicator": indicator, "when": pair[0]}, {"indicator": indicator, "when": pair[1]}]}
    assert any("never" in p for p in problems(data))


@pytest.mark.unit
def test_an_any_of_with_an_indicator_has_no_price_rung_on_the_ladder():
    """RSI can fire it at any price, so it is not 'deeper' than the 245 entry."""
    data = plan_with()
    data["entries"][1]["portfolio_pct"] = 3.5
    data["entries"][1]["trigger"] = {"any_of": [
        {"price": {"when": "at_or_below", "value": 237.0}},
        {"indicator": "rsi_14", "when": "below", "value": 30}]}
    data["entries"][0]["portfolio_pct"] = 3.6    # would break a price ladder
    assert not any("deeper" in p for p in problems(data))


@pytest.mark.unit
def test_the_base_scenario_name_ignores_case_and_spaces():
    data = plan_with()
    data["scenarios"][1]["name"] = " Base "
    assert problems(data) == []


@pytest.mark.unit
def test_exits_step_down_as_they_are_reached():
    data = plan_with()
    data["exits"] = [
        {"kind": "take_profit", "portfolio_pct": 1.0, "reason": "a",
         "trigger": {"price": {"when": "at_or_above", "value": 280.0}}},
        {"kind": "take_profit", "portfolio_pct": 2.0, "reason": "b",    # holds more at a higher price
         "trigger": {"price": {"when": "at_or_above", "value": 300.0}}},
    ]
    assert any("take_profit" in p and "higher" in p for p in problems(data))


@pytest.mark.unit
@pytest.mark.parametrize("field, value", [("summary", "x" * 501), ("horizon", "x" * 61)])
def test_free_text_is_bounded(field, value):
    data = plan_with()
    if field == "summary":
        data["now"]["summary"] = value
    else:
        data["horizon"] = value
    with pytest.raises(ValidationError):
        Plan.model_validate(data)


@pytest.mark.unit
def test_a_price_beyond_any_real_quote_is_rejected():
    data = plan_with()
    data["scenarios"][2]["price"] = 1e30
    with pytest.raises(ValidationError):
        Plan.model_validate(data)


def _target(**changes):
    data = copy.deepcopy(GOOD)
    data["target"] = {**data["target"], **changes}
    return data


@pytest.mark.unit
@pytest.mark.parametrize("rating, price, ok", [
    ("Overweight", 300.0, True), ("Overweight", 200.0, False),
    ("Buy", 200.0, False), ("Sell", 300.0, False), ("Underweight", 300.0, False),
    ("Hold", 300.0, True), ("Hold", 200.0, True),
])
def test_the_target_points_the_way_the_rating_does(rating, price, ok):
    data = _target(price=price, method="valuation")
    found = [p for p in problems(data, rating=rating) if "target" in p and "rating" in p]
    assert (found == []) is ok


@pytest.mark.unit
def test_a_target_taken_from_the_base_case_must_be_the_base_case():
    assert any("base scenario" in p for p in problems(_target(price=320.0)))
    assert problems(_target(price=300.5)) == []                 # within rounding


@pytest.mark.unit
def test_a_moving_average_is_not_a_fair_value():
    found = problems(_target(price=240.5, method="valuation"))
    assert any("200-day average" in p and "technical_reference" in p for p in found)
    assert not any("average" in p for p in problems(_target(price=240.5, method="technical_reference")))
    assert not any("average" in p for p in problems(_target(price=262.0, method="valuation")))


@pytest.mark.unit
def test_a_watch_date_must_be_after_the_plan_date():
    data = copy.deepcopy(GOOD)
    data["watch"][0]["when"] = "2026-11-02"
    assert any("watch" in p for p in problems(data))
    data["watch"][0]["when"] = None
    assert problems(data) == []


@pytest.mark.unit
@pytest.mark.parametrize("remove", ["target", "if_you_own", "if_you_dont", "watch"])
def test_the_decision_fields_are_required(remove):
    data = copy.deepcopy(GOOD)
    del data[remove]
    with pytest.raises(ValidationError):
        Plan.model_validate(data)


@pytest.mark.unit
def test_every_scenario_states_its_driver_and_why_that_probability():
    for field in ("driver", "basis"):
        data = copy.deepcopy(GOOD)
        del data["scenarios"][0][field]
        with pytest.raises(ValidationError):
            Plan.model_validate(data)


@pytest.mark.unit
def test_the_target_method_comes_from_a_closed_list_and_watch_needs_at_least_one_item():
    with pytest.raises(ValidationError):
        Plan.model_validate(_target(method="gut feeling"))
    data = copy.deepcopy(GOOD)
    data["watch"] = []
    with pytest.raises(ValidationError):
        Plan.model_validate(data)


@pytest.mark.unit
def test_an_invented_field_with_no_value_is_dropped_not_a_reason_to_discard_the_plan():
    data = copy.deepcopy(GOOD)
    for leg in (*data["entries"], *data["exits"]):
        leg["entry_note"] = None                                   # what a real model emitted
    data["scenarios"][0]["comment"] = None
    data["made_up"] = None
    plan = Plan.model_validate(data)
    assert plan.entries[0].portfolio_pct == 3.0 and not hasattr(plan.entries[0], "entry_note")


@pytest.mark.unit
def test_an_invented_field_that_says_something_is_still_refused():
    data = copy.deepcopy(GOOD)
    data["entries"][0]["entry_note"] = "buy the dip"
    with pytest.raises(ValidationError):
        Plan.model_validate(data)
    data = copy.deepcopy(GOOD)
    data["surprise"] = 0
    with pytest.raises(ValidationError):
        Plan.model_validate(data)


@pytest.mark.unit
@pytest.mark.parametrize("empty", ["", None, [], {}])
def test_an_invented_field_with_an_empty_value_is_dropped_whatever_kind_of_empty(empty):
    data = copy.deepcopy(GOOD)
    for leg in (*data["entries"], *data["exits"]):
        leg["trigger_note"] = empty                                  # what a real model emitted, as an empty string
    data["now"]["extra_note"] = empty
    data["watch"][0]["note"] = empty
    assert Plan.model_validate(data).entries[0].portfolio_pct == 3.0


@pytest.mark.unit
def test_an_empty_value_in_a_field_we_do_know_is_still_an_error():
    data = copy.deepcopy(GOOD)
    data["entries"][0]["reason"] = ""
    with pytest.raises(ValidationError):
        Plan.model_validate(data)
    data = copy.deepcopy(GOOD)
    data["entries"][0]["trigger"]["timeframe"] = ""                  # empty and unknown inside a trigger: harmless
    assert Plan.model_validate(data)
    data["entries"][0]["trigger"]["timeframe"] = "weekly"            # but one that says something could change its meaning
    with pytest.raises(ValidationError):
        Plan.model_validate(data)
