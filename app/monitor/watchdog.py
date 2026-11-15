"""Watchdog process: evaluates system health, raises / clears alerts, delivers notifications
and pings an optional external dead-man's switch (e.g. healthchecks.io) so that a total
outage of the host — including the watchdog itself — is also reported."""
from __future__ import annotations

import logging
import os
import signal
import time
from datetime import datetime, timedelta, timezone

import httpx

from app import notify as N
from app.db import session_scope
from app.heartbeat import beat
from app.models import Strategy
from app.monitor.status import CORE_COMPONENTS, build_status

log = logging.getLogger(__name__)
UTC = timezone.utc
COMPONENT = "watchdog"


class Watchdog:
    def __init__(self):
        self.down: set[str] = set()  # currently-open incidents
        self.deadman_url = os.environ.get("SL_DEADMAN_URL", "")

    def _open(self, key: str, kind: str, title: str, body: str, severity="critical"):
        if key not in self.down:
            self.down.add(key)
        N.notify(kind, title, body, severity=severity, dedup_key=key)  # cooldown limits repeats

    def _close(self, key: str, title: str):
        if key in self.down:
            self.down.discard(key)
            N.notify("recovered", title, "Back to normal.", severity="info")

    def check(self) -> dict:
        st = build_status()
        if st["database"]["status"] != "up":
            log.error("database unreachable")
            return st  # can't record alerts; external dead-man's switch will fire

        for c in st["components"]:
            name = c["component"]
            if name not in CORE_COMPONENTS or name == COMPONENT:
                continue
            key = f"outage:{name}"
            if not c["alive"]:
                self._open(key, N.SERVICE_OUTAGE, f"Service outage: {name}",
                           f"No heartbeat for {c.get('age_seconds', '?')}s (status {c['status']}).")
            else:
                self._close(key, f"Service recovered: {name}")

        for f in st["market_data"]:
            key = f"feed:{f['instrument']}"
            if f["state"] in ("interrupted", "delayed"):
                self._open(key, N.DATA_INTERRUPTION, f"Market data {f['state']}: {f['instrument']}",
                           f"Market open; last candle {f.get('last_candle_ts')}, last success "
                           f"{f.get('last_success_at')}. Last error: {f.get('last_error')}. "
                           "New entries are blocked by the stale-price guard.")
            else:
                self._close(key, f"Market data recovered: {f['instrument']}")

        # Active strategy not evaluated while its market has been delivering data
        engine_alive = any(c["component"] == "engine" and c["alive"] for c in st["components"])
        feeds = {f["instrument"]: f for f in st["market_data"]}
        for r in st["strategies"]:
            if not r["paper_active"]:
                continue
            key = f"strategy:{r['id']}"
            last = datetime.fromisoformat(r["last_evaluated_at"]) if r["last_evaluated_at"] else None
            live = feeds.get(r["instrument"], {}).get("state") == "live"
            stalled = engine_alive and live and (last is None or datetime.now(UTC) - last > timedelta(minutes=30))
            if r["status"] == "error" or stalled:
                why = r["status_detail"] if r["status"] == "error" else f"not evaluated since {r['last_evaluated_at']}"
                self._open(key, N.STRATEGY_STOPPED, f"Strategy stopped operating: {r['name']}", why)
                if stalled:
                    with session_scope() as s:
                        row = s.get(Strategy, r["id"])
                        if row.status != "error":
                            row.status, row.status_detail = "error", why
            else:
                self._close(key, f"Strategy operating again: {r['name']}")
        return st

    def run_forever(self):
        running = True

        def stop(*_):
            nonlocal running
            running = False

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        log.info("watchdog started")
        beat(COMPONENT, status="starting")
        time.sleep(20)  # give other services time to report in after a deploy
        while running:
            try:
                st = self.check()
                N.deliver_pending()
                beat(COMPONENT, status="ok", overall=st.get("overall"), problems=st.get("problems", []))
                if self.deadman_url and st.get("database", {}).get("status") == "up":
                    httpx.get(self.deadman_url, timeout=10)
            except Exception:
                log.exception("watchdog cycle failed")
            for _ in range(30):
                if not running:
                    break
                time.sleep(1)
