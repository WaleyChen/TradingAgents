"""Jobs kept on disk, so an id still answers after a restart or a deploy.

SQLite on the service's persistent disk. Each job is saved when it is queued, when it starts and
when it ends. A job that was running or queued when the process died cannot be resumed, so on
the next start it is marked failed as interrupted; its id still resolves. If the folder cannot be
written, the service logs it and carries on in memory, as it did before.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api.jobs import AnalysisJob
from api.schemas import CreateAnalysisRequest, JobStatus

logger = logging.getLogger(__name__)

DEFAULT_KEEP = 500          # the newest jobs to keep


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _to_record(job: AnalysisJob) -> dict[str, Any]:
    return {
        "id": job.id, "request": job.request.model_dump(mode="json"), "status": job.status.value,
        "decision": job.decision, "reports": job.reports, "plan": job.plan, "plan_error": job.plan_error,
        "summary": job.summary, "summary_figures": job.summary_figures, "summary_problems": job.summary_problems,
        "error": job.error, "usage": job.usage, "durations": job.durations,
        "created_at": _iso(job.created_at), "started_at": _iso(job.started_at),
        "completed_at": _iso(job.completed_at),
    }


def _from_record(rec: dict[str, Any]) -> AnalysisJob:
    return AnalysisJob(
        id=rec["id"], request=CreateAnalysisRequest.model_validate(rec["request"]),
        status=JobStatus(rec["status"]), decision=rec["decision"], reports=rec["reports"], plan=rec["plan"],
        plan_error=rec["plan_error"], summary=rec["summary"], summary_figures=rec["summary_figures"],
        summary_problems=rec["summary_problems"], error=rec["error"], usage=rec["usage"],
        durations=rec["durations"], created_at=_when(rec["created_at"]), started_at=_when(rec["started_at"]),
        completed_at=_when(rec["completed_at"]),
    )


class JobStore:
    def __init__(self, path: str | os.PathLike, keep: int = DEFAULT_KEEP) -> None:
        self._path, self._keep = Path(path), keep
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        with self._lock, self._conn:
            self._conn.execute("CREATE TABLE IF NOT EXISTS jobs "
                               "(id TEXT PRIMARY KEY, created_at TEXT, status TEXT, data TEXT)")

    def save(self, job: AnalysisJob) -> None:
        data = json.dumps(_to_record(job))
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO jobs (id, created_at, status, data) VALUES (?, ?, ?, ?)",
                               (job.id, _iso(job.created_at), job.status.value, data))
            self._conn.execute("DELETE FROM jobs WHERE id NOT IN "
                               "(SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?)", (self._keep,))

    def load_all(self) -> list[AnalysisJob]:
        """Every kept job, oldest first. A row that no longer parses is skipped, not fatal."""
        with self._lock:
            rows = self._conn.execute("SELECT id, data FROM jobs ORDER BY created_at ASC").fetchall()
        jobs = []
        for job_id, data in rows:
            try:
                jobs.append(_from_record(json.loads(data)))
            except Exception as exc:  # old shape, hand-edited, truncated: lose that job, keep the rest
                logger.warning("Skipping stored job %s that cannot be read (%s)", job_id, type(exc).__name__)
        return jobs


def open_store(path: str | os.PathLike, keep: int = DEFAULT_KEEP) -> JobStore | None:
    """The store at ``path``, or None (with a warning) when that location cannot be used."""
    try:
        return JobStore(path, keep)
    except (OSError, sqlite3.Error) as exc:
        logger.warning("Jobs will not survive a restart: cannot use %s (%s: %s)", path, type(exc).__name__, exc)
        return None


def boot_marker(directory: str | os.PathLike) -> dict[str, Any] | None:
    """Count this start in a small file beside the jobs. Two or more means the disk persisted."""
    folder = Path(directory)
    marker = folder / ".boots.json"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        try:
            previous = json.loads(marker.read_text())
            boots, first_seen = int(previous["boots"]) + 1, previous["first_seen"]
        except (OSError, ValueError, KeyError, TypeError):
            boots, first_seen = 1, datetime.now(UTC).isoformat()
        record = {"boots": boots, "first_seen": first_seen}
        fd, temp = tempfile.mkstemp(dir=folder)
        with os.fdopen(fd, "w") as handle:
            json.dump(record, handle)
        os.replace(temp, marker)
        return record
    except OSError as exc:
        logger.warning("Cannot write the boot marker in %s (%s)", folder, exc)
        return None
