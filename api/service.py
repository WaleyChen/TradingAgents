"""Run a TradingAgents analysis for an API job."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any

from api.schemas import CreateAnalysisRequest
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph


def build_config(request: CreateAnalysisRequest) -> dict[str, Any]:
    """Merge request overrides onto DEFAULT_CONFIG (env overrides already applied)."""
    config = deepcopy(DEFAULT_CONFIG)

    if request.max_debate_rounds is not None:
        config["max_debate_rounds"] = request.max_debate_rounds
    if request.max_risk_discuss_rounds is not None:
        config["max_risk_discuss_rounds"] = request.max_risk_discuss_rounds
    if request.llm_provider:
        config["llm_provider"] = request.llm_provider
    if request.deep_think_llm:
        config["deep_think_llm"] = request.deep_think_llm
    if request.quick_think_llm:
        config["quick_think_llm"] = request.quick_think_llm
    if request.output_language:
        config["output_language"] = request.output_language

    return config


def extract_reports(final_state: dict[str, Any]) -> dict[str, str | None]:
    return {
        "market_report": final_state.get("market_report") or None,
        "sentiment_report": final_state.get("sentiment_report") or None,
        "news_report": final_state.get("news_report") or None,
        "fundamentals_report": final_state.get("fundamentals_report") or None,
        "investment_plan": final_state.get("investment_plan") or None,
        "trader_investment_plan": final_state.get("trader_investment_plan") or None,
        "final_trade_decision": final_state.get("final_trade_decision") or None,
    }


def run_analysis(request: CreateAnalysisRequest) -> tuple[str, dict[str, str | None]]:
    """Execute the graph synchronously; returns (decision, reports)."""
    config = build_config(request)
    graph = TradingAgentsGraph(
        selected_analysts=list(request.analysts),
        debug=False,
        config=config,
    )

    trade_date: date | str = request.trade_date
    final_state, decision = graph.propagate(
        request.ticker,
        trade_date.isoformat() if isinstance(trade_date, date) else str(trade_date),
        asset_type=request.asset_type,
    )

    if request.save_reports:
        graph.save_reports(final_state, request.ticker)

    # process_signal may return a LangChain message-like object in some stubs;
    # normalize to a plain string for JSON responses.
    decision_text = (
        str(decision.content) if hasattr(decision, "content") else str(decision).strip()
    )

    return decision_text, extract_reports(final_state)
