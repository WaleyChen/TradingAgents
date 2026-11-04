"""Verified readings for a plan check, computed from price rows.

Deterministic and model-free: the same rows the market analyst's verification
snapshot uses, returned as numbers instead of a table.
"""

from __future__ import annotations

import math

from stockstats import wrap

from tradingagents.dataflows.vendors.yahoo.snapshot import _verified_rows as verified_rows
from tradingagents.plan.check import Readings

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
