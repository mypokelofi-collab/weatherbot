"""Market feed abstraction.

Every feed - live venue, recorded replay, or the offline simulator - exposes
the same surface: a maintained L2 order book, a stream of prints, and a health
record. The rest of the system never learns which one it is talking to, which
is what lets the backtester and the live bot run identical code.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from ..core.types import BookSnapshot, Candle, Trade
from .book import OrderBook

log = logging.getLogger(__name__)

TradeHandler = Callable[[Trade], None]
BookHandler = Callable[[BookSnapshot], None]
StatusHandler = Callable[[dict], None]


@dataclass
class FeedHealth:
    connected: bool = False
    venue: str = ""
    symbol: str = ""
    real: bool = True
    connected_since: int = 0
    reconnects: int = 0
    resyncs: int = 0
    gaps: int = 0
    trades_seen: int = 0
    book_updates: int = 0
    last_trade_ts: int = 0
    last_book_ts: int = 0
    latency_ms: float = 0.0        # venue event time -> local receive time
    last_error: str = ""

    def to_dict(self) -> dict:
        return {
            "connected": self.connected,
            "venue": self.venue,
            "symbol": self.symbol,
            "real": self.real,
            "connected_since": self.connected_since,
            "reconnects": self.reconnects,
            "resyncs": self.resyncs,
            "gaps": self.gaps,
            "trades_seen": self.trades_seen,
            "book_updates": self.book_updates,
            "last_trade_ts": self.last_trade_ts,
            "last_book_ts": self.last_book_ts,
            "latency_ms": round(self.latency_ms, 1),
            "last_error": self.last_error,
        }


class MarketFeed:
    """Base class. Subclasses implement `run()` and push into `_emit_*`."""

    venue: str = "base"
    real: bool = True
    # `realtime` says whether the host's wall clock is a valid clock for this
    # feed. It is for a live venue (a quiet tape still has to close bars on
    # time); it is not for a replay or the simulator, which carry their own
    # clock and would otherwise be fast-forwarded into the future.
    realtime: bool = True

    def __init__(self, symbol: str, book_levels: int = 1000) -> None:
        self.symbol = symbol
        self.book = OrderBook(max_levels=book_levels)
        self.health = FeedHealth(venue=self.venue, symbol=symbol, real=self.real)
        self._trade_handlers: list[TradeHandler] = []
        self._book_handlers: list[BookHandler] = []
        self._status_handlers: list[StatusHandler] = []
        self._task: asyncio.Task | None = None
        self._stopping = False
        self.last_price: float = 0.0
        # Exponentially smoothed latency so one slow packet does not flap the UI.
        self._lat_alpha = 0.1

    # -- subscription ------------------------------------------------------
    def on_trade(self, fn: TradeHandler) -> None:
        self._trade_handlers.append(fn)

    def on_book(self, fn: BookHandler) -> None:
        self._book_handlers.append(fn)

    def on_status(self, fn: StatusHandler) -> None:
        self._status_handlers.append(fn)

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._supervise(), name=f"feed-{self.venue}")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self.health.connected = False
        self._emit_status("stopped")

    async def _supervise(self) -> None:
        """Run the feed, reconnecting with capped exponential backoff."""
        backoff = 1.0
        while not self._stopping:
            try:
                await self.run()
                if self._stopping:
                    return
                raise ConnectionError("feed ended")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.health.connected = False
                self.health.last_error = f"{type(exc).__name__}: {exc}"
                self.health.reconnects += 1
                self._emit_status("reconnecting")
                log.warning("%s feed error (%s); retrying in %.1fs",
                            self.venue, self.health.last_error, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            else:
                backoff = 1.0

    async def run(self) -> None:  # pragma: no cover - implemented by subclasses
        raise NotImplementedError

    async def backfill_candles(self, interval: str, limit: int) -> list[Candle]:
        """Historical closed bars so indicators are warm before the first tick."""
        return []

    # -- emit --------------------------------------------------------------
    def _emit_trade(self, trade: Trade) -> None:
        self.last_price = trade.price
        self.health.trades_seen += 1
        self.health.last_trade_ts = trade.ts
        for fn in self._trade_handlers:
            fn(trade)

    def _emit_book(self, snap: BookSnapshot) -> None:
        self.health.book_updates += 1
        self.health.last_book_ts = snap.ts
        for fn in self._book_handlers:
            fn(snap)

    def _emit_status(self, state: str, **extra) -> None:
        payload = {"state": state, **self.health.to_dict(), **extra}
        for fn in self._status_handlers:
            fn(payload)

    def _note_latency(self, event_ts: int) -> None:
        if not event_ts:
            return
        sample = (time.time() * 1000) - event_ts
        # Clamp: a venue clock skew of minutes is not network latency.
        if -5000 < sample < 60_000:
            self.health.latency_ms = (
                sample if self.health.latency_ms == 0
                else (1 - self._lat_alpha) * self.health.latency_ms + self._lat_alpha * sample
            )

    def snapshot(self, levels: int = 20) -> BookSnapshot:
        return self.book.snapshot(levels)
