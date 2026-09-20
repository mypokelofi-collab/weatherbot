"""Paper portfolio: the only fake thing in the system.

Accounting model is a linear perpetual account, which is what the bot trades:
one position at a time, PnL in quote currency, fees deducted at each fill.

    realized_equity = start + Σ realised PnL - Σ fees
    equity          = realized_equity + unrealised PnL at the current mark

Marks come from the real book mid, not from the last print, because the mid is
what you could actually transact around. Every fill, every position change and
every equity point is recorded so the dashboard and the ledger show the same
numbers, and so a trade can be reconstructed months later.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from ..core.types import ClosedTrade, Fill, Liquidity, Position, Side

log = logging.getLogger(__name__)


@dataclass
class TradeLeg:
    """Accumulates the fills that make up one side of a round trip."""

    qty: float = 0.0
    notional: float = 0.0
    fees: float = 0.0
    slippage_bps_w: float = 0.0
    first_ts: int = 0
    last_ts: int = 0

    def add(self, fill: Fill) -> None:
        self.qty += fill.qty
        self.notional += fill.qty * fill.price
        self.fees += fill.fee
        self.slippage_bps_w += fill.slippage_bps * fill.qty
        self.first_ts = self.first_ts or fill.ts
        self.last_ts = fill.ts

    @property
    def avg_price(self) -> float:
        return self.notional / self.qty if self.qty else 0.0

    @property
    def avg_slippage_bps(self) -> float:
        return self.slippage_bps_w / self.qty if self.qty else 0.0


class Portfolio:
    def __init__(self, start_equity: float = 10_000.0, max_curve: int = 20_000) -> None:
        self.start_equity = start_equity
        self.realized_equity = start_equity
        self.position: Position | None = None
        self.mark: float = 0.0
        self.trades: list[ClosedTrade] = []
        self.equity_curve: list[tuple[int, float]] = []
        self.max_curve = max_curve
        self.peak_equity = start_equity
        self.fees_paid = 0.0
        self.maker_fills = 0
        self.taker_fills = 0
        self._trade_id = 0
        self._entry_leg = TradeLeg()
        self._exit_leg = TradeLeg()
        self._entry_reason = ""
        self._exit_reason = ""
        self._on_trade_closed: list[Callable[[ClosedTrade], None]] = []
        self._on_position: list[Callable[[Position | None], None]] = []

    # -- wiring ------------------------------------------------------------
    def on_trade_closed(self, fn: Callable[[ClosedTrade], None]) -> None:
        self._on_trade_closed.append(fn)

    def on_position_change(self, fn: Callable[[Position | None], None]) -> None:
        self._on_position.append(fn)

    # -- marks -------------------------------------------------------------
    def set_mark(self, price: float, ts: int, record: bool = True) -> None:
        if price <= 0:
            return
        self.mark = price
        if self.position is not None:
            r = self.position.unrealized_r(price)
            self.position.max_favorable = max(self.position.max_favorable, r)
            self.position.max_adverse = min(self.position.max_adverse, r)
        eq = self.equity
        self.peak_equity = max(self.peak_equity, eq)
        if record:
            if not self.equity_curve or ts - self.equity_curve[-1][0] >= 5_000:
                self.equity_curve.append((ts, round(eq, 4)))
                if len(self.equity_curve) > self.max_curve:
                    self.equity_curve = self.equity_curve[-self.max_curve :]

    @property
    def equity(self) -> float:
        if self.position is None or self.mark <= 0:
            return self.realized_equity
        return self.realized_equity + self.position.unrealized(self.mark)

    @property
    def unrealized(self) -> float:
        if self.position is None or self.mark <= 0:
            return 0.0
        return self.position.unrealized(self.mark)

    @property
    def drawdown(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return (self.equity - self.peak_equity) / self.peak_equity

    @property
    def exposure_notional(self) -> float:
        if self.position is None:
            return 0.0
        return self.position.qty * (self.mark or self.position.entry_price)

    @property
    def leverage(self) -> float:
        eq = self.equity
        return (self.exposure_notional / eq) if eq > 0 else 0.0

    # -- fills -------------------------------------------------------------
    def open_position(
        self,
        side: Side,
        fill: Fill,
        stop: float,
        target: float,
        atr: float,
        signal_score: float,
        reason: str,
    ) -> Position:
        risk_per_unit = abs(fill.price - stop)
        pos = Position(
            side=side,
            qty=fill.qty,
            entry_price=fill.price,
            entry_ts=fill.ts,
            stop=stop,
            target=target,
            risk_per_unit=risk_per_unit if risk_per_unit > 0 else max(atr, 1e-9),
            entry_atr=atr,
            fees_paid=fill.fee,
            entry_signal_score=signal_score,
            tag=reason,
        )
        self.position = pos
        self._entry_leg = TradeLeg()
        self._entry_leg.add(fill)
        self._exit_leg = TradeLeg()
        self._entry_reason = reason
        self.realized_equity -= fill.fee
        self._count_fill(fill)
        self._notify_position()
        return pos

    def add_to_position(self, fill: Fill) -> None:
        pos = self.position
        if pos is None:
            return
        total = pos.qty + fill.qty
        pos.entry_price = (pos.entry_price * pos.qty + fill.price * fill.qty) / total
        pos.qty = total
        pos.fees_paid += fill.fee
        self.realized_equity -= fill.fee
        self._entry_leg.add(fill)
        self._count_fill(fill)
        self._notify_position()

    def reduce_position(self, fill: Fill, reason: str) -> ClosedTrade | None:
        """Apply an exit fill. Returns a ClosedTrade when the position goes flat."""
        pos = self.position
        if pos is None:
            return None
        qty = min(fill.qty, pos.qty)
        gross = (fill.price - pos.entry_price) * qty * pos.side.sign
        self.realized_equity += gross - fill.fee
        pos.realized += gross
        pos.fees_paid += fill.fee
        pos.qty = round(pos.qty - qty, 10)
        self._exit_leg.add(fill)
        self._exit_reason = reason
        self._count_fill(fill)

        if pos.qty <= 1e-9:
            return self._close_out(pos)
        self._notify_position()
        return None

    def _close_out(self, pos: Position) -> ClosedTrade:
        self._trade_id += 1
        entry, exit_ = self._entry_leg, self._exit_leg
        gross = (exit_.avg_price - entry.avg_price) * exit_.qty * pos.side.sign
        fees = entry.fees + exit_.fees
        risk = pos.risk_per_unit * entry.qty
        trade = ClosedTrade(
            id=self._trade_id,
            side=pos.side,
            qty=entry.qty,
            entry_ts=entry.first_ts,
            entry_price=entry.avg_price,
            exit_ts=exit_.last_ts,
            exit_price=exit_.avg_price,
            gross_pnl=gross,
            fees=fees,
            pnl=gross - fees,
            r_multiple=((gross - fees) / risk) if risk > 0 else 0.0,
            bars_held=pos.bars_held,
            entry_reason=self._entry_reason,
            exit_reason=self._exit_reason,
            max_favorable_r=pos.max_favorable,
            max_adverse_r=pos.max_adverse,
            entry_slippage_bps=entry.avg_slippage_bps,
            exit_slippage_bps=exit_.avg_slippage_bps,
        )
        self.trades.append(trade)
        self.position = None
        log.info(
            "trade #%d %s %.6f @%.2f -> %.2f | pnl %.2f (%.2fR) | %s",
            trade.id, trade.side.value, trade.qty, trade.entry_price,
            trade.exit_price, trade.pnl, trade.r_multiple, trade.exit_reason,
        )
        for fn in self._on_trade_closed:
            fn(trade)
        self._notify_position()
        return trade

    def _count_fill(self, fill: Fill) -> None:
        self.fees_paid += fill.fee
        if fill.liquidity is Liquidity.MAKER:
            self.maker_fills += 1
        else:
            self.taker_fills += 1

    def _notify_position(self) -> None:
        for fn in self._on_position:
            fn(self.position)

    # -- reporting ---------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "start_equity": self.start_equity,
            "equity": round(self.equity, 4),
            "realized_equity": round(self.realized_equity, 4),
            "unrealized": round(self.unrealized, 4),
            "pnl": round(self.equity - self.start_equity, 4),
            "pnl_pct": round((self.equity / self.start_equity - 1) * 100, 4),
            "peak_equity": round(self.peak_equity, 4),
            "drawdown_pct": round(self.drawdown * 100, 4),
            "mark": self.mark,
            "exposure": round(self.exposure_notional, 2),
            "leverage": round(self.leverage, 3),
            "fees_paid": round(self.fees_paid, 4),
            "maker_fills": self.maker_fills,
            "taker_fills": self.taker_fills,
            "closed_trades": len(self.trades),
            "position": self.position.to_dict(self.mark) if self.position else None,
        }
