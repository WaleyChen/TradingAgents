"""The contract the Rails app reads: a schema file kept in step with the code, and a free dry-run check."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.jobs import JobManager
from tests.test_context_financials import GOOD
from tradingagents.context.schema import SCHEMA_PATH, context_json_schema


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_API_KEY", raising=False)
    with TestClient(create_app(job_manager=JobManager(max_workers=1, runner=lambda r: ("Hold", {})))) as c:
        yield c


@pytest.mark.unit
def test_the_schema_file_matches_the_code():
    assert json.loads(Path(SCHEMA_PATH).read_text()) == context_json_schema(), (
        "docs/context.schema.json is stale: run `python -m tradingagents.context.schema`")


@pytest.mark.unit
def test_the_schema_describes_the_closed_context():
    schema = context_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"schema_version", "financials", "price", "events", "company", "technicals", "ownership",
                                       "valuation_history", "market_backdrop", "prior_reports", "earnings_release", "notes"}
    assert "Statement" in schema["$defs"] and "Balance" in schema["$defs"]


@pytest.mark.unit
def test_the_check_endpoint_passes_consistent_data_and_shows_the_metrics(client):
    body = client.post("/api/v1/context/check", json={"financials": GOOD}).json()
    assert body["ok"] is True and body["problems"] == []
    assert body["metrics"]["gross_margin_pct"] == pytest.approx(80.71, abs=0.01)


@pytest.mark.unit
def test_the_check_endpoint_lists_every_problem_without_failing(client):
    bad = copy.deepcopy(GOOD)
    bad["quarterly"][3]["income"]["revenue"] = 40e9
    bad["balance"][0]["total_assets"] = 250e9
    response = client.post("/api/v1/context/check", json={"financials": bad})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert any("quarters add up to" in p["message"] for p in body["problems"])
    assert any(p["code"] == "balance_mismatch" for p in body["problems"])


@pytest.mark.unit
def test_the_check_endpoint_still_refuses_a_malformed_context(client):
    assert client.post("/api/v1/context/check", json={"price": {"close": 1}}).status_code == 422


@pytest.mark.unit
def test_the_check_endpoint_needs_the_api_key_when_one_is_set(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_API_KEY", "secret")
    with TestClient(create_app(job_manager=JobManager(max_workers=1, runner=lambda r: ("Hold", {})))) as c:
        assert c.post("/api/v1/context/check", json={"financials": GOOD}).status_code == 401
        ok = c.post("/api/v1/context/check", json={"financials": GOOD}, headers={"X-API-Key": "secret"})
        assert ok.status_code == 200


@pytest.mark.unit
def test_the_check_endpoint_applies_the_same_ticker_and_date_ties_when_asked(client):
    url = "/api/v1/context/check?ticker=AMZN&trade_date=2026-11-04"
    body = client.post(url, json={"financials": GOOD}).json()
    assert body["ok"] is False and body["problems"][0]["code"] == "ticker_mismatch"
    assert client.post("/api/v1/context/check?ticker=MU", json={"financials": GOOD}).json()["ok"] is True


@pytest.mark.unit
def test_the_check_endpoint_returns_the_digest(client):
    from tradingagents.context import Context
    from tradingagents.context.digest import context_digest

    body = client.post("/api/v1/context/check", json={"financials": GOOD}).json()
    assert body["context_digest"] == context_digest(Context.model_validate({"financials": GOOD}))
