"""Persistent data model.

Key safety properties enforced at the database level:
* ``Candle``  — unique (instrument, timeframe, ts): re-ingesting a candle is idempotent.
* ``Signal``  — unique ``dedup_key``: the same setup can never be recorded/executed twice,
  even if two engine processes race or the engine restarts mid-evaluation.
* ``Signal.status`` — every signal ends in an explicit terminal state with a reason
  (executed / rejected / error). Nothing is silently discarded.
* ``PaperPosition`` — open positions live in the DB and are reloaded after a restart.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


TS = DateTime(timezone=True)


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (UniqueConstraint("instrument", "timeframe", "ts", name="uq_candle"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument: Mapped[str] = mapped_column(String(16), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))
    ts: Mapped[datetime] = mapped_column(TS, index=True)  # candle OPEN time, UTC
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    received_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[str] = mapped_column(String(50))  # registry key, e.g. "sma_cross"
    instrument: Mapped[str] = mapped_column(String(16))  # GOLD | NASDAQ
    timeframe: Mapped[str] = mapped_column(String(8), default="5m")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    paper_active: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="idle")  # idle|running|error|degraded
    status_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_evaluated_candle: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    baseline_metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String(200), unique=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), index=True)
    instrument: Mapped[str] = mapped_column(String(16))
    candle_ts: Mapped[datetime] = mapped_column(TS)
    side: Mapped[str] = mapped_column(String(5))  # long|short|exit
    price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # pending -> executed | rejected | error
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


class PaperPosition(Base):
    __tablename__ = "paper_positions"
    __table_args__ = (Index("ix_pos_open", "strategy_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"))
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    instrument: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(5))
    quantity: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    entry_time: Mapped[datetime] = mapped_column(TS)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_price: Mapped[float] = mapped_column(Float)
    last_price_at: Mapped[datetime] = mapped_column(TS)
    unrealised_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(8), default="open")  # open|closed
    version: Mapped[int] = mapped_column(Integer, default=1)  # optimistic locking

    strategy: Mapped[Strategy] = relationship()

    __mapper_args__ = {"version_id_col": version}


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("paper_positions.id"), unique=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), index=True)
    instrument: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(5))
    quantity: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float] = mapped_column(Float)
    entry_time: Mapped[datetime] = mapped_column(TS)
    exit_time: Mapped[datetime] = mapped_column(TS, index=True)
    exit_reason: Mapped[str] = mapped_column(String(20))  # stop_loss|take_profit|signal|manual
    pnl: Mapped[float] = mapped_column(Float)
    return_pct: Mapped[float] = mapped_column(Float)


class Job(Base):
    """Durable background job queue (research / optimisation / validation / reports)."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "run_after"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # queued|running|succeeded|failed
    status: Mapped[str] = mapped_column(String(12), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(TS, default=utcnow)
    locked_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    schedule_id: Mapped[int | None] = mapped_column(ForeignKey("research_schedules.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


class ResearchSchedule(Base):
    __tablename__ = "research_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[str] = mapped_column(String(40))  # job kind
    cron: Mapped[str] = mapped_column(String(100))  # 5-field cron, evaluated in `timezone`
    timezone: Mapped[str] = mapped_column(String(50), default="UTC")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


class PerformanceSnapshot(Base):
    __tablename__ = "performance_snapshots"
    __table_args__ = (UniqueConstraint("strategy_id", "as_of", "source", name="uq_perf"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), index=True)
    as_of: Mapped[datetime] = mapped_column(TS)
    source: Mapped[str] = mapped_column(String(12))  # paper|backtest
    metrics: Mapped[dict] = mapped_column(JSON)


class Heartbeat(Base):
    """One row per long-running component; updated every few seconds."""

    __tablename__ = "heartbeats"

    component: Mapped[str] = mapped_column(String(60), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="ok")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    host: Mapped[str | None] = mapped_column(String(100), nullable=True)
    started_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    cpu_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory_mb: Mapped[float | None] = mapped_column(Float, nullable=True)


class FeedStatus(Base):
    __tablename__ = "feed_status"

    instrument: Mapped[str] = mapped_column(String(16), primary_key=True)
    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    market_open: Mapped[bool] = mapped_column(Boolean, default=False)
    last_success_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_candle_ts: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notif_dedup", "dedup_key", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    severity: Mapped[str] = mapped_column(String(10), default="info")  # info|warning|critical
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    dedup_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # pending -> sent | failed (delivery is retried by the notifier loop)
    delivery_status: Mapped[str] = mapped_column(String(10), default="pending", index=True)
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    delivery_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)
