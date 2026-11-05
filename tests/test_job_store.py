"""Jobs that outlive a restart: the store, the manager on top of it, the ETA history and the health marker."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.jobs import INTERRUPTED_MESSAGE, JobManager
from api.progress import StageTracker
from api.schemas import CreateAnalysisRequest, JobStatus
from api.service import AnalysisResult
from api.store import JobStore, boot_marker, open_store
from tests.test_plan import GOOD
from tradingagents.plan import Plan


def _request(ticker="ACME"):
    return CreateAnalysisRequest(ticker=ticker, trade_date=date(2026, 11, 2))


def _wait(manager, job_id, status=JobStatus.completed):
    end = time.time() + 3
    while time.time() < end:
        job = manager.get(job_id)
        if job is not None and job.status == status:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job never reached {status}")


def _result(*a, **k):
    return AnalysisResult("Underweight", {"market_report": "m"}, Plan.model_validate(GOOD).model_dump(mode="json"),
                          None, "A summary.",
                          [{"label": "x", "value": 1.0, "unit": "pct", "source": "context", "period": None,
                            "verified": True}], [])


@pytest.fixture
def db(tmp_path):
    return tmp_path / "jobs" / "jobs.db"


@pytest.mark.unit
def test_a_finished_job_is_still_there_after_a_restart_with_everything_it_returned(db):
    first = JobManager(runner=_result, store=open_store(db))
    job = first.submit(_request())
    _wait(first, job.id)
    before = first.get(job.id).to_response()

    second = JobManager(runner=_result, store=open_store(db))        # a new process, the same disk
    after = second.get(job.id).to_response()
    assert after == before
    assert after.decision == "Underweight" and after.summary == "A summary." and after.plan is not None
    assert after.summary_figures[0].verified is True and after.rating_label == "Weak Sell"


@pytest.mark.unit
def test_a_job_that_was_running_is_failed_as_interrupted_not_lost(db):
    gate = threading.Event()
    first = JobManager(runner=lambda r: (gate.wait(3), _result())[1], store=open_store(db))
    job = first.submit(_request())
    _wait(first, job.id, JobStatus.running)                          # the process "dies" here

    second = JobManager(runner=_result, store=open_store(db))
    got = second.get(job.id)
    assert got.status == JobStatus.failed and got.error == INTERRUPTED_MESSAGE and got.completed_at is not None
    again = JobManager(runner=_result, store=open_store(db)).get(job.id)    # and it stays failed afterwards
    assert again.status == JobStatus.failed and again.error == INTERRUPTED_MESSAGE
    gate.set()


@pytest.mark.unit
def test_a_queued_job_is_failed_as_interrupted_too(db):
    gate = threading.Event()
    first = JobManager(max_workers=1, runner=lambda r: (gate.wait(3), _result())[1], store=open_store(db))
    running, queued = first.submit(_request()), first.submit(_request())
    _wait(first, running.id, JobStatus.running)
    second = JobManager(runner=_result, store=open_store(db))
    assert second.get(queued.id).status == JobStatus.failed
    gate.set()


@pytest.mark.unit
def test_the_estimate_history_survives_a_restart(db):
    def runner(request, board=None):
        tracker = StageTracker(["analysts", "debate"], board)
        import uuid
        tracker.on_chain_start({}, {}, run_id=uuid.uuid4(), metadata={"langgraph_node": "Market Analyst"})
        tracker.on_chain_start({}, {}, run_id=uuid.uuid4(), metadata={"langgraph_node": "Bull Researcher"})
        return _result()

    first = JobManager(runner=runner, store=open_store(db))
    job = first.submit(_request())
    _wait(first, job.id)
    assert len(first._history) == 1
    second = JobManager(runner=runner, store=open_store(db))
    assert len(second._history) == 1 and set(second._history[0]) == {"analysts", "debate"}


@pytest.mark.unit
def test_only_the_newest_jobs_are_kept(db):
    store = open_store(db, keep=3)
    manager = JobManager(runner=_result, store=store)
    ids = []
    for _ in range(5):
        job = manager.submit(_request())
        _wait(manager, job.id)
        ids.append(job.id)
        time.sleep(0.02)
    kept = {j.id for j in JobManager(runner=_result, store=open_store(db, keep=3)).list_jobs()}
    assert kept == set(ids[-3:])


@pytest.mark.unit
def test_a_row_that_no_longer_parses_is_skipped_not_fatal(db):
    manager = JobManager(runner=_result, store=open_store(db))
    good = manager.submit(_request())
    _wait(manager, good.id)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO jobs (id, created_at, status, data) VALUES ('bad', '2026-01-01', 'completed', '{nope')")
    again = JobManager(runner=_result, store=open_store(db))
    assert again.get(good.id) is not None and again.get("bad") is None


@pytest.mark.unit
def test_a_folder_that_cannot_be_written_means_memory_only_not_a_crash(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert open_store(blocker / "jobs.db") is None                  # its parent is a file
    manager = JobManager(runner=_result, store=open_store(blocker / "jobs.db"))
    job = manager.submit(_request())
    assert _wait(manager, job.id).decision == "Underweight"


@pytest.mark.unit
def test_two_threads_saving_at_once_do_not_lose_jobs(db):
    manager = JobManager(max_workers=4, runner=_result, store=open_store(db))
    jobs = [manager.submit(_request()) for _ in range(12)]
    for job in jobs:
        _wait(manager, job.id)
    assert {j.id for j in JobManager(runner=_result, store=open_store(db)).list_jobs()} == {j.id for j in jobs}


@pytest.mark.unit
def test_the_store_saves_the_request_and_does_not_need_the_process_that_made_it(db):
    store = JobStore(db)
    manager = JobManager(runner=_result, store=store)
    job = manager.submit(CreateAnalysisRequest(ticker="MU", trade_date=date(2026, 11, 4), analysts=["market"]))
    _wait(manager, job.id)
    loaded = JobManager(runner=_result, store=open_store(db)).get(job.id)
    assert loaded.request.ticker == "MU" and loaded.request.analysts == ["market"]


@pytest.mark.unit
def test_the_boot_marker_counts_starts_so_a_persistent_disk_shows_itself(tmp_path):
    assert boot_marker(tmp_path)["boots"] == 1
    again = boot_marker(tmp_path)
    assert again["boots"] == 2 and again["first_seen"]
    assert boot_marker(tmp_path / "missing" / "deeper")["boots"] == 1       # it creates the folder
    blocker = tmp_path / "f"
    blocker.write_text("x")
    assert boot_marker(blocker / "x") is None


@pytest.mark.unit
def test_health_says_whether_jobs_are_kept_and_how_many_times_the_storage_has_started(tmp_path, monkeypatch):
    path = tmp_path / "jobs" / "jobs.db"
    monkeypatch.setenv("TRADINGAGENTS_JOBS_DB_PATH", str(path))
    monkeypatch.delenv("TRADINGAGENTS_API_KEY", raising=False)
    with TestClient(create_app()) as client:
        first = client.get("/health").json()
    with TestClient(create_app()) as client:
        second = client.get("/health").json()
    assert first["jobs_persisted"] is True and first["storage_boots"] == 1
    assert second["storage_boots"] == 2
    with TestClient(create_app(job_manager=JobManager(runner=_result))) as client:
        plain = client.get("/health").json()
    assert plain["jobs_persisted"] is False and plain["storage_boots"] is None


@pytest.mark.unit
def test_a_restarted_service_still_answers_for_an_old_job_id(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_JOBS_DB_PATH", str(tmp_path / "jobs" / "jobs.db"))
    monkeypatch.delenv("TRADINGAGENTS_API_KEY", raising=False)
    manager = JobManager(runner=_result, store=open_store(tmp_path / "jobs" / "jobs.db"))
    with TestClient(create_app(job_manager=manager)) as client:
        job_id = client.post("/api/v1/analyses", json={"ticker": "ACME", "trade_date": "2026-11-02"}).json()["id"]
        _wait(manager, job_id)
    with TestClient(create_app()) as client:                         # the default manager opens the same file
        body = client.get(f"/api/v1/analyses/{job_id}").json()
    assert body["status"] == "completed" and body["decision"] == "Underweight"


@pytest.mark.unit
def test_a_blank_worker_count_means_the_default_not_a_crash(monkeypatch):
    from api.app import _max_workers

    monkeypatch.setenv("TRADINGAGENTS_API_MAX_WORKERS", "")
    assert _max_workers() == 1
    monkeypatch.setenv("TRADINGAGENTS_API_MAX_WORKERS", "  ")
    assert _max_workers() == 1
    monkeypatch.setenv("TRADINGAGENTS_API_MAX_WORKERS", "3")
    assert _max_workers() == 3
    monkeypatch.setenv("TRADINGAGENTS_API_MAX_WORKERS", "x")
    with pytest.raises(ValueError):
        _max_workers()
