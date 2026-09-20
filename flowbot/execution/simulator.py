"""Paper matching engine - real book, real tape, fake money.

This is the piece that decides whether a backtest or a paper session means
anything. The shortcuts it refuses to take:

  * No fills at the mid. A market buy pays the offer and keeps paying up the
    ladder until it is done, level by level, on the real depth that was there.
  * No instant fills. An order exists at the venue only after `latency_ms`;
    it is matched against the book as it is *then*, not as it was when the
    signal fired.
  * No free liquidity. Size we just consumed stays consumed for a moment, so
    two orders in the same second do not both get the top of the book.
  * No magic limit fills. A resting order joins the back of the queue at its
    price, with the real resting size ahead of it, and only fills once the
    real tape has traded through that queue.
  * No infinite depth. If the book cannot fill the order, it fills partially
    and the rest is cancelled, exactly like an IOC.

Fees are charged per fill at the venue's real maker/taker schedule, and every
fill records slippage against the mid at submission time, so the cost of the
strategy is visible rather than assumed.
"""

from __future__ import annotations

import itertools
import logging
from typing import Callable

from ..core.config import ExecConfig
from ..core.instrument import Instrument
from ..core.types import (
    BookSnapshot,
    Fill,
    Liquidity,
    Order,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
    Trade,
)

log = logging.getLogger(__name__)

FillHandler = Callable[[Order, Fill], None]
OrderHandler = Callable[[Order], None]

# How long liquidity we consumed stays missing from the local book.
IMPACT_DECAY_MS = 1500


class MatchingEngine:
    def __init__(
        self,
        cfg: ExecConfig,
        instrument: Instrument,
        book_stale_ms: int | None = None,
    ) -> None:
        self.cfg = cfg
        self.instrument = instrument
        self.book_stale_ms = book_stale_ms if book_stale_ms is not None else cfg.book_stale_ms
        self.book: BookSnapshot | None = None
        self.now: int = 0

        self.orders: dict[str, Order] = {}
        self.pending: list[Order] = []      # submitted, not yet live at the venue
        self.resting: list[Order] = []      # live limit orders on the book
        self._ids = itertools.count(1)
        self._consumed: list[tuple[int, Side, float, float]] = []   # ts, side, price, qty

        self._fill_handlers: list[FillHandler] = []
        self._order_handlers: list[OrderHandler] = []

        self.stats = {
            "submitted": 0, "filled": 0, "rejected": 0, "canceled": 0,
            "partial": 0, "maker_fills": 0, "taker_fills": 0,
            "fees_paid": 0.0, "slippage_bps_sum": 0.0, "slippage_samples": 0,
        }

    # -- wiring ------------------------------------------------------------
    def on_fill(self, fn: FillHandler) -> None:
        self._fill_handlers.append(fn)

    def on_order(self, fn: OrderHandler) -> None:
        self._order_handlers.append(fn)

    def _emit_fill(self, order: Order, fill: Fill) -> None:
        for fn in self._fill_handlers:
            fn(order, fill)

    def _emit_order(self, order: Order) -> None:
        order.updated_ts = self.now
        for fn in self._order_handlers:
            fn(order)

    # -- order entry -------------------------------------------------------
    def new_order_id(self) -> str:
        return f"o{next(self._ids):06d}"

    def submit(
        self,
        side: Side,
        qty: float,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
        tif: TimeInForce = TimeInForce.GTC,
        tag: str = "",
        max_slippage_bps: float | None = None,
        timeout_s: float | None = None,
        link_id: str = "",
    ) -> Order:
        qty = self.instrument.round_qty(qty)
        mid = self.book.mid if self.book else 0.0
        order = Order(
            id=self.new_order_id(),
            ts=self.now,
            side=side,
            type=order_type,
            qty=qty,
            price=self.instrument.round_price(price, side_up=(side is Side.SELL)) if price else None,
            tif=tif,
            tag=tag,
            link_id=link_id,
            submit_mid=mid,
            max_slippage_bps=max_slippage_bps,
            active_at=self.now + self.cfg.latency_ms,
        )
        if timeout_s:
            order.expire_at = order.active_at + int(timeout_s * 1000)

        self.orders[order.id] = order
        self.stats["submitted"] += 1

        ok, why = self.instrument.is_tradable(qty, mid or (price or 0.0))
        if not ok:
            return self._reject(order, why)

        self.pending.append(order)
        self._emit_order(order)
        # If the configured latency has already elapsed (or is zero), the order
        # is live immediately; otherwise it waits for a market event past
        # active_at. Either way the caller sees the real post-submit state.
        self._activate_pending()
        return order

    def cancel(self, order_id: str, reason: str = "canceled") -> bool:
        order = self.orders.get(order_id)
        if not order or not order.is_open:
            return False
        for bucket in (self.pending, self.resting):
            if order in bucket:
                bucket.remove(order)
        order.status = OrderStatus.CANCELED
        order.reject_reason = reason
        self.stats["canceled"] += 1
        self._emit_order(order)
        return True

    def cancel_all(self, reason: str = "cancel_all") -> int:
        n = 0
        for order in list(self.pending) + list(self.resting):
            if self.cancel(order.id, reason):
                n += 1
        return n

    def open_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if o.is_open]

    # -- market events -----------------------------------------------------
    def set_book(self, snap: BookSnapshot) -> None:
        self.book = snap
        self.now = max(self.now, snap.ts)
        self._expire_consumed()
        self._activate_pending()
        self._refresh_queues(snap)
        self._expire_orders()

    def on_trade(self, trade: Trade) -> None:
        self.now = max(self.now, trade.ts)
        self._activate_pending()
        self._match_resting_against_trade(trade)
        self._expire_orders()

    def tick(self, ts: int) -> None:
        """Advance the clock with no market event (used by the trader loop)."""
        self.now = max(self.now, ts)
        self._activate_pending()
        self._expire_orders()

    # -- internals ---------------------------------------------------------
    def _reject(self, order: Order, reason: str) -> Order:
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        self.stats["rejected"] += 1
        log.info("order %s rejected: %s", order.id, reason)
        self._emit_order(order)
        return order

    def _book_is_usable(self) -> bool:
        if self.book is None or not self.book.bids or not self.book.asks:
            return False
        if self.now - self.book.ts > self.book_stale_ms:
            return False
        return True

    def _expire_consumed(self) -> None:
        cutoff = self.now - IMPACT_DECAY_MS
        if self._consumed and self._consumed[0][0] < cutoff:
            self._consumed = [c for c in self._consumed if c[0] >= cutoff]

    def _effective_levels(self, side: Side) -> list[tuple[float, float]]:
        """Book levels minus the size our own recent orders already took."""
        assert self.book is not None
        levels = self.book.asks if side is Side.BUY else self.book.bids
        taken: dict[float, float] = {}
        for ts, cside, price, qty in self._consumed:
            if cside is side and self.now - ts <= IMPACT_DECAY_MS:
                taken[price] = taken.get(price, 0.0) + qty
        out = []
        for lv in levels[: self.cfg.max_book_levels_to_eat]:
            avail = lv.qty - taken.get(lv.price, 0.0)
            if avail > 1e-9:
                out.append((lv.price, avail))
        return out

    def _activate_pending(self) -> None:
        if not self.pending:
            return
        still: list[Order] = []
        for order in list(self.pending):
            if order.active_at > self.now:
                still.append(order)
                continue
            if not self._book_is_usable():
                # Never fill against a book we cannot vouch for; hold the order
                # and let it expire if the feed does not come back.
                if order.expire_at and self.now >= order.expire_at:
                    self.cancel(order.id, "book unavailable")
                else:
                    still.append(order)
                continue
            self._place(order)
        self.pending = still

    def _place(self, order: Order) -> None:
        assert self.book is not None
        best_bid, best_ask = self.book.best_bid, self.book.best_ask

        if order.type is OrderType.MARKET:
            self._take(order, limit_price=None)
            return

        crosses = (
            order.price is not None
            and ((order.side is Side.BUY and order.price >= best_ask)
                 or (order.side is Side.SELL and order.price <= best_bid))
        )
        if crosses:
            if order.tif is TimeInForce.POST_ONLY:
                self._reject(order, "post-only would cross the spread")
                return
            self._take(order, limit_price=order.price)
            if order.remaining > 0 and order.tif is not TimeInForce.IOC:
                self._rest(order)
            elif order.remaining > 0:
                self.cancel(order.id, "ioc remainder")
            return

        if order.tif is TimeInForce.IOC:
            self.cancel(order.id, "ioc did not cross")
            return
        self._rest(order)

    def _rest(self, order: Order) -> None:
        """Join the queue at our price, behind everything already resting."""
        assert self.book is not None
        levels = self.book.bids if order.side is Side.BUY else self.book.asks
        ahead = 0.0
        for lv in levels:
            if abs(lv.price - (order.price or 0.0)) < 1e-9:
                ahead = lv.qty
                break
        order.queue_ahead = ahead
        if order not in self.resting:
            self.resting.append(order)
        self._emit_order(order)

    def _take(self, order: Order, limit_price: float | None) -> None:
        """Cross the spread: walk real depth, pay the real prices."""
        levels = self._effective_levels(order.side)
        if not levels:
            self._reject(order, "no depth on the book")
            return

        mid = self.book.mid if self.book else order.submit_mid
        remaining = order.remaining
        notional = 0.0
        taken: list[tuple[float, float]] = []
        for price, avail in levels:
            if remaining <= 1e-12:
                break
            if limit_price is not None:
                if order.side is Side.BUY and price > limit_price:
                    break
                if order.side is Side.SELL and price < limit_price:
                    break
            take = min(remaining, avail)
            notional += take * price
            remaining -= take
            taken.append((price, take))

        filled = order.remaining - remaining
        if filled <= 0:
            if limit_price is not None:
                self._rest(order)
            else:
                self._reject(order, "no depth on the book")
            return

        avg = notional / filled
        slip_bps = ((avg - mid) / mid * 1e4 * order.side.sign) if mid else 0.0

        # The slippage budget is the bot's own circuit breaker: if crossing
        # would cost more than the edge, the trade is not worth taking.
        if (
            order.max_slippage_bps is not None
            and slip_bps > order.max_slippage_bps
            and order.filled_qty == 0
        ):
            self._reject(
                order,
                f"slippage {slip_bps:.1f}bps over budget {order.max_slippage_bps:.1f}bps",
            )
            return

        for price, qty in taken:
            self._consumed.append((self.now, order.side, price, qty))

        fee = notional * self.cfg.taker_fee_bps / 1e4
        fill = Fill(
            ts=self.now,
            order_id=order.id,
            side=order.side,
            price=round(avg, 8),
            qty=round(filled, 10),
            fee=fee,
            liquidity=Liquidity.TAKER,
            slippage_bps=slip_bps,
            level_depth=len(taken),
        )
        self._apply_fill(order, fill)

        if order.remaining > 1e-12 and order.type is OrderType.MARKET:
            # Market orders behave like IOC: whatever the book could not fill
            # is cancelled rather than silently assumed.
            self.cancel(order.id, "insufficient depth for full size")

    def _apply_fill(self, order: Order, fill: Fill) -> None:
        order.fills.append(fill)
        prev_notional = order.avg_price * order.filled_qty
        order.filled_qty = round(order.filled_qty + fill.qty, 10)
        order.avg_price = (prev_notional + fill.price * fill.qty) / order.filled_qty
        order.fees += fill.fee
        order.status = (
            OrderStatus.FILLED if order.remaining <= 1e-9 else OrderStatus.PARTIALLY_FILLED
        )
        if order.status is OrderStatus.FILLED:
            self.stats["filled"] += 1
            if order in self.resting:
                self.resting.remove(order)
        else:
            self.stats["partial"] += 1
        if fill.liquidity is Liquidity.MAKER:
            self.stats["maker_fills"] += 1
        else:
            self.stats["taker_fills"] += 1
        self.stats["fees_paid"] += fill.fee
        self.stats["slippage_bps_sum"] += fill.slippage_bps
        self.stats["slippage_samples"] += 1
        self._emit_fill(order, fill)
        self._emit_order(order)

    def _refresh_queues(self, snap: BookSnapshot) -> None:
        """Track cancellations ahead of us (optimistic model only).

        FIFO assumes every cancel happens behind us - pessimistic but safe.
        The optimistic model credits us with half of any size that leaves our
        price level without a trade, which is closer to measured behaviour on
        crypto venues but flattering.
        """
        if self.cfg.queue_model != "optimistic" or not self.resting:
            return
        for order in self.resting:
            levels = snap.bids if order.side is Side.BUY else snap.asks
            size = 0.0
            for lv in levels:
                if abs(lv.price - (order.price or 0.0)) < 1e-9:
                    size = lv.qty
                    break
            if size < order.queue_ahead:
                order.queue_ahead = max(0.0, order.queue_ahead - (order.queue_ahead - size) * 0.5)

    def _match_resting_against_trade(self, trade: Trade) -> None:
        """Advance queue position with the real tape, then fill what is due."""
        if not self.resting:
            return
        for order in list(self.resting):
            price = order.price or 0.0
            if order.side is Side.BUY:
                # Sellers hitting the bid consume the queue at our price.
                if trade.side is not Side.SELL or trade.price > price + 1e-9:
                    continue
                through = trade.price < price - 1e-9
            else:
                if trade.side is not Side.BUY or trade.price < price - 1e-9:
                    continue
                through = trade.price > price + 1e-9

            if through:
                # The tape traded past our price: everything ahead of us is gone.
                qty = min(order.remaining, trade.qty)
                order.queue_ahead = 0.0
            else:
                if order.queue_ahead > 0:
                    consumed = min(order.queue_ahead, trade.qty)
                    order.queue_ahead -= consumed
                    leftover = trade.qty - consumed
                    if leftover <= 1e-12:
                        continue
                    qty = min(order.remaining, leftover)
                else:
                    qty = min(order.remaining, trade.qty)

            if qty <= 1e-12:
                continue
            if not self.cfg.allow_partial_fills and qty < order.remaining:
                continue

            mid = order.submit_mid or price
            fee = qty * price * self.cfg.maker_fee_bps / 1e4
            fill = Fill(
                ts=trade.ts,
                order_id=order.id,
                side=order.side,
                price=price,
                qty=round(qty, 10),
                fee=fee,
                liquidity=Liquidity.MAKER,
                slippage_bps=((price - mid) / mid * 1e4 * order.side.sign) if mid else 0.0,
                level_depth=0,
            )
            self._apply_fill(order, fill)

    def _expire_orders(self) -> None:
        for order in list(self.pending) + list(self.resting):
            if order.expire_at and self.now >= order.expire_at and order.is_open:
                self.cancel(order.id, "working timeout")

    # -- reporting ---------------------------------------------------------
    @property
    def avg_slippage_bps(self) -> float:
        n = self.stats["slippage_samples"]
        return self.stats["slippage_bps_sum"] / n if n else 0.0

    def stats_dict(self) -> dict:
        d = dict(self.stats)
        d["avg_slippage_bps"] = round(self.avg_slippage_bps, 3)
        d["open_orders"] = len(self.open_orders())
        d["resting"] = len(self.resting)
        d["pending"] = len(self.pending)
        return d
