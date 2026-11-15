"""Simulated order-fill rules shared by live paper trading and backtests.

Conservative assumptions (documented so results are never flattering):
* Entries fill at the close of the signal candle.
* A stop or target is checked against each later candle's high/low.
* If the candle *opens* beyond the stop/target (a gap), the fill is at the open price.
* If both stop and target lie inside the same candle, the stop is assumed to fill first.
"""
from __future__ import annotations

from app.strategies import Bar


def direction(side: str) -> int:
    return 1 if side == "long" else -1


def check_exit(side: str, stop: float | None, target: float | None, bar: Bar) -> tuple[str, float] | None:
    """Return (reason, fill_price) if the bar triggers the stop or target, else None."""
    if side == "long":
        if stop is not None and bar.open <= stop:
            return "stop_loss", bar.open
        if target is not None and bar.open >= target:
            return "take_profit", bar.open
        if stop is not None and bar.low <= stop:
            return "stop_loss", stop
        if target is not None and bar.high >= target:
            return "take_profit", target
    else:
        if stop is not None and bar.open >= stop:
            return "stop_loss", bar.open
        if target is not None and bar.open <= target:
            return "take_profit", bar.open
        if stop is not None and bar.high >= stop:
            return "stop_loss", stop
        if target is not None and bar.low <= target:
            return "take_profit", target
    return None


def pnl(side: str, entry: float, exit_: float, qty: float) -> float:
    return (exit_ - entry) * qty * direction(side)


def return_pct(side: str, entry: float, exit_: float) -> float:
    return (exit_ - entry) / entry * 100 * direction(side)
