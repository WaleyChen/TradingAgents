"""Run a TradingAgents analysis for an API job."""

from __future__ import annotations

import os
from copy import deepcopy
from datetime import date
from typing import Any, NamedTuple

from api.progress import ProgressBoard, StageTracker, stages_for
from api.schemas import CreateAnalysisRequest
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph


class AnalysisResult(NamedTuple):
    """What a finished run hands back. A runner may return just the first two."""

    decision: str
    reports: dict[str, str | None]
    plan: dict[str, Any] | None = None
    plan_error: str | None = None
    summary: str | None = None
    summary_figures: list[dict[str, Any]] | None = None
    summary_problems: list[dict[str, str]] | None = None
    summary_glossary: list[dict[str, str]] | None = None


def build_config(request: CreateAnalysisRequest) -> dict[str, Any]:
    """Merge request overrides onto DEFAULT_CONFIG (env overrides already applied)."""
    config = deepcopy(DEFAULT_CONFIG)
    if "TRADINGAGENTS_STRUCTURED_PLAN" not in os.environ:
        config["structured_plan"] = True  # the API returns a checked plan unless told not to
    if "TRADINGAGENTS_REPORT_SUMMARY" not in os.environ:
        config["report_summary"] = True  # and a short summary
    if "TRADINGAGENTS_ESTIMATE_NEXT_EARNINGS" not in os.environ:
        config["estimate_next_earnings"] = True  # Yahoo's date, labelled an estimate, when none is sent
    if "TRADINGAGENTS_SUMMARY_READS_REPORTS" not in os.environ:
        config["summary_reads_reports"] = False  # written from the decision-makers' digests: half the cost
    if request.context is not None:
        config["context_data"] = request.context.model_dump(mode="json")
    config["memory_n_cross"] = 0  # a customer's report must not draw on other tickers' lessons

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


def run_analysis(request: CreateAnalysisRequest, board: ProgressBoard | None = None) -> AnalysisResult:
    """Execute the graph synchronously and return its decision, reports and plan."""
    config = build_config(request)
    tracker = StageTracker(stages_for(config), board)   # reports the stage and the usage to the job
    graph = TradingAgentsGraph(
        selected_analysts=list(request.analysts),
        debug=False,
        config=config,
        callbacks=[tracker],
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

    return AnalysisResult(
        decision_text,
        extract_reports(final_state),
        final_state.get("position_plan"),
        final_state.get("plan_error"),
        final_state.get("report_summary"),
        final_state.get("summary_figures"),
        final_state.get("summary_problems"),
        final_state.get("summary_glossary"),
    )
