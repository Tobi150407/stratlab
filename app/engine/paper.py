"""Paper-trading core: candle ingestion, position management and signal execution.

Restart safety: every strategy remembers the last candle it fully processed
(``Strategy.last_evaluated_candle``). Processing a candle for a strategy — exits, P&L
update, signal creation and execution — happens in ONE database transaction together with
advancing that pointer. A crash at any point either commits everything or nothing, so after
a restart the engine simply resumes from the pointer: no candle is skipped, none is
processed twice, open positions are reloaded from the database.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import notify as N
from app.config import get_settings
from app.engine.fills import check_exit, pnl, return_pct
from app.market_hours import candle_close_time, is_market_open, timeframe_seconds
from app.models import Candle, PaperPosition, Signal, Strategy, Trade
from app.strategies import Bar, build

log = logging.getLogger(__name__)
UTC = timezone.utc


def _aware(dt: datetime | None) -> datetime | None:
    # SQLite drops tzinfo; normalise so comparisons always work
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def to_bar(c: Candle) -> Bar:
    return Bar(_aware(c.ts), c.open, c.high, c.low, c.close, c.volume)


# --------------------------------------------------------------------------- ingestion
def ingest_candles(s: Session, instrument: str, timeframe: str, bars: list[Bar]) -> int:
    """Idempotent upsert-ignore of closed candles. Returns number of rows offered."""
    if not bars:
        return 0
    rows = [
        dict(instrument=instrument, timeframe=timeframe, ts=b.ts, open=b.open, high=b.high,
             low=b.low, close=b.close, volume=b.volume, received_at=datetime.now(UTC))
        for b in bars
    ]
    ins = pg_insert if s.bind.dialect.name == "postgresql" else sqlite_insert
    stmt = ins(Candle).values(rows).on_conflict_do_nothing(index_elements=["instrument", "timeframe", "ts"])
    s.execute(stmt)
    return len(rows)


def latest_candle_ts(s: Session, instrument: str, timeframe: str) -> datetime | None:
    return _aware(
        s.scalar(
            select(Candle.ts)
            .where(Candle.instrument == instrument, Candle.timeframe == timeframe)
            .order_by(Candle.ts.desc())
            .limit(1)
        )
    )


def recent_bars(s: Session, instrument: str, timeframe: str, up_to: datetime, n: int) -> list[Bar]:
    rows = s.scalars(
        select(Candle)
        .where(Candle.instrument == instrument, Candle.timeframe == timeframe, Candle.ts <= up_to)
        .order_by(Candle.ts.desc())
        .limit(n)
    ).all()
    return [to_bar(c) for c in reversed(rows)]


def open_position(s: Session, strategy_id: int) -> PaperPosition | None:
    return s.scalar(
        select(PaperPosition).where(PaperPosition.strategy_id == strategy_id, PaperPosition.status == "open")
    )


# --------------------------------------------------------------------------- positions
def close_position(s: Session, pos: PaperPosition, price: float, at: datetime, reason: str) -> Trade:
    pos.status = "closed"
    pos.last_price = price
    pos.last_price_at = at
    pos.unrealised_pnl = 0.0
    tr = Trade(
        position_id=pos.id, strategy_id=pos.strategy_id, instrument=pos.instrument, side=pos.side,
        quantity=pos.quantity, entry_price=pos.entry_price, exit_price=price,
        entry_time=pos.entry_time, exit_time=at, exit_reason=reason,
        pnl=round(pnl(pos.side, pos.entry_price, price, pos.quantity), 6),
        return_pct=round(return_pct(pos.side, pos.entry_price, price), 6),
    )
    s.add(tr)
    s.flush()
    strat_name = pos.strategy.name if pos.strategy else pos.strategy_id
    kind = {"stop_loss": N.STOP_LOSS, "take_profit": N.TAKE_PROFIT}.get(reason, N.TRADE_CLOSED)
    label = {"stop_loss": "Stop loss hit", "take_profit": "Take profit hit"}.get(reason, "Trade closed")
    N.notify(
        kind,
        f"{label}: {strat_name} {pos.side} {pos.instrument}",
        f"Entry {pos.entry_price:.2f} → exit {price:.2f} | P&L {tr.pnl:+.2f} ({tr.return_pct:+.2f}%)",
        severity="info",
        data={"trade_id": tr.id, "strategy_id": pos.strategy_id, "reason": reason, "pnl": tr.pnl},
        session=s,
    )
    return tr


def _manage_open_position(s: Session, pos: PaperPosition, bar: Bar, close_at: datetime) -> Trade | None:
    hit = check_exit(pos.side, pos.stop_loss, pos.take_profit, bar)
    if hit:
        reason, price = hit
        return close_position(s, pos, price, close_at, reason)
    pos.last_price = bar.close
    pos.last_price_at = close_at
    pos.unrealised_pnl = round(pnl(pos.side, pos.entry_price, bar.close, pos.quantity), 6)
    return None


# --------------------------------------------------------------------------- signals
def _record_signal(s: Session, strat: Strategy, bar: Bar, decision) -> Signal | None:
    """Insert the signal under a unique dedup key. Returns None if it already exists."""
    key = f"{strat.id}:{bar.ts.isoformat()}:{decision.side}"
    sig = Signal(
        dedup_key=key, strategy_id=strat.id, instrument=strat.instrument, candle_ts=bar.ts,
        side=decision.side, price=decision.price, stop_loss=decision.stop_loss,
        take_profit=decision.take_profit, reason=decision.reason,
    )
    try:
        with s.begin_nested():
            s.add(sig)
            s.flush()
    except IntegrityError:
        log.info("duplicate signal suppressed: %s", key)
        return None
    return sig


def _finish(sig: Signal, status: str, reason: str) -> None:
    sig.status = status
    sig.status_reason = reason
    sig.processed_at = datetime.now(UTC)


def _execute_signal(s: Session, strat: Strategy, sig: Signal, pos: PaperPosition | None,
                    close_at: datetime, now: datetime) -> None:
    settings = get_settings()
    tf = timeframe_seconds(strat.timeframe)
    stale_after = close_at + timedelta(seconds=tf + settings.stale_grace_seconds)

    if sig.side == "exit":
        if pos is None:
            _finish(sig, "rejected", "no open position to exit")
            return
        # Exits are always honoured: refusing to close on stale data would leave risk open.
        close_position(s, pos, sig.price, close_at, "signal")
        _finish(sig, "executed", "position closed")
        return

    # --- entry guards: never open trades on stale prices or a closed market
    if now > stale_after:
        _finish(sig, "rejected", f"stale candle (closed {close_at.isoformat()}, evaluated {now.isoformat()})")
    elif not is_market_open(strat.instrument, sig.candle_ts):
        _finish(sig, "rejected", "market closed")
    elif pos is not None:
        _finish(sig, "rejected", f"position already open ({pos.side})")
    else:
        new = PaperPosition(
            strategy_id=strat.id, signal_id=sig.id, instrument=strat.instrument, side=sig.side,
            quantity=strat.quantity, entry_price=sig.price, entry_time=close_at,
            stop_loss=sig.stop_loss, take_profit=sig.take_profit, last_price=sig.price,
            last_price_at=close_at, unrealised_pnl=0.0,
        )
        s.add(new)
        s.flush()
        _finish(sig, "executed", f"opened position #{new.id}")
        N.notify(
            N.NEW_TRADE,
            f"New paper trade: {strat.name} {sig.side} {strat.instrument}",
            f"Entry {sig.price:.2f} | SL {sig.stop_loss or 0:.2f} | TP {sig.take_profit or 0:.2f} | {sig.reason}",
            data={"position_id": new.id, "strategy_id": strat.id, "signal_id": sig.id},
            session=s,
        )
        return
    # rejected entries are recorded and surfaced, never silently dropped
    N.notify(
        N.SIGNAL_REJECTED, f"Signal not executed: {strat.name} {sig.side}", sig.status_reason or "",
        severity="info", data={"signal_id": sig.id},
        # the signal row is always kept; only repeated *alerts* are rate-limited
        dedup_key=f"rejected:{strat.id}:{(sig.status_reason or '').split(' ')[0]}", session=s,
    )


# --------------------------------------------------------------------------- main step
def process_candle_for_strategy(s: Session, strat: Strategy, candle: Candle, now: datetime | None = None) -> None:
    """Process exactly one closed candle for one strategy. Caller owns the transaction."""
    now = now or datetime.now(UTC)
    bar = to_bar(candle)
    last = _aware(strat.last_evaluated_candle)
    if last is not None and bar.ts <= last:
        return  # already processed (idempotent replay)
    close_at = candle_close_time(bar.ts, strat.timeframe)

    pos = open_position(s, strat.id)
    if pos is not None and _aware(pos.entry_time) < close_at:
        if _manage_open_position(s, pos, bar, close_at):
            pos = None

    impl = build(strat.kind, strat.params)
    bars = recent_bars(s, strat.instrument, strat.timeframe, bar.ts, impl.warmup() + 5)
    decision = impl.evaluate(bars, pos.side if pos else None)
    if decision is not None:
        sig = _record_signal(s, strat, bar, decision)
        if sig is not None:
            try:
                with s.begin_nested():
                    _execute_signal(s, strat, sig, pos, close_at, now)
            except Exception as e:  # keep the signal, mark it, alert
                log.exception("signal execution failed")
                _finish(sig, "error", f"execution error: {e}")
                N.notify(N.SIGNAL_REJECTED, f"Signal execution error: {strat.name}", str(e),
                         severity="warning", data={"signal_id": sig.id}, session=s)

    strat.last_evaluated_candle = bar.ts
    strat.last_evaluated_at = now
    if strat.status in ("idle", "error"):
        strat.status = "running"
        strat.status_detail = None


def pending_candles(s: Session, strat: Strategy, limit: int = 500) -> list[Candle]:
    q = select(Candle).where(Candle.instrument == strat.instrument, Candle.timeframe == strat.timeframe)
    if strat.last_evaluated_candle is not None:
        q = q.where(Candle.ts > strat.last_evaluated_candle)
    return list(s.scalars(q.order_by(Candle.ts).limit(limit)).all())
