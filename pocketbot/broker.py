"""Feeds (where candles come from) and brokers (where trades go).

Pocket Option has no public trading API. Everything live goes through
BinaryOptionsToolsV2 (`pip install binaryoptionstoolsv2`), an open-source
client that speaks the same socket.io websocket the web terminal uses and
authenticates with the session string ("SSID") copied out of a logged-in
browser. That import is optional: paper mode on synthetic or recorded
candles needs nothing beyond this repository.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import AsyncIterator, Callable, Iterable, Protocol

from .market import Candle, synthetic_candles
from .stats import Trade
from .strategy import CALL

log = logging.getLogger("pocketbot.broker")


# --------------------------------------------------------------------- feeds

class Feed(Protocol):
    period: int

    def __aiter__(self) -> AsyncIterator[list[Candle]]: ...


class ListFeed:
    """Replays a fixed candle list, one closed candle at a time."""

    def __init__(self, candles: Iterable[Candle], period: int, window: int = 300,
                 delay: float = 0.0):
        self.candles = list(candles)
        self.period = period
        self.window = window
        self.delay = delay

    async def __aiter__(self):
        for i in range(1, len(self.candles) + 1):
            yield self.candles[max(0, i - self.window):i]
            if self.delay:
                await asyncio.sleep(self.delay)


class SyntheticFeed(ListFeed):
    """An endless-ish random walk for offline demos. Contains no edge."""

    def __init__(self, period: int = 60, n: int = 5000, seed: int | None = None,
                 delay: float = 0.2, window: int = 300):
        super().__init__(synthetic_candles(n, period, seed), period, window, delay)


class PocketOptionFeed:
    """Gap-free closed candles from the live websocket.

    Uses get_candles_live, which backfills history and then builds candles
    from the tick stream. It yields on every tick; we only pass a candle list
    on when a new candle has closed, so the strategy never sees a forming bar.
    """

    def __init__(self, client, asset: str, period: int, window: int = 300,
                 history_hours: float = 3.0):
        self.client = client
        self.asset = asset
        self.period = period
        self.window = window
        self.history_hours = history_hours

    async def __aiter__(self):
        last_seen = None
        gen = self.client.get_candles_live(self.asset, self.period,
                                           hours=self.history_hours, max_rows=self.window)
        async for closed, _forming in gen:
            if not closed:
                continue
            newest = closed[-1]["time"]
            if newest == last_seen:
                continue
            last_seen = newest
            yield [Candle.from_dict(c) for c in closed]


# ------------------------------------------------------------------- brokers

class Broker(Protocol):
    account: str

    async def balance(self) -> float: ...
    async def payout(self, asset: str) -> float | None: ...
    async def place(self, asset: str, direction: str, stake: float, duration: int,
                    price: float, now: float, reason: str = "") -> Trade: ...
    async def settle(self, bar: Candle, period: int) -> list[Trade]: ...


class PaperBroker:
    """Fills at the signal candle's close and settles at the expiry candle's close.

    That is slightly generous: on the real terminal the entry is the next tick
    after the order lands, a few hundred ms later. Draws refund the stake,
    as they do on Pocket Option.
    """

    account = "paper"

    def __init__(self, balance: float = 1000.0, payout: float | Callable = 0.85):
        self._balance = balance
        self._payout = payout
        self._open: list[Trade] = []
        self._ids = itertools.count(1)

    async def balance(self) -> float:
        return self._balance

    async def payout(self, asset: str) -> float | None:
        if callable(self._payout):
            p = self._payout(asset)
            return await p if asyncio.iscoroutine(p) else p
        return self._payout

    async def place(self, asset, direction, stake, duration, price, now, reason="") -> Trade:
        payout = await self.payout(asset) or 0.0
        t = Trade(id=f"paper-{next(self._ids)}", asset=asset, direction=direction,
                  stake=stake, payout=payout, opened_at=now, expires_at=now + duration,
                  entry=price, reason=reason, account=self.account)
        self._balance -= stake
        self._open.append(t)
        return t

    async def settle(self, bar: Candle, period: int) -> list[Trade]:
        close_time = bar.time + period
        done = [t for t in self._open if t.expires_at <= close_time]
        for t in done:
            self._open.remove(t)
            up = bar.close > t.entry
            down = bar.close < t.entry
            if not up and not down:
                t.settle("draw", 0.0, bar.close)
            elif up == (t.direction == CALL):
                t.settle("win", exit_price=bar.close)
            else:
                t.settle("loss", exit_price=bar.close)
            self._balance += t.stake + t.pnl
        return done


class PocketOptionBroker:
    """Real orders on the account the SSID belongs to (demo or real).

    Results come back from check_win asynchronously; settle() waits a few
    seconds for any trade that has already expired so the result lands on
    the right candle instead of one bar late.
    """

    def __init__(self, client, account: str):
        self.client = client
        self.account = account
        self._pending: dict[str, tuple[Trade, asyncio.Task]] = {}

    async def balance(self) -> float:
        return float(await self.client.balance())

    async def payout(self, asset: str) -> float | None:
        p = await self.client.payout(asset)
        return None if p is None else float(p) / 100.0

    async def place(self, asset, direction, stake, duration, price, now, reason="") -> Trade:
        payout = await self.payout(asset) or 0.0
        fn = self.client.buy if direction == CALL else self.client.sell
        trade_id, info = await fn(asset, stake, duration)
        entry = _num(info, "openPrice", "open_price", default=price)
        t = Trade(id=str(trade_id), asset=asset, direction=direction, stake=stake,
                  payout=payout, opened_at=now, expires_at=now + duration,
                  entry=entry, reason=reason, account=self.account)
        task = asyncio.create_task(self.client.check_win(t.id, timeout_seconds=duration + 30))
        self._pending[t.id] = (t, task)
        return t

    async def settle(self, bar: Candle, period: int) -> list[Trade]:
        close_time = bar.time + period
        due = [task for t, task in self._pending.values() if t.expires_at <= close_time]
        if due:
            await asyncio.wait(due, timeout=10)
        out = []
        for tid, (t, task) in list(self._pending.items()):
            if not task.done():
                continue
            del self._pending[tid]
            try:
                res = task.result()
                t.settle(res.get("result", "error"), float(res.get("profit", 0.0)),
                         _num(res, "closePrice", "close_price"))
            except Exception as exc:          # never let one bad result stop the bot
                log.error("could not read result of %s: %s", tid, exc)
                t.settle("error", 0.0)
            out.append(t)
        return out


def _num(d, *keys, default=None):
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except ValueError:
            return default
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            try:
                return float(d[k])
            except (TypeError, ValueError):
                pass
    return default


# -------------------------------------------------------------- connection

def ssid_is_demo(ssid: str) -> bool | None:
    """Read isDemo out of a `42["auth",{...}]` session string without connecting."""
    try:
        payload = json.loads(ssid.strip()[2:])
        return bool(int(payload[1]["isDemo"]))
    except (ValueError, KeyError, IndexError, TypeError):
        return None


async def connect(ssid: str, ws_url: str | None = None, timeout: float = 60.0):
    try:
        from BinaryOptionsToolsV2.pocketoption import PocketOptionAsync
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise SystemExit(
            "live data needs the websocket client: pip install binaryoptionstoolsv2"
        ) from exc
    client = PocketOptionAsync(ssid, url=ws_url)
    await client.wait_for_assets(timeout=timeout)
    return client

