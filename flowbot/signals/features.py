"""Feature extraction: turn bars + book + tape into the numbers the model scores.

Three sources feed the model, and each answers a different question:

  bars  - where has price been going? (trend, breakout, volatility)
  tape  - who is being aggressive right now? (CVD, aggressor imbalance)
  book  - what is resting in front of us? (imbalance, spread, depth)

Keeping them in one snapshot means a signal, an order and a dashboard row all
reference exactly the same market state - no "the chart said X but the bot saw
Y" ambiguity when you audit a trade later.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Sequence

from ..core.config import SignalConfig
from ..core.types import BookSnapshot, Candle, Side, Trade
from . import indicators as ind


class TapeWindow:
    """Rolling window over the real trade tape (default: last 5 minutes)."""

    def __init__(self, window_ms: int = 300_000, max_items: int = 20_000) -> None:
        self.window_ms = window_ms
        self._dq: deque[Trade] = deque(maxlen=max_items)
        self.cum_delta = 0.0            # session-cumulative, for the CVD chart

    def add(self, t: Trade) -> None:
        self._dq.append(t)
        self.cum_delta += t.qty if t.side is Side.BUY else -t.qty
        self._evict(t.ts)

    def _evict(self, now_ts: int) -> None:
        cutoff = now_ts - self.window_ms
        while self._dq and self._dq[0].ts < cutoff:
            self._dq.popleft()

    def recent(self, n: int = 50) -> list[Trade]:
        return list(self._dq)[-n:]

    def stats(self) -> dict[str, float]:
        if not self._dq:
            return {
                "buy_qty": 0.0, "sell_qty": 0.0, "delta": 0.0, "imbalance": 0.0,
                "notional": 0.0, "trades_per_min": 0.0, "avg_trade_usd": 0.0,
                "big_trade_imbalance": 0.0,
            }
        buy_qty = sell_qty = notional = 0.0
        big_buy = big_sell = 0.0
        sizes = [t.notional for t in self._dq]
        sizes_sorted = sorted(sizes)
        big_cut = sizes_sorted[int(len(sizes_sorted) * 0.9)] if sizes_sorted else 0.0
        for t in self._dq:
            notional += t.notional
            if t.side is Side.BUY:
                buy_qty += t.qty
                if t.notional >= big_cut:
                    big_buy += t.notional
            else:
                sell_qty += t.qty
                if t.notional >= big_cut:
                    big_sell += t.notional
        total = buy_qty + sell_qty
        span_ms = max(1, self._dq[-1].ts - self._dq[0].ts)
        big_total = big_buy + big_sell
        return {
            "buy_qty": buy_qty,
            "sell_qty": sell_qty,
            "delta": buy_qty - sell_qty,
            "imbalance": (buy_qty - sell_qty) / total if total else 0.0,
            "notional": notional,
            "trades_per_min": len(self._dq) / (span_ms / 60_000),
            "avg_trade_usd": notional / len(self._dq),
            # Whales lean the tape more than retail noise does, so size-weight
            # the top decile separately.
            "big_trade_imbalance": (big_buy - big_sell) / big_total if big_total else 0.0,
        }


@dataclass
class Features:
    ts: int = 0
    bar_time: int = 0
    price: float = 0.0

    # trend / volatility
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    ema_spread_atr: float = 0.0
    ema_slow_slope_atr: float = 0.0
    atr: float = 0.0
    atr_pct: float = 0.0
    vol_percentile: float = 0.5
    realized_vol: float = 0.0

    # oscillators
    rsi: float = 50.0
    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0
    macd: float = 0.0
    macd_hist: float = 0.0
    macd_hist_slope: float = 0.0
    roc: float = 0.0
    roc_z: float = 0.0

    # structure
    donchian_up: float = 0.0
    donchian_dn: float = 0.0
    breakout_atr: float = 0.0        # distance beyond the channel, in ATRs
    channel_pos: float = 0.5         # 0 = at the low, 1 = at the high
    close_vs_vwap_atr: float = 0.0

    # bar flow
    bar_delta_ratio: float = 0.0
    cvd_slope_atr: float = 0.0
    volume_z: float = 0.0

    # live micro flow
    tape_imbalance: float = 0.0
    tape_big_imbalance: float = 0.0
    trades_per_min: float = 0.0
    book_imbalance: float = 0.0
    book_imbalance_wide: float = 0.0
    micro_tilt: float = 0.0
    spread_bps: float = 0.0
    depth_bid_usd: float = 0.0
    depth_ask_usd: float = 0.0
    book_levels: int = 0
    bars: int = 0

    extras: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("extras", None)
        d.update(self.extras)
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in d.items()}


def compute_features(
    candles: Sequence[Candle],
    cfg: SignalConfig,
    book: BookSnapshot | None = None,
    tape: TapeWindow | None = None,
) -> Features:
    """Build the feature snapshot from closed bars plus live book/tape."""
    f = Features(bars=len(candles))
    if len(candles) < 5:
        return f

    closes = [c.close for c in candles]
    last_bar = candles[-1]
    f.ts = last_bar.close_time
    f.bar_time = last_bar.open_time
    f.price = last_bar.close

    atr_series = ind.atr(candles, cfg.atr_period)
    atr_v = ind.last(atr_series, 0.0)
    f.atr = atr_v
    f.atr_pct = (atr_v / f.price * 100.0) if f.price else 0.0

    atr_pcts = [
        (a / c.close * 100.0)
        for a, c in zip(atr_series[-cfg.vol_lookback:], candles[-cfg.vol_lookback:])
        if a is not None and c.close
    ]
    f.vol_percentile = ind.percentile_rank(atr_pcts, f.atr_pct) if atr_pcts else 0.5
    f.realized_vol = ind.realized_vol(closes, min(len(closes) - 1, 96))

    ema_f = ind.ema(closes, cfg.ema_fast)
    ema_s = ind.ema(closes, cfg.ema_slow)
    f.ema_fast = ind.last(ema_f, f.price)
    f.ema_slow = ind.last(ema_s, f.price)
    if atr_v > 0:
        f.ema_spread_atr = (f.ema_fast - f.ema_slow) / atr_v
        f.ema_slow_slope_atr = ind.slope(ema_s, 8) / atr_v

    f.rsi = ind.last(ind.rsi(closes, cfg.rsi_period), 50.0)
    adx_s, pdi_s, mdi_s = ind.adx(candles, cfg.adx_period)
    f.adx = ind.last(adx_s, 0.0)
    f.plus_di = ind.last(pdi_s, 0.0)
    f.minus_di = ind.last(mdi_s, 0.0)

    macd_line, _sig, hist = ind.macd(closes)
    f.macd = ind.last(macd_line, 0.0)
    f.macd_hist = ind.last(hist, 0.0)
    f.macd_hist_slope = ind.slope(hist, 5)

    f.roc = ind.last(ind.roc(closes, cfg.roc_period), 0.0)
    roc_series = [v for v in ind.roc(closes, cfg.roc_period) if v is not None]
    f.roc_z = ind.last(ind.zscore(roc_series, min(cfg.z_period, max(2, len(roc_series)))), 0.0)

    up, dn = ind.donchian(candles, cfg.donchian_period)
    f.donchian_up = ind.last(up, f.price)
    f.donchian_dn = ind.last(dn, f.price)
    span = max(1e-9, f.donchian_up - f.donchian_dn)
    f.channel_pos = min(1.5, max(-0.5, (f.price - f.donchian_dn) / span))
    if atr_v > 0:
        if f.price > f.donchian_up:
            f.breakout_atr = (f.price - f.donchian_up) / atr_v
        elif f.price < f.donchian_dn:
            f.breakout_atr = (f.price - f.donchian_dn) / atr_v
        else:
            f.breakout_atr = 0.0
        f.close_vs_vwap_atr = (f.price - last_bar.vwap) / atr_v

    f.bar_delta_ratio = last_bar.delta_ratio
    flow_bars = candles[-cfg.flow_bars:]
    if flow_bars and atr_v > 0:
        cum = 0.0
        cvd_path = []
        for c in flow_bars:
            cum += c.delta
            cvd_path.append(cum)
        avg_vol = sum(c.volume for c in flow_bars) / len(flow_bars) or 1.0
        # Normalise the CVD slope by typical bar volume: a 50 BTC delta means
        # something different on a quiet Sunday than during the US open.
        f.cvd_slope_atr = ind.slope(cvd_path, len(cvd_path)) / avg_vol

    vols = [c.volume for c in candles[-cfg.z_period:]]
    f.volume_z = ind.last(ind.zscore(vols, min(cfg.z_period, max(2, len(vols)))), 0.0)

    if book is not None and book.bids and book.asks:
        f.book_imbalance = book.imbalance(10.0)
        f.book_imbalance_wide = book.imbalance(25.0)
        f.spread_bps = book.spread_bps
        bid_usd, ask_usd = book.depth_notional(10.0)
        f.depth_bid_usd = bid_usd
        f.depth_ask_usd = ask_usd
        f.book_levels = min(len(book.bids), len(book.asks))
        spread = book.spread
        if spread > 0:
            f.micro_tilt = (book.microprice - book.mid) / (spread / 2)

    if tape is not None:
        ts = tape.stats()
        f.tape_imbalance = ts["imbalance"]
        f.tape_big_imbalance = ts["big_trade_imbalance"]
        f.trades_per_min = ts["trades_per_min"]

    return f
