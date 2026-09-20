"""Technical indicators, pure Python, no numpy.

Every function returns a full series aligned to the input (leading values are
None until the window fills) so the dashboard can plot them and the engine can
look at slopes rather than just the latest print. The windows we run are small
(a few hundred 15m bars), so the O(n) loops here cost microseconds per bar and
buy us a dependency-free install.

Wilder's smoothing is used for RSI/ATR/ADX, matching what charting platforms
show - it matters that our numbers line up with what a human sees on a chart
when they are sanity-checking the bot.
"""

from __future__ import annotations

import math
from typing import Sequence

from ..core.types import Candle

Series = list[float | None]


def sma(values: Sequence[float], period: int) -> Series:
    out: Series = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    total = sum(values[:period])
    out[period - 1] = total / period
    for i in range(period, len(values)):
        total += values[i] - values[i - period]
        out[i] = total / period
    return out


def ema(values: Sequence[float], period: int) -> Series:
    out: Series = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def wilder(values: Sequence[float], period: int) -> Series:
    """Wilder's smoothing (RMA): the average used by RSI/ATR/ADX."""
    out: Series = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


def true_range(candles: Sequence[Candle]) -> list[float]:
    out: list[float] = []
    prev_close = None
    for c in candles:
        if prev_close is None:
            out.append(c.high - c.low)
        else:
            out.append(max(
                c.high - c.low,
                abs(c.high - prev_close),
                abs(c.low - prev_close),
            ))
        prev_close = c.close
    return out


def atr(candles: Sequence[Candle], period: int = 14) -> Series:
    return wilder(true_range(candles), period)


def rsi(values: Sequence[float], period: int = 14) -> Series:
    out: Series = [None] * len(values)
    if len(values) <= period:
        return out
    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))
    avg_gain = wilder(gains, period)
    avg_loss = wilder(losses, period)
    for i in range(len(gains)):
        g, l = avg_gain[i], avg_loss[i]
        if g is None or l is None:
            continue
        if l == 0:
            out[i + 1] = 100.0
        else:
            rs = g / l
            out[i + 1] = 100.0 - (100.0 / (1.0 + rs))
    return out


def adx(candles: Sequence[Candle], period: int = 14) -> tuple[Series, Series, Series]:
    """Returns (adx, +DI, -DI). ADX is our trend-vs-chop gate."""
    n = len(candles)
    out_adx: Series = [None] * n
    out_pdi: Series = [None] * n
    out_mdi: Series = [None] * n
    if n <= period + 1:
        return out_adx, out_pdi, out_mdi

    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, n):
        up = candles[i].high - candles[i - 1].high
        down = candles[i - 1].low - candles[i].low
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        trs.append(max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - candles[i - 1].close),
            abs(candles[i].low - candles[i - 1].close),
        ))

    sm_tr = wilder(trs, period)
    sm_plus = wilder(plus_dm, period)
    sm_minus = wilder(minus_dm, period)

    dx: list[float | None] = [None] * len(trs)
    for i in range(len(trs)):
        tr_v, p_v, m_v = sm_tr[i], sm_plus[i], sm_minus[i]
        if tr_v is None or p_v is None or m_v is None or tr_v == 0:
            continue
        pdi = 100.0 * p_v / tr_v
        mdi = 100.0 * m_v / tr_v
        out_pdi[i + 1] = pdi
        out_mdi[i + 1] = mdi
        denom = pdi + mdi
        dx[i] = 100.0 * abs(pdi - mdi) / denom if denom else 0.0

    valid = [(i, v) for i, v in enumerate(dx) if v is not None]
    if len(valid) >= period:
        window = [v for _, v in valid[:period]]
        prev = sum(window) / period
        out_adx[valid[period - 1][0] + 1] = prev
        for i, v in valid[period:]:
            prev = (prev * (period - 1) + v) / period
            out_adx[i + 1] = prev
    return out_adx, out_pdi, out_mdi


def macd(
    values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[Series, Series, Series]:
    fast_e = ema(values, fast)
    slow_e = ema(values, slow)
    line: Series = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_e, slow_e)
    ]
    defined = [v for v in line if v is not None]
    sig_vals = ema(defined, signal)
    sig: Series = [None] * len(line)
    offset = len(line) - len(defined)
    for i, v in enumerate(sig_vals):
        sig[offset + i] = v
    hist: Series = [
        (l - s) if (l is not None and s is not None) else None
        for l, s in zip(line, sig)
    ]
    return line, sig, hist


def donchian(candles: Sequence[Candle], period: int = 20, exclude_current: bool = True):
    """Rolling breakout channel. Excluding the current bar is what makes a
    close above the upper band an actual breakout rather than a tautology."""
    n = len(candles)
    upper: Series = [None] * n
    lower: Series = [None] * n
    shift = 1 if exclude_current else 0
    for i in range(n):
        start = i - period - shift + 1
        end = i - shift + 1
        if start < 0 or end <= start:
            continue
        window = candles[start:end]
        upper[i] = max(c.high for c in window)
        lower[i] = min(c.low for c in window)
    return upper, lower


def stdev(values: Sequence[float], period: int) -> Series:
    out: Series = [None] * len(values)
    if len(values) < period or period < 2:
        return out
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        var = sum((v - mean) ** 2 for v in window) / (period - 1)
        out[i] = math.sqrt(var)
    return out


def zscore(values: Sequence[float], period: int) -> Series:
    out: Series = [None] * len(values)
    sd = stdev(values, period)
    for i in range(len(values)):
        if sd[i] is None or sd[i] == 0:
            continue
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        out[i] = (values[i] - mean) / sd[i]
    return out


def roc(values: Sequence[float], period: int) -> Series:
    """Rate of change in percent."""
    out: Series = [None] * len(values)
    for i in range(period, len(values)):
        base = values[i - period]
        if base:
            out[i] = (values[i] / base - 1.0) * 100.0
    return out


def slope(values: Sequence[float | None], period: int) -> float:
    """Least-squares slope per bar over the last `period` defined points."""
    pts = [v for v in values[-period:] if v is not None]
    n = len(pts)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2
    mean_y = sum(pts) / n
    num = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(pts))
    den = sum((i - mean_x) ** 2 for i in range(n))
    return num / den if den else 0.0


def realized_vol(closes: Sequence[float], period: int, bars_per_day: int = 96) -> float:
    """Annualised realised volatility from log returns of the last `period` bars."""
    if len(closes) < period + 1:
        return 0.0
    rets = [
        math.log(closes[i] / closes[i - 1])
        for i in range(len(closes) - period, len(closes))
        if closes[i - 1] > 0
    ]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(bars_per_day * 365)


def percentile_rank(values: Sequence[float], value: float) -> float:
    """Where `value` sits inside `values`, as 0..1. Used for vol regimes."""
    clean = [v for v in values if v is not None]
    if not clean:
        return 0.5
    below = sum(1 for v in clean if v < value)
    return below / len(clean)


def last(series: Series, default: float = 0.0) -> float:
    for v in reversed(series):
        if v is not None:
            return v
    return default


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def squash(x: float, scale: float = 1.0) -> float:
    """Map an unbounded value into [-1, 1] smoothly (tanh-ish, cheap)."""
    if scale <= 0:
        return 0.0
    return math.tanh(x / scale)
