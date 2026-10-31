"""FastAPI application for TradingAgents."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status

from api.auth import require_api_key
from api.jobs import JobManager
from api.schemas import (
    AnalysisAccepted,
    AnalysisJobResponse,
    AnalysisLinks,
    CreateAnalysisRequest,
    HealthResponse,
    JobStatus,
)


def _max_workers() -> int:
    raw = os.environ.get("TRADINGAGENTS_API_MAX_WORKERS", "1")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"TRADINGAGENTS_API_MAX_WORKERS must be an integer, got {raw!r}"
        ) from exc
    if value < 1:
        raise ValueError("TRADINGAGENTS_API_MAX_WORKERS must be >= 1")
    return value


def create_app(job_manager: JobManager | None = None) -> FastAPI:
    manager = job_manager or JobManager(max_workers=_max_workers())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.job_manager = manager
        yield
        manager.shutdown(wait=False)

    app = FastAPI(
        title="TradingAgents API",
        version="0.1.0",
        description=(
            "HTTP API around TradingAgentsGraph. Submit an analysis job, then "
            "poll until status is completed or failed. Designed for Docker + Rails."
        ),
        lifespan=lifespan,
    )

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    def health() -> HealthResponse:
        return HealthResponse()

    @app.post(
        "/api/v1/analyses",
        response_model=AnalysisAccepted,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_api_key)],
        tags=["analyses"],
    )
    def create_analysis(request: CreateAnalysisRequest) -> AnalysisAccepted:
        job = manager.submit(request)
        return AnalysisAccepted(
            id=job.id,
            status=JobStatus.queued,
            ticker=job.request.ticker,
            trade_date=job.request.trade_date,
            links=AnalysisLinks(self=f"/api/v1/analyses/{job.id}"),
        )

    @app.get(
        "/api/v1/analyses",
        response_model=list[AnalysisJobResponse],
        dependencies=[Depends(require_api_key)],
        tags=["analyses"],
    )
    def list_analyses() -> list[AnalysisJobResponse]:
        """List jobs in this API process (newest first). Useful while debugging queues."""
        return [job.to_response() for job in manager.list_jobs()]

    @app.get(
        "/api/v1/analyses/{job_id}",
        response_model=AnalysisJobResponse,
        dependencies=[Depends(require_api_key)],
        tags=["analyses"],
    )
    def get_analysis(job_id: str) -> AnalysisJobResponse:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Analysis {job_id!r} not found",
            )
        return job.to_response()

    return app
