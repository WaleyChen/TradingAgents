"""Progress, time estimates and per-stage usage for a running job."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import date
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from api.jobs import JobManager
from api.progress import (
    NODE_STAGE,
    ProgressBoard,
    StageTracker,
    eta_seconds,
    stages_for,
)
from api.schemas import CreateAnalysisRequest, JobStatus
from api.service import AnalysisResult
from tests.test_graph_end_to_end import offline  # noqa: F401
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.setup import GraphSetup


def _request():
    return CreateAnalysisRequest(ticker="ACME", trade_date=date(2026, 11, 2))


def _start(tracker, node):
    run = uuid.uuid4()
    tracker.on_chain_start({}, {}, run_id=run, metadata={"langgraph_node": node})
    return run


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _tracker(stages=("analysts", "debate", "decision", "plan", "summary"), clock=None):
    board = ProgressBoard()
    return StageTracker(list(stages), board, clock=clock or Clock()), board


@pytest.mark.unit
def test_the_stages_follow_the_config():
    assert stages_for({"structured_plan": True, "report_summary": True}) == [
        "analysts", "debate", "decision", "plan", "summary"]
    assert stages_for({"structured_plan": False, "report_summary": True}) == [
        "analysts", "debate", "decision", "summary"]
    assert stages_for({}) == ["analysts", "debate", "decision"]


@pytest.mark.unit
def test_every_node_in_the_graph_belongs_to_a_stage():
    setup = GraphSetup(MagicMock(), MagicMock(), ConditionalLogic(), 3)
    workflow = setup.setup_graph(memory_node=lambda s: {}, plan_node=lambda s: {}, summary_node=lambda s: {})
    missing = [n for n in workflow.nodes if not n.startswith("__") and n not in NODE_STAGE]
    assert missing == [], f"map these graph nodes to a stage in api/progress.py: {missing}"


@pytest.mark.unit
def test_the_stage_advances_as_the_graph_moves_and_never_goes_back():
    tracker, board = _tracker()
    _start(tracker, "Memory Log")
    market = _start(tracker, "Market Analyst")
    assert board.snapshot().stage == "analysts" and board.snapshot().stage_index == 1
    _start(tracker, "Bull Researcher")
    assert board.snapshot().stage == "debate" and board.snapshot().stage_index == 2
    tracker.on_chain_end({}, run_id=market)                      # a slow analyst finishing late
    assert board.snapshot().stage == "debate"
    _start(tracker, "Portfolio Manager")
    _start(tracker, "Plan")
    _start(tracker, "Summary")
    snap = board.snapshot()
    assert (snap.stage, snap.stage_index, snap.stages_total) == ("summary", 5, 5)


@pytest.mark.unit
def test_analysts_are_ticked_off_as_they_finish():
    tracker, board = _tracker()
    runs = {n: _start(tracker, n) for n in ("Market Analyst", "News Analyst", "Sentiment Analyst")}
    assert board.snapshot().analysts_done == []
    tracker.on_chain_end({}, run_id=runs["News Analyst"])
    tracker.on_chain_end({}, run_id=runs["Market Analyst"])
    assert board.snapshot().analysts_done == ["market", "news"]
    tracker.on_chain_end({}, run_id=uuid.uuid4())                # an unknown run is ignored


@pytest.mark.unit
def test_a_node_that_is_not_a_stage_marker_is_ignored():
    tracker, board = _tracker()
    tracker.on_chain_start({}, {}, run_id=uuid.uuid4(), metadata={"langgraph_node": "agent"})
    tracker.on_chain_start({}, {}, run_id=uuid.uuid4(), metadata=None)
    assert board.snapshot() is None


@pytest.mark.unit
def test_time_in_each_stage_is_recorded():
    clock = Clock()
    tracker, board = _tracker(clock=clock)
    _start(tracker, "Market Analyst")
    clock.now += 120
    _start(tracker, "Bull Researcher")
    clock.now += 200
    _start(tracker, "Portfolio Manager")
    clock.now += 30
    assert board.durations() == {"analysts": 120.0, "debate": 200.0, "decision": 30.0}


@pytest.mark.unit
def test_the_estimate_is_the_remaining_stages_at_their_recent_median():
    history = [{"analysts": 100, "debate": 200, "decision": 20}, {"analysts": 140, "debate": 220, "decision": 40},
               {"analysts": 120, "debate": 180, "decision": 30}]
    stages = ["analysts", "debate", "decision"]
    # in the debate for 50s: 150 left of the median 200, plus the median 30 for the decision
    assert eta_seconds(stages, "debate", 50.0, history) == 150 + 30
    assert eta_seconds(stages, "decision", 100.0, history) == 0           # past the median: nothing but now
    assert eta_seconds(stages, "debate", 50.0, []) is None
    assert eta_seconds(stages, None, 0.0, history) is None


@pytest.mark.unit
def test_a_stage_with_no_history_leaves_the_estimate_unknown():
    assert eta_seconds(["analysts", "plan"], "analysts", 10.0, [{"analysts": 100}]) is None


def _llm_end(tracker, model="m", tokens_in=100, tokens_out=10):
    message = AIMessage(content="x", usage_metadata={"input_tokens": tokens_in, "output_tokens": tokens_out,
                                                     "total_tokens": tokens_in + tokens_out},
                        response_metadata={"model_name": model})
    tracker.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]))


@pytest.mark.unit
def test_token_usage_is_recorded_against_the_stage_it_happened_in():
    tracker, _ = _tracker()
    _start(tracker, "Market Analyst")
    _llm_end(tracker, tokens_in=1000)
    _llm_end(tracker, tokens_in=500)
    _start(tracker, "Bull Researcher")
    _llm_end(tracker, tokens_in=200, tokens_out=50)
    usage = tracker.stage_usage()
    assert usage["analysts"]["calls"] == 2 and usage["analysts"]["input_tokens"] == 1500
    assert usage["debate"] == {**usage["debate"], "calls": 1, "input_tokens": 200, "output_tokens": 50}
    assert "cost_usd" in usage["debate"]
    assert tracker.get_stats()["tokens_in"] == 1700             # the run total still works


@pytest.mark.unit
def test_a_model_with_no_known_price_has_no_cost_rather_than_a_wrong_one():
    tracker, _ = _tracker()
    _start(tracker, "Market Analyst")
    _llm_end(tracker, model="made-up-model-xyz")
    assert tracker.stage_usage()["analysts"]["cost_usd"] is None


def _wait_for(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met")


@pytest.mark.unit
def test_a_running_job_reports_its_progress_and_a_finished_one_reports_usage():
    release, entered = threading.Event(), threading.Event()

    def runner(request, board=None):
        tracker = StageTracker(["analysts", "debate"], board)
        _start(tracker, "Market Analyst")
        _llm_end(tracker)
        _start(tracker, "Bull Researcher")
        entered.set()
        release.wait(3)
        return AnalysisResult("Hold", {})

    manager = JobManager(runner=runner)
    job = manager.submit(_request())
    assert entered.wait(3)
    running = manager.get(job.id).to_response()
    assert running.status == JobStatus.running and running.usage is None
    assert (running.progress.stage, running.progress.stage_index, running.progress.stages_total) == ("debate", 2, 2)
    assert running.progress.started_at is not None
    release.set()
    _wait_for(lambda: manager.get(job.id).status == JobStatus.completed)
    done = manager.get(job.id).to_response()
    assert done.progress is None and done.usage.stages["analysts"].calls == 1
    assert set(done.usage.stages) == {"analysts"}            # a stage with no model calls has no entry


@pytest.mark.unit
def test_a_queued_job_has_no_progress():
    gate = threading.Event()
    manager = JobManager(max_workers=1, runner=lambda r: (gate.wait(3), AnalysisResult("Hold", {}))[1])
    first, second = manager.submit(_request()), manager.submit(_request())
    assert manager.get(second.id).to_response().progress is None
    gate.set()
    _wait_for(lambda: manager.get(second.id).status == JobStatus.completed)
    assert first.id != second.id


@pytest.mark.unit
def test_a_job_that_started_has_an_estimate_once_earlier_jobs_have_finished():
    clock = Clock()
    release = threading.Event()
    durations = []

    def runner(request, board=None):
        tracker = StageTracker(["analysts", "debate"], board, clock=clock)
        _start(tracker, "Market Analyst")
        clock.now += 100
        _start(tracker, "Bull Researcher")
        clock.now += 60
        if durations:
            release.wait(3)
        durations.append(1)
        return AnalysisResult("Hold", {})

    manager = JobManager(runner=runner)
    first = manager.submit(_request())
    _wait_for(lambda: manager.get(first.id).status == JobStatus.completed)
    second = manager.submit(_request())
    _wait_for(lambda: manager.get(second.id).to_response().progress is not None)
    eta = manager.get(second.id).to_response().progress.eta_seconds
    assert eta is not None and eta >= 0
    release.set()


@pytest.mark.unit
def test_a_failed_job_never_shows_the_exception_text():
    def runner(request, board=None):
        raise RuntimeError("401 Incorrect API key: sk-SECRET-123")

    manager = JobManager(runner=runner)
    job = manager.submit(_request())
    _wait_for(lambda: manager.get(job.id).status == JobStatus.failed)
    error = manager.get(job.id).to_response().error
    assert "sk-SECRET" not in error and "API key" not in error and error


@pytest.mark.unit
def test_a_real_graph_run_reaches_every_stage_and_counts_each_model_call_once(tmp_path, monkeypatch, offline):  # noqa: F811
    import copy

    from tests.test_graph_end_to_end import TRADE_DATE, ScriptedModel, _Client
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph import trading_graph

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(results_dir=str(tmp_path / "results"), data_cache_dir=str(tmp_path / "cache"),
               memory_log_path=str(tmp_path / "log.md"), structured_plan=False, report_summary=False)
    model = ScriptedModel()
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda config, tier, **k: _Client(model))
    board = ProgressBoard()
    tracker = StageTracker(["analysts", "debate", "decision"], board)
    graph = trading_graph.TradingAgentsGraph(config=cfg, debug=False, callbacks=[tracker])
    graph.propagate("NVDA", TRADE_DATE)

    assert set(board.durations()) == {"analysts", "debate", "decision"}
    assert board.snapshot().stage == "decision" and board.snapshot().analysts_done
    assert tracker.get_stats()["llm_calls"] == len(model.calls)        # not doubled by the graph-level handler
