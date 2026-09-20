"""Order book replica and bar aggregation."""

from __future__ import annotations

import pytest

from flowbot.core.types import Side, Trade
from flowbot.data.book import OrderBook
from flowbot.data.candles import CandleAggregator

STEP = 900_000


def test_snapshot_then_diff_updates_and_deletes():
    b = OrderBook()
    b.apply_snapshot([(100.0, 2.0), (99.0, 3.0)], [(101.0, 1.0), (102.0, 4.0)], seq=10, ts=1)
    assert b.ready and b.mid == 100.5

    assert b.apply_diff([(100.0, 5.0)], [(101.0, 0.0)], first_seq=11, final_seq=12, ts=2)
    assert b.bids[100.0] == 5.0
    assert 101.0 not in b.asks           # zero quantity deletes the level
    assert b.best_ask == 102.0


def test_sequence_gap_marks_book_unusable():
    b = OrderBook()
    b.apply_snapshot([(100.0, 1.0)], [(101.0, 1.0)], seq=10, ts=1)
    assert b.apply_diff([], [], first_seq=11, final_seq=11, ts=2) is True
    assert b.apply_diff([], [], first_seq=14, final_seq=15, ts=3) is False
    assert b.ready is False
    assert b.stats.gaps == 1


def test_stale_diff_is_ignored_not_applied():
    b = OrderBook()
    b.apply_snapshot([(100.0, 1.0)], [(101.0, 1.0)], seq=20, ts=1)
    assert b.apply_diff([(100.0, 9.9)], [], first_seq=5, final_seq=15, ts=2) is True
    assert b.bids[100.0] == 1.0          # snapshot already contained it


def test_walk_consumes_levels_in_price_order():
    b = OrderBook()
    b.apply_snapshot(
        [(99.0, 1.0), (98.0, 1.0)],
        [(100.0, 1.0), (101.0, 2.0)],
        seq=1, ts=1,
    )
    filled, notional, levels = b.walk("ask", 2.0)
    assert filled == 2.0
    assert notional == pytest.approx(100.0 + 101.0)
    assert levels == 2


def test_imbalance_microprice_and_depth(book_factory):
    heavy_bid = book_factory(skew=3.0)
    assert heavy_bid.imbalance(25.0) > 0.4
    # Microprice leans toward the thin side (the side likely to be taken).
    assert heavy_bid.microprice > heavy_bid.mid
    bid_usd, ask_usd = heavy_bid.depth_notional(10.0)
    assert bid_usd > ask_usd


def test_candles_aggregate_aggressor_split():
    agg = CandleAggregator(STEP)
    closed = []
    agg.on_close(closed.append)
    agg.add_trade(Trade(ts=STEP, price=100, qty=1, side=Side.BUY))
    agg.add_trade(Trade(ts=STEP + 10, price=104, qty=2, side=Side.SELL))
    agg.add_trade(Trade(ts=STEP + 20, price=98, qty=1, side=Side.BUY))
    agg.add_trade(Trade(ts=2 * STEP + 5, price=99, qty=1, side=Side.BUY))

    assert len(closed) == 1
    bar = closed[0]
    assert (bar.open, bar.high, bar.low, bar.close) == (100, 104, 98, 98)
    assert bar.buy_volume == 2 and bar.sell_volume == 2
    assert bar.delta == 0
    assert bar.vwap == pytest.approx((100 + 104 * 2 + 98) / 4)


def test_quiet_market_still_advances_the_bar_grid():
    agg = CandleAggregator(STEP)
    closed = []
    agg.on_close(closed.append)
    agg.add_trade(Trade(ts=STEP, price=100, qty=1, side=Side.BUY))
    agg.add_trade(Trade(ts=4 * STEP + 1, price=110, qty=1, side=Side.BUY))
    # One real bar plus two flat continuation bars - the grid never stretches.
    assert [c.open_time // STEP for c in closed] == [1, 2, 3]
    assert closed[1].volume == 0 and closed[1].close == 100
    assert agg.current.open_time // STEP == 4
    assert agg.current.open == 110        # first print of the bar sets the open


def test_flush_until_closes_elapsed_bars_without_trades():
    agg = CandleAggregator(STEP)
    agg.add_trade(Trade(ts=STEP, price=100, qty=1, side=Side.BUY))
    out = agg.flush_until(3 * STEP + 1)
    assert len(out) == 2
    assert all(c.closed for c in out)


def test_late_print_from_a_reconnect_is_dropped():
    agg = CandleAggregator(STEP)
    agg.add_trade(Trade(ts=2 * STEP, price=100, qty=1, side=Side.BUY))
    before = agg.current.volume
    agg.add_trade(Trade(ts=STEP, price=50, qty=5, side=Side.BUY))
    assert agg.current.volume == before


def test_bootstrap_current_opens_a_bar_without_a_trade():
    # A depth-only feed (trade tape stalled or just slow to arrive) must
    # still get a bar in progress, or the clock-driven flush in `_housekeeping`
    # has nothing to close and the whole signal pipeline stalls forever.
    agg = CandleAggregator(STEP)
    agg.bootstrap_current(STEP + 5, 200.0)
    assert agg.current is not None
    assert agg.current.open_time // STEP == 1
    assert (agg.current.open, agg.current.high, agg.current.low, agg.current.close) == (
        200.0, 200.0, 200.0, 200.0,
    )
    assert agg.current.trades == 0


def test_bootstrap_current_is_a_noop_once_a_bar_is_open():
    agg = CandleAggregator(STEP)
    agg.add_trade(Trade(ts=STEP, price=100, qty=1, side=Side.BUY))
    agg.bootstrap_current(STEP + 5, 999.0)   # must not clobber the real bar
    assert agg.current.open == 100


def test_open_at_reads_the_live_bar_in_progress():
    agg = CandleAggregator(STEP)
    agg.add_trade(Trade(ts=2 * STEP, price=101.0, qty=1, side=Side.BUY))
    assert agg.open_at(2 * STEP + 500) == 101.0
    assert agg.open_at(2 * STEP) == 101.0        # exactly on the boundary


def test_open_at_reads_a_closed_historical_bar():
    agg = CandleAggregator(STEP)
    agg.add_trade(Trade(ts=STEP, price=100.0, qty=1, side=Side.BUY))
    agg.add_trade(Trade(ts=2 * STEP, price=200.0, qty=1, side=Side.BUY))
    # Bar 1 is closed now that bar 2 has started; its open is still readable.
    assert agg.open_at(STEP + 10) == 100.0
    assert agg.open_at(2 * STEP + 10) == 200.0


def test_open_at_is_none_for_a_bucket_we_never_saw():
    agg = CandleAggregator(STEP)
    agg.add_trade(Trade(ts=5 * STEP, price=100.0, qty=1, side=Side.BUY))
    assert agg.open_at(2 * STEP) is None         # long before anything we have
    assert agg.open_at(9 * STEP) is None         # in the future


def test_first_real_trade_reclaims_a_bootstrapped_bar():
    agg = CandleAggregator(STEP)
    closed = []
    agg.on_close(closed.append)
    agg.bootstrap_current(STEP, 200.0)       # book-mid placeholder
    agg.add_trade(Trade(ts=STEP + 5, price=205.0, qty=1, side=Side.BUY))
    # The first print still defines open/high/low, exactly as it would for
    # any other flat-continuation bar - the bootstrap price is discarded.
    assert (agg.current.open, agg.current.high, agg.current.low) == (205.0, 205.0, 205.0)
    agg.flush_until(2 * STEP + 1)
    assert closed[0].close == 205.0
