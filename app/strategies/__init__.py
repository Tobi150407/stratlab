"""Strategy plug-in interface.

A strategy is a *pure function of closed candles*. The exact same ``evaluate`` code runs
in live paper trading and in historical backtests / optimisation, so research results and
live behaviour cannot drift apart.

To add a strategy: subclass ``StrategyBase``, set ``kind``/``default_params``/``param_grid``
and decorate with ``@register``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Decision:
    side: str  # "long" | "short" | "exit"
    price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    reason: str = ""


class StrategyBase:
    kind: ClassVar[str]
    description: ClassVar[str] = ""
    default_params: ClassVar[dict] = {}
    param_grid: ClassVar[dict[str, list]] = {}

    def __init__(self, params: dict | None = None):
        self.params = {**self.default_params, **(params or {})}

    def warmup(self) -> int:
        """Number of closed bars needed before the strategy can produce a decision."""
        raise NotImplementedError

    def evaluate(self, bars: list[Bar], position_side: str | None) -> Decision | None:
        """Called once per newly closed candle. ``bars[-1]`` is the candle that just closed.
        ``position_side`` is the side of the strategy's open paper position (or None)."""
        raise NotImplementedError


REGISTRY: dict[str, type[StrategyBase]] = {}


def register(cls: type[StrategyBase]) -> type[StrategyBase]:
    REGISTRY[cls.kind] = cls
    return cls


def build(kind: str, params: dict | None = None) -> StrategyBase:
    if kind not in REGISTRY:
        raise ValueError(f"unknown strategy kind {kind!r}; available: {sorted(REGISTRY)}")
    return REGISTRY[kind](params)


# --------------------------------------------------------------------------- indicators
def sma(values: list[float], n: int) -> float:
    return sum(values[-n:]) / n


def atr(bars: list[Bar], n: int) -> float:
    trs = []
    for i in range(len(bars) - n, len(bars)):
        b, prev = bars[i], bars[i - 1]
        trs.append(max(b.high - b.low, abs(b.high - prev.close), abs(b.low - prev.close)))
    return sum(trs) / n


def rsi(closes: list[float], n: int) -> float:
    gains = losses = 0.0
    for i in range(len(closes) - n, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0)
        losses += max(-ch, 0)
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


def _brackets(side: str, price: float, a: float, sl_mult: float, tp_mult: float) -> tuple[float, float]:
    if side == "long":
        return price - sl_mult * a, price + tp_mult * a
    return price + sl_mult * a, price - tp_mult * a


# --------------------------------------------------------------------------- built-ins
@register
class SmaCross(StrategyBase):
    kind = "sma_cross"
    description = "Trend following: fast SMA crossing slow SMA, ATR-based stop / target."
    default_params = {"fast": 10, "slow": 30, "atr_period": 14, "sl_atr": 1.5, "tp_atr": 3.0, "allow_short": True}
    param_grid = {"fast": [5, 10, 20], "slow": [30, 50, 100], "sl_atr": [1.0, 1.5, 2.0], "tp_atr": [2.0, 3.0]}

    def warmup(self) -> int:
        return max(self.params["slow"], self.params["atr_period"]) + 2

    def evaluate(self, bars, position_side):
        p = self.params
        if p["fast"] >= p["slow"] or len(bars) < self.warmup():
            return None
        closes = [b.close for b in bars]
        f_now, s_now = sma(closes, p["fast"]), sma(closes, p["slow"])
        f_prev, s_prev = sma(closes[:-1], p["fast"]), sma(closes[:-1], p["slow"])
        price = closes[-1]
        a = atr(bars, p["atr_period"])
        if f_prev <= s_prev and f_now > s_now:
            side = "long"
        elif f_prev >= s_prev and f_now < s_now and p["allow_short"]:
            side = "short"
        else:
            return None
        if position_side == side:
            return None
        if position_side is not None:
            return Decision("exit", price, reason=f"opposite crossover ({side})")
        sl, tp = _brackets(side, price, a, p["sl_atr"], p["tp_atr"])
        return Decision(side, price, sl, tp, f"SMA{p['fast']} crossed {'above' if side == 'long' else 'below'} SMA{p['slow']}")


@register
class DonchianBreakout(StrategyBase):
    kind = "donchian_breakout"
    description = "Breakout of the N-bar high/low channel, ATR-based stop / target."
    default_params = {"lookback": 20, "atr_period": 14, "sl_atr": 2.0, "tp_atr": 4.0, "allow_short": True}
    param_grid = {"lookback": [10, 20, 55], "sl_atr": [1.5, 2.0, 3.0], "tp_atr": [3.0, 4.0, 6.0]}

    def warmup(self) -> int:
        return max(self.params["lookback"], self.params["atr_period"]) + 2

    def evaluate(self, bars, position_side):
        p = self.params
        if len(bars) < self.warmup() or position_side is not None:
            return None
        window = bars[-p["lookback"] - 1 : -1]
        hi, lo = max(b.high for b in window), min(b.low for b in window)
        last = bars[-1]
        a = atr(bars, p["atr_period"])
        if last.close > hi:
            side = "long"
        elif last.close < lo and p["allow_short"]:
            side = "short"
        else:
            return None
        sl, tp = _brackets(side, last.close, a, p["sl_atr"], p["tp_atr"])
        return Decision(side, last.close, sl, tp, f"close broke {p['lookback']}-bar {'high' if side == 'long' else 'low'}")


@register
class RsiReversion(StrategyBase):
    kind = "rsi_reversion"
    description = "Mean reversion: buy oversold / sell overbought RSI, exit at RSI midline."
    default_params = {"period": 14, "oversold": 30, "overbought": 70, "atr_period": 14, "sl_atr": 2.0, "tp_atr": 2.0}
    param_grid = {"period": [7, 14, 21], "oversold": [20, 25, 30], "overbought": [70, 75, 80], "sl_atr": [1.5, 2.0]}

    def warmup(self) -> int:
        return max(self.params["period"], self.params["atr_period"]) + 2

    def evaluate(self, bars, position_side):
        p = self.params
        if len(bars) < self.warmup():
            return None
        closes = [b.close for b in bars]
        r = rsi(closes, p["period"])
        price = closes[-1]
        if position_side == "long" and r >= 50:
            return Decision("exit", price, reason=f"RSI back to {r:.1f}")
        if position_side == "short" and r <= 50:
            return Decision("exit", price, reason=f"RSI back to {r:.1f}")
        if position_side is not None:
            return None
        a = atr(bars, p["atr_period"])
        if r < p["oversold"]:
            sl, tp = _brackets("long", price, a, p["sl_atr"], p["tp_atr"])
            return Decision("long", price, sl, tp, f"RSI {r:.1f} < {p['oversold']}")
        if r > p["overbought"]:
            sl, tp = _brackets("short", price, a, p["sl_atr"], p["tp_atr"])
            return Decision("short", price, sl, tp, f"RSI {r:.1f} > {p['overbought']}")
        return None
