"""Yahoo's earnings calendar."""

from __future__ import annotations

from datetime import date, datetime

import yfinance as yf


def fetch_yahoo_earnings_dates(ticker: str) -> list[date]:
    """The dates Yahoo's calendar lists for the next report (one date, or the ends of a range)."""
    calendar = yf.Ticker(ticker).calendar
    raw = calendar.get("Earnings Date") if isinstance(calendar, dict) else None
    items = raw if isinstance(raw, list | tuple) else ([raw] if raw is not None else [])
    dates = [x.date() if isinstance(x, datetime) else x for x in items if isinstance(x, date)]
    return sorted(dates)
