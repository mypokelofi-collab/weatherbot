"""Sizing, circuit breakers and the exit ladder."""

from __future__ import annotations

import pytest

from flowbot.bot.portfolio import Portfolio
from flowbot.bot.positions import PositionManager, effective_stop
from flowbot.bot.risk import RiskManager
from flowbot.core.config import ExecConfig, RiskConfig
from flowbot.core.instrument import Instrument
from flowbot.core.types import ClosedTrade, Fill, Liquidity, Position, Side
from tests.conftest import make_book

DAY = 1_758_300_000_000
STEP = 900_000


def rm(instrument, **kw) -> RiskManager:
    return RiskManager(RiskConfig(**kw), ExecConfig(), instrument)


def test_size_risks_exactly_the_configured_fraction(instrument):
    r = rm(instrument, risk_per_trade_pct=0.5, stop_atr_mult=1.6)
    res = r.size_position(10_000, Side.BUY, 64_000, atr=250, book=make_book(size=5.0))
    assert res.ok
    # qty * stop distance == the dollars we are willing to lose
    assert res.qty * res.stop_distance == pytest.approx(50.0, rel=0.02)
    assert res.stop_distance == pytest.approx(400.0)


def test_smaller_atr_buys_more_coin_for_the_same_risk(instrument):
    r = rm(instrument, leverage_cap=50, max_position_pct=10_000)
    calm = r.size_position(10_000, Side.BUY, 64_000, atr=100, book=make_book(size=50.0))
    wild = r.size_position(10_000, Side.BUY, 64_000, atr=400, book=make_book(size=50.0))
    assert calm.qty > wild.qty * 3.5
    assert calm.qty * calm.stop_distance == pytest.approx(wild.qty * wild.stop_distance, rel=0.02)


def test_notional_and_leverage_caps_bind(instrument):
    r = rm(instrument, risk_per_trade_pct=5.0, max_position_pct=100, leverage_cap=3)
    res = r.size_position(10_000, Side.BUY, 64_000, atr=50, book=make_book(size=50.0))
    assert res.ok
    assert res.notional <= 10_000 * 1.0001
    assert res.cap_applied == "notional/leverage cap"


def test_thin_book_caps_the_position(instrument):
    r = rm(instrument, risk_per_trade_pct=5.0)
    thin = make_book(size=0.01, levels=5)
    res = r.size_position(100_000, Side.BUY, 64_000, atr=200, book=thin)
    assert res.cap_applied == "book liquidity" or not res.ok
    if res.ok:
        assert res.qty <= 0.025 + 1e-9        # half of the 0.05 available


def test_tiny_risk_never_produces_a_size_the_venue_would_reject(instrument):
    """Sub-minimum sizing resolves to the venue minimum or to nothing - never
    to an order that would bounce."""
    r = rm(instrument, risk_per_trade_pct=0.0001, min_lot_fallback=True)
    res = r.size_position(100, Side.BUY, 64_000, atr=500, book=make_book())
    if res.ok:
        assert res.qty >= instrument.smallest_tradable(64_000)
        assert res.cap_applied == "venue minimum lot"
        assert instrument.is_tradable(res.qty, 64_000)[0]
    else:
        assert "minimum" in res.reason or "ceiling" in res.reason


def test_daily_loss_limit_blocks_new_entries(instrument):
    r = rm(instrument, daily_loss_limit_pct=2.0, cooldown_bars=0, loss_cooldown_bars=0)
    r.roll_day(DAY, 10_000)
    r.day.realized_pnl = -250          # -2.5%
    blocked = r.check_gates(9_750, DAY, STEP)
    assert any("daily loss" in b for b in blocked)


def test_consecutive_losses_trip_the_kill_switch(instrument):
    r = rm(instrument, max_consecutive_losses=3)
    r.roll_day(DAY, 10_000)
    loss = ClosedTrade(1, Side.BUY, 0.1, 0, 64_000, 1, 63_800, -20, 2, -22, -1.0, 3, "e", "stop")
    for _ in range(3):
        r.on_trade_closed(loss, DAY)
    assert r.kill_switch
    assert any("kill switch" in b for b in r.check_gates(9_900, DAY, STEP))
    r.release_kill_switch()
    assert not r.kill_switch


def test_cooldown_after_a_loss_is_longer(instrument):
    r = rm(instrument, cooldown_bars=1, loss_cooldown_bars=2)
    r.roll_day(DAY, 10_000)
    win = ClosedTrade(1, Side.BUY, 0.1, 0, 64_000, 1, 64_400, 40, 2, 38, 1.0, 3, "e", "tp")
    r.on_trade_closed(win, DAY)
    assert not r.check_gates(10_000, DAY + STEP * 2, STEP)      # 1 bar was enough
    loss = ClosedTrade(2, Side.BUY, 0.1, 0, 64_000, 1, 63_800, -20, 2, -22, -1.0, 3, "e", "stop")
    r.on_trade_closed(loss, DAY)
    assert r.check_gates(10_000, DAY + STEP * 2, STEP)          # 3 bars needed


def test_a_flip_skips_the_cooldown(instrument):
    r = rm(instrument, cooldown_bars=2, reverse_on_flip=True)
    r.roll_day(DAY, 10_000)
    win = ClosedTrade(1, Side.BUY, 0.1, 0, 64_000, 1, 64_400, 40, 2, 38, 1.0, 3, "e", "flip")
    r.on_trade_closed(win, DAY, was_flip=True)
    assert r.check_gates(10_000, DAY + STEP, STEP) == []


def test_max_trades_per_day(instrument):
    r = rm(instrument, max_trades_per_day=2, cooldown_bars=0)
    r.roll_day(DAY, 10_000)
    r.day.trades = 2
    assert any("trades today" in b for b in r.check_gates(10_000, DAY, STEP))


# ------------------------------------------------------------- position

def long_position(**kw) -> Position:
    base = dict(side=Side.BUY, qty=0.1, entry_price=64_000, entry_ts=0,
                stop=63_600, target=64_720, risk_per_unit=400, entry_atr=250)
    base.update(kw)
    return Position(**base)


def test_stop_loss_triggers_on_the_live_price():
    pm = PositionManager(RiskConfig())
    pos = long_position()
    pm.reset(pos)
    assert pm.update(pos, 63_700, 250, 1) == []
    out = pm.update(pos, 63_590, 250, 2)
    assert out and out[0].urgency == "urgent" and "stop" in out[0].reason


def test_breakeven_then_partial_then_trail():
    cfg = RiskConfig(breakeven_at_r=1.0, take_profit_r=1.8, partial_exit_pct=50, trail_atr_mult=2.0)
    pm = PositionManager(cfg)
    pos = long_position()
    pm.reset(pos)

    pm.update(pos, 64_420, 250, 1)                  # +1.05R
    assert pos.breakeven_armed and pos.stop > pos.entry_price

    out = pm.update(pos, 64_760, 250, 2)            # +1.9R
    assert out and out[0].kind == "partial"
    assert out[0].qty == pytest.approx(0.05)
    assert pos.scaled_out

    pm.update(pos, 65_400, 250, 3)
    high_trail = pos.trail
    pm.update(pos, 65_000, 250, 4)                  # pull back
    assert pos.trail == high_trail                  # a trail never loosens


def test_trail_never_undercuts_a_breakeven_stop():
    pm = PositionManager(RiskConfig())
    pos = long_position()
    pm.reset(pos)
    pm.update(pos, 64_300, 250, 1)                  # arms an early trail below entry
    pm.update(pos, 64_450, 250, 2)                  # breakeven moves the stop up
    assert effective_stop(pos) >= pos.entry_price
    assert pos.trail < pos.stop                     # the early trail is looser
    # A price between the trail and the breakeven stop must still close the
    # trade: the tighter of the two is the one in force.
    out = pm.update(pos, 64_020, 250, 3)
    assert out and "breakeven stop" in out[0].reason


def test_runner_target_and_time_stop():
    cfg = RiskConfig(runner_exit_r=3.0, max_bars_in_trade=5)
    pm = PositionManager(cfg)
    pos = long_position()
    pm.reset(pos)
    out = pm.update(pos, 65_300, 250, 1)            # +3.25R
    assert out and "runner" in out[0].reason

    pos2 = long_position()
    pm2 = PositionManager(cfg)
    pm2.reset(pos2)
    for _ in range(4):
        assert pm2.on_bar_close(pos2, 64_100) == []
    out = pm2.on_bar_close(pos2, 64_100)
    assert out and "time stop" in out[0].reason


def test_short_position_ladder_mirrors():
    pm = PositionManager(RiskConfig())
    pos = Position(side=Side.SELL, qty=0.1, entry_price=64_000, entry_ts=0,
                   stop=64_400, target=63_280, risk_per_unit=400, entry_atr=250)
    pm.reset(pos)
    assert pm.update(pos, 63_800, 250, 1) == []
    pm.update(pos, 63_560, 250, 2)                  # +1.1R
    assert pos.breakeven_armed and pos.stop < pos.entry_price
    out = pm.update(pos, 64_450, 250, 3)
    assert out and out[0].urgency == "urgent"


# ------------------------------------------------------------ portfolio

def fill(price, qty, side, fee=1.0, ts=1) -> Fill:
    return Fill(ts=ts, order_id="o", side=side, price=price, qty=qty,
                fee=fee, liquidity=Liquidity.TAKER, slippage_bps=0.5)


def test_long_round_trip_accounting():
    p = Portfolio(10_000)
    p.set_mark(64_000, 0)
    p.open_position(Side.BUY, fill(64_000, 0.05, Side.BUY, fee=1.44),
                    stop=63_600, target=64_720, atr=250, signal_score=0.4, reason="entry")
    p.set_mark(64_400, 1_000)
    assert p.unrealized == pytest.approx(20.0)
    assert p.equity == pytest.approx(10_000 - 1.44 + 20.0)

    trade = p.reduce_position(fill(64_600, 0.05, Side.SELL, fee=1.45, ts=2_000), "trail")
    assert trade is not None
    assert trade.gross_pnl == pytest.approx(30.0)
    assert trade.pnl == pytest.approx(30.0 - 2.89)
    assert trade.r_multiple == pytest.approx((30.0 - 2.89) / (400 * 0.05))
    assert p.position is None
    assert p.equity == pytest.approx(10_000 + trade.pnl)


def test_short_round_trip_makes_money_when_price_falls():
    p = Portfolio(10_000)
    p.set_mark(64_000, 0)
    p.open_position(Side.SELL, fill(64_000, 0.1, Side.SELL, fee=2.88),
                    stop=64_400, target=63_280, atr=250, signal_score=-0.4, reason="entry")
    p.set_mark(63_500, 1_000)
    assert p.unrealized == pytest.approx(50.0)
    trade = p.reduce_position(fill(63_500, 0.1, Side.BUY, fee=2.86, ts=2_000), "target")
    assert trade.pnl == pytest.approx(50.0 - 5.74)


def test_partial_exit_then_close_uses_weighted_prices():
    p = Portfolio(10_000)
    p.set_mark(64_000, 0)
    p.open_position(Side.BUY, fill(64_000, 0.1, Side.BUY, fee=0.0),
                    stop=63_600, target=64_720, atr=250, signal_score=0.4, reason="entry")
    assert p.reduce_position(fill(64_500, 0.05, Side.SELL, fee=0.0, ts=1), "partial") is None
    trade = p.reduce_position(fill(64_300, 0.05, Side.SELL, fee=0.0, ts=2), "trail")
    assert trade.exit_price == pytest.approx(64_400)
    assert trade.pnl == pytest.approx(40.0)


def test_drawdown_tracks_the_peak():
    p = Portfolio(10_000)
    p.set_mark(64_000, 0)
    p.open_position(Side.BUY, fill(64_000, 0.1, Side.BUY, fee=0.0),
                    stop=63_000, target=66_000, atr=500, signal_score=0.5, reason="e")
    p.set_mark(65_000, 10_000)
    assert p.peak_equity == pytest.approx(10_100)
    p.set_mark(64_500, 20_000)
    assert p.drawdown == pytest.approx((10_050 - 10_100) / 10_100)


# ------------------------------------------- small accounts vs venue minimums

def perp_instrument() -> Instrument:
    """Binance USDⓈ-M BTCUSDT: 0.001 lot step, $100 minimum notional."""
    return Instrument(symbol="BTCUSDT", tick_size=0.1, step_size=0.001,
                      min_qty=0.001, min_notional=100.0, contract="perp")


def test_smallest_tradable_respects_both_venue_filters():
    perp = perp_instrument()
    # At $70k the $100 notional needs 0.00143 BTC, which rounds up to 0.002.
    assert perp.smallest_tradable(70_000) == pytest.approx(0.002)
    # At $200k the lot step is the binding filter instead.
    assert perp.smallest_tradable(200_000) == pytest.approx(0.001)
    assert perp.round_qty_up(0.0011) == pytest.approx(0.002)
    assert perp.round_qty_up(0.002) == pytest.approx(0.002)


def test_small_account_takes_the_venue_minimum_and_reports_the_real_risk():
    perp = perp_instrument()
    r = RiskManager(
        RiskConfig(start_equity=50, risk_per_trade_pct=0.5,
                   min_lot_fallback=True, max_risk_per_trade_pct=4.0,
                   max_position_pct=300, leverage_cap=3),
        ExecConfig(), perp,
    )
    price, atr = 70_000.0, 245.0            # 0.35% ATR
    res = r.size_position(50, Side.BUY, price, atr, make_book(mid=price, size=5.0))

    assert res.ok
    assert res.qty == pytest.approx(0.002)
    assert res.cap_applied == "venue minimum lot"
    # The honest number: not the 0.5% target, but what the minimum lot forces.
    assert res.actual_risk_pct == pytest.approx(0.002 * 1.6 * atr / 50 * 100, rel=1e-6)
    assert 1.0 < res.actual_risk_pct < 2.0
    assert res.leverage > 2.0


def test_small_account_refuses_when_the_minimum_lot_risks_too_much():
    perp = perp_instrument()
    r = RiskManager(
        RiskConfig(start_equity=50, risk_per_trade_pct=0.5,
                   min_lot_fallback=True, max_risk_per_trade_pct=4.0,
                   max_position_pct=300, leverage_cap=3),
        ExecConfig(), perp,
    )
    # 0.9% ATR: the minimum lot would risk ~4% of a $50 account.
    res = r.size_position(50, Side.BUY, 70_000, 630.0, make_book(mid=70_000, size=5.0))
    assert not res.ok
    assert "ceiling" in res.reason
    assert res.actual_risk_pct > 4.0


def test_fallback_can_be_turned_off():
    perp = perp_instrument()
    r = RiskManager(
        RiskConfig(start_equity=50, risk_per_trade_pct=0.5, min_lot_fallback=False),
        ExecConfig(), perp,
    )
    res = r.size_position(50, Side.BUY, 70_000, 245.0, make_book(mid=70_000, size=5.0))
    assert not res.ok
    assert "below the venue minimum" in res.reason


def test_a_large_account_is_unaffected_by_the_fallback():
    perp = perp_instrument()
    r = RiskManager(RiskConfig(start_equity=100_000, risk_per_trade_pct=0.5), ExecConfig(), perp)
    res = r.size_position(100_000, Side.BUY, 70_000, 245.0, make_book(mid=70_000, size=50.0))
    assert res.ok
    assert res.cap_applied != "venue minimum lot"
    assert res.actual_risk_pct == pytest.approx(0.5, abs=0.02)


def test_spot_venue_sizes_a_small_account_properly():
    spot = Instrument(symbol="BTCUSDT", tick_size=0.01, step_size=0.00001,
                      min_qty=0.00001, min_notional=5.0)
    r = RiskManager(RiskConfig(start_equity=50, risk_per_trade_pct=0.5), ExecConfig(), spot)
    res = r.size_position(50, Side.BUY, 70_000, 245.0, make_book(mid=70_000, size=5.0))
    assert res.ok
    assert res.cap_applied == ""                      # no minimum-lot fallback needed
    assert res.actual_risk_pct == pytest.approx(0.5, abs=0.05)
    assert res.leverage < 1.0
