"""Stake sizing and circuit breakers.

The one number that matters on a fixed-time trade is the payout p (0.80 means
a winning $1 returns $0.80 profit; a losing $1 is gone). Expected value per $1
staked at win rate w is  w*p - (1-w),  so the breakeven win rate is
1 / (1 + p): 55.6% at 80%, 52.9% at 89%. The payout gate below simply refuses
to trade when that bar is higher than configured.

What is *not* here, on purpose: martingale (doubling after a loss). It is the
most common "strategy" in Pocket Option bots and Telegram signal groups, and
it does not change the expected value of a single trade - it only trades many
small wins for a rare account-ending loss sequence. Stakes here are a fixed
fraction of balance and never go up after a loss.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


def breakeven_win_rate(payout: float) -> float:
    """payout as a fraction, e.g. 0.85 for 85%."""
    return 1.0 / (1.0 + payout)


def expected_value(win_rate: float, payout: float) -> float:
    """Expected profit per unit staked (draws ignored)."""
    return win_rate * payout - (1.0 - win_rate)


@dataclass
class RiskConfig:
    stake_fraction: float = 0.01      # of current balance per trade
    min_stake: float = 1.0            # broker minimum
    max_stake: float = 25.0
    min_payout: float = 0.85          # skip the asset while payout is below this
    daily_loss_limit: float = 0.05    # of the day's starting balance
    max_trades_per_day: int = 40
    max_consecutive_losses: int = 4
    cooldown_minutes: float = 30.0    # pause after the consecutive-loss stop
    max_open_trades: int = 1

    @classmethod
    def from_dict(cls, d: dict | None) -> "RiskConfig":
        d = dict(d or {})
        for banned in ("martingale", "martingale_factor", "multiplier"):
            if banned in d:
                raise ValueError(
                    f"'{banned}' is not supported: raising the stake after a loss "
                    "does not change expected value, it only adds ruin risk"
                )
        known = {k: d.pop(k) for k in list(d) if k in cls.__dataclass_fields__}
        if d:
            raise ValueError(f"unknown risk settings: {sorted(d)}")
        cfg = cls(**known)
        if not 0 < cfg.stake_fraction <= 0.05:
            raise ValueError("stake_fraction must be in (0, 0.05]")
        return cfg


@dataclass
class Decision:
    allowed: bool
    stake: float = 0.0
    reason: str = ""


@dataclass
class RiskManager:
    cfg: RiskConfig
    day: str = ""
    day_start_balance: float = 0.0
    day_pnl: float = 0.0
    day_trades: int = 0
    consecutive_losses: int = 0
    paused_until: float = 0.0
    open_trades: int = 0
    halted: str = ""                  # set when the daily limit trips
    _clock: object = field(default=time.time, repr=False)

    def _roll_day(self, balance: float, now: float) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        if today != self.day:
            self.day = today
            self.day_start_balance = balance
            self.day_pnl = 0.0
            self.day_trades = 0
            self.halted = ""

    def check(self, balance: float, payout: float | None, now: float | None = None) -> Decision:
        now = self._clock() if now is None else now
        self._roll_day(balance, now)
        c = self.cfg
        if self.halted:
            return Decision(False, reason=self.halted)
        if now < self.paused_until:
            return Decision(False, reason=f"cooling down {int(self.paused_until - now)}s "
                                          f"after {c.max_consecutive_losses} losses in a row")
        if self.open_trades >= c.max_open_trades:
            return Decision(False, reason="a trade is already open")
        if self.day_trades >= c.max_trades_per_day:
            return Decision(False, reason=f"daily trade cap {c.max_trades_per_day} reached")
        if payout is None:
            return Decision(False, reason="payout unknown")
        if payout < c.min_payout:
            return Decision(False, reason=f"payout {payout:.0%} < {c.min_payout:.0%} "
                                          f"(needs {breakeven_win_rate(payout):.1%} wins to break even)")
        stake = round(min(c.max_stake, max(c.min_stake, balance * c.stake_fraction)), 2)
        if stake > balance:
            return Decision(False, reason=f"balance {balance:.2f} below minimum stake")
        # Refuse a trade whose loss would breach the daily limit.
        limit = self.day_start_balance * c.daily_loss_limit
        if -(self.day_pnl - stake) > limit:
            return Decision(False, reason=f"a loss here would breach the daily limit ({limit:.2f})")
        return Decision(True, stake)

    def opened(self) -> None:
        self.open_trades += 1
        self.day_trades += 1

    def closed(self, pnl: float, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        self.open_trades = max(0, self.open_trades - 1)
        self.day_pnl += pnl
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.cfg.max_consecutive_losses:
                self.paused_until = now + self.cfg.cooldown_minutes * 60
                self.consecutive_losses = 0
        elif pnl > 0:
            self.consecutive_losses = 0
        limit = self.day_start_balance * self.cfg.daily_loss_limit
        if self.day_pnl <= -limit:
            self.halted = f"daily loss limit hit ({self.day_pnl:.2f}); done for the day"
