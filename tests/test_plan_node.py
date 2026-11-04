"""The Plan step in the graph and the plan in the API response."""

from __future__ import annotations

import time
from datetime import date
from unittest.mock import MagicMock

import pytest

from api.jobs import JobManager
from api.schemas import CreateAnalysisRequest
from api.service import AnalysisResult
from tests.test_plan import GOOD, READINGS
from tests.test_plan_generate import FakeLLM, _bad, _plan
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.setup import GraphSetup
from tradingagents.plan import node as node_mod
from tradingagents.plan.node import create_plan_node

STATE = {"company_of_interest": "ACME", "trade_date": "2026-11-02",
         "final_trade_decision": "Rating: Overweight. Add on dips.", "final_rating": "Overweight"}


@pytest.fixture
def readings(monkeypatch):
    monkeypatch.setattr(node_mod, "compute_readings", lambda symbol, as_of: READINGS)


@pytest.mark.unit
def test_a_passing_plan_lands_in_the_state_as_plain_data(readings):
    out = create_plan_node(FakeLLM(_plan()))(STATE)
    assert out["plan_error"] is None
    assert out["position_plan"]["as_of_price"] == GOOD["as_of_price"]
    assert out["position_plan"]["entries"][0]["trigger"]["price"]["when"] == "at_or_below"


@pytest.mark.unit
def test_a_plan_that_never_passes_leaves_no_plan_and_the_reason(readings):
    out = create_plan_node(FakeLLM(_bad(), _bad(), _bad()))(STATE)
    assert out["position_plan"] is None and "as_of_price" in out["plan_error"]


@pytest.mark.unit
def test_missing_market_data_means_no_plan_not_a_crashed_run(monkeypatch):
    def boom(symbol, as_of):
        raise RuntimeError("vendor down")

    monkeypatch.setattr(node_mod, "compute_readings", boom)
    out = create_plan_node(FakeLLM())(STATE)
    assert out["position_plan"] is None
    assert "market data" in out["plan_error"] and "vendor down" not in out["plan_error"]


@pytest.mark.unit
def test_an_unexpected_failure_in_the_step_does_not_fail_the_run(readings):
    class Exploding:
        def with_structured_output(self, schema):
            raise RuntimeError("boom sk-SECRET")

    out = create_plan_node(Exploding())(STATE)
    assert out["position_plan"] is None and "sk-SECRET" not in out["plan_error"]


@pytest.mark.unit
@pytest.mark.parametrize("with_plan", [True, False])
def test_the_plan_step_follows_the_portfolio_manager_only_when_given(with_plan):
    setup = GraphSetup(MagicMock(), MagicMock(), ConditionalLogic(), 3)
    workflow = setup.setup_graph(plan_node=(lambda s: {}) if with_plan else None)
    assert ("Plan" in workflow.nodes) is with_plan
    assert ("Portfolio Manager", "Plan") in workflow.edges if with_plan else True


def _wait(manager, job_id):
    for _ in range(100):
        job = manager.get(job_id)
        if job.status in ("completed", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def _request():
    return CreateAnalysisRequest(ticker="ACME", trade_date=date(2026, 11, 2))


@pytest.mark.unit
def test_the_api_response_carries_the_plan_and_its_error():
    plan = _plan().model_dump(mode="json")
    with_plan = JobManager(runner=lambda r: AnalysisResult("Overweight", {}, plan, None))
    got = _wait(with_plan, with_plan.submit(_request()).id).to_response()
    assert got.plan.entries[0].portfolio_pct == 3.0 and got.plan_error is None

    without = JobManager(runner=lambda r: AnalysisResult("Overweight", {}, None, "did not pass"))
    got = _wait(without, without.submit(_request()).id).to_response()
    assert got.plan is None and got.plan_error == "did not pass"


@pytest.mark.unit
def test_a_runner_that_returns_only_a_decision_and_reports_still_works():
    manager = JobManager(runner=lambda r: ("Hold", {}))
    got = _wait(manager, manager.submit(_request()).id).to_response()
    assert got.status == "completed" and got.plan is None


@pytest.mark.unit
def test_the_api_turns_the_plan_step_on_unless_the_environment_says_otherwise(monkeypatch):
    from api.service import build_config

    monkeypatch.delenv("TRADINGAGENTS_STRUCTURED_PLAN", raising=False)
    assert build_config(_request())["structured_plan"] is True
    monkeypatch.setenv("TRADINGAGENTS_STRUCTURED_PLAN", "0")
    assert build_config(_request())["structured_plan"] is False
