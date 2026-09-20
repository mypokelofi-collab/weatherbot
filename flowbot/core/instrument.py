"""Instrument metadata - the venue's real trading rules.

Paper trading is only honest if the orders we pretend to send would have been
accepted: prices on the tick grid, quantities on the lot grid, notional above
the venue minimum. These values are pulled from the venue at boot
(exchangeInfo / products) and fall back to sane BTC defaults offline.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def _decimals(step: float) -> int:
    if step <= 0:
        return 8
    d = 0
    while step < 1 and d < 12:
        step *= 10
        d += 1
        if abs(step - round(step)) < 1e-9:
            break
    return d


@dataclass
class Instrument:
    symbol: str = "BTCUSDT"
    base: str = "BTC"
    quote: str = "USDT"
    tick_size: float = 0.01
    step_size: float = 0.00001
    min_qty: float = 0.00001
    min_notional: float = 5.0
    contract: str = "spot"           # spot | perp

    def round_price(self, price: float, side_up: bool = False) -> float:
        if self.tick_size <= 0:
            return price
        n = price / self.tick_size
        n = math.ceil(n) if side_up else math.floor(n)
        return round(n * self.tick_size, _decimals(self.tick_size))

    def round_qty(self, qty: float) -> float:
        if self.step_size <= 0:
            return qty
        n = math.floor(qty / self.step_size)
        return round(n * self.step_size, _decimals(self.step_size))

    def round_qty_up(self, qty: float) -> float:
        if self.step_size <= 0:
            return qty
        # Guard against float dust turning 0.002 into 0.003.
        n = math.ceil(round(qty / self.step_size, 9))
        return round(n * self.step_size, _decimals(self.step_size))

    def smallest_tradable(self, price: float) -> float:
        """The smallest order this venue will accept at `price`.

        Both filters bind: the lot grid and the minimum notional. On Binance
        USDⓈ-M BTCUSDT that is 0.001 BTC *and* $100, so the real floor is
        whichever is larger, rounded up to the grid.
        """
        if price <= 0:
            return self.min_qty
        by_notional = self.min_notional / price
        return self.round_qty_up(max(self.min_qty, by_notional))

    def is_tradable(self, qty: float, price: float) -> tuple[bool, str]:
        if qty < self.min_qty:
            return False, f"qty {qty} below venue min {self.min_qty}"
        if qty * price < self.min_notional:
            return False, f"notional {qty * price:.2f} below venue min {self.min_notional}"
        return True, ""

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "base": self.base,
            "quote": self.quote,
            "tick_size": self.tick_size,
            "step_size": self.step_size,
            "min_qty": self.min_qty,
            "min_notional": self.min_notional,
            "contract": self.contract,
        }
