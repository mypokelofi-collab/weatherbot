"""Trade tape -> OHLCV bars.

We build our own bars from the raw prints instead of consuming the venue's
kline stream for three reasons: the buy/sell aggressor split (our flow signal)
is not in the kline payload, bars stay consistent when we replay a recording,
and we control exactly when a bar is considered closed.
"""

from __future__ import annotations

from typing import Callable, Iterable

from ..core.clock import bar_open
from ..core.types import Candle, Side, Trade


class CandleAggregator:
    """Aggregates trades into fixed-interval candles.

    `on_close` fires once per completed bar, in order. A bar closes when the
    first trade of the next bucket arrives, or when `flush_until` is called by
    the clock (so a quiet market still closes its bars on time).
    """

    def __init__(self, step_ms: int, max_history: int = 2000) -> None:
        self.step_ms = step_ms
        self.max_history = max_history
        self.history: list[Candle] = []
        self.current: Candle | None = None
        self._listeners: list[Callable[[Candle], None]] = []

    def on_close(self, fn: Callable[[Candle], None]) -> None:
        self._listeners.append(fn)

    # -- ingest ------------------------------------------------------------
    def seed(self, candles: Iterable[Candle]) -> None:
        """Backfill closed history (REST klines) before the live feed starts."""
        for c in candles:
            c.closed = True
            self.history.append(c)
        self._trim()

    def bootstrap_current(self, ts: int, price: float) -> None:
        """Open the live bar from a non-trade price (the book mid) so a bar
        exists to close on schedule even if the trade tape is late or briefly
        dead. `add_trade` only ever creates `current` itself, so a feed with
        working depth but a stalled trade stream would otherwise leave the
        bot with no bar in progress, and `flush_until`'s clock-driven close
        never gets a bar to close in the first place. A no-op once a bar is
        already open; the first real print still claims its own open/high/low
        exactly as it would for any other flat-continuation bar.
        """
        if self.current is not None:
            return
        self.current = self._new_bar(bar_open(ts, self.step_ms), price)

    def add_trade(self, trade: Trade) -> Candle | None:
        """Feed one real print. Returns the bar that just closed, if any.

        A gap in the tape (thin venue, feed hiccup, replay of a quiet night)
        still advances the grid: every bucket between the last bar and this
        print is closed as a flat continuation bar, so indicator windows stay
        aligned to real time instead of silently stretching.
        """
        open_time = bar_open(trade.ts, self.step_ms)
        closed_bars: list[Candle] = []

        if self.current is not None:
            if open_time < self.current.open_time:
                return None            # late print from a reconnect; bar is gone
            if open_time > self.current.open_time:
                closed_bars = self.flush_until(trade.ts)
        if self.current is None:
            self.current = self._new_bar(open_time, trade.price)

        c = self.current
        if c.trades == 0:
            # First print of the bar defines the open, even if the bar was
            # created as a flat continuation.
            c.open = c.high = c.low = trade.price
        c.high = max(c.high, trade.price)
        c.low = min(c.low, trade.price)
        c.close = trade.price
        c.volume += trade.qty
        c.quote_volume += trade.qty * trade.price
        c.trades += 1
        if trade.side is Side.BUY:
            c.buy_volume += trade.qty
        else:
            c.sell_volume += trade.qty
        return closed_bars[-1] if closed_bars else None

    def flush_until(self, ts: int) -> list[Candle]:
        """Close any bar whose window has elapsed, even with no trades in it.

        A market that goes quiet still has to advance the bar clock, otherwise
        every indicator window silently stretches in wall-clock terms. Empty
        bars continue at the previous close with zero volume.
        """
        closed: list[Candle] = []
        while self.current is not None and ts >= self.current.close_time:
            next_open = self.current.close_time
            bar = self._close_current()
            if bar is None:
                break
            closed.append(bar)
            self.current = self._new_bar(next_open, bar.close)
        return closed

    # -- internals ---------------------------------------------------------
    def _new_bar(self, open_time: int, price: float) -> Candle:
        return Candle(
            open_time=open_time,
            close_time=open_time + self.step_ms,
            open=price,
            high=price,
            low=price,
            close=price,
        )

    def _close_current(self) -> Candle | None:
        bar = self.current
        if bar is None:
            return None
        bar.closed = True
        self.history.append(bar)
        self._trim()
        self.current = None
        for fn in self._listeners:
            fn(bar)
        return bar

    def _trim(self) -> None:
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history :]

    # -- reads -------------------------------------------------------------
    @property
    def closes(self) -> list[float]:
        return [c.close for c in self.history]

    def recent(self, n: int) -> list[Candle]:
        return self.history[-n:]

    def open_at(self, ts: int) -> float | None:
        """The open price of the bar whose bucket starts at `ts`, if we have
        one - live if it is the bar in progress, from history otherwise.

        This is what makes flowbot's own feed a usable reference series for
        anything that settles against "the price when this window began" -
        Polymarket's short-duration BTC windows are UTC-aligned to the same
        grid as our own 15m/5m bars, so this needs no separate price fetch.
        """
        bucket = bar_open(ts, self.step_ms)
        if self.current is not None and self.current.open_time == bucket:
            return self.current.open
        for c in reversed(self.history):
            if c.open_time == bucket:
                return c.open
            if c.open_time < bucket:
                break
        return None

    def series(self, n: int, include_open: bool = True) -> list[Candle]:
        out = self.history[-n:]
        if include_open and self.current is not None:
            out = out + [self.current]
        return out
