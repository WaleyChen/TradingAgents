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

Source = Literal["sec_xbrl", "financial_datasets", "getmarketalerts"]
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


class Financials(_Company):
    currency: Literal["USD"]
    as_of: date
    fiscal_year_end: date | None = None
    annual: list[Statement] = Field(default_factory=list, max_length=10)
    quarterly: list[Statement] = Field(default_factory=list, max_length=16)
    balance: list[Balance] = Field(default_factory=list, max_length=26)


class Price(_Company):
    """A verified closing price, so the service can derive valuation ratios."""

    currency: Literal["USD"]
    source: Source
    as_of: date
    close: float = Field(gt=0, lt=1e7)


class Context(_Strict):
    """What a caller supplies with a request. Only the blocks defined here are accepted."""

    schema_version: Literal[1] = 1
    financials: Financials | None = None
    price: Price | None = None
    notes: str | None = Field(default=None, max_length=MAX_NOTES,
                              description="Free text. Unverified: context only, never a source for a figure.")
