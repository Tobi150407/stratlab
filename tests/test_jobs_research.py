from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.data.feeds import SimulatedProvider
from app.db import session_scope
from app.jobs import queue, scheduler
from app.jobs.worker import Worker
from app.models import Job, Notification, PaperPosition, ResearchSchedule, Strategy, Trade
from app.research.backtest import optimise, run_backtest, walk_forward
from app.research.tasks import HANDLERS, deterioration_check

U = timezone.utc


def sim_bars(days=20, tf="5m"):
    now = datetime(2026, 11, 13, 21, 0, tzinfo=U)
    return SimulatedProvider().fetch_closed_candles("GOLD", tf, now - timedelta(days=days), now)


def test_simulated_provider_deterministic_and_respects_hours():
    a, b = sim_bars(3), sim_bars(3)
    assert a == b and len(a) > 100
    from app.market_hours import is_market_open

    assert all(is_market_open("GOLD", x.ts) for x in a)


def test_backtest_optimise_walkforward():
    bars = sim_bars(25)
    r = run_backtest("sma_cross", {}, bars)
    assert r["metrics"]["trades"] > 0
    o = optimise("sma_cross", bars, max_combos=10)
    assert o["best"] is not None
    wf = walk_forward("sma_cross", bars, folds=2)
    assert "out_of_sample" in wf


def test_job_retry_then_fail_alert(monkeypatch):
    calls = {"n": 0}

    def boom(p):
        calls["n"] += 1
        raise RuntimeError("kaboom")

    monkeypatch.setitem(HANDLERS, "boom", boom)
    jid = queue.enqueue("boom", max_attempts=2)
    w = Worker()
    assert w.run_one()
    with session_scope() as s:
        j = s.get(Job, jid)
        assert j.status == "queued" and j.attempts == 1
        j.run_after = datetime.now(U) - timedelta(seconds=1)
    assert w.run_one()
    with session_scope() as s:
        assert s.get(Job, jid).status == "failed"
        assert s.scalars(select(Notification).where(Notification.kind == "job_failed")).one()
    assert calls["n"] == 2


def test_expired_lease_is_requeued():
    jid = queue.enqueue("compare")
    job = queue.claim("dead-worker")
    with session_scope() as s:
        s.get(Job, job.id).lease_expires_at = datetime.now(U) - timedelta(seconds=1)
    assert queue.reap_expired() == 1
    with session_scope() as s:
        assert s.get(Job, jid).status == "queued"


def test_scheduler_fires_once_and_advances():
    scheduler.seed_default_schedules()
    with session_scope() as s:
        sch = s.scalars(select(ResearchSchedule).where(ResearchSchedule.name == "daily-performance")).one()
        sch.next_run_at = datetime.now(U) - timedelta(minutes=1)
    assert scheduler.tick() == 1
    assert scheduler.tick() == 0  # not double-fired
    with session_scope() as s:
        assert s.scalars(select(Job).where(Job.kind == "daily_performance")).one()


def test_scheduler_dst_aware():
    # 09:00 New York every weekday: 14:00 UTC in winter, 13:00 UTC in summer
    w = scheduler.next_run("0 9 * * 1-5", "America/New_York", datetime(2026, 1, 5, 0, 0, tzinfo=U))
    s = scheduler.next_run("0 9 * * 1-5", "America/New_York", datetime(2026, 7, 6, 0, 0, tzinfo=U))
    assert (w.hour, s.hour) == (14, 13)


def test_deterioration_detected():
    t = datetime(2026, 11, 1, tzinfo=U)
    with session_scope() as s:
        st = Strategy(name="d", kind="sma_cross", instrument="GOLD", paper_active=True,
                      baseline_metrics={"trades": 50, "sharpe": 2.0, "win_rate": 60})
        s.add(st)
        s.flush()
        for i in range(12):
            win = i % 4 == 0
            pos = PaperPosition(strategy_id=st.id, instrument="GOLD", side="long", quantity=1, entry_price=100,
                                entry_time=t, last_price=100, last_price_at=t, status="closed")
            s.add(pos)
            s.flush()
            s.add(Trade(position_id=pos.id, strategy_id=st.id, instrument="GOLD", side="long", quantity=1,
                        entry_price=100, exit_price=101 if win else 98, entry_time=t, exit_time=t + timedelta(hours=i),
                        exit_reason="x", pnl=1 if win else -2, return_pct=1 if win else -2))
    out = deterioration_check({})
    assert out["d"]["deteriorating"]
    with session_scope() as s:
        assert s.scalars(select(Strategy)).one().status == "degraded"
