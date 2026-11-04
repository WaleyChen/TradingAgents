"""Request/response models for the TradingAgents HTTP API."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from tradingagents.plan import Plan

AnalystName = Literal["market", "social", "news", "fundamentals"]
AssetType = Literal["stock", "crypto"]


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class CreateAnalysisRequest(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=32, examples=["PLTR", "0700.HK"])
    trade_date: date = Field(..., examples=["2026-07-26"])
    analysts: list[AnalystName] = Field(
        default_factory=lambda: ["market", "social", "news", "fundamentals"],
        description="Analyst team members to run (order is normalized).",
    )
    asset_type: AssetType = "stock"
    max_debate_rounds: int | None = Field(default=None, ge=1, le=10)
    max_risk_discuss_rounds: int | None = Field(default=None, ge=1, le=10)
    llm_provider: str | None = None
    deep_think_llm: str | None = None
    quick_think_llm: str | None = None
    output_language: str | None = None
    save_reports: bool = True

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        ticker = value.strip().upper()
        if not ticker:
            raise ValueError("ticker must not be empty")
        return ticker

    @field_validator("analysts")
    @classmethod
    def require_analysts(cls, value: list[AnalystName]) -> list[AnalystName]:
        if not value:
            raise ValueError("at least one analyst is required")
        # Preserve stable analyst order used elsewhere in the project.
        order = ["market", "social", "news", "fundamentals"]
        selected = set(value)
        return [a for a in order if a in selected]  # type: ignore[return-value]


class AnalysisLinks(BaseModel):
    self: str


class AnalysisAccepted(BaseModel):
    id: str
    status: JobStatus
    ticker: str
    trade_date: date
    links: AnalysisLinks


class AnalysisReports(BaseModel):
    market_report: str | None = None
    sentiment_report: str | None = None
    news_report: str | None = None
    fundamentals_report: str | None = None
    investment_plan: str | None = None
    trader_investment_plan: str | None = None
    final_trade_decision: str | None = None


class AnalysisJobResponse(BaseModel):
    id: str
    status: JobStatus
    ticker: str
    trade_date: date
    asset_type: AssetType
    analysts: list[AnalystName]
    decision: str | None = None
    reports: AnalysisReports | None = None
    plan: Plan | None = Field(
        default=None,
        description="Checked position plan; null when none passed the checks (see plan_error).",
    )
    plan_error: str | None = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    links: AnalysisLinks


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str = "tradingagents-api"
