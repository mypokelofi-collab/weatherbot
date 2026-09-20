"""Position management: what happens after the entry fills.

The signal decides when to be in. This decides how to stay in, and it runs on
every print rather than every bar - a stop that only checks at bar close is a
stop that gets hit by 40 basis points more than it should.

The ladder, in the order it is checked:

  1. hard stop      - entry ∓ stop_atr_mult · ATR(entry). Never widened.
  2. trailing stop  - chandelier: extreme price since entry ∓ trail_atr_mult ·
                      ATR(now). Armed only once the trade has paid for itself.
  3. runner target  - a hard ceiling on greed at runner_exit_r.
  4. partial target - takes half off at take_profit_r and moves the stop to
                      breakeven, which is what turns "in profit" into "can
                      never lose on this trade".
  5. time stop      - momentum that has not worked in max_bars_in_trade bars
                      is not momentum; the capital is better off waiting for
                      the next signal.

Everything returns an *intent* (qty, reason, urgency); the trader is the only
thing allowed to send orders.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..core.config import RiskConfig
from ..core.types import Position, Side

log = logging.getLogger(__name__)


def effective_stop(position: Position) -> float:
    """The stop actually in force: the tighter of the hard stop and the trail."""
    if position is None:
        return 0.0
    if not position.trail:
        return position.stop
    if not position.stop:
        return position.trail
    return (
        max(position.stop, position.trail) if position.side is Side.BUY
        else min(position.stop, position.trail)
    )


@dataclass
class ExitIntent:
    qty: float
    reason: str
    urgency: str = "urgent"
    kind: str = "exit"          # exit | partial

    def to_dict(self) -> dict:
        return {"qty": self.qty, "reason": self.reason,
                "urgency": self.urgency, "kind": self.kind}


class PositionManager:
    def __init__(self, cfg: RiskConfig) -> None:
        self.cfg = cfg
        self.extreme_price: float = 0.0     # best price seen since entry
        self.partial_done = False
        self.stop_moves: list[tuple[int, float, str]] = []

    def reset(self, position: Position) -> None:
        self.extreme_price = position.entry_price
        self.partial_done = False
        self.stop_moves = [(position.entry_ts, position.stop, "initial")]

    # -- per-tick ----------------------------------------------------------
    def update(
        self, position: Position, price: float, atr: float, ts: int
    ) -> list[ExitIntent]:
        if position is None or price <= 0:
            return []

        long = position.side is Side.BUY
        self.extreme_price = (
            max(self.extreme_price or price, price) if long
            else min(self.extreme_price or price, price)
        )

        intents: list[ExitIntent] = []
        r = position.unrealized_r(price)

        # 1. Hard / trailing stop. Checked against the live price, and exits
        #    go out as market orders: getting out beats getting a good price.
        #    The level in force is always the *tighter* of the two - a trail
        #    armed early must never loosen a stop that has already moved up to
        #    breakeven.
        stop_level = effective_stop(position)
        if (long and price <= stop_level) or (not long and price >= stop_level):
            if position.trail and abs(stop_level - position.trail) < 1e-9:
                reason = "trailing stop"
            elif position.breakeven_armed:
                reason = "breakeven stop"
            else:
                reason = "stop loss"
            return [ExitIntent(position.qty, f"{reason} @ {stop_level:.2f}", "urgent")]

        # 2. Runner take-profit.
        if self.cfg.runner_exit_r > 0 and r >= self.cfg.runner_exit_r:
            return [ExitIntent(position.qty, f"runner target {r:.2f}R", "normal")]

        # 3. Breakeven: once the trade has made 1R it is not allowed to become
        #    a loser. The stop moves to entry plus the round-trip fee.
        if (
            not position.breakeven_armed
            and self.cfg.breakeven_at_r > 0
            and r >= self.cfg.breakeven_at_r
        ):
            position.breakeven_armed = True
            be = position.entry_price * (1 + 0.0006 * position.side.sign)
            if (long and be > position.stop) or (not long and be < position.stop):
                position.stop = be
                self.stop_moves.append((ts, be, "breakeven"))
                log.info("stop moved to breakeven @ %.2f (%.2fR)", be, r)

        # 4. Partial take-profit: bank half, let the rest run with the trend.
        if (
            not self.partial_done
            and self.cfg.take_profit_r > 0
            and self.cfg.partial_exit_pct > 0
            and r >= self.cfg.take_profit_r
        ):
            qty = position.qty * self.cfg.partial_exit_pct / 100.0
            if qty > 0:
                self.partial_done = True
                position.scaled_out = True
                intents.append(ExitIntent(
                    qty, f"partial {self.cfg.partial_exit_pct:.0f}% at {r:.2f}R",
                    "normal", kind="partial",
                ))

        # 5. Trail: chandelier off the extreme, armed once we are in profit.
        if atr > 0 and self.cfg.trail_atr_mult > 0 and r >= max(0.5, self.cfg.breakeven_at_r * 0.5):
            candidate = (
                self.extreme_price - self.cfg.trail_atr_mult * atr if long
                else self.extreme_price + self.cfg.trail_atr_mult * atr
            )
            floor = effective_stop(position) or candidate
            better = candidate > floor if long else candidate < floor
            if better:
                position.trail = candidate
                self.stop_moves.append((ts, candidate, "trail"))

        return intents

    # -- per-bar -----------------------------------------------------------
    def on_bar_close(self, position: Position, price: float) -> list[ExitIntent]:
        if position is None:
            return []
        position.bars_held += 1
        if (
            self.cfg.max_bars_in_trade > 0
            and position.bars_held >= self.cfg.max_bars_in_trade
        ):
            return [ExitIntent(
                position.qty,
                f"time stop after {position.bars_held} bars",
                "normal",
            )]
        return []

    def can_exit_on_signal(self, position: Position) -> bool:
        """Do not let a one-bar wobble close a trade that just opened."""
        return position.bars_held >= self.cfg.min_bars_in_trade

    def to_dict(self, position: Position | None) -> dict:
        return {
            "extreme_price": self.extreme_price,
            "partial_done": self.partial_done,
            "stop_moves": [
                {"ts": ts, "level": round(level, 2), "why": why}
                for ts, level, why in self.stop_moves[-12:]
            ],
            "effective_stop": effective_stop(position) if position else 0.0,
        }
