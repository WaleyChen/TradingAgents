"""The verified readings a plan is checked against come from price rows, not
from any model's prose."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from tradingagents.plan import readings as mod


def _rows(n: int) -> pd.DataFrame:
    close = [100 + i * 0.5 + (3 if i % 7 == 0 else 0) for i in range(n)]
    return pd.DataFrame({
        "Date": pd.date_range("2025-01-01", periods=n, freq="B"),
        "Open": close, "High": [c + 1 for c in close], "Low": [c - 1 for c in close],
        "Close": close, "Volume": [1_000_000] * n,
    })


@pytest.fixture
def rows(monkeypatch):
    def serve(n):
        monkeypatch.setattr(mod, "verified_rows", lambda symbol, as_of: _rows(n))
    return serve


@pytest.mark.unit
def test_readings_come_from_the_latest_verified_row(rows):
    rows(300)
    got = mod.compute_readings("ACME", "2026-11-02")
    frame = _rows(300)
    assert got.price == pytest.approx(frame["Close"].iloc[-1])
    assert got.sma_20 == pytest.approx(frame["Close"].tail(20).mean())
    assert got.sma_50 == pytest.approx(frame["Close"].tail(50).mean())
    assert got.sma_200 == pytest.approx(frame["Close"].tail(200).mean())
    assert 0 < got.rsi_14 < 100
    assert math.isfinite(got.macd_histogram)
    assert got.date == frame["Date"].iloc[-1].strftime("%Y-%m-%d")


@pytest.mark.unit
def test_an_average_without_enough_history_is_unavailable_not_a_partial_one(rows):
    """A 200-day average over 60 rows would be a number that means something else."""
    rows(60)
    got = mod.compute_readings("ACME", "2026-11-02")
    assert got.sma_200 is None
    assert got.sma_50 is not None
    assert got.price > 0
