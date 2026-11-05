"""Which tools an analyst is offered when verified data already covers the facts it would fetch.

An analyst that pulls its own copy of a figure the caller already verified can disagree with it,
and the report then carries both (a MACD of 13.23 and of 14.08 in one report). With verified
data the tools that fetch the same facts are not offered, which also saves tool rounds. An
analyst is never left without a tool.
"""

from __future__ import annotations

from tradingagents.context import Context

_STATEMENTS = ("get_balance_sheet", "get_cashflow", "get_income_statement")
_INDICATORS = ("get_indicators", "get_verified_market_snapshot")


def tools_for(key: str, tools: tuple, context: Context | None) -> tuple:
    """``tools`` for the analyst ``key``, without those the supplied ``context`` makes redundant."""
    if context is None:
        return tools
    drop: set[str] = set()
    if key == "fundamentals":
        if context.financials is not None:
            drop.update(_STATEMENTS)
        if context.ownership is not None:
            drop.add("get_insider_transactions")
    elif key == "market" and context.technicals is not None:
        drop.update(_INDICATORS)
    kept = tuple(tool for tool in tools if tool.name not in drop)
    return kept or tools
