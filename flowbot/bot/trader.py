"""The trading bot: wires market data, signal, risk and execution together.

One position at a time, one decision per closed 15m bar, and continuous
management in between. The loop in one paragraph:

Every print updates the tape, the bar in progress and the mark, and gives the
position manager a chance to hit a stop or bank a partial. Every closed bar
runs the signal engine: flat and the score clears the entry threshold with no
gate blocking, the risk manager sizes a position from the ATR stop and the
real book, and the broker works the order. In a position and the score decays
or flips, the position is closed and the bot goes back to waiting - which is
the behaviour asked for: go with the flow, take the profit, wait for the next
signal.

The trader owns no market model and no fill logic. It is the state machine
that keeps those two honest about each other.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..core.bus import EventBus, RingBuffer
from ..core.clock import bar_open, interval_ms, ms_to_bar_close
from ..core.config import AppConfig
from ..core.types import (
    BookSnapshot,
    Candle,
    Fill,
    Order,
    Side,
    Signal,
    SignalAction,
    Trade,
)
from ..data.candles import CandleAggregator
from ..data.feed import MarketFeed
from ..execution.broker import Intent, PaperBroker
from ..execution.microstructure import book_pressure, walk_book
from ..signals.engine import SignalEngine
from ..signals.features import TapeWindow
from .portfolio import Portfolio
from .positions import PositionManager
from .risk import RiskManager
from .stats import full_stats
from .store import Store

log = logging.getLogger(__name__)


class Trader:
    def __init__(
        self,
        cfg: AppConfig,
        feed: MarketFeed,
        bus: EventBus | None = None,
        store: Store | None = None,
    ) -> None:
        self.cfg = cfg
        self.feed = feed
        self.bus = bus or EventBus()
        self.store = store
        self.step_ms = interval_ms(cfg.data.interval)

        self.instrument = getattr(feed, "instrument", None)
        if self.instrument is None:  # pragma: no cover - defensive
            from ..core.instrument import Instrument

            self.instrument = Instrument(symbol=cfg.data.symbol)

        self.aggregator = CandleAggregator(self.step_ms)
        self.tape = TapeWindow(window_ms=300_000)
        self.engine = SignalEngine(cfg.signal)
        self.broker = PaperBroker(cfg.execution, self.instrument)
        self.portfolio = Portfolio(cfg.risk.start_equity)
        self.risk = RiskManager(cfg.risk, cfg.execution, self.instrument)
        self.positions = PositionManager(cfg.risk)

        # runtime state
        self.running = False
        self.trading_enabled = True
        self.started_at = 0
        self.bars_seen = 0
        self.bars_in_market = 0
        self.last_signal: Signal | None = None
        self.last_book: BookSnapshot | None = None
        self.last_trade_price = 0.0
        self.events = RingBuffer(200)
        self.tape_ring = RingBuffer(cfg.server.tape_size)
        self.cvd_series = RingBuffer(1500)
        self.markers: list[dict] = []          # entries/exits for the chart
        self._entry_ctx: dict[str, Any] | None = None
        self._exit_reason_by_order: dict[str, str] = {}
        self._inflight_exit_qty = 0.0
        self._exit_is_flip = False
        self._last_equity_store = 0
        self._loop_task: asyncio.Task | None = None
        self.polymarket = None          # optional PolymarketPipeline

        self._wire()

    # -- wiring ------------------------------------------------------------
    def _wire(self) -> None:
        self.feed.on_trade(self._on_trade)
        self.feed.on_book(self._on_book)
        self.feed.on_status(self._on_feed_status)
        self.aggregator.on_close(self._on_bar_close)
        self.broker.on_fill(self._on_fill)
        self.broker.on_order(self._on_order)
        self.broker.on_intent(self._on_intent)
        self.portfolio.on_trade_closed(self._on_trade_closed)

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        if self.running:
            return
        self.started_at = int(time.time() * 1000)
        loader = getattr(self.feed, "load_instrument", None)
        if loader:
            try:
                self.instrument = await loader()
                self.broker.instrument = self.instrument
                self.broker.engine.instrument = self.instrument
                self.risk.instrument = self.instrument
            except Exception as exc:  # noqa: BLE001
                log.warning("instrument load failed: %s", exc)

        await self._backfill()

        if self.store:
            self.store.start_session({
                "started_at": self.started_at,
                "venue": self.feed.venue,
                "symbol": self.cfg.data.symbol,
                "interval": self.cfg.data.interval,
                "mode": self.cfg.mode,
                "start_equity": self.cfg.risk.start_equity,
                "real_data": self.feed.real,
                "config": self.cfg.to_dict(),
            })

        await self.feed.start()
        if self.polymarket is not None:
            await self.polymarket.start()
        self.running = True
        self._loop_task = asyncio.create_task(self._housekeeping(), name="trader-loop")
        self._event("start", f"bot started on {self.feed.venue} {self.cfg.data.symbol} "
                             f"{self.cfg.data.interval} ({'REAL' if self.feed.real else 'SIMULATED'} data)")

    async def stop(self) -> None:
        self.running = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._loop_task = None
        if self.polymarket is not None:
            await self.polymarket.stop()
        await self.feed.stop()
        self._event("stop", "bot stopped")
        if self.store:
            self.store.close()

    async def _backfill(self) -> None:
        try:
            bars = await self.feed.backfill_candles(
                self.cfg.data.interval, self.cfg.data.backfill_bars
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("backfill failed (%s); starting cold", exc)
            bars = []
        if bars:
            self.aggregator.seed(bars)
            self.bars_seen = len(bars)
            log.info("seeded %d closed %s bars (%.2f -> %.2f)", len(bars),
                     self.cfg.data.interval, bars[0].close, bars[-1].close)
            self.portfolio.set_mark(bars[-1].close, bars[-1].close_time)
            self._event("backfill", f"loaded {len(bars)} historical bars")

    @property
    def venue_now(self) -> int:
        """The venue's clock, not ours.

        For a live feed this tracks wall time within the feed latency; for a
        replay or the simulator it is the only clock that means anything, so
        bar closes and order timeouts follow the data rather than the host.
        """
        return max(
            self.broker.now,
            self.feed.health.last_trade_ts or 0,
            self.feed.health.last_book_ts or 0,
        )

    async def _housekeeping(self) -> None:
        """Runs once a second: closes bars in a quiet market, expires orders."""
        while self.running:
            try:
                wall = int(time.time() * 1000)
                # For a live venue the wall clock is authoritative when the
                # tape goes quiet. For a replay or the simulator it is not -
                # using it would fast-forward the fill engine past the data
                # and make every book look stale.
                venue = max(self.venue_now, wall) if self.feed.realtime else self.venue_now
                if venue:
                    self.aggregator.flush_until(venue)
                    self.broker.tick(venue)
                self._store_equity(wall)
                self._check_feed_health(wall)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("housekeeping error")
            await asyncio.sleep(1.0)

    def _check_feed_health(self, now: int) -> None:
        stale_s = self.cfg.data.stale_feed_seconds
        last = max(self.feed.health.last_trade_ts, self.feed.health.last_book_ts)
        if not last or not self.running:
            return
        age = (now - last) / 1000
        if age > stale_s and self.portfolio.position is not None and not self.risk.kill_switch:
            # A position we cannot see is a position we cannot manage.
            self._event("feed_stale", f"no market data for {age:.0f}s - flattening")
            self.flatten("feed went stale")

    # -- market events -----------------------------------------------------
    def _on_trade(self, trade: Trade) -> None:
        self.tape.add(trade)
        self.last_trade_price = trade.price
        self.aggregator.add_trade(trade)
        self.broker.on_trade(trade)
        self.portfolio.set_mark(self.last_book.mid if self.last_book else trade.price, trade.ts)

        self.tape_ring.push({
            "ts": trade.ts, "p": trade.price, "q": trade.qty, "s": trade.side.value,
        })
        if len(self.cvd_series) == 0 or trade.ts - self.cvd_series[-1]["ts"] > 2000:
            self.cvd_series.push({"ts": trade.ts, "cvd": round(self.tape.cum_delta, 4),
                                  "p": trade.price})

        pos = self.portfolio.position
        if pos is not None:
            atr = self.last_signal.atr if self.last_signal else pos.entry_atr
            for intent in self.positions.update(pos, trade.price, atr, trade.ts):
                self._submit_exit(intent.qty, intent.reason, intent.urgency, intent.kind)

    def _on_book(self, snap: BookSnapshot) -> None:
        self.last_book = snap
        self.broker.set_book(snap)
        if snap.mid:
            self.portfolio.set_mark(snap.mid, snap.ts)

    def _on_feed_status(self, payload: dict) -> None:
        state = payload.get("state")
        if state in ("reconnecting", "book_gap", "book_sync_failed", "sequence_gap"):
            self._event("feed", f"{self.feed.venue}: {state} ({payload.get('last_error','')})")
        asyncio.create_task(self.bus.publish("feed", payload)) if self._loop_running() else None

    def _loop_running(self) -> bool:
        try:
            asyncio.get_running_loop()
            return True
        except RuntimeError:
            return False

    # -- the decision point ------------------------------------------------
    def _on_bar_close(self, bar: Candle) -> None:
        self.bars_seen += 1
        pos = self.portfolio.position
        if pos is not None:
            self.bars_in_market += 1
            for intent in self.positions.on_bar_close(pos, bar.close):
                self._submit_exit(intent.qty, intent.reason, intent.urgency, intent.kind)

        self.risk.roll_day(bar.close_time, self.portfolio.equity)

        candles = self.aggregator.history
        pos = self.portfolio.position
        sig = self.engine.evaluate(
            candles=candles,
            book=self.last_book,
            tape=self.tape,
            position_side=pos.side if pos else None,
            now_ts=bar.close_time,
        )
        self.last_signal = sig
        if self.store:
            self.store.record_signal(sig)

        gates = self.risk.check_gates(self.portfolio.equity, bar.open_time, self.step_ms)

        if pos is None:
            self._consider_entry(sig, gates, bar)
        elif sig.action is SignalAction.EXIT and self.positions.can_exit_on_signal(pos):
            flip = abs(sig.score) >= self.cfg.signal.flip_threshold and (
                sig.score * pos.side.sign < 0
            )
            self._exit_is_flip = flip
            self._submit_exit(
                pos.qty,
                (sig.reasons[0] if sig.reasons else "signal exit"),
                urgency="normal" if not flip else "urgent",
                kind="exit",
            )

        asyncio.create_task(self.bus.publish("bar", {
            "bar": bar.to_dict(), "signal": sig.to_dict(),
        })) if self._loop_running() else None

    def _consider_entry(self, sig: Signal, gates: list[str], bar: Candle) -> None:
        side = SignalEngine.wants_entry(sig)
        if side is None:
            return
        if not self.trading_enabled:
            self._event("skip", "entry skipped: trading is paused")
            return
        if side is Side.SELL and not self.cfg.risk.allow_short:
            self._event("skip", "short signal ignored: shorts disabled")
            return
        if gates:
            self._event("blocked", f"entry blocked: {gates[0]}")
            return
        if self._entry_ctx is not None:
            return                                    # an entry is already working

        sizing = self.risk.size_position(
            equity=self.portfolio.equity,
            side=side,
            price=sig.price,
            atr=sig.atr,
            book=self.last_book,
        )
        if not sizing.ok:
            self._event("blocked", f"entry blocked: {sizing.reason}")
            return

        stop_dist = sizing.stop_distance
        stop = sig.price - stop_dist if side is Side.BUY else sig.price + stop_dist
        target = (
            sig.price + stop_dist * self.cfg.risk.take_profit_r if side is Side.BUY
            else sig.price - stop_dist * self.cfg.risk.take_profit_r
        )
        reason = (
            f"{'long' if side is Side.BUY else 'short'} · score {sig.score:+.2f} · "
            f"{sig.regime.value} · {sig.reasons[0] if sig.reasons else ''}"
        )
        self._entry_ctx = {
            "side": side, "stop": stop, "target": target, "atr": sig.atr,
            "score": sig.score, "reason": reason, "sizing": sizing.to_dict(),
            "bar_time": bar.open_time,
        }
        urgency = {"market": "urgent", "limit": "normal", "post_only": "passive"}[
            self.cfg.execution.entry_order
        ]
        intent = self.broker.execute(side, sizing.qty, tag=f"entry · {reason}", urgency=urgency)
        self._event(
            "entry",
            f"entering {side.value} {sizing.qty:g} @~{sig.price:.2f} "
            f"(risk ${sizing.risk_amount:.0f}, stop {stop:.2f}, score {sig.score:+.2f})",
        )
        if intent.done and intent.filled_qty <= 0:
            self._entry_ctx = None
            self._event("blocked", f"entry not filled: {intent.result}")

    # -- exits -------------------------------------------------------------
    def _submit_exit(self, qty: float, reason: str, urgency: str, kind: str = "exit") -> None:
        pos = self.portfolio.position
        if pos is None or qty <= 0:
            return
        available = pos.qty - self._inflight_exit_qty
        qty = min(qty, available)
        qty = self.instrument.round_qty(qty)
        if qty <= 0:
            return
        self._inflight_exit_qty += qty
        side = pos.side.opposite
        tag = f"exit · {reason}"
        intent = self.broker.execute(side, qty, tag=tag, urgency=urgency)
        self._event("exit", f"exiting {qty:g} ({reason})")
        if intent.done and intent.filled_qty <= 0:
            self._inflight_exit_qty = max(0.0, self._inflight_exit_qty - qty)
            self._event("warn", f"exit order did not fill: {intent.result}")

    def flatten(self, reason: str = "manual flatten") -> None:
        pos = self.portfolio.position
        if pos is None:
            return
        self._submit_exit(pos.qty, reason, urgency="urgent")

    # -- fills -------------------------------------------------------------
    def _on_fill(self, order: Order, fill: Fill) -> None:
        if self.store:
            self.store.record_fill(fill)

        tag = order.tag or ""
        pos = self.portfolio.position

        if tag.startswith("entry") and pos is None and self._entry_ctx:
            ctx = self._entry_ctx
            position = self.portfolio.open_position(
                side=ctx["side"], fill=fill, stop=ctx["stop"], target=ctx["target"],
                atr=ctx["atr"], signal_score=ctx["score"], reason=ctx["reason"],
            )
            self.positions.reset(position)
            self.markers.append({
                "ts": fill.ts, "price": fill.price, "side": fill.side.value,
                "kind": "entry", "reason": ctx["reason"], "qty": fill.qty,
            })
            self._event(
                "fill",
                f"ENTRY filled {fill.qty:g} @ {fill.price:.2f} "
                f"({fill.liquidity.value}, {fill.slippage_bps:+.1f}bps, fee ${fill.fee:.2f})",
            )
        elif tag.startswith("entry") and pos is not None and fill.side is pos.side:
            self.portfolio.add_to_position(fill)
        elif pos is not None and fill.side is pos.side.opposite:
            reason = tag.split("·", 1)[1].strip() if "·" in tag else "exit"
            self._inflight_exit_qty = max(0.0, self._inflight_exit_qty - fill.qty)
            self.markers.append({
                "ts": fill.ts, "price": fill.price, "side": fill.side.value,
                "kind": "exit", "reason": reason, "qty": fill.qty,
            })
            self.portfolio.reduce_position(fill, reason)
            self._event(
                "fill",
                f"EXIT filled {fill.qty:g} @ {fill.price:.2f} "
                f"({fill.liquidity.value}, {fill.slippage_bps:+.1f}bps, fee ${fill.fee:.2f})",
            )
        if len(self.markers) > 400:
            self.markers = self.markers[-400:]

    def _on_order(self, order: Order) -> None:
        if self.store:
            self.store.record_order(order)
        if order.status.value == "rejected":
            self._event("reject", f"order {order.id} rejected: {order.reject_reason}")

    def _on_intent(self, intent: Intent) -> None:
        if intent.tag.startswith("entry"):
            if intent.filled_qty <= 0:
                self._entry_ctx = None
                self._event("warn", f"entry intent {intent.id} ended unfilled ({intent.result})")
            elif intent.result in ("filled", "partial"):
                self._entry_ctx = None
        else:
            if intent.remaining > 0 and intent.done:
                self._inflight_exit_qty = max(0.0, self._inflight_exit_qty - intent.remaining)

    def _on_trade_closed(self, trade) -> None:
        bar_time = bar_open(trade.exit_ts, self.step_ms)
        self.risk.on_trade_closed(trade, bar_time, was_flip=self._exit_is_flip)
        self._exit_is_flip = False
        self._inflight_exit_qty = 0.0
        if self.store:
            self.store.record_trade(trade)
        self._event(
            "trade",
            f"closed #{trade.id} {trade.side.value} {trade.qty:g}: "
            f"${trade.pnl:+.2f} ({trade.r_multiple:+.2f}R) · {trade.exit_reason}",
        )
        asyncio.create_task(self.bus.publish("trade", trade.to_dict())) if self._loop_running() else None

    # -- helpers -----------------------------------------------------------
    def _event(self, kind: str, message: str) -> None:
        ts = int(time.time() * 1000)
        entry = {"ts": ts, "kind": kind, "message": message}
        self.events.push(entry)
        log.info("[%s] %s", kind, message)
        if self.store:
            self.store.record_event(kind, message, ts)
        if self._loop_running():
            asyncio.create_task(self.bus.publish("event", entry))

    def _store_equity(self, now: int) -> None:
        if not self.store or now - self._last_equity_store < 15_000:
            return
        self._last_equity_store = now
        pos = self.portfolio.position
        self.store.record_equity(
            now, self.portfolio.equity, self.portfolio.realized_equity,
            self.portfolio.unrealized, self.portfolio.mark,
            pos.to_dict(self.portfolio.mark) if pos else None,
        )

    def attach_polymarket(self, pipeline) -> None:
        """Run the prediction-market pipeline off the same signal and spot.

        It reads; it never touches the perp position or the perp equity.
        """
        self.polymarket = pipeline
        pipeline.bind(
            spot_provider=lambda: (
                (self.last_book.mid if self.last_book else self.last_trade_price),
                (self.last_signal.features.get("realized_vol", 0.5)
                 if self.last_signal else 0.5) or 0.5,
            ),
            signal_provider=lambda: self.last_signal.score if self.last_signal else 0.0,
        )
        self._event("polymarket", "prediction-market pipeline attached (paper, read-only)")

    # -- controls ----------------------------------------------------------
    def set_trading_enabled(self, enabled: bool) -> None:
        self.trading_enabled = enabled
        self._event("control", f"trading {'enabled' if enabled else 'paused'}")

    def kill(self, reason: str = "operator") -> None:
        self.risk.engage_kill_switch(reason, manual=True)
        self.flatten(f"kill switch: {reason}")
        self._event("control", f"kill switch: {reason}")

    def revive(self) -> None:
        self.risk.release_kill_switch()
        self._event("control", "kill switch released")

    def update_params(self, section: str, values: dict) -> dict:
        """Live parameter tweaks from the dashboard (signal/risk/execution)."""
        target = {
            "signal": self.cfg.signal,
            "risk": self.cfg.risk,
            "execution": self.cfg.execution,
        }.get(section)
        if target is None:
            raise ValueError(f"unknown config section: {section}")
        applied = {}
        for key, value in values.items():
            if not hasattr(target, key):
                continue
            current = getattr(target, key)
            try:
                cast = type(current)(value) if not isinstance(current, dict) else value
            except (TypeError, ValueError):
                continue
            setattr(target, key, cast)
            applied[key] = cast
        if applied:
            self._event("control", f"{section} updated: {applied}")
        return applied

    # -- state for the dashboard -------------------------------------------
    def snapshot_next_bar_ms(self) -> int:
        return ms_to_bar_close(self.venue_now or int(time.time() * 1000), self.step_ms)

    def snapshot(self, depth_levels: int = 15, candle_count: int = 240) -> dict:
        now = int(time.time() * 1000)
        venue = self.venue_now or now
        book = self.last_book
        pos = self.portfolio.position
        candles = self.aggregator.series(candle_count)
        equity_curve = self.portfolio.equity_curve[-2000:]

        exit_estimate = None
        if pos is not None and book is not None and book.bids and book.asks:
            est = walk_book(book, pos.side.opposite, pos.qty,
                            self.cfg.execution.max_book_levels_to_eat)
            exit_estimate = est.to_dict()

        return {
            "ts": now,
            "mode": self.cfg.mode,
            "running": self.running,
            "trading_enabled": self.trading_enabled,
            "venue": self.feed.venue,
            "symbol": self.cfg.data.symbol,
            "interval": self.cfg.data.interval,
            "real_data": self.feed.real,
            "started_at": self.started_at,
            "venue_now": venue,
            "next_bar_in_ms": ms_to_bar_close(venue, self.step_ms),
            "bars_seen": self.bars_seen,
            "instrument": self.instrument.to_dict(),
            "feed": self.feed.health.to_dict(),
            "book": book.to_dict(depth_levels) if book else None,
            "pressure": book_pressure(book) if book else None,
            "last_price": self.last_trade_price,
            "candles": [c.to_dict() for c in candles],
            "signal": self.last_signal.to_dict() if self.last_signal else None,
            "portfolio": self.portfolio.to_dict(),
            "position_mgmt": self.positions.to_dict(pos),
            "exit_estimate": exit_estimate,
            "risk": self.risk.to_dict(),
            "stats": full_stats(
                self.portfolio.trades, equity_curve, self.portfolio.start_equity,
                self.bars_in_market, max(1, self.bars_seen),
            ),
            "equity_curve": equity_curve,
            "trades": [t.to_dict() for t in self.portfolio.trades[-60:]],
            "orders": [o.to_dict() for o in self.broker.recent_orders(40)],
            "intents": [i.to_dict() for i in list(self.broker.intents.values())[-20:]],
            "execution": self.broker.stats(),
            "tape": self.tape_ring.tail(80),
            "tape_stats": self.tape.stats(),
            "cvd": self.cvd_series.tail(400),
            "markers": self.markers[-120:],
            "events": self.events.tail(60),
            "polymarket": (
                self.polymarket.state() if self.polymarket is not None
                else {"enabled": False}
            ),
            "config": {
                "signal": self.cfg.signal.model_dump(),
                "risk": self.cfg.risk.model_dump(),
                "execution": self.cfg.execution.model_dump(),
                "data": self.cfg.data.model_dump(),
            },
        }
