"""Cron-style scheduler: turns ``ResearchSchedule`` rows into queued jobs.

Schedules are stored in the DB with an explicit IANA timezone, so "every weekday at
18:00 New York time" stays correct across daylight-saving changes. The ``next_run_at``
pointer is advanced in the same transaction that enqueues the job, so a restart never
double-fires a schedule; missed runs while the scheduler was down fire once on recovery.
"""
from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select

from app.db import is_postgres, session_scope
from app.heartbeat import beat
from app.jobs.queue import enqueue
from app.models import Job, ResearchSchedule

log = logging.getLogger(__name__)
UTC = timezone.utc
COMPONENT = "scheduler"

DEFAULT_SCHEDULES = [
    # name, job kind, cron, timezone, payload
    ("daily-performance", "daily_performance", "15 22 * * *", "UTC", {}),
    ("hourly-deterioration-check", "deterioration_check", "5 * * * *", "UTC", {}),
    ("weekly-optimisation", "optimise", "0 2 * * 6", "UTC", {"days": 90}),
    ("weekly-validation", "validate", "0 4 * * 6", "UTC", {"days": 120, "folds": 4}),
    ("daily-comparison", "compare", "30 22 * * *", "UTC", {"days": 60}),
]


def next_run(cron: str, tz: str, after: datetime) -> datetime:
    local = after.astimezone(ZoneInfo(tz))
    return croniter(cron, local).get_next(datetime).astimezone(UTC)


def seed_default_schedules() -> None:
    now = datetime.now(UTC)
    with session_scope() as s:
        existing = set(s.scalars(select(ResearchSchedule.name)).all())
        for name, kind, cron, tz, payload in DEFAULT_SCHEDULES:
            if name not in existing:
                s.add(ResearchSchedule(name=name, kind=kind, cron=cron, timezone=tz, payload=payload,
                                       next_run_at=next_run(cron, tz, now)))


def tick(now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    fired = 0
    with session_scope() as s:
        q = select(ResearchSchedule).where(ResearchSchedule.enabled.is_(True), ResearchSchedule.next_run_at <= now)
        if is_postgres():
            q = q.with_for_update(skip_locked=True)
        for sch in s.scalars(q).all():
            # don't pile up identical jobs if the previous run is still queued/running
            busy = s.scalar(select(Job.id).where(Job.schedule_id == sch.id, Job.status.in_(("queued", "running"))))
            if busy:
                log.info("schedule %s skipped: previous job %s still in progress", sch.name, busy)
            else:
                enqueue(sch.kind, sch.payload, schedule_id=sch.id, session=s)
                fired += 1
            sch.last_run_at = now
            sch.next_run_at = next_run(sch.cron, sch.timezone, now)
        for sch in s.scalars(select(ResearchSchedule).where(ResearchSchedule.next_run_at.is_(None))).all():
            sch.next_run_at = next_run(sch.cron, sch.timezone, now)
    return fired


def run_forever() -> None:
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    seed_default_schedules()
    log.info("scheduler started")
    while running:
        try:
            n = tick()
            beat(COMPONENT, status="ok", fired_last_tick=n)
        except Exception as e:
            log.exception("scheduler tick failed")
            try:
                beat(COMPONENT, status="error", error=str(e)[:300])
            except Exception:
                pass
        for _ in range(30):
            if not running:
                break
            time.sleep(1)
