"""Risk management: how big, and may we trade at all.

Two jobs, kept separate on purpose.

`size_position` answers "how big", and it is the reason this bot survives a
bad streak: size comes from the stop distance, not from a fixed notional. A
1.6-ATR stop in a quiet market buys more coin than the same stop in a volatile
one, and both risk exactly the configured fraction of equity. The size is then
clipped three more times - by the leverage cap, by the notional cap, and by
what the real book can absorb inside the slippage budget. Position sizing that
ignores liquidity is how backtests print profits that live trading cannot.

`check_gates` answers "may we trade at all", and it is the circuit breaker:
daily loss limit, consecutive losses, trade count, cooldown after an exit, and
a manual kill switch. Gates only ever block *entries* - nothing is allowed to
stop the bot from closing a position it already holds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..core.clock import day_start
from ..core.config import ExecConfig, RiskConfig
from ..core.instrument import Instrument
from ..core.types import BookSnapshot, ClosedTrade, Side
from ..execution.microstructure import max_qty_within_slippage, walk_book

log = logging.getLogger(__name__)


@dataclass
class SizingResult:
    qty: float = 0.0
    ok: bool = False
    reason: str = ""
    risk_amount: float = 0.0
    stop_distance: float = 0.0
    raw_qty: float = 0.0
    notional: float = 0.0
    leverage: float = 0.0
    cap_applied: str = ""
    actual_risk_pct: float = 0.0     # what this size really risks, after rounding
    liquidity_qty: float = 0.0
    expected_slippage_bps: float = 0.0
    expected_cost: float = 0.0

    def to_dict(self) -> dict:
        return {
            "qty": self.qty,
            "ok": self.ok,
            "reason": self.reason,
            "risk_amount": round(self.risk_amount, 2),
            "stop_distance": round(self.stop_distance, 2),
            "raw_qty": round(self.raw_qty, 8),
            "notional": round(self.notional, 2),
            "leverage": round(self.leverage, 3),
            "cap_applied": self.cap_applied,
            "actual_risk_pct": round(self.actual_risk_pct, 3),
            "liquidity_qty": round(self.liquidity_qty, 6),
            "expected_slippage_bps": round(self.expected_slippage_bps, 3),
            "expected_cost": round(self.expected_cost, 2),
        }


@dataclass
class DayState:
    day: int = 0
    start_equity: float = 0.0
    realized_pnl: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0

    def to_dict(self) -> dict:
        return {
            "day": self.day,
            "start_equity": round(self.start_equity, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "pnl_pct": round(
                (self.realized_pnl / self.start_equity * 100) if self.start_equity else 0.0, 3
            ),
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
        }


class RiskManager:
    def __init__(self, cfg: RiskConfig, exec_cfg: ExecConfig, instrument: Instrument) -> None:
        self.cfg = cfg
        self.exec_cfg = exec_cfg
        self.instrument = instrument
        self.day = DayState()
        self.consecutive_losses = 0
        self.last_exit_bar: int = 0
        self.last_exit_was_loss = False
        self.last_exit_was_flip = False
        self.kill_switch = False
        self.kill_reason = ""
        self.halt_until_bar: int = 0
        self.blocked_reasons: list[str] = []

    # -- daily bookkeeping -------------------------------------------------
    def roll_day(self, ts: int, equity: float) -> None:
        d = day_start(ts)
        if d != self.day.day:
            if self.day.day:
                log.info("new trading day; yesterday realised %.2f", self.day.realized_pnl)
            self.day = DayState(day=d, start_equity=equity)

    def on_trade_closed(self, trade: ClosedTrade, bar_time: int, was_flip: bool = False) -> None:
        self.day.realized_pnl += trade.pnl
        self.day.trades += 1
        if trade.pnl > 0:
            self.day.wins += 1
            self.consecutive_losses = 0
            self.last_exit_was_loss = False
        else:
            self.day.losses += 1
            self.consecutive_losses += 1
            self.last_exit_was_loss = True
        self.last_exit_bar = bar_time
        self.last_exit_was_flip = was_flip

        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            self.engage_kill_switch(
                f"{self.consecutive_losses} consecutive losses", manual=False
            )

    def engage_kill_switch(self, reason: str, manual: bool = True) -> None:
        self.kill_switch = True
        self.kill_reason = ("manual: " if manual else "auto: ") + reason
        log.warning("kill switch engaged (%s)", self.kill_reason)

    def release_kill_switch(self) -> None:
        self.kill_switch = False
        self.kill_reason = ""
        self.consecutive_losses = 0

    # -- gates -------------------------------------------------------------
    def check_gates(self, equity: float, bar_time: int, step_ms: int) -> list[str]:
        """Reasons a new entry is not allowed right now (empty list = clear)."""
        out: list[str] = []
        cfg = self.cfg

        if self.kill_switch:
            out.append(f"kill switch engaged ({self.kill_reason})")

        if self.day.start_equity > 0:
            loss_pct = -self.day.realized_pnl / self.day.start_equity * 100
            if loss_pct >= cfg.daily_loss_limit_pct:
                out.append(
                    f"daily loss {loss_pct:.2f}% hit the {cfg.daily_loss_limit_pct}% limit"
                )
            if cfg.daily_profit_target_pct > 0:
                gain_pct = self.day.realized_pnl / self.day.start_equity * 100
                if gain_pct >= cfg.daily_profit_target_pct:
                    out.append(
                        f"daily target {gain_pct:.2f}% reached - done for the day"
                    )

        if self.day.trades >= cfg.max_trades_per_day:
            out.append(f"{self.day.trades} trades today (max {cfg.max_trades_per_day})")

        cooldown = cfg.cooldown_bars + (cfg.loss_cooldown_bars if self.last_exit_was_loss else 0)
        if self.last_exit_was_flip and cfg.reverse_on_flip and not self.last_exit_was_loss:
            cooldown = 0          # the signal did not fade, it reversed
        if self.last_exit_bar and cooldown > 0:
            elapsed = (bar_time - self.last_exit_bar) / step_ms
            if elapsed < cooldown:
                out.append(f"cooldown: {int(cooldown - elapsed)} more bar(s) after last exit")

        if self.halt_until_bar and bar_time < self.halt_until_bar:
            out.append("halted by operator")

        self.blocked_reasons = out
        return out

    # -- sizing ------------------------------------------------------------
    def size_position(
        self,
        equity: float,
        side: Side,
        price: float,
        atr: float,
        book: BookSnapshot | None,
    ) -> SizingResult:
        cfg = self.cfg
        res = SizingResult()
        if price <= 0 or atr <= 0 or equity <= 0:
            res.reason = "no price/ATR/equity"
            return res

        res.stop_distance = cfg.stop_atr_mult * atr
        res.risk_amount = equity * cfg.risk_per_trade_pct / 100.0
        res.raw_qty = res.risk_amount / res.stop_distance
        qty = res.raw_qty
        cap = ""

        notional_cap = equity * cfg.max_position_pct / 100.0
        lev_cap = equity * cfg.leverage_cap
        hard_cap = min(notional_cap, lev_cap)
        if qty * price > hard_cap:
            qty = hard_cap / price
            cap = "notional/leverage cap"

        if book is not None and book.bids and book.asks:
            liq = max_qty_within_slippage(
                book, side, self.exec_cfg.max_slippage_bps, self.exec_cfg.max_book_levels_to_eat
            )
            # Never take more than half of the liquidity inside our budget:
            # the other half is the exit, and it has to be there too.
            res.liquidity_qty = liq * 0.5
            if qty > res.liquidity_qty:
                qty = res.liquidity_qty
                cap = "book liquidity"

        qty = self.instrument.round_qty(qty)
        floor_qty = self.instrument.smallest_tradable(price)

        if qty < floor_qty:
            if not cfg.min_lot_fallback:
                res.reason = (
                    f"risk-based size {qty:g} is below the venue minimum "
                    f"{floor_qty:g} ({self.instrument.min_notional:g} notional)"
                )
                return res
            # Take the smallest order the venue accepts, and be explicit about
            # the risk that forces onto a small bankroll.
            qty = floor_qty
            cap = "venue minimum lot"

        implied_risk = qty * res.stop_distance
        res.actual_risk_pct = implied_risk / equity * 100.0 if equity else 0.0
        if res.actual_risk_pct > cfg.max_risk_per_trade_pct:
            res.reason = (
                f"venue minimum {qty:g} (${qty * price:,.0f}) would risk "
                f"{res.actual_risk_pct:.2f}% of equity, above the "
                f"{cfg.max_risk_per_trade_pct:.2f}% ceiling"
            )
            return res

        if qty * price > hard_cap * 1.0001:
            res.reason = (
                f"venue minimum ${qty * price:,.0f} exceeds the position cap "
                f"${hard_cap:,.0f} ({cfg.leverage_cap:g}x on ${equity:,.0f})"
            )
            return res

        ok, why = self.instrument.is_tradable(qty, price)
        if not ok:
            res.reason = why
            return res

        if book is not None and book.bids and book.asks:
            est = walk_book(book, side, qty, self.exec_cfg.max_book_levels_to_eat)
            res.expected_slippage_bps = est.slippage_bps
            res.expected_cost = (
                est.notional * self.exec_cfg.taker_fee_bps / 1e4
                + abs(est.avg_price - book.mid) * qty
            )
            if est.slippage_bps > self.exec_cfg.max_slippage_bps:
                res.reason = (
                    f"entry would slip {est.slippage_bps:.1f}bps "
                    f"(budget {self.exec_cfg.max_slippage_bps}bps)"
                )
                return res

        res.qty = qty
        res.notional = qty * price
        res.leverage = res.notional / equity if equity else 0.0
        res.cap_applied = cap
        res.ok = True
        return res

    # -- reporting ---------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "kill_switch": self.kill_switch,
            "kill_reason": self.kill_reason,
            "consecutive_losses": self.consecutive_losses,
            "last_exit_bar": self.last_exit_bar,
            "last_exit_was_loss": self.last_exit_was_loss,
            "last_exit_was_flip": self.last_exit_was_flip,
            "blocked": self.blocked_reasons,
            "day": self.day.to_dict(),
            "config": {
                "risk_per_trade_pct": self.cfg.risk_per_trade_pct,
                "stop_atr_mult": self.cfg.stop_atr_mult,
                "trail_atr_mult": self.cfg.trail_atr_mult,
                "take_profit_r": self.cfg.take_profit_r,
                "daily_loss_limit_pct": self.cfg.daily_loss_limit_pct,
                "max_trades_per_day": self.cfg.max_trades_per_day,
                "leverage_cap": self.cfg.leverage_cap,
                "allow_short": self.cfg.allow_short,
                "max_risk_per_trade_pct": self.cfg.max_risk_per_trade_pct,
                "min_lot_fallback": self.cfg.min_lot_fallback,
            },
        }
