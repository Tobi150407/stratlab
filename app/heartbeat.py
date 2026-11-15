"""Component heartbeats + process resource metrics, persisted in the DB so the API,
watchdog and dashboard can see every process's health regardless of where it runs."""
from __future__ import annotations

import os
import socket
from datetime import datetime, timezone

import psutil

from app.db import session_scope
from app.models import Heartbeat

_proc = psutil.Process(os.getpid())
_proc.cpu_percent(None)  # prime the counter


def beat(component: str, status: str = "ok", **detail) -> None:
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        hb = s.get(Heartbeat, component)
        if hb is None:
            hb = Heartbeat(component=component, started_at=now)
            s.add(hb)
        if hb.pid != os.getpid():
            hb.started_at = now  # process restarted
        hb.status = status
        hb.detail = {**(hb.detail or {}), **detail} if detail else (hb.detail or {})
        hb.pid = os.getpid()
        hb.host = socket.gethostname()
        hb.last_seen_at = now
        hb.cpu_percent = _proc.cpu_percent(None)
        hb.memory_mb = round(_proc.memory_info().rss / 1024 / 1024, 1)
