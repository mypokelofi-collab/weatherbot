"""Coinbase Advanced Trade market data - the secondary real venue.

Used when Binance is unreachable (region blocks are common) or as a
cross-venue sanity check on price. The public websocket needs no API key for
the `level2` and `market_trades` channels.

Two venue quirks are handled here:
  * Coinbase sends a full L2 snapshot on subscribe and then absolute-quantity
    updates (not deltas), so `new_quantity == 0` deletes a level.
  * Continuity is tracked with the connection-wide `sequence_num`; a jump
    means we missed a message and must resubscribe to get a fresh snapshot.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

import httpx

try:  # websockets >= 13
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # pragma: no cover
    from websockets.client import connect as ws_connect  # type: ignore

from ..core.instrument import Instrument
from ..core.types import Candle, Side, Trade
from .feed import MarketFeed

log = logging.getLogger(__name__)

WS_URL = "wss://advanced-trade-ws.coinbase.com"
REST_URL = "https://api.exchange.coinbase.com"


def _parse_ts(value: str | None) -> int:
    if not value:
        return int(time.time() * 1000)
    try:
        return int(
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .timestamp()
            * 1000
        )
    except ValueError:
        return int(time.time() * 1000)


class CoinbaseFeed(MarketFeed):
    venue = "coinbase"
    real = True

    def __init__(
        self,
        symbol: str = "BTC-USD",
        depth_levels: int = 1000,
        aggressor_field: str = "taker",
    ) -> None:
        super().__init__(symbol=symbol, book_levels=depth_levels)
        self.health.venue = self.venue
        # Coinbase's `side` on market_trades has flipped meaning between the
        # legacy and Advanced Trade feeds. Default assumes it is the taker
        # (aggressor); set to "maker" if your CVD comes out inverted.
        self.aggressor_field = aggressor_field
        self.instrument = Instrument(
            symbol=symbol, base="BTC", quote="USD",
            tick_size=0.01, step_size=1e-8, min_qty=1e-6, min_notional=1.0,
        )
        self._seq: int | None = None
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=REST_URL, timeout=httpx.Timeout(10.0),
                headers={"User-Agent": "flowbot/0.1"},
            )
        return self._client

    async def load_instrument(self) -> Instrument:
        try:
            client = await self._http()
            r = await client.get(f"/products/{self.symbol}")
            r.raise_for_status()
            p = r.json()
            self.instrument = Instrument(
                symbol=p["id"],
                base=p.get("base_currency", "BTC"),
                quote=p.get("quote_currency", "USD"),
                tick_size=float(p.get("quote_increment", 0.01)),
                step_size=float(p.get("base_increment", 1e-8)),
                min_qty=float(p.get("base_min_size", 1e-6) or 1e-6),
                min_notional=float(p.get("min_market_funds", 1) or 1),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("coinbase product metadata unavailable (%s)", exc)
        return self.instrument

    async def backfill_candles(self, interval: str, limit: int = 300) -> list[Candle]:
        """Coinbase candles carry no aggressor split, so delta starts neutral."""
        gran = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "1d": 86400}
        seconds = gran.get(interval, 900)
        client = await self._http()
        r = await client.get(
            f"/products/{self.symbol}/candles", params={"granularity": seconds}
        )
        r.raise_for_status()
        rows = sorted(r.json(), key=lambda x: x[0])[-limit:]
        out: list[Candle] = []
        for t, low, high, op, close, vol in rows:
            out.append(
                Candle(
                    open_time=int(t) * 1000,
                    close_time=(int(t) + seconds) * 1000,
                    open=float(op), high=float(high), low=float(low), close=float(close),
                    volume=float(vol), quote_volume=float(vol) * float(close),
                    buy_volume=float(vol) / 2, sell_volume=float(vol) / 2,
                    closed=True,
                )
            )
        if out and out[-1].close_time > time.time() * 1000:
            out.pop()
        return out

    async def run(self) -> None:
        async with ws_connect(WS_URL, ping_interval=20, ping_timeout=20, max_queue=4096) as ws:
            for channel in ("level2", "market_trades", "heartbeats"):
                await ws.send(json.dumps({
                    "type": "subscribe",
                    "product_ids": [self.symbol],
                    "channel": channel,
                }))
            self.health.connected = True
            self.health.connected_since = int(time.time() * 1000)
            self.health.last_error = ""
            self._seq = None
            self.book.reset()
            self._emit_status("connected")

            async for raw in ws:
                if self._stopping:
                    return
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue

                seq = msg.get("sequence_num")
                if seq is not None:
                    if self._seq is not None and seq != self._seq + 1:
                        self.health.gaps += 1
                        self._emit_status("sequence_gap")
                        log.warning("coinbase sequence gap %s -> %s; resubscribing",
                                    self._seq, seq)
                        raise ConnectionError("sequence gap")   # supervisor reconnects
                    self._seq = seq

                channel = msg.get("channel")
                if channel == "l2_data":
                    self._handle_l2(msg)
                elif channel == "market_trades":
                    self._handle_trades(msg)

    def _handle_l2(self, msg: dict) -> None:
        ts = _parse_ts(msg.get("timestamp"))
        for ev in msg.get("events", []):
            updates = ev.get("updates", [])
            if ev.get("type") == "snapshot":
                bids = [
                    (float(u["price_level"]), float(u["new_quantity"]))
                    for u in updates if u["side"] in ("bid", "buy")
                ]
                asks = [
                    (float(u["price_level"]), float(u["new_quantity"]))
                    for u in updates if u["side"] in ("offer", "ask", "sell")
                ]
                self.book.apply_snapshot(bids, asks, seq=self._seq or 0, ts=ts)
            else:
                bids = [
                    (float(u["price_level"]), float(u["new_quantity"]))
                    for u in updates if u["side"] in ("bid", "buy")
                ]
                asks = [
                    (float(u["price_level"]), float(u["new_quantity"]))
                    for u in updates if u["side"] in ("offer", "ask", "sell")
                ]
                # Coinbase sends absolute sizes, so sequencing is enforced at the
                # connection level above rather than per-message.
                self.book.apply_diff(bids, asks, first_seq=0, final_seq=self._seq or 0, ts=ts)
        self._note_latency(ts)
        if self.book.ready:
            self._emit_book(self.book.snapshot(25))

    def _handle_trades(self, msg: dict) -> None:
        for ev in msg.get("events", []):
            if ev.get("type") == "snapshot":
                continue                      # backfill of recent prints, not live flow
            for t in ev.get("trades", []):
                raw_side = str(t.get("side", "BUY")).upper()
                side = Side.BUY if raw_side == "BUY" else Side.SELL
                if self.aggressor_field == "maker":
                    side = side.opposite
                trade = Trade(
                    ts=_parse_ts(t.get("time")),
                    price=float(t["price"]),
                    qty=float(t["size"]),
                    side=side,
                    trade_id=int(str(t.get("trade_id", "0")).lstrip("0") or 0),
                )
                self._emit_trade(trade)

    async def stop(self) -> None:
        await super().stop()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
