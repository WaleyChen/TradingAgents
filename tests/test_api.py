"""Unit tests for the TradingAgents HTTP API (no real LLM calls)."""

from __future__ import annotations

import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.jobs import JobManager
from api.schemas import CreateAnalysisRequest, JobStatus


@pytest.fixture
def mock_runner():
    def _runner(request: CreateAnalysisRequest):
        return (
            "BUY",
            {
                "market_report": "m",
                "sentiment_report": "s",
                "news_report": "n",
                "fundamentals_report": "f",
                "investment_plan": "i",
                "trader_investment_plan": "t",
                "final_trade_decision": "Buy with conviction",
            },
        )

    return _runner


@pytest.fixture
def client(mock_runner, monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_API_KEY", raising=False)
    manager = JobManager(max_workers=1, runner=mock_runner)
    app = create_app(job_manager=manager)
    with TestClient(app) as test_client:
        yield test_client
    manager.shutdown(wait=False)


@pytest.fixture
def authed_client(mock_runner, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_API_KEY", "secret-test-key")
    manager = JobManager(max_workers=1, runner=mock_runner)
    app = create_app(job_manager=manager)
    with TestClient(app) as test_client:
        yield test_client
    manager.shutdown(wait=False)


def _wait_for_terminal(client: TestClient, job_id: str, timeout: float = 2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/api/v1/analyses/{job_id}")
        assert resp.status_code == 200
        body = resp.json()
        if body["status"] in (JobStatus.completed.value, JobStatus.failed.value):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish in time")


@pytest.mark.unit
def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.unit
def test_create_and_poll_analysis(client):
    resp = client.post(
        "/api/v1/analyses",
        json={"ticker": "pltr", "trade_date": "2026-07-26"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["ticker"] == "PLTR"
    job_id = body["id"]

    done = _wait_for_terminal(client, job_id)
    assert done["status"] == "completed"
    assert done["decision"] == "BUY"
    assert done["reports"]["final_trade_decision"] == "Buy with conviction"


@pytest.mark.unit
def test_list_analyses(client):
    client.post(
        "/api/v1/analyses",
        json={"ticker": "MSFT", "trade_date": "2026-04-18"},
    )
    listed = client.get("/api/v1/analyses")
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) >= 1
    assert rows[0]["ticker"] == "MSFT"


@pytest.mark.unit
def test_get_unknown_job_404(client):
    resp = client.get("/api/v1/analyses/does-not-exist")
    assert resp.status_code == 404


@pytest.mark.unit
def test_api_key_required_when_configured(authed_client):
    bare = authed_client.post(
        "/api/v1/analyses",
        json={"ticker": "AAPL", "trade_date": "2026-07-26"},
    )
    assert bare.status_code == 401

    ok = authed_client.post(
        "/api/v1/analyses",
        headers={"X-API-Key": "secret-test-key"},
        json={"ticker": "AAPL", "trade_date": "2026-07-26"},
    )
    assert ok.status_code == 202

    bearer = authed_client.get(
        f"/api/v1/analyses/{ok.json()['id']}",
        headers={"Authorization": "Bearer secret-test-key"},
    )
    assert bearer.status_code == 200


@pytest.mark.unit
def test_failed_job_surfaces_error(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_API_KEY", raising=False)

    def boom(_request: CreateAnalysisRequest):
        raise RuntimeError("llm unavailable")

    manager = JobManager(max_workers=1, runner=boom)
    app = create_app(job_manager=manager)
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/analyses",
            json={"ticker": "MSFT", "trade_date": str(date(2026, 4, 18))},
        )
        job_id = resp.json()["id"]
        done = _wait_for_terminal(client, job_id)
        assert done["status"] == "failed"
        assert "llm unavailable" in done["error"]
    manager.shutdown(wait=False)
