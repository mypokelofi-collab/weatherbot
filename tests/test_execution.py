"""Fill mechanics - the part that decides whether any of this means anything."""

from __future__ import annotations

import pytest

from flowbot.core.config import ExecConfig
from flowbot.core.types import (
    BookLevel, BookSnapshot, OrderStatus, OrderType, Side, TimeInForce, Trade,
)
from flowbot.execution.broker import PaperBroker
from flowbot.execution.microstructure import (
    liquidity_score, max_qty_within_slippage, walk_book,
)
from flowbot.execution.simulator import MatchingEngine
from tests.conftest import make_book


def engine(instrument, **kw) -> MatchingEngine:
    cfg = ExecConfig(latency_ms=0, **kw)
    return MatchingEngine(cfg, instrument)


def test_market_buy_pays_the_offer_and_walks_up(instrument):
    e = engine(instrument)
    e.set_book(make_book(mid=64_000, size=0.5))
    o = e.submit(Side.BUY, 1.2, OrderType.MARKET)
    assert o.status is OrderStatus.FILLED
    # Three levels of the real book, priced level by level.
    book = e.book
    expected = (
        book.asks[0].qty * book.asks[0].price
        + book.asks[1].qty * book.asks[1].price
        + (1.2 - book.asks[0].qty - book.asks[1].qty) * book.asks[2].price
    ) / 1.2
    assert o.avg_price == pytest.approx(expected, abs=1e-6)
    assert o.avg_price > 64_000                    # never filled at the mid
    assert o.fills[0].level_depth == 3
    assert o.fees == pytest.approx(1.2 * expected * 4.5 / 1e4)


def test_latency_delays_the_fill_to_a_later_book(instrument):
    cfg = ExecConfig(latency_ms=100)
    e = MatchingEngine(cfg, instrument)
    e.set_book(make_book(ts=1_000, mid=64_000))
    o = e.submit(Side.BUY, 0.1, OrderType.MARKET)
    assert o.status is OrderStatus.NEW             # not live at the venue yet

    e.set_book(make_book(ts=1_050, mid=64_000))
    assert o.status is OrderStatus.NEW

    e.set_book(make_book(ts=1_120, mid=64_100))    # market moved while in flight
    assert o.status is OrderStatus.FILLED
    assert o.avg_price > 64_050                    # we pay the new price


def test_thin_book_gives_a_partial_fill_then_cancels(instrument):
    e = engine(instrument)
    e.set_book(make_book(mid=64_000, size=0.05, levels=4))
    o = e.submit(Side.BUY, 1.0, OrderType.MARKET)
    assert o.filled_qty == pytest.approx(0.2)
    assert o.status is OrderStatus.CANCELED
    assert "depth" in o.reject_reason


def test_slippage_budget_rejects_an_expensive_entry(instrument):
    e = engine(instrument, max_book_levels_to_eat=60)
    bids = [BookLevel(round(64_000 - 0.1 - i * 2, 1), 0.05) for i in range(50)]
    asks = [BookLevel(round(64_000 + 0.1 + i * 2, 1), 0.05) for i in range(50)]
    e.set_book(BookSnapshot(ts=1, bids=bids, asks=asks))
    rejected = e.submit(Side.BUY, 1.0, OrderType.MARKET, max_slippage_bps=1.0)
    assert rejected.status is OrderStatus.REJECTED
    assert "slippage" in rejected.reject_reason
    ok = e.submit(Side.BUY, 0.05, OrderType.MARKET, max_slippage_bps=1.0)
    assert ok.status is OrderStatus.FILLED


def test_our_own_order_consumes_liquidity_for_a_moment(instrument):
    e = engine(instrument)
    e.set_book(make_book(ts=1_000, mid=64_000, size=0.5))
    first = e.submit(Side.BUY, 1.0, OrderType.MARKET)
    e.set_book(make_book(ts=1_100, mid=64_000, size=0.5))
    second = e.submit(Side.BUY, 1.0, OrderType.MARKET)
    assert second.avg_price > first.avg_price      # top of book was eaten


def test_post_only_that_would_cross_is_rejected(instrument):
    e = engine(instrument)
    e.set_book(make_book(mid=64_000))
    o = e.submit(Side.BUY, 0.1, OrderType.LIMIT, price=64_100, tif=TimeInForce.POST_ONLY)
    assert o.status is OrderStatus.REJECTED
    assert "cross" in o.reject_reason


def test_marketable_limit_takes_but_respects_its_price(instrument):
    e = engine(instrument)
    e.set_book(make_book(mid=64_000, size=0.5))
    o = e.submit(Side.BUY, 5.0, OrderType.LIMIT, price=64_000.3, tif=TimeInForce.GTC)
    assert 0 < o.filled_qty < 5.0
    assert o.avg_price <= 64_000.3                 # never paid above the limit
    assert o.status is OrderStatus.PARTIALLY_FILLED


def test_resting_order_waits_behind_the_real_queue(instrument):
    e = engine(instrument)
    e.set_book(make_book(ts=1_000, mid=64_000, size=0.5))
    o = e.submit(Side.BUY, 0.3, OrderType.LIMIT, price=63_999.9, tif=TimeInForce.POST_ONLY)
    assert o.queue_ahead == pytest.approx(0.5)     # the real resting size

    e.on_trade(Trade(ts=1_100, price=63_999.9, qty=0.3, side=Side.SELL))
    assert o.filled_qty == 0                        # still behind 0.2
    e.on_trade(Trade(ts=1_200, price=63_999.9, qty=0.4, side=Side.SELL))
    assert o.filled_qty == pytest.approx(0.2)       # 0.4 - 0.2 queue = 0.2 to us
    assert o.fills[-1].liquidity.value == "maker"
    assert o.fees == pytest.approx(0.2 * 63_999.9 * 1.8 / 1e4)


def test_trade_through_clears_the_queue(instrument):
    e = engine(instrument)
    e.set_book(make_book(ts=1_000, mid=64_000, size=0.5))
    o = e.submit(Side.SELL, 0.2, OrderType.LIMIT, price=64_000.1, tif=TimeInForce.POST_ONLY)
    e.on_trade(Trade(ts=1_100, price=64_000.6, qty=1.0, side=Side.BUY))
    assert o.status is OrderStatus.FILLED
    assert o.avg_price == pytest.approx(64_000.1)


def test_working_timeout_cancels_the_order(instrument):
    e = engine(instrument)
    e.set_book(make_book(ts=1_000, mid=64_000))
    o = e.submit(Side.BUY, 0.1, OrderType.LIMIT, price=63_990,
                 tif=TimeInForce.POST_ONLY, timeout_s=1.0)
    assert o.status is OrderStatus.NEW
    e.tick(2_500)
    assert o.status is OrderStatus.CANCELED
    assert o.reject_reason == "working timeout"


def test_engine_refuses_a_stale_book(instrument):
    e = engine(instrument)
    e.set_book(make_book(ts=1_000, mid=64_000))
    e.tick(60_000)                                  # feed went quiet for a minute
    o = e.submit(Side.BUY, 0.1, OrderType.MARKET)
    assert o.status is OrderStatus.NEW              # held, not filled on old depth
    assert o.filled_qty == 0


def test_sub_minimum_order_is_rejected_by_venue_rules(instrument):
    e = engine(instrument)
    e.set_book(make_book(mid=64_000))
    o = e.submit(Side.BUY, 0.00001, OrderType.MARKET)
    assert o.status is OrderStatus.REJECTED


# ---------------------------------------------------------------- broker

def test_broker_escalates_from_passive_to_market(instrument):
    b = PaperBroker(ExecConfig(latency_ms=0, limit_timeout_s=10), instrument)
    b.set_book(make_book(ts=1_000, mid=64_000))
    intent = b.execute(Side.BUY, 0.25, tag="entry · test", urgency="passive")
    first = b.engine.orders[intent.order_ids[0]]
    assert first.type is OrderType.LIMIT and first.tif is TimeInForce.POST_ONLY

    b.tick(12_000); b.set_book(make_book(ts=12_100, mid=64_000))
    b.tick(24_000); b.set_book(make_book(ts=24_100, mid=64_000))

    assert intent.attempts == 3
    last = b.engine.orders[intent.order_ids[-1]]
    assert last.type is OrderType.MARKET
    assert intent.filled_qty == pytest.approx(0.25)
    assert intent.result == "filled"


def test_urgent_intent_crosses_immediately(instrument):
    b = PaperBroker(ExecConfig(latency_ms=0), instrument)
    b.set_book(make_book(mid=64_000))
    intent = b.execute(Side.SELL, 0.2, tag="exit · stop", urgency="urgent")
    assert intent.done and intent.result == "filled"
    assert b.engine.orders[intent.order_ids[0]].type is OrderType.MARKET


def test_broker_reports_costs(instrument):
    b = PaperBroker(ExecConfig(latency_ms=0), instrument)
    b.set_book(make_book(mid=64_000))
    b.execute(Side.BUY, 0.1, tag="entry", urgency="urgent")
    stats = b.stats()
    assert stats["taker_fills"] == 1
    assert stats["fees_paid"] > 0
    assert stats["avg_slippage_bps"] >= 0


# -------------------------------------------------------- microstructure

def test_walk_book_matches_a_manual_vwap():
    book = make_book(mid=64_000, size=0.5)
    est = walk_book(book, Side.BUY, 1.0)
    assert est.complete
    assert est.avg_price == pytest.approx(
        (book.asks[0].price * 0.5 + book.asks[1].price * 0.5) / 1.0)
    assert est.slippage_bps > 0


def test_max_qty_within_slippage_respects_the_budget():
    book = make_book(mid=64_000, size=0.1, levels=40)
    qty = max_qty_within_slippage(book, Side.BUY, 0.05)
    est = walk_book(book, Side.BUY, qty)
    assert est.slippage_bps <= 0.05 + 1e-6
    bigger = walk_book(book, Side.BUY, qty * 2)
    assert bigger.slippage_bps > est.slippage_bps


def test_liquidity_score_drops_with_a_wide_spread():
    tight = make_book(mid=64_000, size=2.0)
    wide = BookSnapshot(
        ts=1,
        bids=[BookLevel(63_980, 2.0)],
        asks=[BookLevel(64_020, 2.0)],
    )
    assert liquidity_score(tight) > liquidity_score(wide)


def test_market_order_that_cannot_fill_is_cancelled_not_stuck(instrument):
    """A desynced book must not leave an order pending forever - that would
    hold the bot's entry slot for the rest of the session."""
    b = PaperBroker(ExecConfig(latency_ms=0, market_timeout_s=10), instrument)
    b.set_book(make_book(ts=1_000, mid=64_000))
    b.tick(120_000)                       # feed went quiet: the book is stale

    intent = b.execute(Side.BUY, 0.1, tag="entry · test", urgency="urgent")
    order = b.engine.orders[intent.order_ids[0]]
    assert order.status is OrderStatus.NEW
    assert order.expire_at > 0

    b.tick(140_000)
    assert order.status is OrderStatus.CANCELED
    assert intent.done and intent.filled_qty == 0
    assert intent.result == "unfilled"
