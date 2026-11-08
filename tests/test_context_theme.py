"""theme_backdrop: how a theme's basket of stocks is doing, so one stock's chart can be read against it.

Rails measures a fixed basket from stored daily closes and sends the shares and returns. The service
checks them, counts in code which washout signals are met, and tells the agents they may not call a
washout unless enough of the signals agree. Positioning (leverage, crowding) is never measured here.
"""

from __future__ import annotations

import copy
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.app import create_app
from api.jobs import JobManager
from tradingagents.context import Context, check_context
from tradingagents.context.derive import derive_theme_backdrop
from tradingagents.context.models import ThemeBackdrop
from tradingagents.context.render import render_context, render_role_blocks
from tradingagents.summary_check import known_values

BASKET = ["NVDA", "AVGO", "AMD", "TSM", "ASML", "MU", "MRVL", "ARM", "AMAT", "LRCX", "KLAC", "SMCI", "PLTR",
          "MSFT", "GOOGL", "META", "AMZN", "ORCL"]

THEME = {
    "name": "AI & semis", "source": "getmarketalerts", "as_of": "2026-11-06",
    "basket": BASKET, "basket_size_used": 18,
    "below_200d_pct": 77.8, "below_50d_pct": 88.9, "rsi_below_30_pct": 33.3,
    "avg_drawdown_from_52w_close_high_pct": 31.5,
    "return_1m_pct": -14.2, "return_3m_pct": -9.0,
    "benchmark": {"ticker": "SPY", "return_1m_pct": -2.1, "return_3m_pct": 3.4},
    "sector_etf": {"ticker": "SMH", "return_1m_pct": -16.0, "return_3m_pct": -8.5},
    "put_call_ratio": {"value": 1.1, "as_of": "2026-11-06"},
}


def theme(**changes):
    return ThemeBackdrop.model_validate({**copy.deepcopy(THEME), **changes})


def codes(**blocks):
    kw = blocks.pop("kw", {})
    return [(p.code, p.path) for p in check_context(Context.model_validate(blocks), **kw)]


def ctx(**changes):
    return Context.model_validate({"theme_backdrop": {**copy.deepcopy(THEME), **changes}})


# --- schema ------------------------------------------------------------------

@pytest.mark.unit
def test_a_theme_is_accepted_and_tickers_are_normalised():
    t = theme(basket=[" nvda ", "amd", "mu"], basket_size_used=3, below_200d_pct=66.7, below_50d_pct=100,
              rsi_below_30_pct=33.3)
    assert t.basket == ["NVDA", "AMD", "MU"]
    assert codes(theme_backdrop=THEME) == []


@pytest.mark.unit
def test_every_measure_is_optional_but_the_basket_and_its_count_are_not():
    minimal = {"name": "AI", "source": "getmarketalerts", "as_of": "2026-11-06", "basket": BASKET, "basket_size_used": 18}
    assert ThemeBackdrop.model_validate(minimal).below_200d_pct is None
    for gone in ("basket", "basket_size_used", "as_of", "name"):
        with pytest.raises(ValidationError):
            ThemeBackdrop.model_validate({k: v for k, v in minimal.items() if k != gone})


@pytest.mark.unit
@pytest.mark.parametrize("bad", [
    {"surprise": 1}, {"source": "made_up"}, {"below_200d_pct": 101}, {"rsi_below_30_pct": -1},
    {"avg_drawdown_from_52w_close_high_pct": -5}, {"avg_drawdown_from_52w_close_high_pct": 101},
    {"basket": ["NVDA", "AMD"]}, {"basket": [f"T{i}" for i in range(41)]}, {"basket_size_used": 0},
    {"benchmark": {"return_1m_pct": 1.0}}, {"put_call_ratio": {"value": -1, "as_of": "2026-11-06"}},
    {"return_1m_pct": float("nan")},
])
def test_a_malformed_theme_is_refused(bad):
    with pytest.raises(ValidationError):
        theme(**bad)


# --- checks ------------------------------------------------------------------

@pytest.mark.unit
def test_a_repeated_ticker_and_an_impossible_count_are_refused():
    assert ("basket_duplicate", "theme_backdrop.basket[3]") in codes(
        theme_backdrop={**THEME, "basket": ["NVDA", "AMD", "MU", "nvda"], "basket_size_used": 3,
                        "below_200d_pct": 66.7, "below_50d_pct": 100, "rsi_below_30_pct": 33.3})
    assert ("basket_size_inconsistent", "theme_backdrop.basket_size_used") in codes(
        theme_backdrop={**THEME, "basket_size_used": 19})


@pytest.mark.unit
def test_a_share_must_be_a_whole_number_of_the_names_measured():
    # 55.0% of 18 names is 9.9 names, so it cannot be a share of 18.
    assert ("share_not_whole_names", "theme_backdrop.below_200d_pct") in codes(
        theme_backdrop={**THEME, "below_200d_pct": 55.0})
    # Rounded to a whole percent still fits (14/18 = 77.8 -> 78).
    assert codes(theme_backdrop={**THEME, "below_200d_pct": 78}) == []


@pytest.mark.unit
def test_a_stale_or_future_basket_date_is_refused():
    run = date(2026, 11, 6)
    assert codes(theme_backdrop=THEME, kw={"trade_date": run}) == []
    assert ("theme_backdrop_stale", "theme_backdrop.as_of") in codes(
        theme_backdrop={**THEME, "as_of": "2026-10-20"}, kw={"trade_date": run})
    assert ("as_of_after_trade_date", "theme_backdrop.as_of") in codes(
        theme_backdrop=THEME, kw={"trade_date": date(2026, 11, 5)})


# --- derived reading ---------------------------------------------------------

@pytest.mark.unit
def test_names_and_relative_returns_are_computed():
    d = derive_theme_backdrop(theme())
    assert d["names_below_200d"] == 14 and d["names_below_50d"] == 16 and d["names_rsi_below_30"] == 6
    assert d["relative_1m_pct"] == pytest.approx(-12.1) and d["relative_3m_pct"] == pytest.approx(-12.4)
    assert d["basket_size"] == 18 and d["basket_size_used"] == 18


@pytest.mark.unit
def test_every_washout_signal_is_counted_in_code():
    d = derive_theme_backdrop(theme())
    assert (d["signals_met"], d["signals_measured"]) == (5, 5)
    assert all(s["met"] for s in d["signals"])


@pytest.mark.unit
def test_a_calm_theme_meets_none_and_a_missing_measure_is_not_measured_not_met():
    calm = theme(below_200d_pct=22.2, rsi_below_30_pct=5.6, avg_drawdown_from_52w_close_high_pct=6.0,
                 return_1m_pct=2.0, below_50d_pct=33.3)
    assert derive_theme_backdrop(calm)["signals_met"] == 0
    sparse = theme(below_200d_pct=None, rsi_below_30_pct=None, avg_drawdown_from_52w_close_high_pct=None,
                   return_1m_pct=None, benchmark=None, below_50d_pct=None)
    d = derive_theme_backdrop(sparse)
    assert (d["signals_met"], d["signals_measured"]) == (0, 0) and d["relative_1m_pct"] is None


@pytest.mark.unit
def test_a_partial_theme_counts_only_what_it_can_support():
    partial = theme(rsi_below_30_pct=5.6, return_1m_pct=-3.0)       # two calm readings, three washout ones
    d = derive_theme_backdrop(partial)
    assert (d["signals_met"], d["signals_measured"]) == (2, 5)


# --- who reads it ------------------------------------------------------------

@pytest.mark.unit
def test_the_market_analyst_reads_the_theme_with_the_rules_for_calling_a_washout():
    text = render_role_blocks(ctx())["market"]
    assert "AI & semis" in text and "14 of 18" in text and "SMH" in text
    assert "5 of 5" in text
    assert "not measured" in text.lower() and "never claim" in text.lower()
    assert "at least 3" in text and "closing high" in text


@pytest.mark.unit
def test_the_deciders_get_the_signals_in_a_digest_and_the_other_roles_do_not():
    blocks = render_role_blocks(ctx())
    assert "Theme backdrop" in blocks["decision"] and "5 of 5" in blocks["decision"]
    for role in ("news", "fundamentals", "sentiment"):
        assert role not in blocks or "AI & semis" not in blocks[role]


@pytest.mark.unit
def test_the_count_of_names_actually_measured_is_stated():
    text = render_role_blocks(ctx(basket_size_used=12, below_200d_pct=75.0, below_50d_pct=83.3, rsi_below_30_pct=33.3))["market"]
    assert "12 of the 18 names" in text and "9 of 12" in text


@pytest.mark.unit
def test_the_full_render_and_the_summary_figures_include_it():
    assert "AI & semis" in render_context(ctx())
    known = known_values(ctx(), None, None)
    values = [k.value for k in known if k.source == "context"]
    assert 77.8 in values and any(abs(v - -12.1) < 1e-6 for v in values) and 5.0 in values


# --- the API -----------------------------------------------------------------

def _client(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_API_KEY", raising=False)
    manager = JobManager(max_workers=1, runner=lambda request: ("Buy", {"final_trade_decision": "x"}))
    return manager, TestClient(create_app(job_manager=manager))


def _done(client, job_id):
    for _ in range(100):
        body = client.get(f"/api/v1/analyses/{job_id}").json()
        if body["status"] in ("completed", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError("job did not finish")


@pytest.mark.unit
def test_the_response_carries_the_signal_count(monkeypatch):
    manager, client = _client(monkeypatch)
    with client:
        job = client.post("/api/v1/analyses", json={"ticker": "MU", "trade_date": "2026-11-06",
                                                    "context": {"theme_backdrop": THEME}})
        assert job.status_code == 202
        body = _done(client, job.json()["id"])["theme_backdrop"]
    manager.shutdown(wait=False)
    assert body["signals_met"] == 5 and body["signals_measured"] == 5 and body["name"] == "AI & semis"
    assert body["basket_size_used"] == 18 and body["relative_1m_pct"] == pytest.approx(-12.1)
    assert {s["name"] for s in body["signals"]} and all("threshold" in s for s in body["signals"])


@pytest.mark.unit
def test_the_response_has_no_theme_without_one_supplied(monkeypatch):
    manager, client = _client(monkeypatch)
    with client:
        job = client.post("/api/v1/analyses", json={"ticker": "MU", "trade_date": "2026-11-06"})
        assert _done(client, job.json()["id"])["theme_backdrop"] is None
    manager.shutdown(wait=False)


# --- the caller's own count of the list --------------------------------------

@pytest.mark.unit
def test_the_callers_count_of_the_list_is_accepted_and_must_match_it():
    assert theme(basket_size=18).basket_size == 18
    assert theme().basket_size is None
    assert codes(theme_backdrop={**THEME, "basket_size": 18}) == []
    assert ("basket_size_inconsistent", "theme_backdrop.basket_size") in codes(
        theme_backdrop={**THEME, "basket_size": 17})
    with pytest.raises(ValidationError):
        theme(basket_size=2)
