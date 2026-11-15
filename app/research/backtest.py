"""Historical backtesting, optimisation and walk-forward validation.

Uses the same strategy ``evaluate`` code and the same fill rules (``engine.fills``) as live
paper trading, so research and live results are directly comparable.
"""
from __future__ import annotations

import itertools
import math
import random
from dataclasses import asdict, dataclass

from app.engine.fills import check_exit, pnl, return_pct
from app.strategies import Bar, build


@dataclass
class BTTrade:
    side: str
    entry_ts: str
    exit_ts: str
    entry: float
    exit: float
    reason: str
    pnl: float
    return_pct: float


def run_backtest(kind: str, params: dict, bars: list[Bar], quantity: float = 1.0) -> dict:
    strat = build(kind, params)
    warm = strat.warmup()
    trades: list[BTTrade] = []
    pos = None  # dict(side, entry, sl, tp, ts)
    for i in range(warm, len(bars)):
        bar = bars[i]
        if pos is not None:
            hit = check_exit(pos["side"], pos["sl"], pos["tp"], bar)
            if hit:
                reason, price = hit
                trades.append(_mk(pos, bar, price, reason, quantity))
                pos = None
        window = bars[max(0, i - warm - 5) : i + 1]
        d = strat.evaluate(window, pos["side"] if pos else None)
        if d is None:
            continue
        if d.side == "exit":
            if pos is not None:
                trades.append(_mk(pos, bar, d.price, "signal", quantity))
                pos = None
        elif pos is None:
            pos = {"side": d.side, "entry": d.price, "sl": d.stop_loss, "tp": d.take_profit, "ts": bar.ts}
    return {"metrics": metrics([asdict(t) for t in trades]), "trades": [asdict(t) for t in trades],
            "bars": len(bars), "open_position": pos is not None}


def _mk(pos, bar, price, reason, qty) -> BTTrade:
    return BTTrade(pos["side"], pos["ts"].isoformat(), bar.ts.isoformat(), pos["entry"], price, reason,
                   pnl(pos["side"], pos["entry"], price, qty), return_pct(pos["side"], pos["entry"], price))


def metrics(trades: list[dict]) -> dict:
    """Trade-level performance metrics. ``trades`` need 'pnl' and 'return_pct'."""
    n = len(trades)
    if n == 0:
        return {"trades": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_return_pct": 0.0,
                "profit_factor": 0.0, "sharpe": 0.0, "max_drawdown_pct": 0.0, "expectancy": 0.0}
    rets = [t["return_pct"] for t in trades]
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]
    mean = sum(rets) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (n - 1)) if n > 1 else 0.0
    # compounded equity curve from per-trade returns -> drawdown in %
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= 1 + r / 100
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak * 100)
    return {
        "trades": n,
        "win_rate": round(len(wins) / n * 100, 2),
        "total_pnl": round(sum(pnls), 4),
        "avg_return_pct": round(mean, 4),
        "profit_factor": round(sum(wins) / sum(losses), 3) if losses else (999.0 if wins else 0.0),
        # per-trade Sharpe (mean/std * sqrt(n)) – comparable across strategies of similar frequency
        "sharpe": round(mean / sd * math.sqrt(n), 3) if sd > 0 else 0.0,
        "max_drawdown_pct": round(mdd, 3),
        "expectancy": round(sum(pnls) / n, 4),
    }


def score(m: dict) -> float:
    """Optimisation objective: Sharpe, penalising tiny samples and deep drawdowns."""
    if m["trades"] < 5:
        return -1e9
    return m["sharpe"] - 0.05 * m["max_drawdown_pct"]


def optimise(kind: str, bars: list[Bar], grid: dict[str, list] | None = None, base: dict | None = None,
             max_combos: int = 200, seed: int = 7) -> dict:
    from app.strategies import REGISTRY

    grid = grid or REGISTRY[kind].param_grid
    keys = list(grid)
    combos = list(itertools.product(*(grid[k] for k in keys)))
    if len(combos) > max_combos:
        combos = random.Random(seed).sample(combos, max_combos)
    results = []
    for values in combos:
        params = {**(base or {}), **dict(zip(keys, values))}
        try:
            m = run_backtest(kind, params, bars)["metrics"]
        except Exception as e:  # invalid combo (e.g. fast >= slow)
            m = {"trades": 0, "error": str(e)}
        if m.get("trades", 0) == 0:
            continue
        results.append({"params": params, "metrics": m, "score": round(score(m), 4)})
    results.sort(key=lambda r: r["score"], reverse=True)
    return {"evaluated": len(combos), "best": results[0] if results else None, "top": results[:10]}


def walk_forward(kind: str, bars: list[Bar], folds: int = 4, train_frac: float = 0.7, base: dict | None = None) -> dict:
    """Anchored-window walk-forward: optimise on each in-sample window, test on the next
    unseen segment. Reports aggregated out-of-sample performance."""
    n = len(bars)
    seg = n // (folds + 1)
    if seg < 100:
        raise ValueError(f"not enough history for walk-forward: {n} bars")
    out = []
    oos_trades: list[dict] = []
    for f in range(folds):
        train = bars[: seg * (f + 1)]
        test = bars[max(0, seg * (f + 1) - 200) : seg * (f + 2)]  # include warm-up context
        best = optimise(kind, train, base=base, max_combos=60)["best"]
        if not best:
            out.append({"fold": f, "status": "no viable parameters"})
            continue
        res = run_backtest(kind, best["params"], test)
        cutoff = bars[seg * (f + 1)].ts.isoformat()
        tr = [t for t in res["trades"] if t["entry_ts"] >= cutoff]
        oos_trades += tr
        out.append({"fold": f, "params": best["params"], "in_sample": best["metrics"], "out_of_sample": metrics(tr)})
    oos = metrics(oos_trades)
    ins = [f["in_sample"]["sharpe"] for f in out if "in_sample" in f]
    return {
        "folds": out,
        "out_of_sample": oos,
        "in_sample_avg_sharpe": round(sum(ins) / len(ins), 3) if ins else None,
        "robust": bool(oos["trades"] >= 10 and oos["sharpe"] > 0 and oos["profit_factor"] > 1),
    }
