"""System health snapshot — shared by the API, dashboard and watchdog."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.config import get_settings
from app.db import db_ping, session_scope
from app.market_hours import INSTRUMENTS, market_state, timeframe_seconds
from app.models import FeedStatus, Heartbeat, Job, Notification, PaperPosition, Strategy

UTC = timezone.utc
CORE_COMPONENTS = ("engine", "research-worker", "scheduler", "watchdog")
# components that report infrequently (backup runs every 6h by default)
STALE_AFTER = {"backup": 13 * 3600}


def _a(dt):
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def _iso(dt):
    return _a(dt).isoformat() if dt else None


def build_status(now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    cfg = get_settings()
    db_ok = db_ping()
    out: dict = {"as_of": now.isoformat(), "database": {"status": "up" if db_ok else "down"}}
    if not db_ok:
        out["overall"] = "down"
        return out

    with session_scope() as s:
        # ---- components
        hbs = {h.component: h for h in s.scalars(select(Heartbeat)).all()}
        comps = []
        for name in sorted(set(CORE_COMPONENTS) | set(hbs)):
            h = hbs.get(name)
            if h is None:
                comps.append({"component": name, "status": "missing", "alive": False})
                continue
            age = (now - _a(h.last_seen_at)).total_seconds()
            alive = age < STALE_AFTER.get(name, cfg.component_dead_after_seconds)
            comps.append({
                "component": name, "status": h.status if alive else "dead", "alive": alive,
                "last_seen_at": _iso(h.last_seen_at), "age_seconds": round(age, 1),
                "started_at": _iso(h.started_at), "pid": h.pid, "host": h.host,
                "cpu_percent": h.cpu_percent, "memory_mb": h.memory_mb, "detail": h.detail,
            })

        # ---- market data
        strategies = s.scalars(select(Strategy)).all()
        tfs_by_inst: dict[str, set[str]] = {}
        for st in strategies:
            if st.paper_active:
                tfs_by_inst.setdefault(st.instrument, set()).add(st.timeframe)
        feeds = []
        for inst in INSTRUMENTS:
            fs = s.get(FeedStatus, inst)
            ms = market_state(inst, now)
            item = {"instrument": inst, "market": ms.as_dict(), "in_use": inst in tfs_by_inst}
            if fs:
                item.update(connected=fs.connected, last_success_at=_iso(fs.last_success_at),
                            last_candle_ts=_iso(fs.last_candle_ts), last_error=fs.last_error,
                            consecutive_failures=fs.consecutive_failures)
            # delayed = market has been open long enough that a newer candle should exist
            state = "closed"
            if ms.is_open and item["in_use"]:
                tf = min(timeframe_seconds(t) for t in tfs_by_inst[inst])
                expected_by = timedelta(seconds=2 * tf + cfg.stale_grace_seconds)
                last_c = _a(fs.last_candle_ts) if fs else None
                last_ok = _a(fs.last_success_at) if fs else None
                if fs is None or not fs.connected or last_ok is None or now - last_ok > timedelta(seconds=cfg.feed_timeout_seconds):
                    state = "interrupted"
                elif last_c is None or now - last_c > expected_by and _opened_for(inst, now) > expected_by:
                    state = "delayed"
                else:
                    state = "live"
            elif ms.is_open:
                state = "open (unused)"
            item["state"] = state
            feeds.append(item)

        # ---- strategies
        strat_rows = []
        for st in strategies:
            pos = s.scalar(select(PaperPosition).where(PaperPosition.strategy_id == st.id, PaperPosition.status == "open"))
            strat_rows.append({
                "id": st.id, "name": st.name, "kind": st.kind, "instrument": st.instrument,
                "timeframe": st.timeframe, "paper_active": st.paper_active, "status": st.status,
                "status_detail": st.status_detail, "last_evaluated_at": _iso(st.last_evaluated_at),
                "last_evaluated_candle": _iso(st.last_evaluated_candle),
                "open_position": None if not pos else {
                    "id": pos.id, "side": pos.side, "entry_price": pos.entry_price,
                    "last_price": pos.last_price, "unrealised_pnl": pos.unrealised_pnl,
                    "stop_loss": pos.stop_loss, "take_profit": pos.take_profit,
                },
            })

        # ---- jobs
        counts = dict(s.execute(select(Job.status, func.count()).group_by(Job.status)).all())
        failed = s.scalars(select(Job).where(Job.status == "failed").order_by(Job.finished_at.desc()).limit(10)).all()
        recent = s.scalars(select(Job).order_by(Job.id.desc()).limit(15)).all()
        last_feed_ok = max((_a(f.last_success_at) for f in s.scalars(select(FeedStatus)).all() if f.last_success_at), default=None)
        last_eval = max((_a(st.last_evaluated_at) for st in strategies if st.last_evaluated_at), default=None)
        unread = s.scalar(select(func.count()).select_from(Notification).where(Notification.read.is_(False)))

        def job_dict(j: Job):
            return {"id": j.id, "kind": j.kind, "status": j.status, "attempts": j.attempts,
                    "created_at": _iso(j.created_at), "started_at": _iso(j.started_at),
                    "finished_at": _iso(j.finished_at),
                    "error": (j.error or "").strip().splitlines()[-1] if j.error else None}

        out.update(
            components=comps,
            market_data=feeds,
            strategies=strat_rows,
            active_paper_strategies=sum(1 for r in strat_rows if r["paper_active"]),
            last_successful_market_update=_iso(last_feed_ok),
            last_strategy_evaluation=_iso(last_eval),
            jobs={"counts": counts, "recent": [job_dict(j) for j in recent], "failed": [job_dict(j) for j in failed]},
            unread_notifications=unread,
        )

    problems = []
    for c in comps:
        if c["component"] in CORE_COMPONENTS and not c["alive"]:
            problems.append(f"{c['component']} not running")
    for f in feeds:
        if f["state"] in ("interrupted", "delayed"):
            problems.append(f"{f['instrument']} data {f['state']}")
    for r in strat_rows:
        if r["paper_active"] and r["status"] == "error":
            problems.append(f"strategy {r['name']} in error")
    out["problems"] = problems
    out["overall"] = "ok" if not problems else ("degraded" if all("data" in p or "strategy" in p for p in problems) else "critical")
    return out


def _opened_for(instrument: str, now: datetime) -> timedelta:
    """How long the market has been continuously open (capped at 1 day)."""
    t = now
    step = timedelta(minutes=5)
    from app.market_hours import is_market_open

    while now - t < timedelta(days=1) and is_market_open(instrument, t - step):
        t -= step
    return now - t
