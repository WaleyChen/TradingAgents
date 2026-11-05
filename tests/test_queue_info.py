"""A job waiting in line says where it is and when it should start and finish."""

from __future__ import annotations

import threading
import time
from datetime import date

import pytest

from api.jobs import JobManager
from api.progress import median_total, queue_estimate
from api.schemas import CreateAnalysisRequest, JobStatus
from api.service import AnalysisResult


def _request():
    return CreateAnalysisRequest(ticker="ACME", trade_date=date(2026, 11, 2))


# --- the arithmetic ------------------------------------------------------------

@pytest.mark.unit
def test_one_worker_each_queued_job_starts_when_the_one_before_it_ends():
    results = [queue_estimate(1, [300], k, 600) for k in range(3)]
    assert results == [(300, 900), (900, 1500), (1500, 2100)]            # (starts in, finishes in)


@pytest.mark.unit
def test_two_workers_a_job_takes_whichever_slot_frees_first():
    assert queue_estimate(2, [300, 100], 0, 600) == (100, 700)
    assert queue_estimate(2, [300, 100], 1, 600) == (300, 900)
    assert queue_estimate(2, [300, 100], 2, 600) == (700, 1300)


@pytest.mark.unit
def test_an_idle_slot_means_it_starts_at_once():
    assert queue_estimate(3, [300], 0, 600) == (0, 600)
    assert queue_estimate(3, [300], 1, 600) == (0, 600)
    assert queue_estimate(3, [300], 2, 600) == (300, 900)


@pytest.mark.unit
def test_a_running_job_with_no_estimate_counts_as_a_typical_job():
    assert queue_estimate(1, [None], 0, 600) == (600, 1200)


@pytest.mark.unit
def test_with_no_history_the_times_are_unknown_not_guessed():
    assert queue_estimate(1, [300], 0, None) == (None, None)
    assert queue_estimate(1, [], 0, None) == (0, None)         # nothing ahead: it starts now, the length is unknown


@pytest.mark.unit
def test_the_typical_job_is_the_median_of_recent_totals():
    history = [{"a": 100, "b": 200}, {"a": 120, "b": 280}, {"a": 90, "b": 410}]
    assert median_total(history) == 400                         # totals 300, 400, 500
    assert median_total([]) is None


# --- in the manager ------------------------------------------------------------

def _wait_for(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met")


def _held(workers=1):
    gate = threading.Event()
    manager = JobManager(max_workers=workers, runner=lambda r: (gate.wait(5), AnalysisResult("Hold", {}))[1])
    manager._history.append({"analysts": 60.0, "debate": 300.0, "decision": 40.0})      # a 400 s typical job
    return manager, gate


@pytest.mark.unit
def test_a_job_in_line_says_its_place_and_how_long_until_it_starts_and_finishes():
    manager, gate = _held()
    first, second, third = (manager.submit(_request()) for _ in range(3))
    _wait_for(lambda: manager.get(first.id).status == JobStatus.running)
    running, queued_one, queued_two = (manager.get(j.id).to_response() for j in (first, second, third))
    assert running.queue is None
    assert (queued_one.queue.position, queued_one.queue.jobs_ahead) == (1, 1)
    assert (queued_two.queue.position, queued_two.queue.jobs_ahead) == (2, 2)
    assert queued_two.queue.starts_in_seconds > queued_one.queue.starts_in_seconds
    assert queued_two.queue.eta_seconds == queued_two.queue.starts_in_seconds + 400
    assert queued_one.progress is None
    gate.set()


@pytest.mark.unit
def test_the_line_moves_up_as_jobs_finish_and_a_started_job_has_no_queue():
    manager, gate = _held()
    first, second, third = (manager.submit(_request()) for _ in range(3))
    gate.set()
    for job in (first, second, third):
        _wait_for(lambda j=job: manager.get(j.id).status == JobStatus.completed)
        assert manager.get(job.id).to_response().queue is None


@pytest.mark.unit
def test_with_two_workers_two_run_and_the_third_waits_behind_both():
    manager, gate = _held(workers=2)
    jobs = [manager.submit(_request()) for _ in range(3)]
    _wait_for(lambda: sum(manager.get(j.id).status == JobStatus.running for j in jobs) == 2)
    waiting = manager.get(jobs[2].id).to_response()
    assert waiting.queue.position == 1 and waiting.queue.jobs_ahead == 2
    gate.set()


@pytest.mark.unit
def test_with_no_history_the_place_in_line_is_still_shown_without_times():
    gate = threading.Event()
    manager = JobManager(max_workers=1, runner=lambda r: (gate.wait(5), AnalysisResult("Hold", {}))[1])
    first, second = manager.submit(_request()), manager.submit(_request())
    _wait_for(lambda: manager.get(first.id).status == JobStatus.running)
    queue = manager.get(second.id).to_response().queue
    assert queue.position == 1 and queue.starts_in_seconds is None and queue.eta_seconds is None
    gate.set()


@pytest.mark.unit
def test_a_finished_or_failed_job_has_no_queue():
    manager = JobManager(runner=lambda r: AnalysisResult("Hold", {}))
    job = manager.submit(_request())
    _wait_for(lambda: manager.get(job.id).status == JobStatus.completed)
    assert manager.get(job.id).to_response().queue is None

    def boom(r):
        raise RuntimeError("x")

    failing = JobManager(runner=boom)
    bad = failing.submit(_request())
    _wait_for(lambda: failing.get(bad.id).status == JobStatus.failed)
    assert failing.get(bad.id).to_response().queue is None
