"""Indicator maths. Values are checked against hand-computable cases."""

from __future__ import annotations

import math

import pytest

from flowbot.core.types import Candle
from flowbot.signals import indicators as ind


def bars(prices, highs=None, lows=None):
    out = []
    for i, p in enumerate(prices):
        h = highs[i] if highs else p + 1
        l = lows[i] if lows else p - 1
        out.append(Candle(open_time=i * 900_000, close_time=(i + 1) * 900_000,
                          open=p, high=h, low=l, close=p))
    return out


def test_sma_and_ema_seed_and_shape():
    vals = [float(i) for i in range(1, 21)]
    sma = ind.sma(vals, 5)
    assert sma[:4] == [None] * 4
    assert sma[4] == pytest.approx(3.0)          # mean of 1..5
    ema = ind.ema(vals, 5)
    assert ema[4] == pytest.approx(3.0)          # seeded with the SMA
    # On a perfectly linear ramp both converge to price - (period-1)/2, and
    # both lag the price itself.
    assert sma[-1] == pytest.approx(18.0)
    assert ema[-1] == pytest.approx(18.0, abs=0.01)
    assert ema[-1] < vals[-1]


def test_wilder_matches_manual_recursion():
    vals = [1, 2, 3, 4, 5, 6]
    out = ind.wilder(vals, 3)
    assert out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx((2.0 * 2 + 4) / 3)
    assert out[4] == pytest.approx((out[3] * 2 + 5) / 3)


def test_rsi_bounds():
    up = [float(i) for i in range(1, 40)]
    assert ind.last(ind.rsi(up, 14)) == pytest.approx(100.0)
    down = list(reversed(up))
    assert ind.last(ind.rsi(down, 14)) == pytest.approx(0.0, abs=1e-9)
    flat = [100.0] * 40
    assert ind.last(ind.rsi(flat, 14), 50.0) in (50.0, 100.0)   # no movement


def test_true_range_uses_previous_close():
    cs = [
        Candle(open_time=0, close_time=1, open=10, high=12, low=9, close=11),
        Candle(open_time=1, close_time=2, open=11, high=20, low=18, close=19),
    ]
    tr = ind.true_range(cs)
    assert tr[0] == 3                      # first bar: high - low
    assert tr[1] == 9                      # gap up: high - previous close


def test_atr_on_constant_range():
    cs = bars([100] * 40)                  # every bar has range 2
    assert ind.last(ind.atr(cs, 14)) == pytest.approx(2.0)


def test_adx_perfect_trend_is_maximal():
    cs = bars([100 + i for i in range(60)])
    adx, pdi, mdi = ind.adx(cs, 14)
    assert ind.last(adx) == pytest.approx(100.0)
    assert ind.last(pdi) > ind.last(mdi)
    assert ind.last(mdi) == pytest.approx(0.0)


def test_adx_is_low_in_chop():
    prices = [100 + (2 if i % 2 else -2) for i in range(80)]
    adx, _, _ = ind.adx(bars(prices), 14)
    assert ind.last(adx) < 30


def test_macd_zero_on_flat_series():
    line, sig, hist = ind.macd([100.0] * 80)
    assert ind.last(line) == pytest.approx(0.0)
    assert ind.last(hist) == pytest.approx(0.0)


def test_donchian_excludes_the_current_bar():
    cs = bars([100] * 20 + [120])
    up, dn = ind.donchian(cs, 20)
    assert up[-1] == 101                   # prior 20 highs, not the breakout bar
    assert cs[-1].close > up[-1]


def test_zscore_and_slope():
    vals = [float(i) for i in range(30)]
    assert ind.slope(vals, 10) == pytest.approx(1.0)
    z = ind.zscore(vals, 10)
    assert z[-1] > 1.4                     # last point of a ramp is high in its window


def test_realized_vol_scales_with_noise():
    calm = [100 * (1 + 0.0001 * ((-1) ** i)) for i in range(100)]
    wild = [100 * (1 + 0.01 * ((-1) ** i)) for i in range(100)]
    assert ind.realized_vol(wild, 50) > ind.realized_vol(calm, 50) * 10


def test_squash_and_clamp_are_bounded():
    for v in (-1e6, -3, 0, 3, 1e6):
        assert -1.0 <= ind.squash(v) <= 1.0
        assert -1.0 <= ind.clamp(v) <= 1.0
    assert ind.squash(0) == 0.0
    assert math.isclose(ind.squash(1, 1), math.tanh(1))


def test_percentile_rank():
    assert ind.percentile_rank([1, 2, 3, 4], 0) == 0.0
    assert ind.percentile_rank([1, 2, 3, 4], 5) == 1.0
    assert ind.percentile_rank([1, 2, 3, 4], 2.5) == 0.5
