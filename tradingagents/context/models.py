"""The caller's verified data, as a closed schema.

Amounts are whole dollars (integers). Unknown is null, never 0. Every statement
says which company, which period and how long, so a quarter, a year to date and
a full year cannot be mistaken for each other, and one company's figures cannot
ride on another's request.

Versioning: ``schema_version`` changes only for a breaking change (a removed or
renamed field, a new required field, a tighter rule). New optional fields, new
blocks and new enum values are additive and do not change it. The versions the
service reads are in ``SUPPORTED_SCHEMA_VERSIONS`` and listed by ``/health``.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SUPPORTED_SCHEMA_VERSIONS = (1,)

MAX_AMOUNT = 10**15
MAX_LABEL = 40
MAX_NOTES = 4000

Source = Literal["sec_xbrl", "financial_datasets", "getmarketalerts", "yahoo_finance", "sec_13f", "sec_form4",
                 "congress_disclosures", "sec_edgar"]
Amount = Annotated[int | None, Field(default=None, gt=-MAX_AMOUNT, lt=MAX_AMOUNT)]
PerShare = Annotated[float | None, Field(default=None, gt=-1e6, lt=1e6)]
Label = Annotated[str, Field(min_length=1, max_length=MAX_LABEL)]
Ticker = Annotated[str, Field(min_length=1, max_length=32)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class _Company(_Strict):
    """The company the data is for; the request's ticker must match it."""

    ticker: Ticker

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        ticker = value.strip().upper()
        if not ticker:
            raise ValueError("ticker must not be empty")
        return ticker


class Income(_Strict):
    revenue: Amount
    gross_profit: Amount
    operating_income: Amount
    net_income: Amount
    eps_basic: PerShare
    eps_diluted: PerShare
    depreciation_and_amortization: Amount


class CashFlow(_Strict):
    net_cash_flow_from_operations: Amount
    capital_expenditure: Amount
    free_cash_flow: Amount


class Statement(_Strict):
    """Income and cash flow over one period; ``kind`` says how long it is."""

    fiscal_period: Label
    period_start: date | None = None
    period_end: date
    kind: Literal["standalone", "ytd", "annual"]
    source: Source
    filed_at: date | None = None
    income: Income | None = None
    cash_flow: CashFlow | None = None


class Balance(_Strict):
    """A balance sheet on one date; at most one per date."""

    fiscal_period: Label
    as_of: date
    source: Source
    total_assets: Amount
    total_liabilities: Amount
    shareholders_equity: Amount
    cash_and_equivalents: Amount
    current_assets: Amount
    current_liabilities: Amount
    total_debt: Amount


class Financials(_Company):
    currency: Literal["USD"]
    as_of: date
    fiscal_year_end: date | None = None
    annual: list[Statement] = Field(default_factory=list, max_length=10)
    quarterly: list[Statement] = Field(default_factory=list, max_length=16)
    balance: list[Balance] = Field(default_factory=list, max_length=26)


Name = Annotated[str, Field(min_length=1, max_length=120)]
PositiveCount = Annotated[int | None, Field(default=None, gt=0, lt=MAX_AMOUNT)]


class Company(_Company):
    """Who the company is, and the figures per-share and enterprise-value maths need."""

    currency: Literal["USD"]
    source: Source
    as_of: date
    name: Name | None = None
    sector: Annotated[str, Field(min_length=1, max_length=60)] | None = None
    industry: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    exchange: Annotated[str, Field(min_length=1, max_length=30)] | None = None
    market_cap: PositiveCount
    shares_outstanding: PositiveCount


Level = Annotated[float | None, Field(default=None, gt=0, lt=1e7)]
Signed = Annotated[float | None, Field(default=None, gt=-1e7, lt=1e7)]
Volume = Annotated[int | None, Field(default=None, ge=0, lt=10**13)]


class Macd(_Strict):
    line: Signed
    signal: Signed
    histogram: Signed


class Technicals(_Company):
    """The levels the caller's own chart shows; with these the report and the chart agree."""

    currency: Literal["USD"]
    source: Source
    as_of: date
    close: float = Field(gt=0, lt=1e7)
    sma_20: Level
    sma_50: Level
    sma_200: Level
    ema_10: Level
    rsi_14: Annotated[float | None, Field(default=None, ge=0, le=100)]
    macd: Macd | None = None
    volume: Volume
    volume_avg_50d: Volume
    high_52w: Level
    low_52w: Level


class Price(_Company):
    """A verified closing price, so the service can derive valuation ratios."""

    currency: Literal["USD"]
    source: Source
    as_of: date
    close: float = Field(gt=0, lt=1e7)


class NextEarnings(_Strict):
    date: date
    fiscal_period: Label
    confirmed: bool


class EarningsReport(_Strict):
    """One reported quarter. Estimates may be null: not every feed has them."""

    fiscal_period: Label
    period_end: date
    reported_on: date
    eps_actual: PerShare
    eps_estimate: PerShare
    revenue_actual: Amount
    revenue_estimate: Amount


class Events(_Company):
    """When the company reports next and what it last reported."""

    currency: Literal["USD"]
    source: Source
    as_of: date
    next_earnings: NextEarnings | None = None
    recent_earnings: list[EarningsReport] = Field(default_factory=list, max_length=4)


Count = Annotated[int, Field(ge=0, lt=10**9)]
Shares = Annotated[int, Field(ge=0, lt=10**13)]
Person = Annotated[str, Field(min_length=1, max_length=80)]


class FundHolder(_Strict):
    """One tracked manager's 13F position at the quarter end."""

    manager: Person
    firm: Person | None = None
    shares: Shares
    value_usd: Amount
    weight_pct: Annotated[float | None, Field(default=None, ge=0, le=100)]
    shares_change: Annotated[int | None, Field(default=None, gt=-10**13, lt=10**13)]
    shares_change_pct: Annotated[float | None, Field(default=None, gt=-100.01, lt=1e7)]
    action: Literal["new", "added", "trimmed", "exited", "unchanged"]
    filed_on: date
    estimated_cost: Level


class Funds(_Strict):
    source: Literal["sec_13f"]
    quarter_end: date
    managers_tracked: Annotated[int, Field(ge=1, lt=10**6)]
    coverage: Annotated[str, Field(max_length=300)] | None = None
    holders: list[FundHolder] = Field(default_factory=list, max_length=10)


class InsiderTrade(_Strict):
    name: Person
    title: Person
    is_director: bool
    transaction_date: date
    filing_date: date
    kind: Literal["buy", "sale"]
    shares: Annotated[int, Field(gt=0, lt=10**13)]
    price_per_share: Annotated[float, Field(gt=0, lt=1e7)]
    value_usd: Amount
    shares_owned_after: Annotated[int | None, Field(default=None, ge=0, lt=10**13)]
    form_type: Annotated[str, Field(min_length=1, max_length=10)]
    under_10b5_1_plan: Annotated[bool | None, Field(strict=True)] = None       # the Form 4 checkbox; None if unknown
    from_option_exercise: Annotated[bool | None, Field(strict=True)] = None    # from exercising options; None if unknown


class Insiders(_Strict):
    source: Literal["sec_form4"]
    window_days: Annotated[int, Field(ge=1, le=365)]
    buys: Count
    sells: Count
    net_value_usd: Amount
    trades: list[InsiderTrade] = Field(default_factory=list, max_length=20)


class CongressTrade(_Strict):
    member: Person
    chamber: Literal["House", "Senate"]
    district: Annotated[str, Field(min_length=1, max_length=10)] | None = None
    owner: Literal["self", "spouse", "dependent", "joint"]
    side: Literal["buy", "sell"]
    traded_on: date
    disclosed_on: date
    amount_min: Annotated[int, Field(ge=0, lt=10**12)]
    amount_max: Annotated[int, Field(ge=0, lt=10**12)]


class Congress(_Strict):
    source: Literal["congress_disclosures"]
    window_days: Annotated[int, Field(ge=1, le=365)]
    buys: Count
    sells: Count
    trades: list[CongressTrade] = Field(default_factory=list, max_length=20)


class Ownership(_Company):
    """Who holds and trades the stock, with the dates that say how old each fact is."""

    currency: Literal["USD"]
    as_of: date
    funds: Funds | None = None
    insiders: Insiders | None = None
    congress: Congress | None = None


class PeTtm(_Strict):
    """Price over trailing-twelve-month EPS now, and over the last few years."""

    latest: Annotated[float, Field(gt=0, lt=1e5)]
    min_5y: Annotated[float | None, Field(default=None, gt=0, lt=1e5)]
    median_5y: Annotated[float | None, Field(default=None, gt=0, lt=1e5)]
    max_5y: Annotated[float | None, Field(default=None, gt=0, lt=1e5)]
    percentile_5y: Annotated[float | None, Field(default=None, ge=0, le=1)]
    years: Annotated[int | None, Field(default=None, ge=1, le=30)]


class ValuationHistory(_Company):
    """The stock's valuation against its own past."""

    currency: Literal["USD"]
    source: Source
    as_of: date
    pe_ttm: PeTtm | None = None
    peg: Annotated[float | None, Field(default=None, gt=-1000, lt=1000)]


class Reading(_Strict):
    value: Annotated[float, Field(ge=0, lt=1e6)]
    as_of: date


class FearGreed(_Strict):
    value: Annotated[float, Field(ge=0, le=100)]
    label: Annotated[str, Field(min_length=1, max_length=30)]
    as_of: date


class MarketBackdrop(_Strict):
    """Market-wide measures, each with its own date because they update on different days."""

    source: Source
    as_of: date
    vix: Reading | None = None
    fear_and_greed: FearGreed | None = None
    buffett_indicator_pct: Reading | None = None
    credit_spread_hy_bps: Reading | None = None


Pct = Annotated[float | None, Field(default=None, ge=0, le=100)]
Move = Annotated[float | None, Field(default=None, gt=-100.01, lt=1e4)]


class Performance(_Strict):
    """A comparison series (the market, a sector fund) and how it did over the same windows as the basket."""

    ticker: Ticker
    return_1m_pct: Move
    return_3m_pct: Move


class ThemeBackdrop(_Strict):
    """How a theme's basket of stocks is doing, measured by the caller on a fixed list from daily closes.

    The distance from the high is from the 52-week closing high, not the intraday high. Positioning
    (leverage, crowding, short interest) is not part of this block and is never inferred from it.
    """

    name: Annotated[str, Field(min_length=1, max_length=60)]
    source: Source
    as_of: date       # the oldest latest-close date among the names measured
    basket: list[Ticker] = Field(min_length=3, max_length=40)
    basket_size: Annotated[int, Field(ge=3, le=40)] | None = None    # the caller's own count of the list, checked
    basket_size_used: Annotated[int, Field(ge=1, le=40)]    # names with enough history to measure
    below_200d_pct: Pct
    below_50d_pct: Pct
    rsi_below_30_pct: Pct
    avg_drawdown_from_52w_close_high_pct: Pct
    return_1m_pct: Move       # equal-weight basket return
    return_3m_pct: Move
    benchmark: Performance | None = None
    sector_etf: Performance | None = None
    put_call_ratio: Reading | None = None    # market-wide, not per stock

    @field_validator("basket")
    @classmethod
    def _tickers(cls, value: list[str]) -> list[str]:
        return [t.strip().upper() for t in value]


class PriorReport(_Company):
    """One of our own earlier reports on this company."""

    as_of: date
    decision: Literal["Buy", "Overweight", "Hold", "Underweight", "Sell"]
    target: Level
    price_then: Annotated[float, Field(gt=0, lt=1e7)]
    horizon: Annotated[str, Field(min_length=1, max_length=40)] | None = None


class EarningsRelease(_Company):
    """The company's earnings press release (8-K Exhibit 99.1) as plain text, for the service to read."""

    source: Literal["sec_edgar"]
    form: Literal["8-K", "6-K"]
    filed_on: date
    period_end: date | None = None
    accession: Annotated[str, Field(pattern=r"^\d{10}-\d{2}-\d{6}$")]
    url: Annotated[str, Field(max_length=300)]
    text: Annotated[str, Field(min_length=500, max_length=120_000)]

    @field_validator("url")
    @classmethod
    def sec_archive_only(cls, value: str) -> str:
        if not value.startswith("https://www.sec.gov/Archives/"):
            raise ValueError("url must start with https://www.sec.gov/Archives/")
        return value


class Segment(_Strict):
    """One bucket of revenue as the company reports it (a product line, a segment or a region)."""

    name: Label
    revenue: Annotated[int, Field(gt=-MAX_AMOUNT, lt=MAX_AMOUNT)]     # may be negative: hedging, eliminations
    prior_year_revenue: Amount    # the same period a year earlier; null if the company did not restate it
    includes: Annotated[str, Field(min_length=1, max_length=200)] | None = None


class RevenueView(_Strict):
    """The whole of revenue cut one way. Buckets cover the total, so they add up to it."""

    dimension: Literal["product", "segment", "geography"]
    segments: list[Segment] = Field(min_length=2, max_length=20)


class RevenueBreakdown(_Company):
    """Where one period's revenue came from. Shares and growth are computed here from these amounts."""

    currency: Literal["USD"]
    source: Source
    as_of: date
    fiscal_period: Label
    period_end: date
    kind: Literal["standalone", "annual"]
    total_revenue: Annotated[int, Field(gt=0, lt=MAX_AMOUNT)]
    prior_year_total: Annotated[int | None, Field(default=None, gt=0, lt=MAX_AMOUNT)]
    views: list[RevenueView] = Field(min_length=1, max_length=3)
    footnotes: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(default_factory=list, max_length=5)


class Context(_Strict):
    """What a caller supplies with a request. Only the blocks defined here are accepted."""

    schema_version: Literal[1] = 1
    financials: Financials | None = None
    price: Price | None = None
    events: Events | None = None
    company: Company | None = None
    technicals: Technicals | None = None
    ownership: Ownership | None = None
    valuation_history: ValuationHistory | None = None
    market_backdrop: MarketBackdrop | None = None
    prior_reports: list[PriorReport] = Field(default_factory=list, max_length=3)
    earnings_release: EarningsRelease | None = None
    revenue_breakdown: RevenueBreakdown | None = None
    theme_backdrop: ThemeBackdrop | None = None
    notes: str | None = Field(default=None, max_length=MAX_NOTES,
                              description="Free text. Unverified: context only, never a source for a figure.")
