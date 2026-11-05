"""When verified data covers a fact, the analysts do not fetch another copy of it."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.test_context_company import PRICE
from tests.test_context_financials import GOOD as FINANCIALS
from tests.test_context_ownership import OWNERSHIP
from tests.test_context_technicals import TECH
from tradingagents.agents.analysts import fundamentals_analyst, market_analyst, news_analyst
from tradingagents.context import Context
from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS, build_analyst_execution_plan
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.setup import GraphSetup
from tradingagents.graph.tool_policy import tools_for


def names(tools):
    return {t.name for t in tools}


def _ctx(**blocks):
    return Context.model_validate(blocks)


FUNDAMENTALS = ANALYST_NODE_SPECS["fundamentals"].tools
MARKET = ANALYST_NODE_SPECS["market"].tools
STATEMENTS = {"get_balance_sheet", "get_cashflow", "get_income_statement"}


@pytest.mark.unit
def test_with_no_context_every_analyst_keeps_every_tool():
    for key, spec in ANALYST_NODE_SPECS.items():
        assert tools_for(key, spec.tools, None) == spec.tools
        assert tools_for(key, spec.tools, Context()) == spec.tools


@pytest.mark.unit
def test_verified_financials_replace_the_statement_tools_and_nothing_else():
    kept = names(tools_for("fundamentals", FUNDAMENTALS, _ctx(financials=FINANCIALS)))
    assert kept == names(FUNDAMENTALS) - STATEMENTS
    assert {"get_fundamentals", "get_insider_transactions"} <= kept


@pytest.mark.unit
def test_verified_ownership_replaces_the_insider_tool():
    kept = names(tools_for("fundamentals", FUNDAMENTALS, _ctx(ownership=OWNERSHIP)))
    assert "get_insider_transactions" not in kept and kept >= STATEMENTS


@pytest.mark.unit
def test_both_together_leave_the_overview_tool():
    kept = names(tools_for("fundamentals", FUNDAMENTALS, _ctx(financials=FINANCIALS, ownership=OWNERSHIP)))
    assert kept == {"get_fundamentals"}


@pytest.mark.unit
def test_supplied_technicals_replace_the_indicator_tools_and_keep_the_price_history():
    kept = names(tools_for("market", MARKET, _ctx(technicals=TECH)))
    assert kept == {"get_stock_data"}


@pytest.mark.unit
def test_a_price_alone_does_not_change_the_market_tools():
    assert tools_for("market", MARKET, _ctx(price=PRICE)) == MARKET


@pytest.mark.unit
def test_other_analysts_are_never_changed():
    ctx = _ctx(financials=FINANCIALS, technicals=TECH, ownership=OWNERSHIP)
    for key in ("news", "social"):
        assert tools_for(key, ANALYST_NODE_SPECS[key].tools, ctx) == ANALYST_NODE_SPECS[key].tools


@pytest.mark.unit
def test_an_analyst_is_never_left_without_a_tool():
    only_dropped = (fundamentals_analyst.get_balance_sheet, fundamentals_analyst.get_cashflow)
    assert tools_for("fundamentals", only_dropped, _ctx(financials=FINANCIALS)) == only_dropped


@pytest.mark.unit
def test_the_plan_applies_the_policy_to_each_selected_analyst():
    plan = build_analyst_execution_plan(["market", "fundamentals", "news"], _ctx(financials=FINANCIALS, technicals=TECH))
    by_key = {spec.key: names(spec.tools) for spec in plan.specs}
    assert by_key["market"] == {"get_stock_data"}
    assert not (STATEMENTS & by_key["fundamentals"]) and "get_fundamentals" in by_key["fundamentals"]
    assert by_key["news"] == names(ANALYST_NODE_SPECS["news"].tools)
    assert build_analyst_execution_plan(["market"]).specs[0].tools == MARKET            # no context: unchanged


@pytest.mark.unit
def test_the_graph_builds_each_analyst_with_the_tools_the_policy_left_it(monkeypatch):
    import tradingagents.graph.setup as setup_module

    seen = {}
    monkeypatch.setattr(setup_module, "create_market_analyst", lambda llm, tools: seen.setdefault("market", tools) and (lambda s: {}))
    monkeypatch.setattr(setup_module, "create_fundamentals_analyst", lambda llm, tools: seen.setdefault("fundamentals", tools) and (lambda s: {}))
    monkeypatch.setattr(setup_module, "_analyst_graph", lambda spec, node, rounds: (lambda s: {}))
    GraphSetup(MagicMock(), MagicMock(), ConditionalLogic(), 3).setup_graph(
        ["market", "fundamentals"], context=_ctx(financials=FINANCIALS, technicals=TECH))
    assert names(seen["market"]) == {"get_stock_data"}
    assert not (STATEMENTS & names(seen["fundamentals"]))


@pytest.mark.unit
def test_the_run_hands_the_graph_the_callers_context(tmp_path, monkeypatch):
    from tests.test_graph_end_to_end import ScriptedModel, _graph

    captured = {}
    original = GraphSetup.setup_graph

    def spy(self, *args, **kwargs):
        captured["context"] = kwargs.get("context")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(GraphSetup, "setup_graph", spy)
    data = _ctx(financials=FINANCIALS).model_dump(mode="json")
    _graph(tmp_path, monkeypatch, ScriptedModel(), context_data=data)
    assert captured["context"] is not None and captured["context"].financials.ticker == "MU"


# --- the prompts say only what the analyst can do ---------------------------------

@pytest.mark.unit
def test_the_fundamentals_prompt_names_only_the_tools_it_has_and_points_to_the_supplied_data():
    full = fundamentals_analyst.fundamentals_system_message(FUNDAMENTALS)
    assert "`get_balance_sheet`" in full and "supplied verified data" not in full
    kept = tools_for("fundamentals", FUNDAMENTALS, _ctx(financials=FINANCIALS, ownership=OWNERSHIP))
    lean = " ".join(fundamentals_analyst.fundamentals_system_message(kept).split())
    for gone in ("get_balance_sheet", "get_cashflow", "get_income_statement", "get_insider_transactions"):
        assert gone not in lean
    assert "`get_fundamentals`" in lean
    assert "financial statements" in lean and "supplied verified data" in lean and "do not look for other copies" in lean
    assert "insider and fund activity" in lean.lower()


@pytest.mark.unit
def test_the_market_prompt_with_supplied_levels_selects_no_indicators():
    full = market_analyst.market_system_message(MARKET)
    assert "select the **most relevant indicators**" in full and "get_verified_market_snapshot" in full
    lean = " ".join(market_analyst.market_system_message(tools_for("market", MARKET, _ctx(technicals=TECH))).split())
    assert "only indicator values you may use" in lean and "get_stock_data" in lean
    for gone in ("select the **most relevant indicators**", "get_indicators", "get_verified_market_snapshot",
                 "Indicator Selection"):
        assert gone not in lean
    assert "at most 350 words" in lean and "RSI under 30 or over 70" in lean


@pytest.mark.unit
def test_every_analyst_builds_with_a_tool_subset():
    for factory, tools in ((market_analyst.create_market_analyst, MARKET[:1]),
                           (fundamentals_analyst.create_fundamentals_analyst, FUNDAMENTALS[:1]),
                           (news_analyst.create_news_analyst, ANALYST_NODE_SPECS["news"].tools[:1])):
        assert callable(factory(MagicMock(), tools=tools))
