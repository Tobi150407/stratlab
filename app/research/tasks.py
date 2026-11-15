"""Background job handlers. Each takes the job payload and returns a JSON-able result."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import notify as N
from app.config import get_settings
from app.data.feeds import build_provider
from app.db import session_scope
from app.engine.paper import ingest_candles, to_bar
from app.market_hours import timeframe_seconds
from app.models import Candle, PerformanceSnapshot, Strategy, Trade
from app.research.backtest import metrics, optimise, run_backtest, walk_forward

log = logging.getLogger(__name__)
UTC = timezone.utc


def _bars(instrument: str, timeframe: str, days: int):
    since = datetime.now(UTC) - timedelta(days=days)
    with session_scope() as s:
        rows = s.scalars(
            select(Candle).where(Candle.instrument == instrument, Candle.timeframe == timeframe, Candle.ts >= since)
            .order_by(Candle.ts)
        ).all()
        return [to_bar(c) for c in rows]


def _strategies(payload: dict) -> list[Strategy]:
    with session_scope() as s:
        q = select(Strategy)
        if payload.get("strategy_id"):
            q = q.where(Strategy.id == payload["strategy_id"])
        elif payload.get("only_active", True):
            q = q.where(Strategy.paper_active.is_(True))
        return list(s.scalars(q).all())


# --------------------------------------------------------------------------- handlers
def backfill(p: dict) -> dict:
    """Fetch history from the provider so research has data to work with."""
    inst, tf, days = p["instrument"], p.get("timeframe", "5m"), int(p.get("days", 30))
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    provider = build_provider()
    total, cursor = 0, since
    chunk = timedelta(seconds=timeframe_seconds(tf) * 1500)
    while cursor < now:
        bars = provider.fetch_closed_candles(inst, tf, cursor, min(now, cursor + chunk))
        with session_scope() as s:
            total += ingest_candles(s, inst, tf, bars)
        cursor += chunk
    return {"instrument": inst, "timeframe": tf, "candles": total}


def backtest(p: dict) -> dict:
    bars = _bars(p["instrument"], p.get("timeframe", "5m"), int(p.get("days", 60)))
    res = run_backtest(p["kind"], p.get("params", {}), bars)
    res["trades"] = res["trades"][-100:]  # keep stored result small
    return res


def optimise_job(p: dict) -> dict:
    out = {}
    for st in _strategies(p):
        bars = _bars(st.instrument, st.timeframe, int(p.get("days", 90)))
        out[st.name] = optimise(st.kind, bars, base=st.params, max_combos=int(p.get("max_combos", 150)))
        out[st.name]["current_params_metrics"] = run_backtest(st.kind, st.params, bars)["metrics"]
    return out


def validate(p: dict) -> dict:
    """Walk-forward validation; stores the out-of-sample metrics as the strategy baseline."""
    out = {}
    for st in _strategies(p):
        bars = _bars(st.instrument, st.timeframe, int(p.get("days", 120)))
        try:
            wf = walk_forward(st.kind, bars, folds=int(p.get("folds", 4)), base=st.params)
        except ValueError as e:
            out[st.name] = {"skipped": str(e)}
            continue
        current = run_backtest(st.kind, st.params, bars)["metrics"]
        with session_scope() as s:
            row = s.get(Strategy, st.id)
            row.baseline_metrics = {"source": "backtest", "as_of": datetime.now(UTC).isoformat(), **current}
            s.add(PerformanceSnapshot(strategy_id=st.id, as_of=datetime.now(UTC), source="backtest", metrics=current))
        out[st.name] = {"walk_forward": wf, "current_params": current}
    return out


def daily_performance(p: dict) -> dict:
    out = {}
    now = datetime.now(UTC)
    for st in _strategies({**p, "only_active": False}):
        with session_scope() as s:
            trades = s.scalars(select(Trade).where(Trade.strategy_id == st.id).order_by(Trade.exit_time)).all()
            m = metrics([{"pnl": t.pnl, "return_pct": t.return_pct} for t in trades])
            day = [t for t in trades if t.exit_time.replace(tzinfo=t.exit_time.tzinfo or UTC) >= now - timedelta(days=1)]
            m["last_24h"] = metrics([{"pnl": t.pnl, "return_pct": t.return_pct} for t in day])
            s.add(PerformanceSnapshot(strategy_id=st.id, as_of=now, source="paper", metrics=m))
        out[st.name] = m
    return out


def deterioration_check(p: dict) -> dict:
    """Flag strategies whose recent paper performance has degraded vs their baseline
    (backtest/validation) or that breach the drawdown limit."""
    cfg = get_settings()
    window = int(p.get("window", 20))
    out = {}
    for st in _strategies(p):
        with session_scope() as s:
            trades = s.scalars(
                select(Trade).where(Trade.strategy_id == st.id).order_by(Trade.exit_time.desc()).limit(window)
            ).all()
            if len(trades) < cfg.deterioration_min_trades:
                out[st.name] = {"status": "insufficient trades", "trades": len(trades)}
                continue
            recent = metrics([{"pnl": t.pnl, "return_pct": t.return_pct} for t in reversed(trades)])
            base = st.baseline_metrics or {}
            reasons = []
            if base.get("trades", 0) >= 5 and recent["sharpe"] < base.get("sharpe", 0) - cfg.deterioration_sharpe_drop:
                reasons.append(f"Sharpe {recent['sharpe']:.2f} vs baseline {base['sharpe']:.2f}")
            if recent["max_drawdown_pct"] > cfg.deterioration_max_drawdown_pct:
                reasons.append(f"drawdown {recent['max_drawdown_pct']:.1f}% > {cfg.deterioration_max_drawdown_pct}%")
            if base.get("win_rate") and recent["win_rate"] < base["win_rate"] - 15:
                reasons.append(f"win rate {recent['win_rate']:.0f}% vs baseline {base['win_rate']:.0f}%")
            row = s.get(Strategy, st.id)
            if reasons:
                row.status = "degraded"
                row.status_detail = "; ".join(reasons)
                N.notify(N.DETERIORATION, f"Performance deterioration: {st.name}", "; ".join(reasons),
                         severity="warning", data={"strategy_id": st.id, "recent": recent, "baseline": base},
                         dedup_key=f"deterioration:{st.id}", session=s)
            elif row.status == "degraded":
                row.status, row.status_detail = "running", None
            out[st.name] = {"recent": recent, "baseline": base, "deteriorating": bool(reasons), "reasons": reasons}
    return out


def compare(p: dict) -> dict:
    rows = []
    for st in _strategies({**p, "only_active": False}):
        with session_scope() as s:
            trades = s.scalars(select(Trade).where(Trade.strategy_id == st.id)).all()
            paper_m = metrics([{"pnl": t.pnl, "return_pct": t.return_pct} for t in trades])
        bars = _bars(st.instrument, st.timeframe, int(p.get("days", 60)))
        bt = run_backtest(st.kind, st.params, bars)["metrics"] if bars else None
        rows.append({"strategy": st.name, "instrument": st.instrument, "paper": paper_m, "backtest": bt})
    rows.sort(key=lambda r: (r["paper"]["sharpe"], r["paper"]["total_pnl"]), reverse=True)
    return {"ranking": rows}


HANDLERS = {
    "backfill": backfill,
    "backtest": backtest,
    "optimise": optimise_job,
    "validate": validate,
    "daily_performance": daily_performance,
    "deterioration_check": deterioration_check,
    "compare": compare,
}
