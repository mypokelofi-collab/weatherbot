"""Shared fixtures.

The builders here produce deterministic market data - a book with known depth,
a tape with a known trend - so assertions can be about exact numbers rather
than "roughly". Randomness in tests is how flaky suites start.
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

import pytest

from flowbot.core.config import AppConfig, ExecConfig
from flowbot.core.instrument import Instrument
from flowbot.core.types import BookLevel, BookSnapshot, Candle, Side, Trade

STEP_MS = 900_000


@pytest.fixture
def instrument() -> Instrument:
    return Instrument(
        symbol="BTCUSDT", tick_size=0.1, step_size=0.001,
        min_qty=0.001, min_notional=5.0, contract="perp",
    )


def make_book(
    ts: int = 1_000,
    mid: float = 64_000.0,
    size: float = 0.5,
    levels: int = 25,
    tick: float = 0.1,
    skew: float = 1.0,
) -> BookSnapshot:
    """A symmetric book unless `skew` is given (>1 means bid-heavy)."""
    bids = [BookLevel(round(mid - tick / 2 - i * tick, 1), round(size * skew, 6)) for i in range(levels)]
    asks = [BookLevel(round(mid + tick / 2 + i * tick, 1), round(size / skew, 6)) for i in range(levels)]
    return BookSnapshot(ts=ts, bids=bids, asks=asks, seq=ts)


@pytest.fixture
def book() -> BookSnapshot:
    return make_book()


@pytest.fixture
def book_factory():
    return make_book


def make_candles(
    n: int = 120,
    start: float = 60_000.0,
    drift: float = 0.0,
    vol: float = 0.004,
    seed: int = 3,
    start_ts: int = 1_600_000_000_000,
    buy_bias: float = 0.5,
) -> list[Candle]:
    """Synthetic bars with a controllable trend, for signal tests."""
    rng = random.Random(seed)
    out: list[Candle] = []
    price = start
    ts = start_ts - (start_ts % STEP_MS)
    for i in range(n):
        o = price
        hi = lo = o
        for _ in range(8):
            price *= math.exp(drift / 8 + vol / math.sqrt(8) * rng.gauss(0, 1))
            hi = max(hi, price)
            lo = min(lo, price)
        vol_total = 100 + rng.random() * 20
        out.append(Candle(
            open_time=ts + i * STEP_MS,
            close_time=ts + (i + 1) * STEP_MS,
            open=round(o, 1), high=round(hi, 1), low=round(lo, 1), close=round(price, 1),
            volume=vol_total, quote_volume=vol_total * price,
            buy_volume=vol_total * buy_bias, sell_volume=vol_total * (1 - buy_bias),
            trades=int(vol_total), closed=True,
        ))
    return out


@pytest.fixture
def candles() -> list[Candle]:
    return make_candles()


@pytest.fixture
def exec_cfg() -> ExecConfig:
    return ExecConfig(latency_ms=0, limit_timeout_s=30, taker_fee_bps=4.5, maker_fee_bps=1.8)


def write_recording(
    path: Path,
    bars: int = 180,
    drift_per_bar: float = 0.003,
    seed: int = 5,
    start_price: float = 60_000.0,
    trades_per_bar: int = 40,
    noise: float = 0.0006,
) -> Path:
    """Write a deterministic recording in the on-disk replay format.

    The path trends hard, reverses at the halfway mark, and carries an
    aggressor bias that matches the direction. That is deliberately more
    tradeable than a real tape: the fixture exists to drive the machinery
    through entries, partials, trails and reversals, not to say anything
    about the strategy's edge. Book snapshots are written frequently so the
    fill engine sees fresh depth, as it would live.
    """
    rng = random.Random(seed)
    ts = 1_600_000_000_000
    ts -= ts % STEP_MS
    price = start_price
    lines = [json.dumps({
        "k": "meta", "venue": "fixture", "symbol": "BTCUSDT", "interval": "15m",
        "instrument": Instrument(symbol="BTCUSDT", tick_size=0.1, step_size=0.001,
                                 min_qty=0.001, min_notional=5.0, contract="perp").to_dict(),
        "real": False,
    })]
    trade_id = 0
    for b in range(bars):
        # Trend reverses halfway so the fixture exercises both sides.
        drift = drift_per_bar if b < bars * 0.55 else -drift_per_bar
        for k in range(trades_per_bar):
            t = ts + b * STEP_MS + int(k * STEP_MS / trades_per_bar)
            price *= math.exp(drift / trades_per_bar + noise * rng.gauss(0, 1))
            price = round(price, 1)
            trade_id += 1
            buy = rng.random() < (0.5 + (0.18 if drift > 0 else -0.18))
            lines.append(json.dumps({
                "k": "t", "ts": t, "p": price,
                "q": round(abs(rng.gauss(0.08, 0.05)) + 0.001, 3),
                "s": "buy" if buy else "sell", "i": trade_id,
            }))
            if k % 3 == 0:
                bids = [[round(price - 0.05 - i * 0.1, 2), 0.6] for i in range(25)]
                asks = [[round(price + 0.05 + i * 0.1, 2), 0.6] for i in range(25)]
                lines.append(json.dumps({"k": "s", "ts": t, "seq": trade_id, "b": bids, "a": asks}))
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture
def recording(tmp_path: Path) -> Path:
    return write_recording(tmp_path / "fixture.jsonl")


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.state_dir = str(tmp_path / "state")
    cfg.data.venue = "simulator"
    cfg.data.backfill_bars = 150
    cfg.server.port = 0
    return cfg
