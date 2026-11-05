"""The graph step that turns the final decision into a checked position plan."""

from __future__ import annotations

import logging
from dataclasses import asdict

from tradingagents.context import Events, Technicals
from tradingagents.plan.generate import FAILED, generate_plan
from tradingagents.plan.readings import compute_readings, readings_from

logger = logging.getLogger(__name__)


def create_plan_node(llm, events: Events | None = None, technicals: Technicals | None = None):
    def plan_node(state) -> dict:
        ticker, trade_date = state["company_of_interest"], state["trade_date"]
        try:
            readings = readings_from(technicals, ticker, trade_date, fetch=compute_readings)
        except Exception as exc:  # no data, vendor down: no plan, but the report stands
            logger.warning("Plan: no verified readings for %s on %s (%s)", ticker, trade_date, exc)
            return {"position_plan": None, "verified_readings": None,
                    "plan_error": "no verified market data to check a plan against"}

        try:
            outcome = generate_plan(
                llm, decision=state["final_trade_decision"],
                rating=state.get("final_rating", ""), readings=readings, events=events,
            )
        except Exception:  # the report is done; a bug here must not discard it
            logger.exception("Plan: unexpected failure for %s on %s", ticker, trade_date)
            return {"position_plan": None, "plan_error": FAILED, "verified_readings": asdict(readings)}
        plan = outcome.plan.model_dump(mode="json") if outcome.plan else None
        return {"position_plan": plan, "plan_error": outcome.error, "verified_readings": asdict(readings)}

    return plan_node
