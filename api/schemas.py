"""Request/response models for the TradingAgents HTTP API."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from tradingagents.context import SUPPORTED_SCHEMA_VERSIONS, Context, check_context
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
    context: Context | None = Field(
        default=None,
        description="Verified data from the caller (see docs/API.md); refused when its checks fail.",
    )

    @model_validator(mode="after")
    def context_must_be_consistent(self) -> CreateAnalysisRequest:
        if self.context is not None:
            problems = check_context(self.context, ticker=self.ticker, trade_date=self.trade_date)
            if problems:
                raise PydanticCustomError(
                    "context_inconsistent", "context is inconsistent: {summary}",
                    {"summary": "; ".join(p.message for p in problems),
                     "problems": [{"code": p.code, "path": p.path, "message": p.message} for p in problems]},
                )
        return self

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
    context_digest: str | None = Field(
        default=None, description="Fingerprint of the context as the service parsed it; null when none was sent.")


class SummaryFigure(BaseModel):
    label: str
    value: float
    unit: Literal["usd", "usd_millions", "usd_billions", "pct", "x", "number"]
    source: Literal["context", "readings", "plan", "filing", "analyst_report", "derived"]
    period: str | None = None
    verified: bool = Field(
        description="True only when the figure was checked in code against the supplied context, "
                    "the market readings or the position plan.")


class SummaryProblem(BaseModel):
    code: str
    subject: str
    message: str
    severity: Literal["error", "warning"] = Field(
        default="error",
        description="error: a figure claims verified data it is not in; warning: a figure was left "
                    "out of the figures list, so it is unverified.")


class Progress(BaseModel):
    stage: Literal["analysts", "debate", "decision", "plan", "summary"] = Field(
        description="Where the run is. The analysts run in parallel, so they are one stage.")
    stage_index: int = Field(description="1-based position of the stage among the ones this run has.")
    stages_total: int
    started_at: datetime
    analysts_done: list[str] = Field(description="Which analysts have finished (market, news, fundamentals, sentiment).")
    eta_seconds: int | None = Field(
        description="Seconds left, from the median stage times of recent finished jobs; null until there is history.")


class QueueInfo(BaseModel):
    position: int = Field(description="1 is next to start.")
    jobs_ahead: int = Field(description="Jobs running or waiting in front of this one.")
    starts_in_seconds: int | None = Field(
        description="Seconds until it starts, from the typical job length of recent finished jobs; null until there is history.")
    eta_seconds: int | None = Field(description="Seconds until it should finish, queue included; null until there is history.")


class StageUsage(BaseModel):
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None = Field(description="Estimated; null when a model has no known price.")


class Usage(BaseModel):
    stages: dict[str, StageUsage]
    total_cost_usd: float | None = Field(description="Sum over the stages; null if any stage has no cost.")


class GlossaryTerm(BaseModel):
    term: str
    meaning: str


class AnalysisReports(BaseModel):
    market_report: str | None = None
    sentiment_report: str | None = None
    news_report: str | None = None
    fundamentals_report: str | None = None
    investment_plan: str | None = None
    trader_investment_plan: str | None = None
    final_trade_decision: str | None = None


class RevenueRow(BaseModel):
    name: str
    revenue: int = Field(description="Whole US dollars for the period; can be negative (hedging, eliminations).")
    share_pct: float = Field(description="Share of the reported total revenue, computed by the service.")
    yoy_pct: float | None = Field(
        default=None, description="Growth over the same period a year earlier; null when no year-ago figure was sent.")
    includes: str | None = Field(default=None, description="What the company says the bucket covers, as sent.")


class RevenueTableView(BaseModel):
    dimension: Literal["product", "segment", "geography"]
    rows: list[RevenueRow]


class RevenueTable(BaseModel):
    fiscal_period: str
    period_end: date
    source: str
    total_revenue: int
    total_yoy_pct: float | None = None
    views: list[RevenueTableView]
    footnotes: list[str] = Field(description="The company's own disclosure notes, as sent by the caller.")


class ThemeSignal(BaseModel):
    name: str
    met: bool
    value: float
    threshold: float
    direction: Literal["at least", "at most"]


class ThemeTable(BaseModel):
    name: str
    as_of: date
    basket_size: int = Field(description="Names on the fixed list.")
    basket_size_used: int = Field(description="Names with enough history to be measured.")
    names_below_200d: int | None = None
    names_below_50d: int | None = None
    names_rsi_below_30: int | None = None
    relative_1m_pct: float | None = Field(default=None, description="Basket return minus the market's, in points.")
    relative_3m_pct: float | None = None
    signals_met: int = Field(description="Washout signals met, counted by the service from fixed thresholds.")
    signals_measured: int
    signals: list[ThemeSignal]


class AnalysisJobResponse(BaseModel):
    id: str
    status: JobStatus
    ticker: str
    trade_date: date
    asset_type: AssetType
    analysts: list[AnalystName]
    decision: str | None = None
    rating_label: str | None = Field(
        default=None, description="The decision in plain words for retail readers: Strong Buy, Weak Buy, Hold, Weak Sell or Strong Sell.")
    rating_meaning: str | None = Field(
        default=None, description="One sentence on what the label means for someone who owns the stock and someone who doesn't.")
    reports: AnalysisReports | None = None
    plan: Plan | None = Field(
        default=None,
        description="Checked position plan; null when none passed the checks (see plan_error).",
    )
    plan_error: str | None = None
    context_digest: str | None = None
    summary: str | None = Field(
        default=None,
        description="About 450 words in one analyst voice, in plain language for retail readers; null when the step is off or none fit the length limit.",
    )
    summary_figures: list[SummaryFigure] | None = Field(
        default=None,
        description="Every dollar, percent and multiple in the summary, with its source; null when none were declared.",
    )
    glossary: list[GlossaryTerm] | None = Field(
        default=None,
        description="Plain meanings of the defined market terms the summary uses, in the order they first "
                    "appear. Show them as tooltips or a list; null when there is no summary.",
    )
    summary_problems: list[SummaryProblem] | None = Field(
        default=None,
        description="Figure checks the summary still fails; empty or null when it passed.",
    )
    revenue_breakdown: RevenueTable | None = Field(
        default=None,
        description="Revenue by product, segment or region for the latest period, with each bucket's share of the "
                    "total and its growth, computed from the request's context.revenue_breakdown; null when none was sent. "
                    "Render it as a table or chart.")
    theme_backdrop: ThemeTable | None = Field(
        default=None,
        description="How many washout signals the theme's basket meets, with each signal's value and threshold, "
                    "computed from the request's context.theme_backdrop; null when none was sent.")
    queue: QueueInfo | None = Field(
        default=None, description="Present while the job is waiting for a free worker; null once it starts.")
    progress: Progress | None = Field(
        default=None, description="Present while the job is running; null before it starts and once it is done.")
    usage: Usage | None = Field(
        default=None, description="Tokens and estimated cost per stage; present once the job has finished.")
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    links: AnalysisLinks


class ContextProblem(BaseModel):
    code: str = Field(description="Stable identifier of the check that failed.")
    path: str = Field(description="Where in the context the problem is, e.g. financials.annual[0].income.net_income.")
    message: str


class ContextCheckResponse(BaseModel):
    ok: bool
    problems: list[ContextProblem] = Field(description="Every check the context fails; empty when ok.")
    context_digest: str | None = None
    metrics: dict[str, float] = Field(
        description="Metrics the service computes from the values (so do not send them).")


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    context_schema_versions: list[int] = Field(default_factory=lambda: list(SUPPORTED_SCHEMA_VERSIONS))
    jobs_persisted: bool = Field(default=False, description="Whether finished jobs are kept across restarts.")
    storage_boots: int | None = Field(
        default=None, description="Times the service has started on this storage; 2 or more shows the disk persists.")
    service: str = "tradingagents-api"
