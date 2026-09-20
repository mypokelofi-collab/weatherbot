"""Offline market simulator - NOT REAL DATA.

This exists for one reason: so the bot, the fill engine, the tests and the
dashboard can all be exercised in an environment with no exchange
connectivity (CI, a locked-down container, a plane). Everything it produces is
synthetic and it reports `real = False`, which the dashboard renders as a loud
banner so nobody ever mistakes a demo run for a live one.

The generator is deliberately microstructural rather than a toy random walk:
a regime-switching drift drives both the price diffusion and the aggressor
probability, so order-flow imbalance, CVD and price momentum are correlated
the way they are in a real tape. Book depth decays with distance from the mid
and skews with the prevailing flow. It is good enough to prove plumbing and
catch logic bugs; it is not a market model and no result from it means
anything about live profitability.
"""

from __future__ import annotations

import asyncio
import math
import random
import time

from ..core.clock import interval_ms
from ..core.instrument import Instrument
from ..core.types import Candle, Side, Trade
from .feed import MarketFeed

# Regime -> (drift per second, persistence in seconds)
# Drift is kept small relative to diffusion on purpose. Real BTC 15m bars
# have a drift/noise ratio well under 0.3 even in a strong trend; an
# overcooked simulator would make the strategy look far better than it is.
REGIMES = [
    ("strong_up", 1.0e-6),
    ("up", 0.45e-6),
    ("flat", 0.0),
    ("down", -0.45e-6),
    ("strong_down", -1.0e-6),
]
MAX_DRIFT = 1.0e-6


class SimulatedFeed(MarketFeed):
    venue = "simulator"
    real = False
    realtime = False        # it runs its own accelerated clock

    def __init__(
        self,
        symbol: str = "BTCUSDT",
        start_price: float = 64000.0,
        speed: float = 60.0,
        seed: int | None = 7,
        tick_wall_s: float = 0.05,
        trades_per_sec: float = 2.5,
        vol_daily: float = 0.028,
        book_levels: int = 40,
    ) -> None:
        super().__init__(symbol=symbol, book_levels=book_levels)
        self.health.venue = self.venue
        self.health.real = False
        self.instrument = Instrument(
            symbol=symbol, tick_size=0.1, step_size=0.001,
            min_qty=0.001, min_notional=5.0, contract="perp",
        )
        self.rng = random.Random(seed)
        self.speed = speed
        self.tick_wall_s = tick_wall_s
        self.trades_per_sec = trades_per_sec
        self.sigma_s = vol_daily / math.sqrt(86400)
        self.book_levels = book_levels
        self.mid = start_price
        self.sim_ts = int(time.time() * 1000)
        self.regime_idx = 2
        self.regime_left = 0.0
        self.cum_delta = 0.0

    # -- price process -----------------------------------------------------
    @property
    def drift(self) -> float:
        return REGIMES[self.regime_idx][1]

    @property
    def regime_name(self) -> str:
        return REGIMES[self.regime_idx][0]

    def _maybe_switch_regime(self, dt_s: float) -> None:
        self.regime_left -= dt_s
        if self.regime_left > 0:
            return
        # Markov-ish: mostly step to an adjacent regime, occasionally jump.
        if self.rng.random() < 0.75:
            # Pull toward the flat regime so trends decay instead of compounding
            # into a one-way market that no real tape produces.
            pull = 0 if self.regime_idx == 2 else (1 if self.regime_idx < 2 else -1)
            step = pull if self.rng.random() < 0.6 else self.rng.choice([-1, 1])
            self.regime_idx = max(0, min(len(REGIMES) - 1, self.regime_idx + step))
        else:
            self.regime_idx = self.rng.randrange(len(REGIMES))
        # 12-50 simulated minutes per regime: long enough for a 15m bot to act.
        self.regime_left = self.rng.uniform(720, 3000)

    def _advance_price(self, dt_s: float) -> None:
        self._maybe_switch_regime(dt_s)
        shock = self.rng.gauss(0.0, 1.0)
        # Rare fat-tailed jump, ~ once per simulated day.
        if self.rng.random() < dt_s / 86400 * 6:
            shock += self.rng.choice([-1, 1]) * self.rng.uniform(4, 9)
        ret = self.drift * dt_s + self.sigma_s * math.sqrt(dt_s) * shock
        self.mid = max(100.0, self.mid * math.exp(ret))

    # -- book --------------------------------------------------------------
    def _rebuild_book(self) -> None:
        tick = self.instrument.tick_size
        half_spread = tick * self.rng.choice([0.5, 0.5, 0.5, 1.5, 2.5])
        best_bid = math.floor((self.mid - half_spread) / tick) * tick
        best_ask = math.ceil((self.mid + half_spread) / tick) * tick
        if best_ask <= best_bid:
            best_ask = best_bid + tick

        # Depth skew follows the flow regime: trending tapes really do show a
        # thicker resting book on the side the market is leaning toward.
        skew = 1.0 + 2.5 * (self.drift / MAX_DRIFT) * 0.15
        bids: list[tuple[float, float]] = []
        asks: list[tuple[float, float]] = []
        for i in range(self.book_levels):
            depth_factor = 1.0 / (1.0 + 0.55 * i) + 0.12
            base = 1.4 * depth_factor
            bq = base * skew * math.exp(self.rng.gauss(0, 0.35))
            aq = base / skew * math.exp(self.rng.gauss(0, 0.35))
            gap = tick * (1 + int(i * 1.5))
            bids.append((round(best_bid - gap + tick, 1), round(bq, 3)))
            asks.append((round(best_ask + gap - tick, 1), round(aq, 3)))
        self.book.apply_snapshot(bids, asks, seq=self.book.seq + 1, ts=self.sim_ts)

    # -- main loop ---------------------------------------------------------
    async def run(self) -> None:
        self.health.connected = True
        self.health.connected_since = int(time.time() * 1000)
        self._emit_status("connected")
        self._rebuild_book()
        trade_id = 0

        while not self._stopping:
            dt_s = self.tick_wall_s * self.speed
            self.sim_ts += int(dt_s * 1000)
            self._advance_price(dt_s)
            self._rebuild_book()
            self._emit_book(self.book.snapshot(25))

            # Prints are stamped inside the interval that just elapsed, so the
            # book snapshot above is always the most recent event. Without
            # this, a high `speed` makes every book look seconds stale to the
            # fill engine and nothing ever fills.
            n_trades = self._poisson(self.trades_per_sec * dt_s)
            lean = self.drift / MAX_DRIFT                     # -1..1
            p_buy = min(0.92, max(0.08, 0.5 + 0.22 * lean + self.rng.gauss(0, 0.05)))
            for _ in range(n_trades):
                trade_id += 1
                is_buy = self.rng.random() < p_buy
                side = Side.BUY if is_buy else Side.SELL
                price = self.book.best_ask if is_buy else self.book.best_bid
                qty = round(math.exp(self.rng.gauss(math.log(0.04), 1.0)), 3)
                qty = max(self.instrument.min_qty, min(qty, 8.0))
                self.cum_delta += qty if is_buy else -qty
                self._emit_trade(Trade(
                    ts=self.sim_ts - self.rng.randrange(0, max(1, int(dt_s * 1000))),
                    price=price, qty=qty, side=side, trade_id=trade_id,
                ))
            await asyncio.sleep(self.tick_wall_s)

    def _poisson(self, lam: float) -> int:
        if lam <= 0:
            return 0
        if lam > 30:                                  # normal approximation
            return max(0, int(self.rng.gauss(lam, math.sqrt(lam))))
        l, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= l:
                return k
            k += 1
            if k > 500:
                return k

    # -- history -----------------------------------------------------------
    async def backfill_candles(self, interval: str, limit: int = 400) -> list[Candle]:
        """Generate a synthetic past consistent with the live path."""
        step = interval_ms(interval)
        step_s = step / 1000
        now = int(time.time() * 1000)
        start = (now - (now % step)) - step * limit
        price = self.mid * math.exp(-self.rng.gauss(0, 0.02))
        out: list[Candle] = []
        regime = 2
        left = 0.0
        for i in range(limit):
            left -= step_s
            if left <= 0:
                pull = 0 if regime == 2 else (1 if regime < 2 else -1)
                move = pull if self.rng.random() < 0.6 else self.rng.choice([-1, 0, 1])
                regime = max(0, min(4, regime + move))
                left = self.rng.uniform(720, 3000)
            drift = REGIMES[regime][1]
            o = price
            hi = lo = o
            buy_v = sell_v = 0.0
            # 15 intra-bar steps keep the high/low geometry believable.
            for _ in range(15):
                sub = step_s / 15
                price *= math.exp(drift * sub + self.sigma_s * math.sqrt(sub) * self.rng.gauss(0, 1))
                hi = max(hi, price)
                lo = min(lo, price)
                lean = drift / MAX_DRIFT
                v = abs(self.rng.gauss(18, 7))
                bfrac = min(0.9, max(0.1, 0.5 + 0.18 * lean))
                buy_v += v * bfrac
                sell_v += v * (1 - bfrac)
            ot = start + i * step
            out.append(Candle(
                open_time=ot, close_time=ot + step,
                open=round(o, 1), high=round(hi, 1), low=round(lo, 1), close=round(price, 1),
                volume=round(buy_v + sell_v, 3),
                quote_volume=round((buy_v + sell_v) * price, 2),
                trades=int((buy_v + sell_v) * 3),
                buy_volume=round(buy_v, 3), sell_volume=round(sell_v, 3),
                closed=True,
            ))
        self.mid = price
        return out
