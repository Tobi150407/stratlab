"""Durable PostgreSQL-backed job queue.

* ``claim`` uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so any number of workers can pull
  jobs safely without double execution.
* Each claimed job holds a lease. If a worker dies mid-job, ``reap_expired`` returns the job
  to the queue (counting the attempt) so it is retried by another worker.
* Failures are retried with exponential backoff up to ``max_attempts``; the final failure
  marks the job ``failed`` and raises a JOB_FAILED notification.
"""
from __future__ import annotations

import logging
import traceback
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import notify as N
from app.config import get_settings
from app.db import is_postgres, session_scope
from app.models import Job

log = logging.getLogger(__name__)
UTC = timezone.utc


def enqueue(kind: str, payload: dict | None = None, *, schedule_id: int | None = None,
            max_attempts: int | None = None, session=None) -> int:
    def _do(s):
        j = Job(kind=kind, payload=payload or {}, schedule_id=schedule_id,
                max_attempts=max_attempts or get_settings().job_max_attempts, run_after=datetime.now(UTC))
        s.add(j)
        s.flush()
        return j.id

    if session is not None:
        return _do(session)
    with session_scope() as s:
        return _do(s)


def claim(worker_id: str) -> Job | None:
    now = datetime.now(UTC)
    with session_scope() as s:
        q = (
            select(Job)
            .where(Job.status == "queued", Job.run_after <= now)
            .order_by(Job.run_after, Job.id)
            .limit(1)
        )
        if is_postgres():
            q = q.with_for_update(skip_locked=True)
        job = s.scalar(q)
        if job is None:
            return None
        job.status = "running"
        job.attempts += 1
        job.locked_by = worker_id
        job.started_at = now
        job.lease_expires_at = now + timedelta(seconds=get_settings().job_lease_seconds)
        s.flush()
        s.expunge(job)
        return job


def complete(job_id: int, result: dict | None) -> None:
    with session_scope() as s:
        j = s.get(Job, job_id)
        j.status = "succeeded"
        j.result = result or {}
        j.error = None
        j.finished_at = datetime.now(UTC)
        j.lease_expires_at = None


def fail(job_id: int, exc: BaseException | str) -> str:
    err = exc if isinstance(exc, str) else "".join(traceback.format_exception(exc))[-4000:]
    with session_scope() as s:
        j = s.get(Job, job_id)
        j.error = err
        j.lease_expires_at = None
        j.locked_by = None
        if j.attempts < j.max_attempts:
            j.status = "queued"
            j.run_after = datetime.now(UTC) + timedelta(seconds=60 * 2 ** (j.attempts - 1))
            log.warning("job %s (%s) failed attempt %s/%s; will retry", j.id, j.kind, j.attempts, j.max_attempts)
        else:
            j.status = "failed"
            j.finished_at = datetime.now(UTC)
            N.notify(N.JOB_FAILED, f"Background job failed: {j.kind} #{j.id}",
                     f"Gave up after {j.attempts} attempts.\n{err.strip().splitlines()[-1] if err.strip() else ''}",
                     severity="warning", data={"job_id": j.id, "kind": j.kind}, session=s)
        return j.status


def reap_expired() -> int:
    """Requeue (or fail) jobs whose worker vanished."""
    now = datetime.now(UTC)
    n = 0
    with session_scope() as s:
        rows = s.scalars(select(Job).where(Job.status == "running", Job.lease_expires_at < now)).all()
        ids = [j.id for j in rows]
    for jid in ids:
        fail(jid, "worker lease expired (worker crashed or was restarted)")
        n += 1
    return n
