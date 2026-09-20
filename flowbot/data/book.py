"""Live L2 order book maintenance.

The book is the thing the fill engine charges against, so correctness here is
what makes the paper fills honest. Two rules drive the implementation:

1. Never serve a book we know is stale. Every venue diff stream carries
   sequence numbers; a gap means we dropped a message and the local book is
   now fiction. We flag it and the caller resyncs from a REST snapshot.
2. Keep the depth we actually use. A market order for 0.5 BTC rarely walks
   past the first few levels, but a thin book at 3am is exactly when it does,
   so we keep the full venue depth rather than just the top 5.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.types import BookLevel, BookSnapshot


@dataclass
class BookStats:
    updates: int = 0
    resyncs: int = 0
    gaps: int = 0
    last_update_ts: int = 0
    last_seq: int = 0


class OrderBook:
    """A local replica of the venue's L2 book, driven by snapshot + diffs."""

    def __init__(self, max_levels: int = 1000) -> None:
        self.max_levels = max_levels
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.seq: int = 0
        self.ts: int = 0
        self.ready: bool = False
        self.stats = BookStats()

    # -- lifecycle ---------------------------------------------------------
    def reset(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.seq = 0
        self.ready = False

    def apply_snapshot(
        self,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
        seq: int,
        ts: int,
    ) -> None:
        self.bids = {float(p): float(q) for p, q in bids if float(q) > 0}
        self.asks = {float(p): float(q) for p, q in asks if float(q) > 0}
        self.seq = seq
        self.ts = ts
        self.ready = True
        self.stats.resyncs += 1
        self.stats.last_seq = seq
        self.stats.last_update_ts = ts
        self._trim()

    def apply_diff(
        self,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
        first_seq: int,
        final_seq: int,
        ts: int,
    ) -> bool:
        """Apply a depth delta. Returns False when a sequence gap is detected.

        A False return means the local book is unreliable and the caller must
        re-fetch a REST snapshot before trusting any price from it.
        """
        if not self.ready:
            return False

        # Already-seen update: harmless, ignore it.
        if final_seq and final_seq <= self.seq:
            return True

        # A gap: the venue's next expected sequence is seq + 1, and this
        # message starts beyond it, so at least one update was lost.
        if first_seq and first_seq > self.seq + 1:
            self.stats.gaps += 1
            self.ready = False
            return False

        for price, qty in bids:
            p, q = float(price), float(qty)
            if q <= 0:
                self.bids.pop(p, None)
            else:
                self.bids[p] = q
        for price, qty in asks:
            p, q = float(price), float(qty)
            if q <= 0:
                self.asks.pop(p, None)
            else:
                self.asks[p] = q

        self.seq = final_seq or self.seq
        self.ts = ts
        self.stats.updates += 1
        self.stats.last_seq = self.seq
        self.stats.last_update_ts = ts
        self._trim()
        return True

    def _trim(self) -> None:
        """Bound memory: venues stream levels far outside anything we trade."""
        if len(self.bids) > self.max_levels * 2:
            keep = sorted(self.bids.items(), key=lambda kv: -kv[0])[: self.max_levels]
            self.bids = dict(keep)
        if len(self.asks) > self.max_levels * 2:
            keep = sorted(self.asks.items(), key=lambda kv: kv[0])[: self.max_levels]
            self.asks = dict(keep)

    # -- reads -------------------------------------------------------------
    def snapshot(self, levels: int = 50) -> BookSnapshot:
        bids = [
            BookLevel(p, q)
            for p, q in sorted(self.bids.items(), key=lambda kv: -kv[0])[:levels]
        ]
        asks = [
            BookLevel(p, q)
            for p, q in sorted(self.asks.items(), key=lambda kv: kv[0])[:levels]
        ]
        return BookSnapshot(ts=self.ts, bids=bids, asks=asks, seq=self.seq)

    @property
    def best_bid(self) -> float:
        return max(self.bids) if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return min(self.asks) if self.asks else 0.0

    @property
    def mid(self) -> float:
        bb, ba = self.best_bid, self.best_ask
        if bb and ba:
            return (bb + ba) / 2
        return bb or ba

    def is_crossed(self) -> bool:
        bb, ba = self.best_bid, self.best_ask
        return bool(bb and ba and bb >= ba)

    def size_at(self, side: str, price: float) -> float:
        book = self.bids if side == "bid" else self.asks
        return book.get(price, 0.0)

    def walk(self, side: str, qty: float) -> tuple[float, float, int]:
        """Simulate eating `qty` from one side of the real book.

        Returns (filled_qty, notional, levels_consumed). `side` is the side of
        the book being consumed: a buy order eats the asks.
        """
        levels = (
            sorted(self.asks.items(), key=lambda kv: kv[0])
            if side == "ask"
            else sorted(self.bids.items(), key=lambda kv: -kv[0])
        )
        remaining = qty
        notional = 0.0
        used = 0
        for price, size in levels:
            if remaining <= 1e-12:
                break
            take = min(remaining, size)
            notional += take * price
            remaining -= take
            used += 1
        return (qty - remaining, notional, used)
