"""Record the real feed to disk so it can be replayed bit-for-bit.

This is what makes the backtest honest: rather than backtesting on klines and
guessing at fills, we replay the actual prints and the actual book depth that
existed at the time, through the same fill engine the live bot uses.

Fidelity note: we persist throttled top-N book snapshots rather than every
raw diff. A 25-level snapshot every 250ms reproduces the depth any order we
send could realistically consume, at roughly a tenth of the disk of full diff
capture. Raise `levels`/lower `throttle_ms` if you plan to simulate size that
walks deeper than the top of book.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
from pathlib import Path
from typing import Any, TextIO

from ..core.types import BookSnapshot, Trade
from .feed import MarketFeed

log = logging.getLogger(__name__)


class Recorder:
    def __init__(
        self,
        path: str | Path,
        levels: int = 25,
        throttle_ms: int = 250,
        compress: bool = False,
    ) -> None:
        self.path = Path(path)
        self.levels = levels
        self.throttle_ms = throttle_ms
        self.compress = compress or str(path).endswith(".gz")
        self._fh: TextIO | None = None
        self._last_book_ts = 0
        self.trades_written = 0
        self.books_written = 0

    def open(self, meta: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = (
            gzip.open(self.path, "at", encoding="utf-8")
            if self.compress
            else open(self.path, "a", encoding="utf-8")
        )
        self._write({"k": "meta", **meta})

    def close(self) -> None:
        if self._fh:
            self._fh.flush()
            self._fh.close()
            self._fh = None

    def attach(self, feed: MarketFeed) -> None:
        feed.on_trade(self.record_trade)
        feed.on_book(self.record_book)

    def _write(self, obj: dict) -> None:
        if not self._fh:
            return
        self._fh.write(json.dumps(obj, separators=(",", ":")) + "\n")

    def record_trade(self, t: Trade) -> None:
        self._write({"k": "t", "ts": t.ts, "p": t.price, "q": t.qty,
                     "s": t.side.value, "i": t.trade_id})
        self.trades_written += 1
        if self.trades_written % 500 == 0 and self._fh:
            self._fh.flush()

    def record_book(self, b: BookSnapshot) -> None:
        if b.ts - self._last_book_ts < self.throttle_ms:
            return
        self._last_book_ts = b.ts
        self._write({
            "k": "s",
            "ts": b.ts,
            "seq": b.seq,
            "b": [[lv.price, lv.qty] for lv in b.bids[: self.levels]],
            "a": [[lv.price, lv.qty] for lv in b.asks[: self.levels]],
        })
        self.books_written += 1

    def stats(self) -> dict:
        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "path": str(self.path),
            "trades": self.trades_written,
            "books": self.books_written,
            "bytes": size,
            "mb": round(size / 1e6, 2),
        }


def open_recording(path: str | Path):
    """Open a recording for reading, transparently handling gzip."""
    p = Path(path)
    if str(p).endswith(".gz"):
        return gzip.open(p, "rt", encoding="utf-8")
    return open(p, "r", encoding="utf-8")


def recording_info(path: str | Path) -> dict:
    """Cheap header scan: venue, symbol, time span, counts."""
    first_ts = last_ts = 0
    trades = books = 0
    meta: dict = {}
    book_ts: list[int] = []
    with open_recording(path) as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            kind = obj.get("k")
            if kind == "meta":
                meta = obj
                continue
            ts = int(obj.get("ts", 0))
            first_ts = first_ts or ts
            last_ts = ts
            if kind == "t":
                trades += 1
            elif kind == "s":
                books += 1
                book_ts.append(ts)
    gaps = sorted(b - a for a, b in zip(book_ts, book_ts[1:])) if len(book_ts) > 1 else []
    return {
        "path": str(path),
        "venue": meta.get("venue", "?"),
        "symbol": meta.get("symbol", "?"),
        "instrument": meta.get("instrument"),
        "start": first_ts,
        "end": last_ts,
        "hours": round((last_ts - first_ts) / 3_600_000, 2) if last_ts else 0,
        "trades": trades,
        "books": books,
        # Book cadence matters: fills are only as honest as how often the
        # depth behind them was captured.
        "book_gap_ms_p50": gaps[len(gaps) // 2] if gaps else 0,
        "book_gap_ms_p95": gaps[int(len(gaps) * 0.95)] if gaps else 0,
        "size_mb": round(os.path.getsize(path) / 1e6, 2),
    }
