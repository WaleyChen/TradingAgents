"""In-process async job queue for long-running analyses.

Single-process / single-worker friendly. For multi-replica Docker deploys,
replace this store with Redis or another shared backend.
"""

from __future__ import annotations

import inspect
import logging
import threading
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime

from api.progress import ProgressBoard, eta_seconds
from api.schemas import (
    AnalysisJobResponse,
    AnalysisLinks,
    AnalysisReports,
    CreateAnalysisRequest,
    JobStatus,
    Progress,
    StageUsage,
    Usage,
)
from api.service import AnalysisResult, run_analysis
from tradingagents.context import context_digest
from tradingagents.rating_label import plain_rating

logger = logging.getLogger(__name__)

FAILED_MESSAGE = "the analysis failed; see the service log"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class AnalysisJob:
    id: str
    request: CreateAnalysisRequest
    status: JobStatus = JobStatus.queued
    decision: str | None = None
    reports: dict[str, str | None] | None = None
    plan: dict | None = None
    plan_error: str | None = None
    summary: str | None = None
    summary_figures: list[dict] | None = None
    summary_problems: list[dict] | None = None
    error: str | None = None
    usage: dict[str, dict] | None = None
    board: ProgressBoard = field(default_factory=ProgressBoard, repr=False)
    history: deque = field(default_factory=lambda: deque(maxlen=0), repr=False)   # the manager's recent stage times
    created_at: datetime = field(default_factory=_utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def _progress(self) -> Progress | None:
        snap = self.board.snapshot() if self.status == JobStatus.running else None
        if snap is None:
            return None
        eta = eta_seconds(self.board.stages(), snap.stage, self.board.elapsed_in_stage(), list(self.history))
        return Progress(stage=snap.stage, stage_index=snap.stage_index, stages_total=snap.stages_total,
                        started_at=snap.started_at, analysts_done=snap.analysts_done, eta_seconds=eta)

    def _usage(self) -> Usage | None:
        if not self.usage:
            return None
        costs = [u["cost_usd"] for u in self.usage.values()]
        return Usage(stages={stage: StageUsage(**u) for stage, u in self.usage.items()},
                     total_cost_usd=round(sum(costs), 4) if all(c is not None for c in costs) else None)

    def to_response(self) -> AnalysisJobResponse:
        plain = plain_rating(self.decision)
        return AnalysisJobResponse(
            id=self.id,
            status=self.status,
            ticker=self.request.ticker,
            trade_date=self.request.trade_date,
            asset_type=self.request.asset_type,
            analysts=list(self.request.analysts),
            decision=self.decision,
            rating_label=plain.label if plain else None,
            rating_meaning=plain.meaning if plain else None,
            reports=AnalysisReports(**self.reports) if self.reports else None,
            plan=self.plan,
            plan_error=self.plan_error,
            context_digest=context_digest(self.request.context) if self.request.context else None,
            summary=self.summary,
            summary_figures=self.summary_figures,
            summary_problems=self.summary_problems,
            progress=self._progress(),
            usage=self._usage(),
            error=self.error,
            created_at=self.created_at,
            started_at=self.started_at,
            completed_at=self.completed_at,
            links=AnalysisLinks(self=f"/api/v1/analyses/{self.id}"),
        )


class JobManager:
    """Thread-safe job registry + bounded worker pool."""

    def __init__(
        self,
        max_workers: int = 1,
        runner: Callable[[CreateAnalysisRequest], AnalysisResult | tuple] | None = None,
    ):
        self._jobs: dict[str, AnalysisJob] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="tradingagents-job",
        )
        self._runner = runner or run_analysis
        self._takes_board = "board" in inspect.signature(self._runner).parameters
        self._history: deque = deque(maxlen=20)    # stage durations of recently finished jobs

    def submit(self, request: CreateAnalysisRequest) -> AnalysisJob:
        job = AnalysisJob(id=str(uuid.uuid4()), request=request, history=self._history)
        with self._lock:
            self._jobs[job.id] = job
        logger.info(
            "Queued analysis job %s ticker=%s date=%s",
            job.id,
            request.ticker,
            request.trade_date,
        )
        self._executor.submit(self._run_job, job.id)
        return job

    def get(self, job_id: str) -> AnalysisJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[AnalysisJob]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def shutdown(self, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def _record_durations(self, job: AnalysisJob) -> None:
        durations = job.board.durations()
        if durations:
            self._history.append(durations)

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = JobStatus.running
            job.started_at = _utcnow()
            request = job.request

        logger.info(
            "Started analysis job %s ticker=%s date=%s",
            job_id,
            request.ticker,
            request.trade_date,
        )
        try:
            run = self._runner(request, board=job.board) if self._takes_board else self._runner(request)
            decision, reports, plan, plan_error, summary, figures, problems = AnalysisResult(*run)
            with self._lock:
                job = self._jobs[job_id]
                job.decision = decision
                job.reports = reports
                job.plan = plan
                job.plan_error = plan_error
                job.summary = summary
                job.summary_figures = figures
                job.summary_problems = problems
                job.usage = job.board.stage_usage() or None
                self._record_durations(job)
                job.status = JobStatus.completed
                job.completed_at = _utcnow()
            logger.info("Completed analysis job %s decision=%s", job_id, decision)
        except Exception:
            logger.exception("Analysis job %s failed", job_id)
            with self._lock:
                job = self._jobs[job_id]
                job.status = JobStatus.failed
                job.error = FAILED_MESSAGE   # the detail is in the log: a provider error can echo a key
                job.usage = job.board.stage_usage() or None
                job.completed_at = _utcnow()
