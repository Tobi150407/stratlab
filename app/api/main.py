"""HTTP API + health dashboard.

The API is a *read/control surface only*: it never runs strategies itself. Stopping the API
(or every browser) has no effect on paper trading, which lives in the engine process.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.config import get_settings
from app.db import db_ping, session_scope
from app.jobs.queue import enqueue
from app.jobs.scheduler import next_run
from app.market_hours import INSTRUMENTS, TIMEFRAMES, market_state
from app.models import Job, Notification, PaperPosition, PerformanceSnapshot, ResearchSchedule, Signal, Strategy, Trade
from app.monitor.status import build_status
from app.research.tasks import HANDLERS
from app.strategies import REGISTRY

UTC = timezone.utc
app = FastAPI(title="StrategyLab", version="1.0.0",
              description="Simulated (paper) trading only. No broker connectivity, no real orders.")
STATIC = Path(__file__).resolve().parent.parent / "static"


def require_token(authorization: str = Header(default="")) -> None:
    if authorization != f"Bearer {get_settings().api_token}":
        raise HTTPException(401, "invalid or missing bearer token")


def row(o) -> dict:
    out = {}
    for c in o.__table__.columns:
        v = getattr(o, c.name)
        if isinstance(v, datetime):
            v = (v if v.tzinfo else v.replace(tzinfo=UTC)).isoformat()
        out[c.name] = v
    return out


# --------------------------------------------------------------------------- health
@app.get("/health/live", tags=["health"])
def live():
    return {"status": "alive"}


@app.get("/health/ready", tags=["health"])
def ready():
    """For load balancers / uptime monitors: 200 only when DB is reachable and the core
    services are running. 503 otherwise."""
    if not db_ping():
        return JSONResponse({"status": "down", "problems": ["database unreachable"]}, status_code=503)
    st = build_status()
    code = 503 if st["overall"] == "critical" else 200
    return JSONResponse({"status": st["overall"], "problems": st["problems"]}, status_code=code)


@app.get("/api/status", tags=["health"])
def status():
    return build_status()


@app.get("/metrics", response_class=PlainTextResponse, tags=["health"])
def metrics():
    """Prometheus exposition format."""
    st = build_status()
    lines = [f'strategylab_database_up {1 if st["database"]["status"] == "up" else 0}']
    for c in st.get("components", []):
        lbl = f'component="{c["component"]}"'
        lines.append(f"strategylab_component_alive{{{lbl}}} {1 if c['alive'] else 0}")
        if c.get("cpu_percent") is not None:
            lines.append(f"strategylab_component_cpu_percent{{{lbl}}} {c['cpu_percent']}")
            lines.append(f"strategylab_component_memory_mb{{{lbl}}} {c['memory_mb']}")
    for f in st.get("market_data", []):
        lbl = f'instrument="{f["instrument"]}"'
        lines.append(f"strategylab_market_open{{{lbl}}} {1 if f['market']['is_open'] else 0}")
        lines.append(f"strategylab_feed_live{{{lbl}}} {1 if f['state'] in ('live', 'closed', 'open (unused)') else 0}")
    for k, v in st.get("jobs", {}).get("counts", {}).items():
        lines.append(f'strategylab_jobs{{status="{k}"}} {v}')
    lines.append(f"strategylab_active_paper_strategies {st.get('active_paper_strategies', 0)}")
    return "\n".join(lines) + "\n"


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC / "dashboard.html")


@app.get("/api/markets", tags=["markets"])
def markets():
    return [market_state(i).as_dict() for i in INSTRUMENTS]


# --------------------------------------------------------------------------- strategies
class StrategyIn(BaseModel):
    name: str
    kind: str
    instrument: str
    timeframe: str = "5m"
    params: dict[str, Any] = Field(default_factory=dict)
    quantity: float = 1.0
    paper_active: bool = False


class StrategyPatch(BaseModel):
    params: dict[str, Any] | None = None
    quantity: float | None = None
    paper_active: bool | None = None


@app.get("/api/strategy-kinds", tags=["strategies"])
def strategy_kinds():
    return {k: {"description": c.description, "default_params": c.default_params, "param_grid": c.param_grid}
            for k, c in REGISTRY.items()}


@app.get("/api/strategies", tags=["strategies"])
def list_strategies():
    with session_scope() as s:
        return [row(x) for x in s.scalars(select(Strategy).order_by(Strategy.id)).all()]


@app.post("/api/strategies", dependencies=[Depends(require_token)], tags=["strategies"], status_code=201)
def create_strategy(body: StrategyIn):
    if body.kind not in REGISTRY:
        raise HTTPException(400, f"unknown kind; choose from {sorted(REGISTRY)}")
    if body.instrument not in INSTRUMENTS:
        raise HTTPException(400, f"instrument must be one of {INSTRUMENTS}")
    if body.timeframe not in TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {sorted(TIMEFRAMES)}")
    with session_scope() as s:
        if s.scalar(select(Strategy.id).where(Strategy.name == body.name)):
            raise HTTPException(409, "name already exists")
        st = Strategy(**body.model_dump())
        s.add(st)
        s.flush()
        return row(st)


@app.patch("/api/strategies/{sid}", dependencies=[Depends(require_token)], tags=["strategies"])
def patch_strategy(sid: int, body: StrategyPatch):
    with session_scope() as s:
        st = s.get(Strategy, sid) or _404()
        if body.params is not None:
            st.params = {**st.params, **body.params}
        if body.quantity is not None:
            st.quantity = body.quantity
        if body.paper_active is not None and body.paper_active != st.paper_active:
            st.paper_active = body.paper_active
            if body.paper_active:
                st.last_evaluated_candle = None  # start from the current candle, not history
                st.status, st.status_detail = "idle", None
            else:
                st.status = "idle"
        return row(st)


@app.post("/api/positions/{pid}/close", dependencies=[Depends(require_token)], tags=["paper trading"])
def close_position_manual(pid: int):
    from app.engine.paper import close_position

    with session_scope() as s:
        pos = s.get(PaperPosition, pid) or _404()
        if pos.status != "open":
            raise HTTPException(409, "position not open")
        tr = close_position(s, pos, pos.last_price, datetime.now(UTC), "manual")
        return row(tr)


# --------------------------------------------------------------------------- paper trading
@app.get("/api/positions", tags=["paper trading"])
def positions(status: str = "open"):
    with session_scope() as s:
        return [row(p) for p in s.scalars(select(PaperPosition).where(PaperPosition.status == status)
                                         .order_by(PaperPosition.id.desc())).all()]


@app.get("/api/trades", tags=["paper trading"])
def trades(strategy_id: int | None = None, limit: int = Query(100, le=1000)):
    with session_scope() as s:
        q = select(Trade).order_by(Trade.exit_time.desc()).limit(limit)
        if strategy_id:
            q = q.where(Trade.strategy_id == strategy_id)
        return [row(t) for t in s.scalars(q).all()]


@app.get("/api/signals", tags=["paper trading"])
def signals(status: str | None = None, limit: int = Query(100, le=1000)):
    with session_scope() as s:
        q = select(Signal).order_by(Signal.id.desc()).limit(limit)
        if status:
            q = q.where(Signal.status == status)
        return [row(x) for x in s.scalars(q).all()]


@app.get("/api/performance/{sid}", tags=["research"])
def performance(sid: int, limit: int = 60):
    with session_scope() as s:
        return [row(p) for p in s.scalars(select(PerformanceSnapshot).where(PerformanceSnapshot.strategy_id == sid)
                                         .order_by(PerformanceSnapshot.as_of.desc()).limit(limit)).all()]


# --------------------------------------------------------------------------- jobs & schedules
class JobIn(BaseModel):
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


@app.get("/api/jobs", tags=["research"])
def jobs(status: str | None = None, limit: int = Query(50, le=500)):
    with session_scope() as s:
        q = select(Job).order_by(Job.id.desc()).limit(limit)
        if status:
            q = q.where(Job.status == status)
        return [row(j) for j in s.scalars(q).all()]


@app.get("/api/jobs/{jid}", tags=["research"])
def job(jid: int):
    with session_scope() as s:
        return row(s.get(Job, jid) or _404())


@app.post("/api/jobs", dependencies=[Depends(require_token)], tags=["research"], status_code=202)
def create_job(body: JobIn):
    if body.kind not in HANDLERS:
        raise HTTPException(400, f"kind must be one of {sorted(HANDLERS)}")
    return {"id": enqueue(body.kind, body.payload)}


@app.post("/api/jobs/{jid}/retry", dependencies=[Depends(require_token)], tags=["research"])
def retry_job(jid: int):
    with session_scope() as s:
        j = s.get(Job, jid) or _404()
        if j.status != "failed":
            raise HTTPException(409, "only failed jobs can be retried")
        j.status, j.attempts, j.run_after, j.error = "queued", 0, datetime.now(UTC), None
        return row(j)


class ScheduleIn(BaseModel):
    name: str
    kind: str
    cron: str
    timezone: str = "UTC"
    payload: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


@app.get("/api/schedules", tags=["research"])
def schedules():
    with session_scope() as s:
        return [row(x) for x in s.scalars(select(ResearchSchedule).order_by(ResearchSchedule.id)).all()]


@app.post("/api/schedules", dependencies=[Depends(require_token)], tags=["research"], status_code=201)
def create_schedule(body: ScheduleIn):
    if body.kind not in HANDLERS:
        raise HTTPException(400, f"kind must be one of {sorted(HANDLERS)}")
    try:
        nxt = next_run(body.cron, body.timezone, datetime.now(UTC))
    except Exception as e:
        raise HTTPException(400, f"invalid cron/timezone: {e}") from e
    with session_scope() as s:
        sch = ResearchSchedule(**body.model_dump(), next_run_at=nxt)
        s.add(sch)
        s.flush()
        return row(sch)


@app.patch("/api/schedules/{sid}", dependencies=[Depends(require_token)], tags=["research"])
def toggle_schedule(sid: int, enabled: bool):
    with session_scope() as s:
        sch = s.get(ResearchSchedule, sid) or _404()
        sch.enabled = enabled
        return row(sch)


# --------------------------------------------------------------------------- notifications
@app.get("/api/notifications", tags=["notifications"])
def notifications(unread_only: bool = False, limit: int = Query(100, le=500)):
    with session_scope() as s:
        q = select(Notification).order_by(Notification.id.desc()).limit(limit)
        if unread_only:
            q = q.where(Notification.read.is_(False))
        return [row(n) for n in s.scalars(q).all()]


@app.post("/api/notifications/read-all", dependencies=[Depends(require_token)], tags=["notifications"])
def read_all():
    with session_scope() as s:
        n = 0
        for x in s.scalars(select(Notification).where(Notification.read.is_(False))).all():
            x.read = True
            n += 1
        return {"marked": n}


@app.get("/api/summary", tags=["paper trading"])
def summary():
    with session_scope() as s:
        realised = s.scalar(select(func.coalesce(func.sum(Trade.pnl), 0.0)))
        unreal = s.scalar(select(func.coalesce(func.sum(PaperPosition.unrealised_pnl), 0.0))
                          .where(PaperPosition.status == "open"))
        n = s.scalar(select(func.count()).select_from(Trade))
    return {"realised_pnl": realised, "unrealised_pnl": unreal, "closed_trades": n}


def _404():
    raise HTTPException(404, "not found")
