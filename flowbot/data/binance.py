"""Binance market data: real trades, real L2 book, real klines.

Spot and USDⓈ-M perpetual are both supported. The perp is the default for the
bot because the strategy takes short signals as readily as long ones, and the
spot book cannot express a short without borrowing.

Book synchronisation follows Binance's documented procedure exactly:
buffer the diff stream, fetch a REST snapshot, discard diffs the snapshot
already contains, verify the first applied diff straddles the snapshot id, and
from then on require unbroken sequence continuity. Any break drops the book to
not-ready and triggers a fresh snapshot - we would rather have a two-second
hole in the feed than fill an order against a book that silently drifted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

try:  # websockets >= 13
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # pragma: no cover - older websockets
    from websockets.client import connect as ws_connect  # type: ignore

from ..core.instrument import Instrument
from ..core.types import Candle, Side, Trade
from .feed import MarketFeed

log = logging.getLogger(__name__)

SPOT = {
    "rest": "https://api.binance.com",
    "ws": "wss://stream.binance.com:9443/stream",
    "depth": "/api/v3/depth",
    "klines": "/api/v3/klines",
    "info": "/api/v3/exchangeInfo",
}
FUTURES = {
    "rest": "https://fapi.binance.com",
    "ws": "wss://fstream.binance.com/stream",
    "depth": "/fapi/v1/depth",
    "klines": "/fapi/v1/klines",
    "info": "/fapi/v1/exchangeInfo",
}


class BinanceFeed(MarketFeed):
    venue = "binance"
    real = True

    def __init__(
        self,
        symbol: str = "BTCUSDT",
        market: str = "futures",
        depth_limit: int = 1000,
        depth_speed: str = "100ms",
        rest_base: str | None = None,
        ws_base: str | None = None,
    ) -> None:
        super().__init__(symbol=symbol, book_levels=depth_limit)
        self.market = market
        self.endpoints = dict(FUTURES if market == "futures" else SPOT)
        if rest_base:
            self.endpoints["rest"] = rest_base
        if ws_base:
            self.endpoints["ws"] = ws_base
        self.venue = f"binance-{market}"
        self.health.venue = self.venue
        self.depth_limit = min(depth_limit, 1000)
        self.depth_speed = depth_speed
        self.instrument = Instrument(
            symbol=symbol, contract="perp" if market == "futures" else "spot"
        )
        self._buffer: list[dict] = []
        self._syncing = False
        self._client: httpx.AsyncClient | None = None

    # -- REST --------------------------------------------------------------
    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.endpoints["rest"],
                timeout=httpx.Timeout(10.0),
                headers={"User-Agent": "flowbot/0.1"},
            )
        return self._client

    async def load_instrument(self) -> Instrument:
        """Read the venue's real tick/lot/notional filters for this symbol."""
        try:
            client = await self._http()
            r = await client.get(self.endpoints["info"], params={"symbol": self.symbol})
            r.raise_for_status()
            info = r.json()
            sym = info["symbols"][0]
            inst = Instrument(
                symbol=sym["symbol"],
                base=sym.get("baseAsset", "BTC"),
                quote=sym.get("quoteAsset", "USDT"),
                contract="perp" if self.market == "futures" else "spot",
            )
            for f in sym.get("filters", []):
                ftype = f.get("filterType")
                if ftype == "PRICE_FILTER":
                    inst.tick_size = float(f["tickSize"])
                elif ftype == "LOT_SIZE":
                    inst.step_size = float(f["stepSize"])
                    inst.min_qty = float(f["minQty"])
                elif ftype in ("MIN_NOTIONAL", "NOTIONAL"):
                    inst.min_notional = float(f.get("minNotional") or f.get("notional") or 5)
            self.instrument = inst
            log.info("instrument loaded: %s", inst.to_dict())
        except Exception as exc:  # noqa: BLE001 - offline boot keeps defaults
            log.warning("exchangeInfo unavailable (%s); using default filters", exc)
        return self.instrument

    async def backfill_candles(self, interval: str, limit: int = 500) -> list[Candle]:
        """Closed historical bars, including the taker-buy split we need."""
        client = await self._http()
        r = await client.get(
            self.endpoints["klines"],
            params={"symbol": self.symbol, "interval": interval, "limit": min(limit, 1500)},
        )
        r.raise_for_status()
        out: list[Candle] = []
        for k in r.json():
            volume = float(k[5])
            taker_buy = float(k[9])
            out.append(
                Candle(
                    open_time=int(k[0]),
                    close_time=int(k[6]) + 1,
                    open=float(k[1]),
                    high=float(k[2]),
                    low=float(k[3]),
                    close=float(k[4]),
                    volume=volume,
                    quote_volume=float(k[7]),
                    trades=int(k[8]),
                    buy_volume=taker_buy,
                    sell_volume=max(0.0, volume - taker_buy),
                    closed=True,
                )
            )
        # The last kline is the bar still in progress; the aggregator owns it.
        if out and out[-1].close_time > time.time() * 1000:
            out.pop()
        return out

    async def _fetch_depth_snapshot(self) -> dict:
        client = await self._http()
        r = await client.get(
            self.endpoints["depth"],
            params={"symbol": self.symbol, "limit": self.depth_limit},
        )
        r.raise_for_status()
        return r.json()

    # -- websocket ---------------------------------------------------------
    @property
    def stream_url(self) -> str:
        s = self.symbol.lower()
        streams = f"{s}@aggTrade/{s}@depth@{self.depth_speed}"
        return f"{self.endpoints['ws']}?streams={streams}"

    async def run(self) -> None:
        url = self.stream_url
        log.info("connecting %s", url)
        async with ws_connect(url, ping_interval=20, ping_timeout=20, max_queue=4096) as ws:
            self.health.connected = True
            self.health.connected_since = int(time.time() * 1000)
            self.health.last_error = ""
            self._emit_status("connected")
            self.book.reset()
            self._buffer.clear()
            asyncio.create_task(self._resync_book())

            async for raw in ws:
                if self._stopping:
                    return
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                data = msg.get("data", msg)
                etype = data.get("e")
                if etype == "aggTrade":
                    self._handle_trade(data)
                elif etype == "depthUpdate":
                    self._handle_depth(data)

    def _handle_trade(self, d: dict) -> None:
        # m = true means the buyer was the maker, i.e. a seller hit the bid.
        side = Side.SELL if d.get("m") else Side.BUY
        trade = Trade(
            ts=int(d.get("T") or d.get("E") or time.time() * 1000),
            price=float(d["p"]),
            qty=float(d["q"]),
            side=side,
            trade_id=int(d.get("a") or 0),
        )
        self._note_latency(int(d.get("E") or 0))
        self._emit_trade(trade)

    def _handle_depth(self, d: dict) -> None:
        if not self.book.ready:
            self._buffer.append(d)
            if len(self._buffer) > 5000:          # snapshot is clearly stuck
                self._buffer = self._buffer[-2000:]
            if not self._syncing:
                asyncio.create_task(self._resync_book())
            return

        if not self._apply_depth(d):
            self.health.gaps += 1
            self.health.resyncs += 1
            self._emit_status("book_gap")
            log.warning("depth sequence gap; resyncing book")
            self._buffer.append(d)
            asyncio.create_task(self._resync_book())
            return

        self._note_latency(int(d.get("E") or 0))
        self._emit_book(self.book.snapshot(25))

    def _apply_depth(self, d: dict) -> bool:
        first = int(d.get("U") or 0)
        final = int(d.get("u") or 0)
        if self.market == "futures" and d.get("pu") is not None:
            # Futures guarantees pu == previous u, which is a tighter check
            # than U alone (futures diffs may overlap).
            first = int(d["pu"]) + 1
        return self.book.apply_diff(
            bids=[(float(p), float(q)) for p, q in d.get("b", [])],
            asks=[(float(p), float(q)) for p, q in d.get("a", [])],
            first_seq=first,
            final_seq=final,
            ts=int(d.get("E") or time.time() * 1000),
        )

    async def _resync_book(self) -> None:
        """Snapshot + replay buffered diffs, per the venue's documented recipe."""
        if self._syncing:
            return
        self._syncing = True
        try:
            for attempt in range(5):
                try:
                    snap = await self._fetch_depth_snapshot()
                except Exception as exc:  # noqa: BLE001
                    log.warning("depth snapshot failed (%s), retry %d", exc, attempt + 1)
                    await asyncio.sleep(1 + attempt)
                    continue

                last_id = int(snap["lastUpdateId"])
                self.book.apply_snapshot(
                    bids=[(float(p), float(q)) for p, q in snap["bids"]],
                    asks=[(float(p), float(q)) for p, q in snap["asks"]],
                    seq=last_id,
                    ts=int(time.time() * 1000),
                )
                self.health.resyncs += 1

                pending, self._buffer = self._buffer, []
                applied = 0
                for d in pending:
                    if int(d.get("u") or 0) <= last_id:
                        continue                      # snapshot already has it
                    if not self._apply_depth(d):
                        break                         # gap inside the buffer
                    applied += 1
                if self.book.ready:
                    log.info(
                        "book synced @%s (%d buffered diffs applied)", last_id, applied
                    )
                    self._emit_book(self.book.snapshot(25))
                    self._emit_status("book_synced")
                    return
            self._emit_status("book_sync_failed")
        finally:
            self._syncing = False

    async def stop(self) -> None:
        await super().stop()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
