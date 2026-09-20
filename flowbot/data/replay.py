"""Replay a recorded real feed.

Two modes:
  * `speed=0`  - as fast as the CPU allows, for backtests.
  * `speed=N`  - wall-clock paced at N times real speed, for watching a past
                 session play out on the live dashboard.

Either way the events, timestamps and book depth are the real ones that were
captured, so the fill engine sees exactly the liquidity that existed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from ..core.instrument import Instrument
from ..core.types import Side, Trade
from .feed import MarketFeed
from .recorder import open_recording

log = logging.getLogger(__name__)


class ReplayFeed(MarketFeed):
    venue = "replay"
    real = True          # the data is real; only the clock is synthetic
    realtime = False

    def __init__(
        self,
        path: str | Path,
        speed: float = 0.0,
        symbol: str = "BTCUSDT",
        loop: bool = False,
    ) -> None:
        super().__init__(symbol=symbol)
        self.path = Path(path)
        self.speed = speed
        self.loop = loop
        self.venue = f"replay:{self.path.name}"
        self.health.venue = self.venue
        self.instrument = Instrument(symbol=symbol)
        self.clock_ts = 0
        self.finished = False
        self._on_finish: list = []

    def on_finish(self, fn) -> None:
        self._on_finish.append(fn)

    async def load_instrument(self) -> Instrument:
        """Read the venue's trading rules from the recording header.

        Without this the bot would round prices and sizes with defaults rather
        than the grid the captured venue actually used.
        """
        try:
            with open_recording(self.path) as fh:
                for line in fh:
                    obj = json.loads(line)
                    if obj.get("k") != "meta":
                        break
                    self.symbol = obj.get("symbol", self.symbol)
                    inst = obj.get("instrument")
                    if inst:
                        self.instrument = Instrument(**inst)
                    break
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read recording header (%s)", exc)
        return self.instrument

    async def run(self) -> None:
        while True:
            await self._play_once()
            if not self.loop or self._stopping:
                break
        self.finished = True
        for fn in self._on_finish:
            fn()
        self._emit_status("replay_finished")
        # Hold the task open so the supervisor does not treat completion as a
        # dropped connection and reconnect in a loop.
        while not self._stopping:
            await asyncio.sleep(0.5)

    async def _play_once(self) -> None:
        self.health.connected = True
        self.health.connected_since = int(time.time() * 1000)
        self._emit_status("replaying")
        start_wall = time.time()
        first_ts = 0
        count = 0

        with open_recording(self.path) as fh:
            for line in fh:
                if self._stopping:
                    return
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                kind = obj.get("k")
                if kind == "meta":
                    inst = obj.get("instrument")
                    if inst:
                        self.instrument = Instrument(**inst)
                    self.symbol = obj.get("symbol", self.symbol)
                    continue

                ts = int(obj.get("ts", 0))
                self.clock_ts = ts
                first_ts = first_ts or ts

                if self.speed > 0:
                    target = (ts - first_ts) / 1000.0 / self.speed
                    drift = target - (time.time() - start_wall)
                    if drift > 0:
                        await asyncio.sleep(min(drift, 5.0))
                elif count % 2000 == 0:
                    await asyncio.sleep(0)        # keep the loop responsive

                if kind == "t":
                    self._emit_trade(Trade(
                        ts=ts, price=float(obj["p"]), qty=float(obj["q"]),
                        side=Side(obj["s"]), trade_id=int(obj.get("i", 0)),
                    ))
                elif kind == "s":
                    self.book.apply_snapshot(
                        bids=[(float(p), float(q)) for p, q in obj["b"]],
                        asks=[(float(p), float(q)) for p, q in obj["a"]],
                        seq=int(obj.get("seq", 0)),
                        ts=ts,
                    )
                    self._emit_book(self.book.snapshot(25))
                count += 1

    async def play_sync(self, on_event=None) -> None:
        """Synchronous-ish drain used by the backtester (no wall-clock pacing)."""
        self.speed = 0.0
        await self._play_once()
        if on_event:
            on_event()
