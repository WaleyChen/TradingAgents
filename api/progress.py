"""Where a running job is, how long it should take, and what each stage cost.

A ``StageTracker`` is a LangChain callback handler: the graph reports each node as it
starts, and the tracker turns that into a stage. The analysts run in parallel, so they
are one stage (``analysts``) with a list of which have finished. A stage never goes
backwards, so an analyst that finishes late does not move the job back.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from cli.stats_handler import StatsCallbackHandler
from cli.usage_report import build_usage_rows, load_pricing_overrides

# Every node of the graph, by the stage it belongs to. A test fails if a node is missing.
NODE_STAGE = {
    "Memory Log": "analysts", "Market Analyst": "analysts", "Sentiment Analyst": "analysts",
    "News Analyst": "analysts", "Fundamentals Analyst": "analysts",
    "Bull Researcher": "debate", "Bear Researcher": "debate", "Research Manager": "debate",
    "Trader": "debate", "Aggressive Analyst": "debate", "Neutral Analyst": "debate",
    "Conservative Analyst": "debate",
    "Portfolio Manager": "decision", "Plan": "plan", "Summary": "summary",
}
_ANALYST_KEY = {"Market Analyst": "market", "Sentiment Analyst": "sentiment", "News Analyst": "news",
                "Fundamentals Analyst": "fundamentals"}


def stages_for(config: dict[str, Any]) -> list[str]:
    """The stages a run with this config goes through, in order."""
    stages = ["analysts", "debate", "decision"]
    if config.get("structured_plan"):
        stages.append("plan")
    if config.get("report_summary"):
        stages.append("summary")
    return stages


@dataclass(frozen=True)
class Snapshot:
    stage: str
    stage_index: int
    stages_total: int
    started_at: datetime
    analysts_done: list[str]


def eta_seconds(stages: list[str], stage: str | None, elapsed_in_stage: float,
                history: list[dict[str, float]]) -> int | None:
    """Time left: the rest of this stage and every later stage, each at its recent median.

    ``history`` is the per-stage durations of recently finished jobs. A stage nobody has
    timed yet makes the estimate unknown rather than a guess.
    """
    if stage is None or stage not in stages or not history:
        return None
    median: dict[str, float] = {}
    for s in stages[stages.index(stage):]:
        seen = [h[s] for h in history if s in h]
        if not seen:
            return None
        median[s] = statistics.median(seen)
    left = max(median[stage] - elapsed_in_stage, 0.0)
    left += sum(median[s] for s in stages[stages.index(stage) + 1:])
    return round(left)


def median_total(history: list[dict[str, float]]) -> float | None:
    """How long a whole job usually takes: the median of the recent finished jobs' stage times added up."""
    totals = [sum(h.values()) for h in history if h]
    return statistics.median(totals) if totals else None


def queue_estimate(workers: int, running_remaining: list[float | None], ahead_queued: int,
                   median: float | None) -> tuple[int | None, int | None]:
    """(seconds until a waiting job starts, seconds until it finishes), given who is running and who is ahead.

    Each worker is a slot that frees up when its running job ends. A queued job takes the slot that frees
    first and holds it for a typical job. With no history the times are unknown rather than guessed, except
    that a free slot means it starts at once.
    """
    free = max(workers - len(running_remaining), 0)
    if median is None:
        return (0 if free else None), None
    slots = sorted([median if r is None else float(r) for r in running_remaining] + [0.0] * free)
    for _ in range(ahead_queued):
        slots[0] += median
        slots.sort()
    return round(slots[0]), round(slots[0] + median)


class ProgressBoard:
    """A job's view of its tracker; empty until the tracker is attached and the graph has started."""

    def __init__(self) -> None:
        self._tracker: StageTracker | None = None

    def attach(self, tracker: StageTracker) -> None:
        self._tracker = tracker

    def snapshot(self) -> Snapshot | None:
        return self._tracker.snapshot() if self._tracker else None

    def elapsed_in_stage(self) -> float:
        return self._tracker.elapsed_in_stage() if self._tracker else 0.0

    def stages(self) -> list[str]:
        return list(self._tracker.stages) if self._tracker else []

    def durations(self) -> dict[str, float]:
        return self._tracker.durations() if self._tracker else {}

    def stage_usage(self) -> dict[str, dict[str, Any]]:
        return self._tracker.stage_usage() if self._tracker else {}


class StageTracker(StatsCallbackHandler):
    """Tracks the stage of a run, how long each took, and the token usage inside each."""

    raise_error = False   # a bug here must never fail the analysis

    def __init__(self, stages: list[str], board: ProgressBoard | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self.stages, self._clock = list(stages), clock
        self._lock_stage = threading.Lock()
        self._index = -1
        self._began: dict[str, float] = {}
        self._finished: dict[str, float] = {}
        self._started_at: datetime | None = None
        self._runs: dict[UUID, str] = {}
        self._analysts_done: set[str] = set()
        self._stage_stats: dict[str, StatsCallbackHandler] = {}
        if board is not None:
            board.attach(self)

    # --- stage ---------------------------------------------------------------

    def on_chain_start(self, serialized: dict[str, Any], inputs: Any, *, run_id: UUID,
                       metadata: dict[str, Any] | None = None, **kwargs: Any) -> None:
        node = (metadata or {}).get("langgraph_node")
        stage = NODE_STAGE.get(node)
        if stage is None or stage not in self.stages:
            return
        with self._lock_stage:
            if node in _ANALYST_KEY:
                self._runs[run_id] = node
            index = self.stages.index(stage)
            if index > self._index:
                now = self._clock()
                if self._index >= 0:
                    previous = self.stages[self._index]
                    self._finished[previous] = now - self._began[previous]
                self._index, self._began[stage] = index, now
                self._started_at = self._started_at or datetime.now(UTC)

    def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        with self._lock_stage:
            node = self._runs.pop(run_id, None)
            if node is not None:
                self._analysts_done.add(_ANALYST_KEY[node])

    def snapshot(self) -> Snapshot | None:
        with self._lock_stage:
            if self._index < 0 or self._started_at is None:
                return None
            return Snapshot(self.stages[self._index], self._index + 1, len(self.stages),
                            self._started_at, sorted(self._analysts_done))

    def elapsed_in_stage(self) -> float:
        with self._lock_stage:
            return self._clock() - self._began[self.stages[self._index]] if self._index >= 0 else 0.0

    def durations(self) -> dict[str, float]:
        """Seconds spent in each stage so far, the current one included."""
        with self._lock_stage:
            done = dict(self._finished)
            if self._index >= 0:
                current = self.stages[self._index]
                done[current] = self._clock() - self._began[current]
            return done

    # --- usage ---------------------------------------------------------------

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        super().on_llm_end(response, **kwargs)
        with self._lock_stage:
            stage = self.stages[self._index] if self._index >= 0 else self.stages[0]
            handler = self._stage_stats.setdefault(stage, StatsCallbackHandler())
        handler.on_llm_end(response, **kwargs)

    def stage_usage(self) -> dict[str, dict[str, Any]]:
        """Calls, tokens and an estimated cost per stage; no cost when a model has no known price."""
        overrides = load_pricing_overrides()
        usage = {}
        for stage, handler in self._stage_stats.items():
            rows, total = build_usage_rows(handler.get_model_usage(), overrides)
            usage[stage] = {
                "calls": sum(r["calls"] for r in rows),
                "input_tokens": sum(r["input_tokens"] for r in rows),
                "output_tokens": sum(r["output_tokens"] for r in rows),
                "cost_usd": round(total, 4) if rows and all(r["cost"] is not None for r in rows) else None,
            }
        return usage
