from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db import session_scope
from app.engine import paper
from app.engine.runner import Engine
from app.models import Candle, FeedStatus, Notification, PaperPosition, Signal, Strategy, Trade
from app.strategies import REGISTRY, Bar, Decision, StrategyBase, register

U = timezone.utc
T0 = datetime(2026, 11, 17, 15, 0, tzinfo=U)  # Tuesday, both markets open


@register
class Scripted(StrategyBase):
    """Test strategy: enters long on the candle whose ts matches params['enter_at']."""

    kind = "scripted"
    default_params = {"enter_at": None, "exit_at": None}

    def warmup(self):
        return 1

    def evaluate(self, bars, position_side):
        ts = bars[-1].ts.isoformat()
        if ts == self.params.get("exit_at") and position_side:
            return Decision("exit", bars[-1].close, reason="scripted exit")
        if ts == self.params.get("enter_at") and not position_side:
            c = bars[-1].close
            return Decision("long", c, c - 5, c + 10, "scripted entry")
        return None


def mk_strategy(**params):
    with session_scope() as s:
        st = Strategy(name="t", kind="scripted", instrument="GOLD", timeframe="5m", params=params,
                      paper_active=True, last_evaluated_candle=T0 - timedelta(minutes=5))
        s.add(st)
        s.flush()
        return st.id


def add_candles(bars):
    with session_scope() as s:
        paper.ingest_candles(s, "GOLD", "5m", bars)


def b(i, o, h, l, c):
    return Bar(T0 + timedelta(minutes=5 * i), o, h, l, c)


def run(now):
    e = Engine.__new__(Engine)  # skip provider construction
    with session_scope() as s:
        ids = s.scalars(select(Strategy.id)).all()
    for sid in ids:
        e._run_strategy(sid, now)


def test_entry_then_take_profit_and_notifications():
    sid = mk_strategy(enter_at=T0.isoformat())
    add_candles([b(0, 100, 101, 99, 100), b(1, 100, 104, 99, 103), b(2, 103, 111, 102, 108)])
    run(T0 + timedelta(minutes=11))  # within the staleness window of candle 0
    with session_scope() as s:
        tr = s.scalars(select(Trade)).all()
        assert len(tr) == 1 and tr[0].exit_reason == "take_profit" and tr[0].exit_price == 110
        assert tr[0].pnl == 10
        kinds = {n.kind for n in s.scalars(select(Notification)).all()}
        assert {"trade_opened", "take_profit_hit"} <= kinds
        assert s.get(Strategy, sid).last_evaluated_candle.replace(tzinfo=U) == T0 + timedelta(minutes=10)


def test_stop_wins_when_both_hit_same_bar():
    mk_strategy(enter_at=T0.isoformat())
    add_candles([b(0, 100, 100, 100, 100), b(1, 100, 120, 80, 100)])
    run(T0 + timedelta(minutes=11))
    with session_scope() as s:
        tr = s.scalars(select(Trade)).one()
        assert tr.exit_reason == "stop_loss" and tr.exit_price == 95


def test_stale_candle_entry_rejected_not_discarded():
    mk_strategy(enter_at=T0.isoformat())
    add_candles([b(0, 100, 101, 99, 100)])
    run(T0 + timedelta(hours=3))  # processed long after the candle closed
    with session_scope() as s:
        sig = s.scalars(select(Signal)).one()
        assert sig.status == "rejected" and "stale" in sig.status_reason
        assert s.scalars(select(PaperPosition)).all() == []


def test_idempotent_replay_and_duplicate_signal_prevention():
    sid = mk_strategy(enter_at=T0.isoformat())
    add_candles([b(0, 100, 101, 99, 100)])
    run(T0 + timedelta(minutes=6))
    # Simulate a crash that lost the pointer update -> candle replayed
    with session_scope() as s:
        s.get(Strategy, sid).last_evaluated_candle = T0 - timedelta(minutes=5)
    run(T0 + timedelta(minutes=6))
    # Re-ingesting the same candle is a no-op
    add_candles([b(0, 100, 101, 99, 100)])
    with session_scope() as s:
        assert len(s.scalars(select(Signal)).all()) == 1
        assert len(s.scalars(select(PaperPosition)).all()) == 1
        assert len(s.scalars(select(Candle)).all()) == 1


def test_open_position_survives_restart():
    mk_strategy(enter_at=T0.isoformat())
    add_candles([b(0, 100, 101, 99, 100)])
    run(T0 + timedelta(minutes=6))
    # "restart": brand-new engine object, state only from DB
    from app.db import reset_engine

    reset_engine()
    add_candles([b(1, 100, 106, 99, 104)])
    run(T0 + timedelta(minutes=11))
    with session_scope() as s:
        pos = s.scalars(select(PaperPosition)).one()
        assert pos.status == "open" and pos.unrealised_pnl == 4


def test_market_closed_candle_not_traded():
    with session_scope() as s:
        sat = datetime(2026, 11, 14, 15, 0, tzinfo=U)
        st = Strategy(name="w", kind="scripted", instrument="NASDAQ", timeframe="5m",
                      params={"enter_at": sat.isoformat()}, paper_active=True,
                      last_evaluated_candle=sat - timedelta(minutes=5))
        s.add(st)
        s.flush()
        paper.ingest_candles(s, "NASDAQ", "5m", [Bar(sat, 1, 1, 1, 1)])
        c = s.scalars(select(Candle)).one()
        paper.process_candle_for_strategy(s, st, c, now=sat + timedelta(minutes=6))
        sig = s.scalars(select(Signal)).one()
        assert sig.status == "rejected" and sig.status_reason == "market closed"


class FlakyProvider:
    name = "flaky"

    def __init__(self):
        self.calls = 0

    def fetch_closed_candles(self, *a):
        self.calls += 1
        raise ConnectionError("feed down")


def test_feed_failure_backoff_and_alert():
    e = Engine(provider=FlakyProvider())
    for _ in range(3):
        e._backoff.clear()
        e.poll_feed("GOLD", "5m", T0)
    with session_scope() as s:
        fs = s.get(FeedStatus, "GOLD")
        assert fs.consecutive_failures == 3 and not fs.connected
        assert s.scalars(select(Notification).where(Notification.kind == "market_data_interruption")).one()
    # backoff prevents hammering immediately afterwards
    calls = e.provider.calls
    e.poll_feed("GOLD", "5m", T0)
    assert e.provider.calls == calls


def test_registry_has_builtins():
    assert {"sma_cross", "donchian_breakout", "rsi_reversion"} <= set(REGISTRY)


def test_new_strategy_never_trades_backfilled_history():
    with session_scope() as s:
        s.add(Strategy(name="new", kind="scripted", instrument="GOLD", timeframe="5m",
                       params={"enter_at": T0.isoformat()}, paper_active=True))
    now = T0 + timedelta(days=2)
    run(now)  # first tick anchors at the current candle
    add_candles([b(0, 100, 101, 99, 100)])  # backfill arrives later with older data
    run(now)
    with session_scope() as s:
        assert s.scalars(select(Signal)).all() == []
