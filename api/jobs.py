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

from api.progress import ProgressBoard, eta_seconds, median_total, queue_estimate
from api.schemas import (
    AnalysisJobResponse,
    AnalysisLinks,
    AnalysisReports,
    CreateAnalysisRequest,
    JobStatus,
    Progress,
    QueueInfo,
    StageUsage,
    Usage,
)
from api.service import AnalysisResult, run_analysis
from tradingagents.context import context_digest, derive_revenue_breakdown, derive_theme_backdrop
from tradingagents.rating_label import plain_rating

logger = logging.getLogger(__name__)

FAILED_MESSAGE = "the analysis failed; see the service log"
INTERRUPTED_MESSAGE = "the service restarted while this analysis was running; submit it again"


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
    summary_glossary: list[dict] | None = None
    error: str | None = None
    usage: dict[str, dict] | None = None
    durations: dict[str, float] | None = None   # seconds per stage, kept for the estimate
    board: ProgressBoard = field(default_factory=ProgressBoard, repr=False)
    history: deque = field(default_factory=lambda: deque(maxlen=0), repr=False)   # the manager's recent stage times
    queue_probe: Callable[[AnalysisJob], dict | None] | None = field(default=None, repr=False)
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

    def _queue(self) -> QueueInfo | None:
        info = self.queue_probe(self) if self.queue_probe and self.status == JobStatus.queued else None
        return QueueInfo(**info) if info else None

    def _usage(self) -> Usage | None:
        if not self.usage:
            return None
        costs = [u["cost_usd"] for u in self.usage.values()]
        return Usage(stages={stage: StageUsage(**u) for stage, u in self.usage.items()},
                     total_cost_usd=round(sum(costs), 4) if all(c is not None for c in costs) else None)

    def _revenue_breakdown(self) -> dict | None:
        context = self.request.context
        return derive_revenue_breakdown(context.revenue_breakdown) if context and context.revenue_breakdown else None

    def _theme_backdrop(self) -> dict | None:
        context = self.request.context
        return derive_theme_backdrop(context.theme_backdrop) if context and context.theme_backdrop else None

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
            glossary=self.summary_glossary,
            revenue_breakdown=self._revenue_breakdown(),
            theme_backdrop=self._theme_backdrop(),
            queue=self._queue(),
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
        store=None,
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
        self._store = store
        self._workers = max_workers
        if store is not None:
            self._restore(store)

    @property
    def persisted(self) -> bool:
        return self._store is not None

    def _queue_info(self, job: AnalysisJob) -> dict | None:
        """Where a waiting job stands: its place, who is ahead, and when it should start and finish."""
        with self._lock:
            queued = sorted((j for j in self._jobs.values() if j.status == JobStatus.queued),
                            key=lambda j: j.created_at)
            running = [j for j in self._jobs.values() if j.status == JobStatus.running]
        ids = [j.id for j in queued]
        if job.id not in ids:
            return None
        ahead = ids.index(job.id)
        remaining = [(p.eta_seconds if (p := j._progress()) else None) for j in running]
        starts, finishes = queue_estimate(self._workers, remaining, ahead, median_total(list(self._history)))
        return {"position": ahead + 1, "jobs_ahead": len(running) + ahead,
                "starts_in_seconds": starts, "eta_seconds": finishes}

    def _save(self, job: AnalysisJob) -> None:
        if self._store is None:
            return
        try:
            self._store.save(job)
        except Exception:  # a full disk must not fail the analysis
            logger.exception("Could not save job %s", job.id)

    def _restore(self, store) -> None:
        """Load the jobs from before the restart; what was in flight cannot continue, so it is failed."""
        for job in store.load_all():
            job.history = self._history
            if job.status in (JobStatus.queued, JobStatus.running):
                job.status, job.error, job.completed_at = JobStatus.failed, INTERRUPTED_MESSAGE, _utcnow()
                self._save(job)
            elif job.status == JobStatus.completed and job.durations:
                self._history.append(job.durations)
            self._jobs[job.id] = job

    def submit(self, request: CreateAnalysisRequest) -> AnalysisJob:
        job = AnalysisJob(id=str(uuid.uuid4()), request=request, history=self._history, queue_probe=self._queue_info)
        with self._lock:
            self._jobs[job.id] = job
            self._save(job)
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
            job.durations = durations
            self._history.append(durations)

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = JobStatus.running
            job.started_at = _utcnow()
            request = job.request
            self._save(job)

        logger.info(
            "Started analysis job %s ticker=%s date=%s",
            job_id,
            request.ticker,
            request.trade_date,
        )
        try:
            run = self._runner(request, board=job.board) if self._takes_board else self._runner(request)
            decision, reports, plan, plan_error, summary, figures, problems, glossary = AnalysisResult(*run)
            with self._lock:
                job = self._jobs[job_id]
                job.decision = decision
                job.reports = reports
                job.plan = plan
                job.plan_error = plan_error
                job.summary = summary
                job.summary_figures = figures
                job.summary_problems = problems
                job.summary_glossary = glossary
                job.usage = job.board.stage_usage() or None
                self._record_durations(job)
                job.status = JobStatus.completed
                job.completed_at = _utcnow()
                self._save(job)
            logger.info("Completed analysis job %s decision=%s", job_id, decision)
        except Exception:
            logger.exception("Analysis job %s failed", job_id)
            with self._lock:
                job = self._jobs[job_id]
                job.status = JobStatus.failed
                job.error = FAILED_MESSAGE   # the detail is in the log: a provider error can echo a key
                job.usage = job.board.stage_usage() or None
                job.completed_at = _utcnow()
                self._save(job)
