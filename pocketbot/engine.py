"""The loop: on each closed candle, settle what expired, then maybe trade.

Backtests, synthetic demos, paper-on-live-data and real demo/real accounts
all run through this same function; only the feed and broker change. That is
what makes a backtest number mean something about the live bot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .broker import Broker, Feed
from .monitor import Monitor
from .risk import RiskManager, breakeven_win_rate
from .stats import Ledger, Scorecard
from .strategy import StrategyConfig, evaluate

log = logging.getLogger("pocketbot")


@dataclass
class Engine:
    feed: Feed
    broker: Broker
    risk: RiskManager
    strategy: StrategyConfig
    asset: str
    expiry_candles: int = 3
    ledger: Ledger = field(default_factory=lambda: Ledger(None))
    scorecard: Scorecard = field(default_factory=Scorecard)
    max_trades: int | None = None          # stop after this many settled trades
    quiet: bool = False
    monitor: Monitor | None = None         # live state for the dashboard

    @property
    def duration(self) -> int:
        return self.feed.period * self.expiry_candles

    async def run(self) -> dict:
        period = self.feed.period
        mon = self.monitor
        async for candles in self.feed:
            bar = candles[-1]
            now = bar.time + period                 # the moment this candle closed
            if mon:
                mon.on_candles(candles, period)

            settled = await self.broker.settle(bar, period)
            for t in settled:
                self.risk.closed(t.pnl, now)
                self.scorecard.add(t)
                self.ledger.append(t)
                self._say("%-4s %s %s stake %.2f pnl %+.2f | %s", t.result.upper(), t.asset,
                          t.direction, t.stake, t.pnl, self._line())
            if mon and (settled or mon.balance is None):
                mon.on_settled(settled, await self.broker.balance(), now)
            if self.max_trades and len(self.scorecard.trades) >= self.max_trades:
                break

            sig = evaluate(candles, self.strategy)
            if mon:
                mon.on_signal(sig, now)
            if not sig:
                continue
            if mon and mon.paused:
                mon.on_skip("paused from the dashboard")
                continue
            payout = await self.broker.payout(self.asset)
            balance = await self.broker.balance()
            d = self.risk.check(balance, payout, now)
            if mon:
                mon.payout = payout
            if not d.allowed:
                self._say("skip %s %s: %s", sig.direction, self.asset, d.reason)
                if mon:
                    mon.on_skip(d.reason)
                continue
            try:
                t = await self.broker.place(self.asset, sig.direction, d.stake, self.duration,
                                            bar.close, now, sig.reason)
            except Exception as exc:
                log.error("order rejected: %s", exc)
                continue
            self.risk.opened()
            if mon:
                mon.on_open(t, await self.broker.balance())
            self._say("OPEN %s %s stake %.2f %ds @ %.5f payout %.0f%% (breakeven %.1f%%) - %s",
                      t.asset, t.direction.upper(), t.stake, self.duration, t.entry or 0,
                      t.payout * 100, breakeven_win_rate(t.payout) * 100, sig.reason)
        return self.scorecard.summary()

    def _line(self) -> str:
        s = self.scorecard.summary()
        return (f"{s['wins']}W/{s['losses']}L win {s['win_rate']:.1%} "
                f"(breakeven {s['breakeven']:.1%}) pnl {s['pnl']:+.2f}")

    def _say(self, msg, *args):
        if not self.quiet:
            log.info(msg, *args)
