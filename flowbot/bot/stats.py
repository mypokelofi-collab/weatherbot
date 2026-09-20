"""Performance statistics over closed trades and the equity curve.

Deliberately R-multiple first. A momentum bot that takes a 0.5% risk per trade
should be judged on expectancy per unit of risk, not on a dollar total that
just reflects how big the account happened to be.
"""

from __future__ import annotations

import math
from typing import Sequence

from ..core.types import ClosedTrade

BARS_PER_YEAR_15M = 35_040      # 96 bars/day * 365


def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default


def trade_stats(trades: Sequence[ClosedTrade]) -> dict:
    n = len(trades)
    if n == 0:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "avg_r": 0.0, "expectancy_r": 0.0, "avg_win_r": 0.0, "avg_loss_r": 0.0,
            "profit_factor": 0.0, "gross_profit": 0.0, "gross_loss": 0.0,
            "net_pnl": 0.0, "fees": 0.0, "best_r": 0.0, "worst_r": 0.0,
            "avg_bars_held": 0.0, "payoff_ratio": 0.0, "longs": 0, "shorts": 0,
            "long_win_rate": 0.0, "short_win_rate": 0.0,
            "avg_entry_slippage_bps": 0.0, "avg_exit_slippage_bps": 0.0,
            "max_consecutive_losses": 0, "max_consecutive_wins": 0,
        }

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    longs = [t for t in trades if t.side.value == "buy"]
    shorts = [t for t in trades if t.side.value == "sell"]

    streak = best_loss_streak = best_win_streak = 0
    for t in trades:
        if t.pnl <= 0:
            streak = streak - 1 if streak < 0 else -1
            best_loss_streak = min(best_loss_streak, streak)
        else:
            streak = streak + 1 if streak > 0 else 1
            best_win_streak = max(best_win_streak, streak)

    avg_win_r = _safe_div(sum(t.r_multiple for t in wins), len(wins))
    avg_loss_r = _safe_div(sum(t.r_multiple for t in losses), len(losses))

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(_safe_div(len(wins), n) * 100, 2),
        "avg_r": round(_safe_div(sum(t.r_multiple for t in trades), n), 3),
        "expectancy_r": round(_safe_div(sum(t.r_multiple for t in trades), n), 3),
        "avg_win_r": round(avg_win_r, 3),
        "avg_loss_r": round(avg_loss_r, 3),
        "payoff_ratio": round(abs(_safe_div(avg_win_r, avg_loss_r)), 3) if avg_loss_r else 0.0,
        "profit_factor": round(_safe_div(gross_profit, gross_loss), 3),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net_pnl": round(sum(t.pnl for t in trades), 2),
        "fees": round(sum(t.fees for t in trades), 2),
        "best_r": round(max(t.r_multiple for t in trades), 3),
        "worst_r": round(min(t.r_multiple for t in trades), 3),
        "avg_bars_held": round(_safe_div(sum(t.bars_held for t in trades), n), 2),
        "longs": len(longs),
        "shorts": len(shorts),
        "long_win_rate": round(_safe_div(len([t for t in longs if t.pnl > 0]), len(longs)) * 100, 2),
        "short_win_rate": round(_safe_div(len([t for t in shorts if t.pnl > 0]), len(shorts)) * 100, 2),
        "avg_entry_slippage_bps": round(_safe_div(sum(t.entry_slippage_bps for t in trades), n), 3),
        "avg_exit_slippage_bps": round(_safe_div(sum(t.exit_slippage_bps for t in trades), n), 3),
        "max_consecutive_losses": abs(best_loss_streak),
        "max_consecutive_wins": best_win_streak,
    }


def curve_stats(curve: Sequence[tuple[int, float]], start_equity: float) -> dict:
    """Drawdown, Sharpe and CAGR from the sampled equity curve."""
    if len(curve) < 2:
        return {
            "max_drawdown_pct": 0.0, "current_drawdown_pct": 0.0,
            "sharpe": 0.0, "sortino": 0.0, "cagr_pct": 0.0,
            "return_pct": 0.0, "days": 0.0, "time_in_market_pct": 0.0,
        }

    peak = curve[0][1]
    max_dd = 0.0
    rets: list[float] = []
    prev = curve[0][1]
    for _ts, eq in curve:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = min(max_dd, (eq - peak) / peak)
        if prev > 0:
            rets.append(eq / prev - 1)
        prev = eq

    span_ms = curve[-1][0] - curve[0][0]
    days = span_ms / 86_400_000 if span_ms > 0 else 0.0
    final = curve[-1][1]
    total_ret = (final / start_equity - 1) if start_equity else 0.0

    mean = sum(rets) / len(rets) if rets else 0.0
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) if len(rets) > 1 else 0.0
    sd = math.sqrt(var)
    downside = [r for r in rets if r < 0]
    dvar = sum(r * r for r in downside) / len(downside) if downside else 0.0
    dsd = math.sqrt(dvar)
    # Samples are ~5s apart; annualise by sample count per year.
    per_year = (len(rets) / days * 365) if days > 0 else 0.0
    ann = math.sqrt(per_year) if per_year > 0 else 0.0

    cur_peak = max(eq for _t, eq in curve)
    return {
        "max_drawdown_pct": round(max_dd * 100, 3),
        "current_drawdown_pct": round(((final - cur_peak) / cur_peak * 100) if cur_peak else 0.0, 3),
        "sharpe": round(_safe_div(mean, sd) * ann, 3),
        "sortino": round(_safe_div(mean, dsd) * ann, 3),
        "return_pct": round(total_ret * 100, 3),
        "cagr_pct": round(((final / start_equity) ** (365 / days) - 1) * 100, 3)
        if days > 1 and start_equity > 0 and final > 0 else 0.0,
        "days": round(days, 3),
    }


def full_stats(
    trades: Sequence[ClosedTrade],
    curve: Sequence[tuple[int, float]],
    start_equity: float,
    bars_in_market: int = 0,
    total_bars: int = 0,
) -> dict:
    out = trade_stats(trades)
    out.update(curve_stats(curve, start_equity))
    out["time_in_market_pct"] = round(_safe_div(bars_in_market, total_bars) * 100, 2)
    out["bars_in_market"] = bars_in_market
    out["total_bars"] = total_bars
    return out
