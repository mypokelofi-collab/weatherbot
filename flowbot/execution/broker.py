"""Broker: the layer that turns "get me long 0.12 BTC" into worked orders.

The trader never talks to the matching engine directly. It expresses an
*intent* - a side, a size, a reason and an urgency - and the broker works it
the way a human trader would:

  passive : post inside the spread, wait for the market to come to us, and
            only cross if it does not (saves the taker fee on entries).
  normal  : post once, briefly; cross on timeout.
  urgent  : cross now. Stops and flips do not get to be patient.

Escalation is what makes this honest. A backtest that assumes every passive
order gets filled at the touch is fiction; here an unfilled post becomes a
market order that pays the spread, which is exactly what happens live.

`Broker` is a protocol with one implementation, `PaperBroker`. Live order
routing is deliberately not implemented - see LiveBroker at the bottom.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..core.config import ExecConfig
from ..core.instrument import Instrument
from ..core.types import (
    BookSnapshot,
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
    Trade,
)
from .simulator import MatchingEngine

log = logging.getLogger(__name__)

URGENCY_PLAN = {
    # urgency -> ordered list of attempts
    "passive": ["post_only", "post_only", "market"],
    "normal": ["limit", "market"],
    "urgent": ["market"],
}


@dataclass
class Intent:
    id: str
    side: Side
    qty: float
    tag: str
    urgency: str
    created_ts: int
    filled_qty: float = 0.0
    avg_price: float = 0.0
    fees: float = 0.0
    attempts: int = 0
    order_ids: list[str] = field(default_factory=list)
    done: bool = False
    result: str = ""
    slippage_bps: float = 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.qty - self.filled_qty)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "side": self.side.value,
            "qty": self.qty,
            "filled_qty": self.filled_qty,
            "avg_price": self.avg_price,
            "fees": self.fees,
            "tag": self.tag,
            "urgency": self.urgency,
            "attempts": self.attempts,
            "orders": self.order_ids,
            "done": self.done,
            "result": self.result,
            "slippage_bps": round(self.slippage_bps, 3),
            "created_ts": self.created_ts,
        }


class Broker(Protocol):
    def execute(self, side: Side, qty: float, tag: str, urgency: str) -> Intent: ...
    def cancel_all(self) -> int: ...
    def on_fill(self, fn: Callable[[Order, Fill], None]) -> None: ...


class PaperBroker:
    def __init__(self, cfg: ExecConfig, instrument: Instrument) -> None:
        self.cfg = cfg
        self.instrument = instrument
        self.engine = MatchingEngine(cfg, instrument)
        self.intents: dict[str, Intent] = {}
        self._by_order: dict[str, str] = {}
        self._ids = itertools.count(1)
        self._fill_handlers: list[Callable[[Order, Fill], None]] = []
        self._intent_handlers: list[Callable[[Intent], None]] = []
        self._order_handlers: list[Callable[[Order], None]] = []

        self.engine.on_fill(self._handle_fill)
        self.engine.on_order(self._handle_order)

    # -- wiring ------------------------------------------------------------
    def on_fill(self, fn: Callable[[Order, Fill], None]) -> None:
        self._fill_handlers.append(fn)

    def on_intent(self, fn: Callable[[Intent], None]) -> None:
        self._intent_handlers.append(fn)

    def on_order(self, fn: Callable[[Order], None]) -> None:
        self._order_handlers.append(fn)

    # -- market events pass through to the engine --------------------------
    def set_book(self, snap: BookSnapshot) -> None:
        self.engine.set_book(snap)

    def on_trade(self, trade: Trade) -> None:
        self.engine.on_trade(trade)

    def tick(self, ts: int) -> None:
        self.engine.tick(ts)

    @property
    def now(self) -> int:
        return self.engine.now

    @property
    def book(self) -> BookSnapshot | None:
        return self.engine.book

    # -- intents -----------------------------------------------------------
    def execute(
        self, side: Side, qty: float, tag: str = "", urgency: str = "normal"
    ) -> Intent:
        qty = self.instrument.round_qty(qty)
        intent = Intent(
            id=f"i{next(self._ids):05d}",
            side=side,
            qty=qty,
            tag=tag,
            urgency=urgency if urgency in URGENCY_PLAN else "normal",
            created_ts=self.engine.now,
        )
        self.intents[intent.id] = intent
        if qty <= 0:
            intent.done = True
            intent.result = "zero size"
            return intent
        self._next_attempt(intent)
        return intent

    def _next_attempt(self, intent: Intent) -> None:
        plan = URGENCY_PLAN[intent.urgency]
        if intent.attempts >= len(plan) or intent.remaining <= 0:
            self._finish(intent)
            return

        style = plan[intent.attempts]
        intent.attempts += 1
        book = self.engine.book
        if book is None or not book.bids or not book.asks:
            intent.done = True
            intent.result = "no book"
            self._notify(intent)
            return

        tick = self.instrument.tick_size
        offset = max(0, self.cfg.limit_offset_ticks) * tick
        # Exits and urgent orders have no slippage budget: refusing to fill an
        # exit because the book got thin is how a stop becomes a disaster.
        budget = None if intent.urgency == "urgent" or "exit" in intent.tag else self.cfg.max_slippage_bps

        if style == "market":
            order = self.engine.submit(
                side=intent.side, qty=intent.remaining, order_type=OrderType.MARKET,
                tag=intent.tag, max_slippage_bps=budget, link_id=intent.id,
                timeout_s=self.cfg.market_timeout_s,
            )
        else:
            if intent.side is Side.BUY:
                price = book.best_bid + offset
                price = min(price, book.best_ask - tick)   # stay passive
            else:
                price = book.best_ask - offset
                price = max(price, book.best_bid + tick)
            tif = TimeInForce.POST_ONLY if style == "post_only" else TimeInForce.GTC
            order = self.engine.submit(
                side=intent.side, qty=intent.remaining, order_type=OrderType.LIMIT,
                price=price, tif=tif, tag=intent.tag,
                timeout_s=self.cfg.limit_timeout_s, link_id=intent.id,
            )
        intent.order_ids.append(order.id)

        # A rejected post-only (crossed spread) should escalate immediately
        # rather than waiting for a timeout that will never come.
        if order.status in (OrderStatus.REJECTED, OrderStatus.CANCELED):
            self._next_attempt(intent)

    def _handle_fill(self, order: Order, fill: Fill) -> None:
        intent_id = order.link_id or self._by_order.get(order.id)
        if intent_id and intent_id in self.intents:
            intent = self.intents[intent_id]
            prev = intent.avg_price * intent.filled_qty
            intent.filled_qty = round(intent.filled_qty + fill.qty, 10)
            intent.avg_price = (prev + fill.price * fill.qty) / intent.filled_qty
            intent.fees += fill.fee
            intent.slippage_bps = (
                (intent.slippage_bps * (intent.filled_qty - fill.qty) + fill.slippage_bps * fill.qty)
                / intent.filled_qty
            )
        for fn in self._fill_handlers:
            fn(order, fill)

    def _handle_order(self, order: Order) -> None:
        for fn in self._order_handlers:
            fn(order)
        intent_id = order.link_id or self._by_order.get(order.id)
        if not intent_id or intent_id not in self.intents:
            return
        intent = self.intents[intent_id]
        if intent.done:
            return
        if order.status is OrderStatus.FILLED:
            self._finish(intent)
        elif order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED):
            if intent.remaining > 0:
                log.info(
                    "intent %s escalating after %s (%s), %.6f left",
                    intent.id, order.status.value, order.reject_reason or "-", intent.remaining,
                )
                self._next_attempt(intent)
            else:
                self._finish(intent)

    def _finish(self, intent: Intent) -> None:
        intent.done = True
        if intent.filled_qty <= 0:
            intent.result = intent.result or "unfilled"
        elif intent.remaining > 1e-9:
            intent.result = "partial"
        else:
            intent.result = "filled"
        self._notify(intent)

    def _notify(self, intent: Intent) -> None:
        for fn in self._intent_handlers:
            fn(intent)

    # -- misc --------------------------------------------------------------
    def cancel_all(self, reason: str = "flatten") -> int:
        for intent in self.intents.values():
            if not intent.done:
                intent.done = True
                intent.result = "canceled"
        return self.engine.cancel_all(reason)

    def open_orders(self) -> list[Order]:
        return self.engine.open_orders()

    def working_intents(self) -> list[Intent]:
        return [i for i in self.intents.values() if not i.done]

    def recent_orders(self, n: int = 50) -> list[Order]:
        return sorted(self.engine.orders.values(), key=lambda o: o.ts)[-n:]

    def stats(self) -> dict:
        return self.engine.stats_dict()


class LiveBroker:
    """Placeholder for real order routing.

    Deliberately not implemented. The whole point of this system is that the
    market data, the book, the fills and the costs are real while the money is
    not. Wiring this class up means signing orders with an exchange API key,
    which is a different project with a different risk review - the seam is
    here so that work does not require rewriting the trader.
    """

    def __init__(self, *_args, **_kwargs) -> None:
        raise NotImplementedError(
            "live order routing is intentionally not implemented; flowbot is paper-only"
        )
