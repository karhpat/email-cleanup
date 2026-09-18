"""Single background job runner with progress. See docs/ARCHITECTURE.md "Jobs"."""
from __future__ import annotations

import threading
import time
import uuid
from typing import Callable, Optional

from backend.models import JobProgress, JobState


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class JobBusy(Exception):
    """Raised by JobRunner.start() when a job is already running."""


class Job:
    """Handle passed to a running job's fn: id, kind, cancellation, progress."""

    def __init__(self, job_id: str, kind: str, runner: "JobRunner"):
        self.id = job_id
        self.kind = kind
        self._runner = runner
        self._lock = threading.RLock()
        self.state = JobState(id=job_id, kind=kind, state="running", started_at=_now_iso())

    @property
    def cancelled(self) -> bool:
        return self._runner._cancel_flag

    def progress(self, done: int, total: int, message: str = "") -> None:
        with self._lock:
            self.state.progress = JobProgress(done=done, total=total, message=message)


class JobRunner:
    """Runs at most one job at a time in a background thread."""

    def __init__(self):
        self._lock = threading.RLock()
        self._job: Optional[Job] = None
        self._cancel_flag = False

    def is_running(self) -> bool:
        with self._lock:
            return self._job is not None and self._job.state.state == "running"

    def current(self) -> Optional[JobState]:
        with self._lock:
            return self._job.state.model_copy(deep=True) if self._job else None

    def start(self, kind: str, fn: Callable[[Job], Optional[dict]]) -> str:
        with self._lock:
            if self.is_running():
                raise JobBusy(f"a '{self._job.kind}' job is already running")
            job_id = uuid.uuid4().hex
            self._cancel_flag = False
            job = Job(job_id, kind, self)
            self._job = job

        def _run() -> None:
            try:
                result = fn(job)
                with job._lock:
                    if job.cancelled:
                        job.state.state = "cancelled"
                    else:
                        job.state.state = "done"
                        job.state.result = result
            except Exception as exc:  # noqa: BLE001
                with job._lock:
                    job.state.state = "error"
                    job.state.error = str(exc)
            finally:
                with job._lock:
                    job.state.finished_at = _now_iso()

        threading.Thread(target=_run, daemon=True, name=f"job-{kind}-{job_id}").start()
        return job_id

    def cancel(self) -> None:
        with self._lock:
            self._cancel_flag = True
