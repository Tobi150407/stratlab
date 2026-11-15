"""Research worker process.

Runs in its OWN container with CPU/memory limits and a lowered OS priority, so heavy
backtests and optimisations can never starve the paper-trading engine.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from datetime import datetime, timezone

from app.heartbeat import beat
from app.jobs import queue
from app.research.tasks import HANDLERS

log = logging.getLogger(__name__)
COMPONENT = "research-worker"


class Worker:
    def __init__(self):
        self.running = True
        self.id = f"{socket.gethostname()}:{os.getpid()}"
        self.current: dict | None = None

    def _heartbeat_loop(self):
        # separate thread: keeps reporting "alive" while a long job is running
        while self.running:
            try:
                beat(COMPONENT, status="busy" if self.current else "idle", current_job=self.current)
            except Exception:
                log.exception("heartbeat failed")
            time.sleep(15)

    def run_one(self) -> bool:
        job = queue.claim(self.id)
        if job is None:
            return False
        self.current = {"id": job.id, "kind": job.kind, "started": datetime.now(timezone.utc).isoformat()}
        log.info("running job %s (%s) attempt %s", job.id, job.kind, job.attempts)
        try:
            handler = HANDLERS.get(job.kind)
            if handler is None:
                raise ValueError(f"unknown job kind {job.kind!r}")
            result = handler(job.payload or {})
            queue.complete(job.id, result)
            log.info("job %s succeeded", job.id)
        except Exception as e:
            log.exception("job %s failed", job.id)
            queue.fail(job.id, e)
        finally:
            self.current = None
        return True

    def run_forever(self):
        try:
            os.nice(10)  # lower CPU priority than the engine if co-located
        except OSError:
            pass
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        log.info("research worker %s started", self.id)
        last_reap = 0.0
        while self.running:
            try:
                if time.time() - last_reap > 60:
                    queue.reap_expired()
                    last_reap = time.time()
                if not self.run_one():
                    time.sleep(3)
            except Exception:
                log.exception("worker loop error")
                time.sleep(5)

    def _stop(self, *_):
        # finish the current job, then exit (compose stop_grace_period allows for this)
        self.running = False
