"""Market data providers.

Providers return *closed* candles only. The ``FeedSupervisor`` wraps a provider with
reconnection (exponential backoff), feed-status bookkeeping, and interruption alerts.
"""
from __future__ import annotations

import hashlib
import logging
import math
from datetime import datetime, timedelta, timezone

import httpx

from app.config import get_settings
from app.market_hours import (
    candle_close_time,
    floor_to_timeframe,
    is_market_open,
    timeframe_seconds,
)
from app.strategies import Bar

log = logging.getLogger(__name__)
UTC = timezone.utc


class FeedError(Exception):
    pass


class MarketDataProvider:
    name = "base"

    def fetch_closed_candles(self, instrument: str, timeframe: str, since: datetime | None, now: datetime) -> list[Bar]:
        raise NotImplementedError


# --------------------------------------------------------------------------- simulated
class SimulatedProvider(MarketDataProvider):
    """Deterministic synthetic prices — identical after a restart, only produced while
    the instrument's market is open. Lets the whole system run with no API key."""

    name = "simulated"
    BASE = {"GOLD": 2400.0, "NASDAQ": 20000.0}
    VOL = {"GOLD": 0.012, "NASDAQ": 0.02}

    def _price(self, instrument: str, t: datetime) -> float:
        x = t.timestamp()
        waves = (
            0.6 * math.sin(x / 86400 / 7 * 2 * math.pi)
            + 0.3 * math.sin(x / 86400 * 2 * math.pi + 1.3)
            + 0.15 * math.sin(x / 7200 * 2 * math.pi + 0.7)
            + 0.08 * math.sin(x / 1500 * 2 * math.pi + 2.1)
        )
        h = int(hashlib.sha256(f"{instrument}{int(x)}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        noise = (h - 0.5) * 0.08
        return self.BASE[instrument] * math.exp(self.VOL[instrument] * (waves + noise))

    def fetch_closed_candles(self, instrument, timeframe, since, now):
        tf = timeframe_seconds(timeframe)
        end = floor_to_timeframe(now, timeframe)  # open time of the forming candle
        start = since + timedelta(seconds=tf) if since else end - timedelta(seconds=tf * 300)
        start = max(start, end - timedelta(seconds=tf * 2000))
        bars: list[Bar] = []
        t = floor_to_timeframe(start, timeframe)
        while t < end:
            if is_market_open(instrument, t):
                pts = [self._price(instrument, t + timedelta(seconds=tf * k / 4)) for k in range(5)]
                bars.append(Bar(t, pts[0], max(pts), min(pts), pts[-1], 100.0))
            t += timedelta(seconds=tf)
        return bars


# --------------------------------------------------------------------------- Twelve Data
_TD_INTERVAL = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1day"}


class TwelveDataProvider(MarketDataProvider):
    """REST polling against twelvedata.com (free tier covers XAU/USD and NDX)."""

    name = "twelvedata"

    def __init__(self):
        s = get_settings()
        if not s.twelvedata_api_key:
            raise FeedError("SL_TWELVEDATA_API_KEY is not set")
        self.s = s
        self.symbols = {"GOLD": s.gold_provider_symbol, "NASDAQ": s.nasdaq_provider_symbol}
        self.client = httpx.Client(base_url=s.twelvedata_base_url, timeout=15)

    def fetch_closed_candles(self, instrument, timeframe, since, now):
        tf = timeframe_seconds(timeframe)
        outputsize = 300 if since is None else min(5000, max(5, int((now - since).total_seconds() // tf) + 5))
        try:
            r = self.client.get(
                "/time_series",
                params={
                    "symbol": self.symbols[instrument],
                    "interval": _TD_INTERVAL[timeframe],
                    "outputsize": outputsize,
                    "timezone": "UTC",
                    "order": "ASC",
                    "apikey": self.s.twelvedata_api_key,
                },
            )
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise FeedError(f"twelvedata request failed: {e}") from e
        if data.get("status") == "error":
            raise FeedError(f"twelvedata error: {data.get('message')}")
        bars = []
        for v in data.get("values", []):
            ts = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S" if " " in v["datetime"] else "%Y-%m-%d").replace(tzinfo=UTC)
            if candle_close_time(ts, timeframe) > now:  # still forming
                continue
            if since and ts <= since:
                continue
            bars.append(Bar(ts, float(v["open"]), float(v["high"]), float(v["low"]), float(v["close"]), float(v.get("volume") or 0)))
        return bars


def build_provider() -> MarketDataProvider:
    p = get_settings().data_provider
    if p == "simulated":
        return SimulatedProvider()
    if p == "twelvedata":
        return TwelveDataProvider()
    raise ValueError(f"unknown data provider {p!r}")
