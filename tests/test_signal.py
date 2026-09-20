"""Signal engine: does it fire where it should, and stay out where it shouldn't."""

from __future__ import annotations

import pytest

from flowbot.core.config import SignalConfig
from flowbot.core.types import Regime, Side, SignalAction
from flowbot.signals.engine import SignalEngine
from flowbot.signals.features import TapeWindow, compute_features
from flowbot.signals.momentum import classify_regime, composite_score, score_components
from tests.conftest import make_book, make_candles


def deep_book():
    return make_book(mid=64_000, size=3.0, levels=25)


def test_features_describe_an_uptrend():
    cs = make_candles(n=150, drift=0.006, vol=0.002, buy_bias=0.68)
    f = compute_features(cs, SignalConfig(), deep_book())
    assert f.ema_spread_atr > 0
    assert f.rsi > 55
    assert f.adx > 18
    assert f.bar_delta_ratio > 0
    assert f.atr > 0 and f.atr_pct > 0


def test_composite_is_positive_in_an_uptrend_and_negative_down():
    cfg = SignalConfig()
    up = compute_features(make_candles(n=150, drift=0.006, vol=0.002, buy_bias=0.7), cfg, deep_book())
    down = compute_features(make_candles(n=150, drift=-0.006, vol=0.002, buy_bias=0.3), cfg, deep_book())
    assert composite_score(score_components(up, cfg)) > 0.3
    assert composite_score(score_components(down, cfg)) < -0.3


def test_components_are_bounded_and_weighted():
    cfg = SignalConfig()
    f = compute_features(make_candles(n=150, drift=0.02, vol=0.001), cfg, deep_book())
    comps = score_components(f, cfg)
    assert {c.name for c in comps} == set(cfg.weights)
    for c in comps:
        assert -1.0 <= c.score <= 1.0
        assert c.note
    assert sum(c.weight for c in comps) == pytest.approx(1.0)
    assert -1.0 <= composite_score(comps) <= 1.0


def test_entry_fires_in_a_clean_trend():
    engine = SignalEngine(SignalConfig())
    cs = make_candles(n=150, drift=0.006, vol=0.002, buy_bias=0.7)
    sig = engine.evaluate(cs, deep_book(), TapeWindow(), position_side=None)
    assert sig.action is SignalAction.ENTER_LONG
    assert sig.regime is Regime.TREND_UP
    assert sig.blockers == []
    assert sig.reasons


def test_chop_blocks_entries():
    engine = SignalEngine(SignalConfig())
    cs = make_candles(n=150, drift=0.0, vol=0.0015, seed=11)
    sig = engine.evaluate(cs, deep_book(), TapeWindow(), position_side=None)
    assert sig.action is not SignalAction.ENTER_LONG or sig.blockers
    if sig.regime is Regime.CHOP:
        assert any("ADX" in b or "disagree" in b for b in sig.blockers)


def test_wide_spread_marks_the_book_illiquid():
    engine = SignalEngine(SignalConfig(max_spread_bps=0.5))
    cs = make_candles(n=150, drift=0.006, vol=0.002)
    wide = make_book(mid=64_000, size=3.0, tick=20.0)
    sig = engine.evaluate(cs, wide, TapeWindow(), position_side=None)
    assert sig.regime is Regime.ILLIQUID
    assert any("spread" in b for b in sig.blockers)
    assert sig.action is SignalAction.NONE


def test_thin_book_blocks_entry():
    engine = SignalEngine(SignalConfig(min_depth_usd=10_000_000))
    cs = make_candles(n=150, drift=0.006, vol=0.002)
    sig = engine.evaluate(cs, make_book(size=0.01), TapeWindow(), position_side=None)
    assert any("resting" in b for b in sig.blockers)


def test_dead_volatility_blocks_entry():
    engine = SignalEngine(SignalConfig(min_atr_pct=5.0))
    cs = make_candles(n=150, drift=0.006, vol=0.002)
    sig = engine.evaluate(cs, deep_book(), TapeWindow(), position_side=None)
    assert any("too small to pay costs" in b for b in sig.blockers)


def test_warmup_blocks_until_enough_bars():
    engine = SignalEngine(SignalConfig(warmup_bars=120))
    cs = make_candles(n=60, drift=0.006, vol=0.002)
    sig = engine.evaluate(cs, deep_book(), TapeWindow(), position_side=None)
    assert any("warming up" in b for b in sig.blockers)


def test_holding_a_winner_keeps_holding():
    engine = SignalEngine(SignalConfig())
    cs = make_candles(n=150, drift=0.006, vol=0.002, buy_bias=0.7)
    sig = engine.evaluate(cs, deep_book(), TapeWindow(), position_side=Side.BUY)
    assert sig.action is SignalAction.HOLD


def test_momentum_decay_exits_a_long():
    engine = SignalEngine(SignalConfig(exit_threshold=0.9))   # anything short of huge exits
    cs = make_candles(n=150, drift=0.001, vol=0.003, seed=7)
    sig = engine.evaluate(cs, deep_book(), TapeWindow(), position_side=Side.BUY)
    assert sig.action is SignalAction.EXIT
    assert "momentum" in sig.reasons[0]


def test_flip_exits_the_opposite_position():
    engine = SignalEngine(SignalConfig())
    cs = make_candles(n=150, drift=-0.006, vol=0.002, buy_bias=0.3)
    sig = engine.evaluate(cs, deep_book(), TapeWindow(), position_side=Side.BUY)
    assert sig.action is SignalAction.EXIT
    assert "flipped" in sig.reasons[0]


def test_counter_trend_score_is_blocked_by_alignment():
    cfg = SignalConfig(require_trend_alignment=True)
    engine = SignalEngine(cfg)
    cs = make_candles(n=150, drift=-0.004, vol=0.002, seed=4)
    # Force a long-ish composite by making the book and tape very bullish
    # while price is below the slow EMA.
    sig = engine.evaluate(cs, make_book(mid=64_000, size=3.0, skew=6.0), TapeWindow(), None)
    if sig.score > 0:
        assert any("below the slow EMA" in b for b in sig.blockers)


def test_tape_window_stats_and_eviction():
    from flowbot.core.types import Trade

    tw = TapeWindow(window_ms=1_000)
    tw.add(Trade(ts=1_000, price=100, qty=2, side=Side.BUY))
    tw.add(Trade(ts=1_500, price=100, qty=1, side=Side.SELL))
    s = tw.stats()
    assert s["delta"] == pytest.approx(1.0)
    assert s["imbalance"] == pytest.approx(1 / 3)
    assert tw.cum_delta == pytest.approx(1.0)
    tw.add(Trade(ts=5_000, price=100, qty=1, side=Side.SELL))
    assert tw.stats()["buy_qty"] == 0          # old prints evicted
