"""An estimated next-earnings date from Yahoo, for when the caller sends none.

Yahoo's date is often a projection, so it is always unconfirmed and labelled as Yahoo's
estimate wherever an agent reads it. A date the caller sent is never replaced, and a lookup
that fails or finds nothing in the future changes nothing: the plan then simply may not date
an earnings item, as before.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

from tradingagents.context.models import Context, Events, NextEarnings
from tradingagents.dataflows.vendors.yahoo.calendar import fetch_yahoo_earnings_dates

logger = logging.getLogger(__name__)


def estimate_next_earnings(ticker: str, as_of: date,
                           fetch: Callable[[str], list[date]] = fetch_yahoo_earnings_dates) -> NextEarnings | None:
    """The first listed date after ``as_of``, unconfirmed; None when there is none or the lookup fails."""
    try:
        upcoming = [d for d in fetch(ticker) if d > as_of]
    except Exception as exc:  # Yahoo is unofficial and flaky; no estimate is the safe answer
        logger.warning("Earnings estimate for %s unavailable (%s: %s)", ticker, type(exc).__name__, exc)
        return None
    return NextEarnings(date=upcoming[0], fiscal_period="next report", confirmed=False) if upcoming else None


def apply_estimate(context: Context | None, ticker: str, as_of: date, estimate: NextEarnings | None) -> Context | None:
    """The context with the estimate as its next earnings date; the caller's own context is not changed."""
    if estimate is None:
        return context
    context = context or Context()
    if context.events is None:
        events = Events(ticker=ticker, currency="USD", source="yahoo_finance", as_of=as_of, next_earnings=estimate)
    else:
        events = context.events.model_copy(update={"next_earnings": estimate})
    return context.model_copy(update={"events": events})


def with_estimated_earnings(context: Context | None, ticker: str, as_of: date,
                            fetch: Callable[[str], list[date]] = fetch_yahoo_earnings_dates) -> Context | None:
    if context is not None and context.events is not None and context.events.next_earnings is not None:
        return context
    return apply_estimate(context, ticker, as_of, estimate_next_earnings(ticker, as_of, fetch))
