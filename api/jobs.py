"""In-process async job queue for long-running analyses.

Single-process / single-worker friendly. For multi-replica Docker deploys,
replace this store with Redis or another shared backend.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime

from api.schemas import (
    AnalysisJobResponse,
    AnalysisLinks,
    AnalysisReports,
    CreateAnalysisRequest,
    JobStatus,
)
from api.service import run_analysis

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class AnalysisJob:
    id: str
    request: CreateAnalysisRequest
    status: JobStatus = JobStatus.queued
    decision: str | None = None
    reports: dict[str, str | None] | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def to_response(self) -> AnalysisJobResponse:
        return AnalysisJobResponse(
            id=self.id,
            status=self.status,
            ticker=self.request.ticker,
            trade_date=self.request.trade_date,
            asset_type=self.request.asset_type,
            analysts=list(self.request.analysts),
            decision=self.decision,
            reports=AnalysisReports(**self.reports) if self.reports else None,
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
        runner: Callable[[CreateAnalysisRequest], tuple[str, dict]] | None = None,
    ):
        self._jobs: dict[str, AnalysisJob] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="tradingagents-job",
        )
        self._runner = runner or run_analysis

    def submit(self, request: CreateAnalysisRequest) -> AnalysisJob:
        job = AnalysisJob(id=str(uuid.uuid4()), request=request)
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
            decision, reports = self._runner(request)
            with self._lock:
                job = self._jobs[job_id]
                job.decision = decision
                job.reports = reports
                job.status = JobStatus.completed
                job.completed_at = _utcnow()
            logger.info("Completed analysis job %s decision=%s", job_id, decision)
        except Exception as exc:
            logger.exception("Analysis job %s failed", job_id)
            with self._lock:
                job = self._jobs[job_id]
                job.status = JobStatus.failed
                job.error = str(exc)
                job.completed_at = _utcnow()
