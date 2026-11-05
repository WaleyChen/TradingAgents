"""Verified readings for a plan check, computed from price rows.

Deterministic and model-free: the same rows the market analyst's verification
snapshot uses, returned as numbers instead of a table.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import fields

from stockstats import wrap

from tradingagents.context.models import Technicals
from tradingagents.dataflows.vendors.yahoo.snapshot import _verified_rows as verified_rows
from tradingagents.plan.check import Readings

logger = logging.getLogger(__name__)

# Rows an indicator needs before its value means what its name says; fewer
# would give a partial average or a still-warming-up oscillator.
_SMA_WINDOWS = {"sma_20": 20, "sma_50": 50, "sma_200": 200}
_RSI_ROWS = 15
_MACD_ROWS = 35


def _last(frame, column: str, needed: int) -> float | None:
    if len(frame) < needed:
        return None
    value = float(frame[column].iloc[-1])  # indexing by name computes the indicator
    return value if math.isfinite(value) else None


def compute_readings(symbol: str, as_of_date: str) -> Readings:
    """Price and indicator readings on the last verified row on or before the date.

    Raises NoMarketDataError when there are no usable rows. An indicator
    without enough history is None, so a trigger that needs it is reported as
    unavailable instead of being checked against a partial value.
    """
    df = verified_rows(symbol, as_of_date)
    stats = wrap(df.copy())
    latest = df.iloc[-1]
    return Readings(
        price=float(latest["Close"]),
        rsi_14=_last(stats, "rsi_14", _RSI_ROWS),
        macd_histogram=_last(stats, "macdh", _MACD_ROWS),
        date=latest["Date"].strftime("%Y-%m-%d"),
        **{name: _last(stats, f"close_{n}_sma", n) for name, n in _SMA_WINDOWS.items()},
    )


_INDICATORS = ("rsi_14", "sma_20", "sma_50", "sma_200", "macd_histogram")


def readings_from(technicals: Technicals | None, symbol: str, as_of_date: str,
                  fetch: Callable[[str, str], Readings] = compute_readings) -> Readings:
    """The caller's technicals where given, else our own readings, so the report and their chart agree.

    Complete technicals are used as they are and nothing is fetched. Anything left out is
    filled from our own computation; if that fails too, the supplied values stand alone.
    Without technicals, or without a supplied close, a failed fetch raises as it always did.
    """
    if technicals is None:
        return fetch(symbol, as_of_date)
    macd = technicals.macd
    supplied = {"price": technicals.close, "rsi_14": technicals.rsi_14, "sma_20": technicals.sma_20,
                "sma_50": technicals.sma_50, "sma_200": technicals.sma_200,
                "macd_histogram": macd.histogram if macd else None, "date": technicals.as_of.isoformat()}
    if all(supplied[name] is not None for name in _INDICATORS):
        return Readings(**supplied)
    try:
        ours = fetch(symbol, as_of_date)
    except Exception as exc:  # the caller's close still gives a usable, if partial, set of readings
        logger.warning("Readings: our own fetch failed for %s (%s); using the supplied technicals", symbol, exc)
        return Readings(**supplied)
    merged = {f.name: getattr(ours, f.name) for f in fields(Readings)}
    return Readings(**{**merged, **{k: v for k, v in supplied.items() if v is not None}})
