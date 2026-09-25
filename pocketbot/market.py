"""Candles, CSV recordings and an offline synthetic market.

Pocket Option quotes are plain OHLC with no volume, so the candle type here is
deliberately smaller than flowbot's: time is the candle's open, in Unix
seconds, which is what BinaryOptionsToolsV2's live feed yields.
"""

from __future__ import annotations

import csv
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


@dataclass(frozen=True)
class Candle:
    time: int          # open time, Unix seconds
    open: float
    high: float
    low: float
    close: float

    @classmethod
    def from_dict(cls, d: dict) -> "Candle":
        return cls(int(d["time"]), float(d["open"]), float(d["high"]),
                   float(d["low"]), float(d["close"]))


FIELDS = ("time", "open", "high", "low", "close")


def save_csv(path: str | Path, candles: Iterable[Candle]) -> int:
    n = 0
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        for c in candles:
            w.writerow([c.time, c.open, c.high, c.low, c.close])
            n += 1
    return n


def load_csv(path: str | Path) -> list[Candle]:
    with open(path, newline="") as fh:
        rows = [Candle.from_dict(r) for r in csv.DictReader(fh)]
    rows.sort(key=lambda c: c.time)
    return rows


def synthetic_candles(n: int, period: int = 60, seed: int | None = None,
                      start_price: float = 1.1000, start_time: int = 1_700_000_000,
                      vol: float = 0.00025) -> Iterator[Candle]:
    """A driftless random walk with regime-switching volatility.

    It has no edge in it by construction, which is the point: a strategy that
    "wins" on this is overfitting noise, and the backtest should say so.
    """
    rng = random.Random(seed)
    price = start_price
    t = start_time - start_time % period
    sigma = vol
    for _ in range(n):
        if rng.random() < 0.02:
            sigma = vol * rng.choice((0.5, 1.0, 1.0, 2.0))
        o = price
        hi = lo = o
        steps = 12
        for _ in range(steps):
            price *= math.exp(rng.gauss(0.0, sigma / math.sqrt(steps)))
            hi = max(hi, price)
            lo = min(lo, price)
        yield Candle(t, round(o, 6), round(hi, 6), round(lo, 6), round(price, 6))
        t += period
