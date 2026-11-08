"""revenue_breakdown: schema, checks, the table computed in code, who reads it, and the API response.

The caller sends each bucket's revenue (and the same quarter a year earlier). The share of total
and the growth are worked out here, never by a model, so the table cannot carry an invented figure.
"""

from __future__ import annotations

import copy
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.app import create_app
from api.jobs import JobManager
from tradingagents.context import Context, check_context
from tradingagents.context.derive import derive_revenue_breakdown
from tradingagents.context.models import RevenueBreakdown
from tradingagents.context.render import render_context, render_role_blocks
from tradingagents.summary_check import known_values

B = 1_000_000_000

BREAKDOWN = {
    "ticker": "GOOG", "currency": "USD", "source": "financial_datasets", "as_of": "2026-11-04",
    "fiscal_period": "Q3 2026", "period_end": "2026-09-30", "kind": "standalone",
    "total_revenue": 100 * B, "prior_year_total": 80 * B,
    "views": [{
        "dimension": "product",
        "segments": [
            {"name": "Search & other", "revenue": 50 * B, "prior_year_revenue": 45 * B,
             "includes": "Search and ads on other Google properties"},
            {"name": "Cloud", "revenue": 25 * B, "prior_year_revenue": 12 * B},
            {"name": "YouTube ads", "revenue": 10 * B, "prior_year_revenue": 9 * B},
            {"name": "Network", "revenue": 8 * B, "prior_year_revenue": 8 * B},
            {"name": "Other Bets", "revenue": 1 * B},
            {"name": "Hedging gains", "revenue": 6 * B, "prior_year_revenue": 6 * B},
        ],
    }],
    "footnotes": ["Workspace revenue is reported inside Cloud."],
}


def breakdown(**changes):
    return RevenueBreakdown.model_validate({**copy.deepcopy(BREAKDOWN), **changes})


def codes(**blocks):
    kw = blocks.pop("kw", {})
    return [(p.code, p.path) for p in check_context(Context.model_validate(blocks), **kw)]


# --- schema ------------------------------------------------------------------

@pytest.mark.unit
def test_a_breakdown_is_accepted_and_the_ticker_is_normalised():
    rb = breakdown(ticker=" goog ")
    assert rb.ticker == "GOOG" and rb.views[0].segments[0].name == "Search & other"
    assert codes(revenue_breakdown=BREAKDOWN) == []


@pytest.mark.unit
@pytest.mark.parametrize("bad", [
    {"surprise": 1}, {"currency": "EUR"}, {"source": "made_up"}, {"kind": "ytd"}, {"total_revenue": 0},
    {"views": []},
    {"views": [{"dimension": "colour", "segments": BREAKDOWN["views"][0]["segments"]}]},
    {"views": [{"dimension": "product", "segments": BREAKDOWN["views"][0]["segments"][:1]}]},
    {"views": [{"dimension": "product", "segments": [{"name": "A", "revenue": 1}, {"name": "B"}]}]},
    {"views": [{"dimension": "product", "segments": [{"name": "A", "revenue": 1, "note": "x"}, {"name": "B", "revenue": 1}]}]},
    {"footnotes": ["x"] * 6},
])
def test_a_malformed_breakdown_is_refused(bad):
    with pytest.raises(ValidationError):
        breakdown(**bad)


# --- checks ------------------------------------------------------------------

@pytest.mark.unit
def test_buckets_that_do_not_add_to_the_total_are_refused():
    views = copy.deepcopy(BREAKDOWN["views"])
    views[0]["segments"][1]["revenue"] = 40 * B
    assert ("revenue_breakdown_total", "revenue_breakdown.views[0]") in codes(
        revenue_breakdown={**BREAKDOWN, "views": views})


@pytest.mark.unit
def test_buckets_within_rounding_of_the_total_pass():
    views = copy.deepcopy(BREAKDOWN["views"])
    views[0]["segments"][1]["revenue"] = int(25.2 * B)    # 0.2% over
    assert codes(revenue_breakdown={**BREAKDOWN, "views": views}) == []


@pytest.mark.unit
def test_prior_year_buckets_must_add_to_the_prior_year_total_when_all_are_given():
    views = copy.deepcopy(BREAKDOWN["views"])
    views[0]["segments"][4]["prior_year_revenue"] = 1 * B       # now every bucket has one
    views[0]["segments"][1]["prior_year_revenue"] = 30 * B
    assert ("revenue_breakdown_prior_total", "revenue_breakdown.views[0]") in codes(
        revenue_breakdown={**BREAKDOWN, "views": views})


@pytest.mark.unit
def test_duplicate_and_zero_buckets_are_refused():
    views = copy.deepcopy(BREAKDOWN["views"])
    views[0]["segments"][4] = {"name": "cloud", "revenue": 1 * B}
    found = codes(revenue_breakdown={**BREAKDOWN, "views": views})
    assert ("segment_duplicate", "revenue_breakdown.views[0].segments[4].name") in found
    views[0]["segments"][4] = {"name": "Other Bets", "revenue": 0}
    views[0]["segments"][1]["prior_year_revenue"] = 0
    found = codes(revenue_breakdown={**BREAKDOWN, "views": views})
    assert ("zero_placeholder", "revenue_breakdown.views[0].segments[4].revenue") in found
    assert ("zero_placeholder", "revenue_breakdown.views[0].segments[1].prior_year_revenue") in found


@pytest.mark.unit
def test_the_total_must_match_the_financials_for_the_same_period():
    financials = {
        "ticker": "GOOG", "currency": "USD", "as_of": "2026-11-04",
        "quarterly": [{"fiscal_period": "Q3 2026", "period_start": "2026-07-01", "period_end": "2026-09-30",
                       "kind": "standalone", "source": "sec_xbrl", "income": {"revenue": 100 * B}}],
    }
    assert codes(revenue_breakdown=BREAKDOWN, financials=financials) == []
    financials["quarterly"][0]["income"]["revenue"] = 90 * B
    assert ("revenue_breakdown_vs_financials", "revenue_breakdown.total_revenue") in codes(
        revenue_breakdown=BREAKDOWN, financials=financials)


@pytest.mark.unit
def test_the_period_and_dates_must_be_possible():
    assert ("breakdown_period_after_as_of", "revenue_breakdown.period_end") in codes(
        revenue_breakdown={**BREAKDOWN, "period_end": "2026-11-30"})
    found = codes(revenue_breakdown=BREAKDOWN, kw={"ticker": "GOOG", "trade_date": __import__("datetime").date(2026, 11, 1)})
    assert ("as_of_after_trade_date", "revenue_breakdown.as_of") in found
    assert ("ticker_mismatch", "revenue_breakdown.ticker") in codes(
        revenue_breakdown=BREAKDOWN, kw={"ticker": "MSFT"})


# --- derived table -----------------------------------------------------------

@pytest.mark.unit
def test_shares_and_growth_are_computed_from_the_amounts():
    table = derive_revenue_breakdown(breakdown())
    rows = {r["name"]: r for r in table["views"][0]["rows"]}
    assert rows["Search & other"]["share_pct"] == pytest.approx(50.0)
    assert rows["Search & other"]["yoy_pct"] == pytest.approx(11.111, abs=0.01)
    assert rows["Cloud"]["yoy_pct"] == pytest.approx(108.333, abs=0.01)
    assert rows["Other Bets"]["yoy_pct"] is None            # no year-ago figure sent: not 0
    assert rows["Search & other"]["includes"].startswith("Search and ads")
    assert table["total_yoy_pct"] == pytest.approx(25.0)
    assert table["fiscal_period"] == "Q3 2026" and table["footnotes"] == BREAKDOWN["footnotes"]


@pytest.mark.unit
def test_growth_is_undefined_not_infinite_from_a_nonpositive_base():
    views = copy.deepcopy(BREAKDOWN["views"])
    views[0]["segments"][1]["prior_year_revenue"] = -1 * B
    rows = derive_revenue_breakdown(breakdown(views=views))["views"][0]["rows"]
    assert rows[1]["yoy_pct"] is None


@pytest.mark.unit
def test_total_growth_is_absent_without_a_prior_total():
    assert derive_revenue_breakdown(breakdown(prior_year_total=None))["total_yoy_pct"] is None


# --- who reads it ------------------------------------------------------------

@pytest.mark.unit
def test_the_fundamentals_analyst_reads_the_table_with_computed_shares():
    blocks = render_role_blocks(Context.model_validate({"revenue_breakdown": BREAKDOWN}))
    text = blocks["fundamentals"]
    assert "Cloud" in text and "25.0% of total" in text and "+108.3%" in text
    assert "Workspace revenue is reported inside Cloud" in text
    assert "not broken out" in text.lower() or "do not split" in text.lower()
    for role in ("market", "news", "sentiment"):
        assert role not in blocks or "Cloud" not in blocks[role]


@pytest.mark.unit
def test_the_deciders_get_the_mix_in_one_line():
    decision = render_role_blocks(Context.model_validate({"revenue_breakdown": BREAKDOWN}))["decision"]
    assert "Revenue mix" in decision and "Cloud 25.0%" in decision and len(decision.splitlines()) < 12


@pytest.mark.unit
def test_the_full_render_includes_the_breakdown():
    assert "Search & other" in render_context(Context.model_validate({"revenue_breakdown": BREAKDOWN}))


@pytest.mark.unit
def test_the_summary_may_cite_the_checked_bucket_figures():
    known = known_values(Context.model_validate({"revenue_breakdown": BREAKDOWN}), None, None)
    values = [k.value for k in known if k.source == "context"]
    assert 25 * B in values and any(abs(v - 25.0) < 1e-6 for v in values) and any(abs(v - 108.333) < 0.01 for v in values)


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
def test_the_response_carries_the_computed_table(monkeypatch):
    manager, client = _client(monkeypatch)
    with client:
        job = client.post("/api/v1/analyses", json={"ticker": "GOOG", "trade_date": "2026-11-04",
                                                    "context": {"revenue_breakdown": BREAKDOWN}})
        assert job.status_code == 202
        table = _done(client, job.json()["id"])["revenue_breakdown"]
    manager.shutdown(wait=False)
    row = next(r for r in table["views"][0]["rows"] if r["name"] == "Cloud")
    assert row["revenue"] == 25 * B and row["share_pct"] == pytest.approx(25.0) and row["yoy_pct"] == pytest.approx(108.333, abs=0.01)
    assert table["total_revenue"] == 100 * B and table["fiscal_period"] == "Q3 2026"


@pytest.mark.unit
def test_the_response_has_no_table_without_one_supplied(monkeypatch):
    manager, client = _client(monkeypatch)
    with client:
        job = client.post("/api/v1/analyses", json={"ticker": "GOOG", "trade_date": "2026-11-04"})
        assert _done(client, job.json()["id"])["revenue_breakdown"] is None
    manager.shutdown(wait=False)


@pytest.mark.unit
def test_the_dry_run_lists_a_breakdown_that_does_not_add_up(monkeypatch):
    manager, client = _client(monkeypatch)
    views = copy.deepcopy(BREAKDOWN["views"])
    views[0]["segments"][1]["revenue"] = 40 * B
    with client:
        body = client.post("/api/v1/context/check", json={"revenue_breakdown": {**BREAKDOWN, "views": views}}).json()
    manager.shutdown(wait=False)
    assert not body["ok"] and body["problems"][0]["code"] == "revenue_breakdown_total"
