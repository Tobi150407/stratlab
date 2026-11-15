"""Always-on paper-trading engine process.

Loop (every ``SL_POLL_INTERVAL_SECONDS``):
  1. For every instrument/timeframe used by an active strategy, poll the data provider for
     newly closed candles (with exponential-backoff reconnection) and store them.
  2. For every active strategy, process all stored candles after its last processed candle,
     in order, one transaction per candle (see ``paper.py``).
  3. Write a heartbeat.

Only one engine may trade at a time: on PostgreSQL a session-level advisory lock elects a
leader, so a second replica (or an accidentally doubled container) waits as hot standby
instead of duplicating trades. Research jobs run in a different process/container and can
never delay this loop.
"""
from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app import notify as N
from app.config import get_settings
from app.data.feeds import FeedError, MarketDataProvider, build_provider
from app.db import get_engine, is_postgres, session_scope
from app.engine import paper
from app.heartbeat import beat
from app.market_hours import INSTRUMENTS, floor_to_timeframe, market_state, timeframe_seconds
from app.models import Candle, FeedStatus, Strategy

log = logging.getLogger(__name__)
UTC = timezone.utc
COMPONENT = "engine"
LEADER_LOCK_ID = 727_001


class Engine:
    def __init__(self, provider: MarketDataProvider | None = None):
        self.settings = get_settings()
        self.provider = provider or build_provider()
        self.running = True
        self._leader_conn = None
        self._backoff: dict[str, float] = {}  # feed key -> next attempt epoch
        self._failures: dict[str, int] = {}

    # ------------------------------------------------------------------ leadership
    def acquire_leadership(self) -> bool:
        if not is_postgres():
            return True
        if self._leader_conn is None:
            self._leader_conn = get_engine().connect()
        try:
            got = self._leader_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LEADER_LOCK_ID}).scalar()
            self._leader_conn.commit()
            return bool(got)
        except Exception:
            log.exception("leader lock check failed")
            try:
                self._leader_conn.close()
            finally:
                self._leader_conn = None
            return False

    def still_leader(self) -> bool:
        if not is_postgres():
            return True
        try:
            assert self._leader_conn is not None
            self._leader_conn.execute(text("SELECT 1"))
            return True
        except Exception:
            log.error("lost DB connection holding leader lock; re-electing")
            self._leader_conn = None
            return False

    # ------------------------------------------------------------------ feeds
    def _feeds_needed(self) -> set[tuple[str, str]]:
        with session_scope() as s:
            rows = s.execute(
                select(Strategy.instrument, Strategy.timeframe).where(Strategy.paper_active.is_(True))
            ).all()
        return {(r[0], r[1]) for r in rows}

    def poll_feed(self, instrument: str, timeframe: str, now: datetime) -> None:
        key = f"{instrument}:{timeframe}"
        if time.time() < self._backoff.get(key, 0):
            return
        state = market_state(instrument, now)
        with session_scope() as s:
            last = paper.latest_candle_ts(s, instrument, timeframe)
        # When closed, still poll occasionally (to collect the final candle of the session
        # and to detect reopen), but no need to hammer the provider.
        if not state.is_open and last is not None:
            closed_for = now - (last + timedelta(seconds=timeframe_seconds(timeframe)))
            if closed_for > timedelta(minutes=30) and int(now.timestamp()) % 300 > self.settings.poll_interval_seconds:
                self._update_feed_status(instrument, state.is_open, ok=None)
                return
        try:
            bars = self.provider.fetch_closed_candles(instrument, timeframe, last, now)
        except (FeedError, Exception) as e:  # noqa: BLE001 - any provider failure = reconnect
            n = self._failures.get(key, 0) + 1
            self._failures[key] = n
            delay = min(300, 2 ** min(n, 8))
            self._backoff[key] = time.time() + delay
            log.warning("feed %s failed (%d consecutive), retry in %ss: %s", key, n, delay, e)
            self._update_feed_status(instrument, state.is_open, ok=False, error=str(e))
            return
        self._failures[key] = 0
        self._backoff.pop(key, None)
        with session_scope() as s:
            paper.ingest_candles(s, instrument, timeframe, bars)
        self._update_feed_status(instrument, state.is_open, ok=True, last_candle=bars[-1].ts if bars else last)

    def _update_feed_status(self, instrument, market_open, ok, error=None, last_candle=None):
        now = datetime.now(UTC)
        with session_scope() as s:
            fs = s.get(FeedStatus, instrument) or FeedStatus(instrument=instrument)
            s.add(fs)
            fs.market_open = market_open
            fs.updated_at = now
            if ok is True:
                was_down = (fs.consecutive_failures or 0) >= 3
                fs.connected = True
                fs.last_success_at = now
                fs.consecutive_failures = 0
                fs.last_error = None
                if last_candle is not None:
                    fs.last_candle_ts = last_candle
                if was_down:
                    N.notify(N.DATA_RESTORED, f"Market data restored: {instrument}", "Feed reconnected.",
                             severity="info", session=s)
            elif ok is False:
                fs.consecutive_failures = (fs.consecutive_failures or 0) + 1
                fs.last_error = error
                if fs.consecutive_failures >= 3:
                    fs.connected = False
                    N.notify(N.DATA_INTERRUPTION, f"Market data interruption: {instrument}",
                             f"{fs.consecutive_failures} consecutive failures. Last error: {error}",
                             severity="critical", dedup_key=f"feed-down:{instrument}", session=s)

    # ------------------------------------------------------------------ strategies
    def run_strategies(self, now: datetime) -> int:
        processed = 0
        with session_scope() as s:
            ids = s.scalars(select(Strategy.id).where(Strategy.paper_active.is_(True))).all()
        for sid in ids:
            try:
                processed += self._run_strategy(sid, now)
            except Exception as e:  # one broken strategy must not stop the others
                log.exception("strategy %s failed", sid)
                with session_scope() as s:
                    st = s.get(Strategy, sid)
                    st.status = "error"
                    st.status_detail = str(e)[:1000]
                    N.notify(N.STRATEGY_STOPPED, f"Strategy stopped: {st.name}", f"Error: {e}",
                             severity="critical", dedup_key=f"strategy-error:{sid}", session=s)
        return processed

    def _run_strategy(self, sid: int, now: datetime) -> int:
        with session_scope() as s:
            st = s.get(Strategy, sid)
            if st.last_evaluated_candle is None:
                # Freshly activated: start from the current candle and never trade history,
                # even if a backfill inserts older candles later.
                st.last_evaluated_candle = floor_to_timeframe(now, st.timeframe) - timedelta(
                    seconds=timeframe_seconds(st.timeframe))
                st.status = "running"
                return 0
            candle_ids = [c.id for c in paper.pending_candles(s, st)]
        n = 0
        for cid in candle_ids:
            with session_scope() as s:  # one atomic transaction per candle
                st = s.get(Strategy, sid)
                paper.process_candle_for_strategy(s, st, s.get(Candle, cid), now=now)
            n += 1
        return n

    # ------------------------------------------------------------------ loop
    def tick(self) -> dict:
        now = datetime.now(UTC)
        feeds = self._feeds_needed()
        for inst, tf in sorted(feeds):
            self.poll_feed(inst, tf, now)
        # keep market-open status fresh even for instruments no strategy uses yet
        for inst in INSTRUMENTS:
            if not any(f[0] == inst for f in feeds):
                with session_scope() as s:
                    fs = s.get(FeedStatus, inst) or FeedStatus(instrument=inst)
                    s.add(fs)
                    fs.market_open = market_state(inst, now).is_open
                    fs.updated_at = now
        processed = self.run_strategies(now)
        return {"feeds": len(feeds), "candles_processed": processed}

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        log.info("engine starting (provider=%s)", self.provider.name)
        leader = False
        while self.running:
            started = time.time()
            try:
                if not leader:
                    leader = self.acquire_leadership()
                    if not leader:
                        beat(COMPONENT + "-standby", status="standby")
                        self._sleep(10)
                        continue
                    log.info("acquired engine leadership")
                elif not self.still_leader():
                    leader = False
                    continue
                info = self.tick()
                beat(COMPONENT, status="ok", last_tick=datetime.now(UTC).isoformat(),
                     tick_seconds=round(time.time() - started, 3), **info)
            except Exception as e:
                # DB outage etc.: log, report if possible, retry. Process stays alive;
                # if it truly wedges, the container healthcheck restarts it.
                log.exception("engine tick failed")
                try:
                    beat(COMPONENT, status="error", error=str(e)[:500])
                except Exception:
                    pass
                self._sleep(5)
            self._sleep(max(1.0, self.settings.poll_interval_seconds - (time.time() - started)))
        log.info("engine stopped")

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while self.running and time.time() < end:
            time.sleep(min(1.0, end - time.time()))

    def _stop(self, *_):
        log.info("shutdown requested")
        self.running = False
