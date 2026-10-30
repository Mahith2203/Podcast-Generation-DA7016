"""Tiny in-process job runner. One job per project at a time; progress is polled by the UI."""
from __future__ import annotations

import logging
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from .util import now_iso

log = logging.getLogger("podcast.jobs")


class JobCancelled(Exception):
    """Raised inside a job when the user asked to stop it."""
    pass


class JobBusy(RuntimeError):
    """Raised when a project already has a job queued or running."""
    pass


@dataclass
class Job:
    """One background task (generate, re-voice, video...) with its status, progress and message, as shown by the progress
    bar.
    """
    id: str
    project_id: str
    kind: str
    status: str = "queued"  # queued | running | done | error | cancelled
    progress: float = 0.0
    message: str = ""
    error: Optional[str] = None
    created_at: str = field(default_factory=now_iso)
    finished_at: Optional[str] = None
    cancel_requested: bool = False

    def public(self) -> dict:
        d = asdict(self)
        d.pop("cancel_requested", None)
        return d


ProgressFn = Callable[[float, str], None]


class JobManager:
    """Runs background jobs on a small thread pool and allows one active job per project."""
    def __init__(self, max_workers: int = 2):
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="job")
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def submit(self, project_id: str, kind: str, fn: Callable[[ProgressFn], None]) -> Job:
        """Queue `fn(progress)` for a project. Raises `JobBusy` if that project already has an active job."""
        with self._lock:
            for j in self._jobs.values():
                if j.project_id == project_id and j.status in {"queued", "running"}:
                    raise JobBusy(f"'{j.kind}' is already running for this project.")
            job = Job(id=uuid.uuid4().hex[:12], project_id=project_id, kind=kind)
            self._jobs[job.id] = job
            # keep memory bounded
            if len(self._jobs) > 200:
                for jid in [k for k, v in self._jobs.items() if v.status in {"done", "error", "cancelled"}][:100]:
                    self._jobs.pop(jid, None)
        self._pool.submit(self._run, job, fn)
        return job

    def _run(self, job: Job, fn: Callable[[ProgressFn], None]) -> None:
        def progress(frac: float, msg: str = "") -> None:
            if job.cancel_requested:
                raise JobCancelled()
            job.progress = max(0.0, min(1.0, float(frac)))
            if msg:
                job.message = msg

        job.status = "running"
        try:
            fn(progress)
            job.status = "done"
            job.progress = 1.0
        except JobCancelled:
            job.status = "cancelled"
            job.message = "Stopped"
        except Exception as e:  # noqa: BLE001
            log.error("job %s (%s) failed: %s\n%s", job.id, job.kind, e, traceback.format_exc())
            job.status = "error"
            job.error = str(e) or e.__class__.__name__
        finally:
            job.finished_at = now_iso()

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def active_for(self, project_id: str) -> Optional[Job]:
        for j in self._jobs.values():
            if j.project_id == project_id and j.status in {"queued", "running"}:
                return j
        return None

    def latest_for(self, project_id: str) -> Optional[Job]:
        js = [j for j in self._jobs.values() if j.project_id == project_id]
        return max(js, key=lambda j: j.created_at) if js else None

    def cancel(self, job_id: str) -> bool:
        j = self._jobs.get(job_id)
        if j and j.status in {"queued", "running"}:
            j.cancel_requested = True
            return True
        return False
