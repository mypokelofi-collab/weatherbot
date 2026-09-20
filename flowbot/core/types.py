"""Core domain types shared by the data, signal, execution and bot layers.

These are plain dataclasses on purpose: they sit in the hot path (every trade
print and every book update allocates one), so we avoid validation overhead.
Configuration - which is parsed once at boot - uses pydantic instead.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


def now_ms() -> int:
    """Wall-clock milliseconds. All timestamps in the system are ms since epoch, UTC."""
    return int(time.time() * 1000)


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(str, Enum):
    GTC = "gtc"          # rest on the book until filled or cancelled
    IOC = "ioc"          # take what is available now, cancel the rest
    POST_ONLY = "post"   # never take; rejected if it would cross


class OrderStatus(str, Enum):
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


class Liquidity(str, Enum):
    MAKER = "maker"
    TAKER = "taker"


@dataclass(slots=True)
class Trade:
    """A real print from the venue's public trade tape."""

    ts: int
    price: float
    qty: float
    side: Side          # aggressor side: BUY = buyer lifted the offer
    trade_id: int = 0

    @property
    def notional(self) -> float:
        return self.price * self.qty


@dataclass(slots=True)
class BookLevel:
    price: float
    qty: float

    @property
    def notional(self) -> float:
        return self.price * self.qty


@dataclass(slots=True)
class BookSnapshot:
    """An immutable view of the top of the real L2 book at a point in time."""

    ts: int
    bids: list[BookLevel]   # descending price
    asks: list[BookLevel]   # ascending price
    seq: int = 0

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 0.0

    @property
    def mid(self) -> float:
        if not self.bids or not self.asks:
            return self.best_bid or self.best_ask
        return (self.bids[0].price + self.asks[0].price) / 2.0

    @property
    def spread(self) -> float:
        if not self.bids or not self.asks:
            return 0.0
        return self.asks[0].price - self.bids[0].price

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        return (self.spread / mid) * 1e4 if mid else 0.0

    @property
    def microprice(self) -> float:
        """Size-weighted mid: leans toward the side with less resting size."""
        if not self.bids or not self.asks:
            return self.mid
        bq, aq = self.bids[0].qty, self.asks[0].qty
        total = bq + aq
        if total <= 0:
            return self.mid
        return (self.bids[0].price * aq + self.asks[0].price * bq) / total

    def depth_notional(self, bps: float) -> tuple[float, float]:
        """Resting notional on each side within `bps` of the mid (bid, ask)."""
        mid = self.mid
        if not mid:
            return (0.0, 0.0)
        band = mid * bps / 1e4
        bid_n = sum(lv.notional for lv in self.bids if lv.price >= mid - band)
        ask_n = sum(lv.notional for lv in self.asks if lv.price <= mid + band)
        return (bid_n, ask_n)

    def imbalance(self, bps: float = 10.0) -> float:
        """Book imbalance in [-1, 1]. Positive = more bid notional than ask."""
        bid_n, ask_n = self.depth_notional(bps)
        total = bid_n + ask_n
        if total <= 0:
            return 0.0
        return (bid_n - ask_n) / total

    def to_dict(self, levels: int = 15) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "seq": self.seq,
            "bids": [[lv.price, lv.qty] for lv in self.bids[:levels]],
            "asks": [[lv.price, lv.qty] for lv in self.asks[:levels]],
            "mid": self.mid,
            "spread_bps": self.spread_bps,
            "microprice": self.microprice,
            "imbalance": self.imbalance(),
        }


@dataclass(slots=True)
class Candle:
    """A 15m (or any interval) OHLCV bar built from the real trade tape."""

    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    buy_volume: float = 0.0      # volume where the buyer was the aggressor
    sell_volume: float = 0.0
    trades: int = 0
    quote_volume: float = 0.0
    closed: bool = False

    @property
    def vwap(self) -> float:
        return self.quote_volume / self.volume if self.volume > 0 else self.close

    @property
    def delta(self) -> float:
        """Aggressor volume delta - the raw 'market flow' of the bar."""
        return self.buy_volume - self.sell_volume

    @property
    def delta_ratio(self) -> float:
        return self.delta / self.volume if self.volume > 0 else 0.0

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return self.close - self.open

    def to_dict(self) -> dict[str, Any]:
        return {
            "t": self.open_time,
            "T": self.close_time,
            "o": self.open,
            "h": self.high,
            "l": self.low,
            "c": self.close,
            "v": self.volume,
            "bv": self.buy_volume,
            "sv": self.sell_volume,
            "n": self.trades,
            "vwap": self.vwap,
            "closed": self.closed,
        }


class SignalAction(str, Enum):
    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT = "exit"
    HOLD = "hold"
    NONE = "none"


class Regime(str, Enum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    CHOP = "chop"
    ILLIQUID = "illiquid"


@dataclass(slots=True)
class SignalComponent:
    """One scored input to the composite momentum signal."""

    name: str
    raw: float
    score: float     # normalised to [-1, 1]
    weight: float
    note: str = ""

    @property
    def contribution(self) -> float:
        return self.score * self.weight

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "raw": self.raw,
            "score": self.score,
            "weight": self.weight,
            "contribution": self.contribution,
            "note": self.note,
        }


@dataclass(slots=True)
class Signal:
    ts: int
    bar_time: int
    action: SignalAction
    score: float                      # composite momentum score in [-1, 1]
    confidence: float                 # 0..1, how much of the max score was achieved
    regime: Regime
    components: list[SignalComponent] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    features: dict[str, float] = field(default_factory=dict)
    price: float = 0.0
    atr: float = 0.0

    @property
    def direction(self) -> int:
        if self.score > 0:
            return 1
        if self.score < 0:
            return -1
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "bar_time": self.bar_time,
            "action": self.action.value,
            "score": self.score,
            "confidence": self.confidence,
            "regime": self.regime.value,
            "components": [c.to_dict() for c in self.components],
            "reasons": self.reasons,
            "blockers": self.blockers,
            "features": self.features,
            "price": self.price,
            "atr": self.atr,
        }


@dataclass(slots=True)
class Fill:
    ts: int
    order_id: str
    side: Side
    price: float
    qty: float
    fee: float
    liquidity: Liquidity
    slippage_bps: float = 0.0     # vs the mid at order submission
    level_depth: int = 0          # how many book levels the order had to eat

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "order_id": self.order_id,
            "side": self.side.value,
            "price": self.price,
            "qty": self.qty,
            "fee": self.fee,
            "liquidity": self.liquidity.value,
            "slippage_bps": self.slippage_bps,
            "levels": self.level_depth,
        }


@dataclass(slots=True)
class Order:
    id: str
    ts: int
    side: Side
    type: OrderType
    qty: float
    price: float | None = None
    tif: TimeInForce = TimeInForce.GTC
    status: OrderStatus = OrderStatus.NEW
    filled_qty: float = 0.0
    avg_price: float = 0.0
    fees: float = 0.0
    tag: str = ""                       # why the bot sent it
    submit_mid: float = 0.0             # mid at submission, for slippage accounting
    queue_ahead: float = 0.0            # resting size ahead of us at our price
    max_slippage_bps: float | None = None   # abort rather than pay more than this
    active_at: int = 0                  # venue-side arrival = submit + latency
    expire_at: int = 0                  # 0 = no working timeout
    fills: list[Fill] = field(default_factory=list)
    updated_ts: int = 0
    reject_reason: str = ""

    @property
    def remaining(self) -> float:
        return max(0.0, self.qty - self.filled_qty)

    @property
    def is_open(self) -> bool:
        return self.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "side": self.side.value,
            "type": self.type.value,
            "qty": self.qty,
            "price": self.price,
            "tif": self.tif.value,
            "status": self.status.value,
            "filled_qty": self.filled_qty,
            "avg_price": self.avg_price,
            "fees": self.fees,
            "tag": self.tag,
            "queue_ahead": self.queue_ahead,
            "active_at": self.active_at,
            "expire_at": self.expire_at,
            "max_slippage_bps": self.max_slippage_bps,
            "reject_reason": self.reject_reason,
            "updated_ts": self.updated_ts,
        }


@dataclass(slots=True)
class Position:
    side: Side
    qty: float
    entry_price: float
    entry_ts: int
    stop: float
    target: float
    risk_per_unit: float               # |entry - stop|, the 1R distance
    entry_atr: float = 0.0
    fees_paid: float = 0.0
    realized: float = 0.0              # realized PnL from partial exits
    max_favorable: float = 0.0         # best unrealised PnL seen, in R
    max_adverse: float = 0.0
    bars_held: int = 0
    scaled_out: bool = False
    breakeven_armed: bool = False
    trail: float = 0.0                 # current trailing stop level (0 = inactive)
    entry_signal_score: float = 0.0
    tag: str = ""

    def unrealized(self, mark: float) -> float:
        return (mark - self.entry_price) * self.qty * self.side.sign

    def unrealized_r(self, mark: float) -> float:
        if self.risk_per_unit <= 0:
            return 0.0
        return (mark - self.entry_price) * self.side.sign / self.risk_per_unit

    def to_dict(self, mark: float) -> dict[str, Any]:
        return {
            "side": self.side.value,
            "qty": self.qty,
            "entry_price": self.entry_price,
            "entry_ts": self.entry_ts,
            "stop": self.stop,
            "target": self.target,
            "trail": self.trail,
            "risk_per_unit": self.risk_per_unit,
            "entry_atr": self.entry_atr,
            "mark": mark,
            "notional": mark * self.qty,
            "unrealized": self.unrealized(mark),
            "unrealized_r": self.unrealized_r(mark),
            "fees_paid": self.fees_paid,
            "realized": self.realized,
            "bars_held": self.bars_held,
            "max_favorable_r": self.max_favorable,
            "max_adverse_r": self.max_adverse,
            "scaled_out": self.scaled_out,
            "breakeven_armed": self.breakeven_armed,
            "entry_signal_score": self.entry_signal_score,
            "tag": self.tag,
        }


@dataclass(slots=True)
class ClosedTrade:
    """A completed round trip, the unit the performance stats are built from."""

    id: int
    side: Side
    qty: float
    entry_ts: int
    entry_price: float
    exit_ts: int
    exit_price: float
    gross_pnl: float
    fees: float
    pnl: float
    r_multiple: float
    bars_held: int
    entry_reason: str
    exit_reason: str
    max_favorable_r: float = 0.0
    max_adverse_r: float = 0.0
    entry_slippage_bps: float = 0.0
    exit_slippage_bps: float = 0.0

    @property
    def duration_ms(self) -> int:
        return self.exit_ts - self.entry_ts

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "side": self.side.value,
            "qty": self.qty,
            "entry_ts": self.entry_ts,
            "entry_price": self.entry_price,
            "exit_ts": self.exit_ts,
            "exit_price": self.exit_price,
            "gross_pnl": self.gross_pnl,
            "fees": self.fees,
            "pnl": self.pnl,
            "r_multiple": self.r_multiple,
            "bars_held": self.bars_held,
            "entry_reason": self.entry_reason,
            "exit_reason": self.exit_reason,
            "max_favorable_r": self.max_favorable_r,
            "max_adverse_r": self.max_adverse_r,
            "entry_slippage_bps": self.entry_slippage_bps,
            "exit_slippage_bps": self.exit_slippage_bps,
            "duration_ms": self.duration_ms,
        }
