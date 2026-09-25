"""Signals for fixed-time trades.

A fixed-time trade only asks one question: will price be above (CALL) or
below (PUT) where it is now, `expiry` seconds from now? Size of the move does
not matter, so the signal only needs direction and a confidence.

Two strategies, picked in config:

reversion  Short-term mean reversion. Price closes outside a Bollinger band,
           RSI agrees it is stretched, and the slow EMA is flat (a ranging
           market). Bet on the snap back. This is the setup most of the
           Pocket Option bots on GitHub and Reddit use, mostly on OTC pairs.

momentum   Trend continuation. Fast EMA over slow EMA, both sloping the same
           way, RSI on the trend side of 50 but not exhausted, and the last
           candle closed with the trend. Bet the move carries on.

Neither is a proven edge. `pocketbot backtest` and the live stats exist to
measure that, against the breakeven win rate the payout implies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from flowbot.signals.indicators import ema, last, rsi, sma, stdev

from .market import Candle

CALL = "call"
PUT = "put"


@dataclass
class StrategyConfig:
    name: str = "reversion"
    bb_period: int = 20
    bb_k: float = 2.2
    rsi_period: int = 7
    rsi_low: float = 25.0
    rsi_high: float = 75.0
    ema_fast: int = 9
    ema_slow: int = 50
    # Max slope of the slow EMA over `trend_lookback` candles, in band widths,
    # for reversion to count the market as ranging.
    max_trend: float = 0.35
    trend_lookback: int = 10
    min_confidence: float = 0.0

    @classmethod
    def from_dict(cls, d: dict | None) -> "StrategyConfig":
        d = dict(d or {})
        known = {k: d.pop(k) for k in list(d) if k in cls.__dataclass_fields__}
        if d:
            raise ValueError(f"unknown strategy settings: {sorted(d)}")
        return cls(**known)


@dataclass
class Signal:
    direction: str | None          # CALL, PUT or None
    confidence: float = 0.0        # 0..1
    reason: str = ""
    features: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.direction is not None


def warmup(cfg: StrategyConfig) -> int:
    return max(cfg.bb_period, cfg.ema_slow + cfg.trend_lookback, cfg.rsi_period + 1) + 1


def evaluate(candles: Sequence[Candle], cfg: StrategyConfig) -> Signal:
    """Decide on the most recent *closed* candle. Never pass a forming candle."""
    if len(candles) < warmup(cfg):
        return Signal(None, reason=f"warming up ({len(candles)}/{warmup(cfg)} candles)")

    closes = [c.close for c in candles]
    mid = sma(closes, cfg.bb_period)
    sd = stdev(closes, cfg.bb_period)
    r = rsi(closes, cfg.rsi_period)
    fast = ema(closes, cfg.ema_fast)
    slow = ema(closes, cfg.ema_slow)

    price = closes[-1]
    m, s = last(mid), last(sd)
    upper, lower = m + cfg.bb_k * s, m - cfg.bb_k * s
    band = max(upper - lower, 1e-12)
    rsi_now = last(r, 50.0)
    slow_now = last(slow)
    slow_then = slow[-1 - cfg.trend_lookback] or slow_now
    trend = (slow_now - slow_then) / band          # band widths per lookback
    features = {
        "price": price, "bb_upper": upper, "bb_lower": lower, "rsi": rsi_now,
        "ema_fast": last(fast), "ema_slow": slow_now, "trend": trend,
    }

    if cfg.name == "reversion":
        sig = _reversion(price, upper, lower, band, rsi_now, trend, cfg)
    elif cfg.name == "momentum":
        sig = _momentum(candles[-1], last(fast), slow_now, trend, rsi_now, cfg)
    else:
        raise ValueError(f"unknown strategy {cfg.name!r}")
    sig.features = features
    if sig and sig.confidence < cfg.min_confidence:
        return Signal(None, sig.confidence,
                      f"{sig.reason}; confidence {sig.confidence:.2f} < {cfg.min_confidence:.2f}",
                      features)
    return sig


def _reversion(price, upper, lower, band, rsi_now, trend, cfg) -> Signal:
    if abs(trend) > cfg.max_trend:
        return Signal(None, reason=f"trending ({trend:+.2f} band widths), no reversion")
    if price < lower and rsi_now <= cfg.rsi_low:
        depth = (lower - price) / band
        stretch = (cfg.rsi_low - rsi_now) / max(cfg.rsi_low, 1e-9)
        return Signal(CALL, _conf(depth, stretch), f"below lower band, RSI {rsi_now:.0f}")
    if price > upper and rsi_now >= cfg.rsi_high:
        depth = (price - upper) / band
        stretch = (rsi_now - cfg.rsi_high) / max(100 - cfg.rsi_high, 1e-9)
        return Signal(PUT, _conf(depth, stretch), f"above upper band, RSI {rsi_now:.0f}")
    return Signal(None, reason="inside the bands")


def _momentum(bar: Candle, fast, slow, trend, rsi_now, cfg) -> Signal:
    body_up = bar.close > bar.open
    body_dn = bar.close < bar.open
    if fast > slow and trend > 0 and 50 < rsi_now < cfg.rsi_high and body_up:
        return Signal(CALL, _conf(trend, (rsi_now - 50) / 50), "uptrend continuation")
    if fast < slow and trend < 0 and cfg.rsi_low < rsi_now < 50 and body_dn:
        return Signal(PUT, _conf(-trend, (50 - rsi_now) / 50), "downtrend continuation")
    return Signal(None, reason="no aligned trend")


def _conf(a: float, b: float) -> float:
    return max(0.0, min(1.0, 0.5 * min(a, 1.0) + 0.5 * min(b, 1.0)))
