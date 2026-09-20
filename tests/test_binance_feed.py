"""Binance feed watchdog logic (no network - see docs/ARCHITECTURE.md).

Regression: observed live against BTCUSDT futures - the depth stream kept
flowing normally while the aggTrade stream delivered zero prints for over an
hour, even though Binance's own REST API confirmed real trades printing
several times a second the whole time. Nothing in the app code was wrong;
the socket itself was withholding one of its two subscribed streams. The
fix is a watchdog that treats a stalled trade tape like any other feed
fault and reconnects.
"""

from __future__ import annotations

import time

from flowbot.data.binance import BinanceFeed


def make_feed() -> BinanceFeed:
    return BinanceFeed(symbol="BTCUSDT", market="futures")


def test_no_stall_reported_before_a_connection_exists():
    feed = make_feed()
    assert feed.health.connected_since == 0
    assert feed.health.last_trade_ts == 0
    assert feed._trade_stream_stalled() is False


def test_stall_detected_when_no_trade_has_ever_arrived():
    feed = make_feed()
    feed.health.connected_since = int(time.time() * 1000) - feed.TRADE_STALL_MS - 1_000
    assert feed._trade_stream_stalled() is True


def test_no_stall_just_after_connecting_with_no_trade_yet():
    feed = make_feed()
    feed.health.connected_since = int(time.time() * 1000) - 1_000
    assert feed._trade_stream_stalled() is False


def test_stall_detected_after_trades_stop_mid_session():
    feed = make_feed()
    feed.health.connected_since = int(time.time() * 1000) - 3_600_000
    feed.health.last_trade_ts = int(time.time() * 1000) - feed.TRADE_STALL_MS - 1_000
    assert feed._trade_stream_stalled() is True


def test_no_stall_while_trades_keep_arriving():
    feed = make_feed()
    feed.health.connected_since = int(time.time() * 1000) - 3_600_000
    feed.health.last_trade_ts = int(time.time() * 1000) - 500
    assert feed._trade_stream_stalled() is False
