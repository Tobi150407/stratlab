"""Command-line entry point.

    python -m app.cli init-db                 # create tables + default schedules (idempotent)
    python -m app.cli engine | worker | scheduler | watchdog | api
    python -m app.cli healthcheck <component> # exit 1 if component heartbeat is stale
    python -m app.cli seed-demo               # add example strategies + backfill jobs
    python -m app.cli enqueue <kind> '<json>'
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone

from app.logging_setup import setup_logging


def _wait_for_db(timeout: int = 120) -> None:
    from app.db import db_ping

    end = time.time() + timeout
    while not db_ping():
        if time.time() > end:
            sys.exit("database not reachable")
        time.sleep(2)


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        sys.exit(__doc__)
    cmd, args = argv[0], argv[1:]
    setup_logging(cmd)

    if cmd == "healthcheck":
        from app.config import get_settings
        from app.db import session_scope
        from app.models import Heartbeat

        comp = args[0]
        try:
            with session_scope() as s:
                hb = s.get(Heartbeat, comp)
                if hb is None:
                    sys.exit(1)
                seen = hb.last_seen_at if hb.last_seen_at.tzinfo else hb.last_seen_at.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - seen).total_seconds()
                sys.exit(0 if age < get_settings().component_dead_after_seconds else 1)
        except SystemExit:
            raise
        except Exception:
            sys.exit(1)

    _wait_for_db()
    from app.db import init_db

    if cmd == "init-db":
        from app.jobs.scheduler import seed_default_schedules

        init_db()
        seed_default_schedules()
        print("database initialised")
    elif cmd == "engine":
        from app.engine.runner import Engine

        Engine().run_forever()
    elif cmd == "worker":
        from app.jobs.worker import Worker

        Worker().run_forever()
    elif cmd == "scheduler":
        from app.jobs.scheduler import run_forever

        run_forever()
    elif cmd == "watchdog":
        from app.monitor.watchdog import Watchdog

        Watchdog().run_forever()
    elif cmd == "api":
        import os

        import uvicorn

        uvicorn.run("app.api.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
                    proxy_headers=True, log_config=None)
    elif cmd == "seed-demo":
        from sqlalchemy import select

        from app.db import session_scope
        from app.jobs.queue import enqueue
        from app.models import Strategy

        demo = [
            ("Gold SMA 10/30", "sma_cross", "GOLD", "5m", {}),
            ("Gold Donchian 20", "donchian_breakout", "GOLD", "15m", {}),
            ("Nasdaq RSI 14", "rsi_reversion", "NASDAQ", "5m", {}),
        ]
        with session_scope() as s:
            for name, kind, inst, tf, p in demo:
                if not s.scalar(select(Strategy.id).where(Strategy.name == name)):
                    s.add(Strategy(name=name, kind=kind, instrument=inst, timeframe=tf, params=p, paper_active=True))
        for inst, tf in {("GOLD", "5m"), ("GOLD", "15m"), ("NASDAQ", "5m")}:
            enqueue("backfill", {"instrument": inst, "timeframe": tf, "days": 60})
        print("demo strategies created; backfill jobs queued")
    elif cmd == "enqueue":
        from app.jobs.queue import enqueue

        print(enqueue(args[0], json.loads(args[1]) if len(args) > 1 else {}))
    else:
        sys.exit(f"unknown command {cmd}\n{__doc__}")


if __name__ == "__main__":
    main()
