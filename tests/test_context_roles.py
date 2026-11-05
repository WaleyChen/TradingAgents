"""Each agent reads only the verified data it uses; the decision-makers get a short digest."""

from __future__ import annotations

import copy
import inspect

import pytest

from tests.test_context_company import COMPANY
from tests.test_context_events import EVENTS
from tests.test_context_financials import GOOD as FINANCIALS
from tests.test_context_technicals import PRICE, TECH
from tests.test_graph_end_to_end import TRADE_DATE, ScriptedModel, _graph, offline  # noqa: F401
from tests.test_structured_agent_prompts import _capturing_llm, _prompt_text
from tradingagents.agents.context import get_instrument_context_from_state
from tradingagents.context import Context
from tradingagents.context.render import render_context, render_role_blocks

FULL = Context.model_validate({
    "financials": FINANCIALS, "price": PRICE, "technicals": TECH, "company": COMPANY,
    "events": EVENTS, "notes": "Fed meets next week",
})


@pytest.fixture(scope="module")
def blocks():
    return render_role_blocks(FULL)


@pytest.mark.unit
def test_the_market_analyst_gets_the_technicals_and_the_price_only(blocks):
    text = blocks["market"]
    assert "Technicals for MU" in text and "Verified price for MU" in text
    assert "Financials for" not in text and "Events for" not in text and "Company:" not in text


@pytest.mark.unit
def test_the_news_analyst_gets_the_events_only(blocks):
    text = blocks["news"]
    assert "Events for MU" in text and "2026-12-17" in text
    assert "Financials for" not in text and "Technicals for" not in text


@pytest.mark.unit
def test_the_fundamentals_analyst_gets_financials_company_price_and_events(blocks):
    text = blocks["fundamentals"]
    for expected in ("Financials for MU", "Company: ", "Verified price for MU", "Events for MU", "FY2026"):
        assert expected in text
    assert "Technicals for" not in text


@pytest.mark.unit
def test_the_sentiment_analyst_gets_no_verified_data(blocks):
    assert "sentiment" not in blocks


@pytest.mark.unit
def test_the_decision_makers_get_a_short_digest_of_the_headline_facts(blocks):
    text = blocks["decision"]
    assert "Key verified facts" in text
    for expected in ("$795.23", "Computed metrics", "net margin", "18.2% below the 50-day average",
                     "Next earnings: 2026-12-17"):
        assert expected in text
    assert "Financials for" not in text and "Balance sheets" not in text and "- FY2026 (" not in text
    assert len(text) < len(render_context(FULL)) / 2


@pytest.mark.unit
def test_every_block_opens_with_the_rule_to_use_the_data_over_the_tools(blocks):
    for text in blocks.values():
        assert text.startswith("Verified data supplied by the caller")


@pytest.mark.unit
def test_notes_reach_only_the_agents_that_reason_over_everything(blocks):
    for role in ("fundamentals", "decision"):
        assert "Fed meets next week" in blocks[role] and "unverified" in blocks[role].lower()
    for role in ("market", "news"):
        assert "Fed meets next week" not in blocks[role]


@pytest.mark.unit
def test_a_block_with_nothing_for_a_role_is_left_out():
    only_fin = render_role_blocks(Context.model_validate({"financials": FINANCIALS}))
    assert set(only_fin) == {"fundamentals", "decision"}
    assert render_role_blocks(Context()) == {}


@pytest.mark.unit
def test_an_agents_context_is_the_identity_plus_its_own_block():
    state = {"company_of_interest": "MU", "instrument_context": "BASE-IDENTITY",
             "verified_blocks": {"market": "MARKET-BLOCK", "decision": "DIGEST"}}
    assert get_instrument_context_from_state(state, "market") == "BASE-IDENTITY\n\nMARKET-BLOCK"
    assert get_instrument_context_from_state(state, "news") == "BASE-IDENTITY"       # no block for it
    assert get_instrument_context_from_state(state) == "BASE-IDENTITY"
    assert get_instrument_context_from_state({"company_of_interest": "MU", "instrument_context": "BASE"},
                                             "market") == "BASE"


_ROLES = {
    "analysts.market_analyst": "market", "analysts.news_analyst": "news",
    "analysts.fundamentals_analyst": "fundamentals", "analysts.sentiment_analyst": "sentiment",
    "researchers.bull_researcher": "decision", "researchers.bear_researcher": "decision",
    "risk_mgmt.aggressive_debator": "decision", "risk_mgmt.conservative_debator": "decision",
    "risk_mgmt.neutral_debator": "decision", "managers.research_manager": "decision",
    "managers.portfolio_manager": "decision", "trader.trader": "decision",
}


@pytest.mark.unit
@pytest.mark.parametrize("module, role", sorted(_ROLES.items()))
def test_each_agent_asks_for_its_own_role(module, role):
    import importlib

    source = inspect.getsource(importlib.import_module(f"tradingagents.agents.{module}"))
    assert f'get_instrument_context_from_state(state, "{role}")' in source


@pytest.mark.unit
def test_the_deciders_prompts_carry_the_digest():
    from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
    from tradingagents.agents.schemas import (
        PortfolioDecision,
        PortfolioRating,
        TraderAction,
        TraderProposal,
    )
    from tradingagents.agents.trader.trader import create_trader

    captured: dict = {}
    llm = _capturing_llm(captured, PortfolioDecision(
        rating=PortfolioRating.HOLD, executive_summary="x", investment_thesis="y"))
    risk = {"history": "h", "aggressive_history": "a", "conservative_history": "c", "neutral_history": "n",
            "current_aggressive_response": "", "current_conservative_response": "",
            "current_neutral_response": "", "latest_speaker": "Neutral", "count": 1}
    create_portfolio_manager(llm)({"company_of_interest": "MU", "risk_debate_state": risk,
                                   "investment_plan": "p", "trader_investment_plan": "t",
                                   "verified_blocks": {"decision": "DIGEST-TEXT", "market": "MARKET-ONLY"}})
    text = _prompt_text(captured["prompt"])
    assert "DIGEST-TEXT" in text and "MARKET-ONLY" not in text

    captured.clear()
    trader_llm = _capturing_llm(captured, TraderProposal(action=TraderAction.BUY, reasoning="x"))
    create_trader(trader_llm)({"company_of_interest": "MU", "investment_plan": "p", "market_report": "m",
                               "verified_blocks": {"decision": "DIGEST-TEXT", "market": "MARKET-ONLY"}})
    text = _prompt_text(captured["prompt"])
    assert "DIGEST-TEXT" in text and "MARKET-ONLY" not in text


@pytest.mark.unit
def test_the_run_state_carries_the_blocks_and_the_identity_stays_clean(tmp_path, monkeypatch, offline):  # noqa: F811
    graph = _graph(tmp_path, monkeypatch, ScriptedModel(), context_data=FULL.model_dump(mode="json"))
    state = graph.create_run_state("MU", TRADE_DATE)
    assert set(state["verified_blocks"]) == {"market", "news", "fundamentals", "decision"}
    assert "Technicals for" not in state["instrument_context"]
    bare = _graph(tmp_path, monkeypatch, ScriptedModel())
    assert bare.create_run_state("MU", TRADE_DATE)["verified_blocks"] == {}


@pytest.mark.unit
def test_the_api_config_carries_the_parsed_context_and_no_prerendered_block():
    from datetime import date

    from api.schemas import CreateAnalysisRequest
    from api.service import build_config

    request = CreateAnalysisRequest(ticker="MU", trade_date=date(2026, 11, 4),
                                    context={"financials": copy.deepcopy(FINANCIALS)})
    config = build_config(request)
    assert "verified_context" not in config and config["context_data"]["financials"]["ticker"] == "MU"
