"""FastAPI application for TradingAgents."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, HTTPException, status

from api.auth import require_api_key
from api.jobs import JobManager
from api.schemas import (
    AnalysisAccepted,
    AnalysisJobResponse,
    AnalysisLinks,
    ContextCheckResponse,
    CreateAnalysisRequest,
    HealthResponse,
    JobStatus,
)
from tradingagents.context import (
    Context,
    check_context as run_context_checks,
    context_digest,
    derive_metrics,
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
        "/api/v1/context/check",
        response_model=ContextCheckResponse,
        dependencies=[Depends(require_api_key)],
        tags=["analyses"],
    )
    def check_context(context: Context, ticker: str | None = None,
                      trade_date: date | None = None) -> ContextCheckResponse:
        """Run the checks a request's context must pass, without starting an analysis.

        Free: no model call. Give ``ticker`` and ``trade_date`` to apply the same ties
        to the request that ``POST /api/v1/analyses`` applies. A malformed context is
        still a 422; a well-formed one that fails a check is a 200 with every problem listed.
        """
        ticker = ticker.strip().upper() if ticker else None
        problems = run_context_checks(context, ticker=ticker, trade_date=trade_date)
        metrics = derive_metrics(context.financials, context.price, context.company) if context.financials else {}
        return ContextCheckResponse(
            ok=not problems, metrics=metrics, context_digest=context_digest(context),
            problems=[{"code": p.code, "path": p.path, "message": p.message} for p in problems])

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
            context_digest=context_digest(job.request.context) if job.request.context else None,
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
