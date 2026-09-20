"""Whole-system tests: data in one end, trades and a dashboard out the other."""

from __future__ import annotations

import asyncio

import pytest

from flowbot.backtest.runner import run_backtest
from flowbot.bot.trader import Trader
from flowbot.core.bus import EventBus
from flowbot.core.config import AppConfig
from flowbot.data.factory import build_feed
from flowbot.data.recorder import Recorder, recording_info
from flowbot.data.replay import ReplayFeed
from flowbot.data.simulated import SimulatedFeed


def backtest_config(recording, tmp_path) -> AppConfig:
    cfg = AppConfig()
    cfg.state_dir = str(tmp_path / "state")
    cfg.data.venue = "replay"
    cfg.data.replay_path = str(recording)
    cfg.data.replay_speed = 0.0
    cfg.signal.warmup_bars = 40
    cfg.risk.cooldown_bars = 0
    cfg.risk.loss_cooldown_bars = 0
    cfg.risk.max_trades_per_day = 50
    cfg.execution.latency_ms = 50
    return cfg


async def test_backtest_replays_real_events_through_the_live_code_path(recording, tmp_path):
    result = await run_backtest(backtest_config(recording, tmp_path))

    assert result["bars_seen"] > 100
    assert result["signals"] > 100
    stats = result["stats"]
    assert stats["trades"] >= 1, "a trending fixture should produce at least one round trip"

    # Every trade is fully accounted: costs, R multiples and the reason it ended.
    for t in result["trades"]:
        assert t["fees"] > 0
        assert t["exit_reason"]
        assert t["entry_reason"]
        assert t["qty"] > 0
        assert abs(t["pnl"] - (t["gross_pnl"] - t["fees"])) < 1e-6

    # The equity number and the sum of the trades agree.
    net = sum(t["pnl"] for t in result["trades"])
    assert result["portfolio"]["equity"] == pytest.approx(
        result["portfolio"]["start_equity"] + net, abs=0.01
    )
    assert result["portfolio"]["position"] is None      # flattened at the end
    assert any("sample" in w for w in result["warnings"])


async def test_backtest_is_deterministic(recording, tmp_path):
    a = await run_backtest(backtest_config(recording, tmp_path))
    b = await run_backtest(backtest_config(recording, tmp_path))
    assert a["stats"]["trades"] == b["stats"]["trades"]
    assert a["portfolio"]["equity"] == pytest.approx(b["portfolio"]["equity"])


async def test_higher_fees_reduce_net_pnl(recording, tmp_path):
    cheap = backtest_config(recording, tmp_path)
    cheap.execution.taker_fee_bps = 1.0
    dear = backtest_config(recording, tmp_path)
    dear.execution.taker_fee_bps = 20.0

    a = await run_backtest(cheap)
    b = await run_backtest(dear)
    if a["stats"]["trades"] and b["stats"]["trades"]:
        assert b["stats"]["fees"] > a["stats"]["fees"]


async def test_risk_per_trade_scales_position_size(recording, tmp_path):
    small = backtest_config(recording, tmp_path)
    small.risk.risk_per_trade_pct = 0.1
    big = backtest_config(recording, tmp_path)
    big.risk.risk_per_trade_pct = 1.0

    a = await run_backtest(small)
    b = await run_backtest(big)
    if a["trades"] and b["trades"]:
        assert b["trades"][0]["qty"] > a["trades"][0]["qty"] * 5


async def test_kill_switch_stops_new_entries(recording, tmp_path):
    cfg = backtest_config(recording, tmp_path)
    feed = ReplayFeed(path=cfg.data.replay_path, speed=0.0, symbol="BTCUSDT")
    trader = Trader(cfg, feed, EventBus(), store=None)
    trader.risk.engage_kill_switch("test", manual=True)

    done = asyncio.Event()
    feed.on_finish(done.set)
    await trader.start()
    await asyncio.wait_for(done.wait(), timeout=120)
    await trader.stop()

    assert trader.portfolio.position is None
    assert trader.portfolio.trades == []
    assert any("kill switch" in e["message"] for e in trader.events.tail(200))


async def test_live_loop_on_the_simulator_produces_a_full_snapshot(tmp_path):
    cfg = AppConfig()
    cfg.state_dir = str(tmp_path / "state")
    cfg.data.venue = "simulator"
    cfg.data.sim_speed = 2400
    cfg.data.sim_seed = 4
    cfg.data.backfill_bars = 200
    cfg.risk.cooldown_bars = 0

    feed = build_feed(cfg.data)
    trader = Trader(cfg, feed, EventBus(), store=None)
    await trader.start()
    await asyncio.sleep(6)
    snap = trader.snapshot()
    await trader.stop()

    assert snap["running"] is True
    assert snap["real_data"] is False            # the banner depends on this
    assert snap["mode"] == "paper"
    assert len(snap["candles"]) > 50
    assert snap["book"]["bids"] and snap["book"]["asks"]
    assert snap["signal"]["components"]
    assert snap["portfolio"]["equity"] > 0
    assert "liquidity_score" in snap["pressure"]
    assert snap["feed"]["connected"] is True
    assert snap["stats"]["total_bars"] > 0
    assert snap["next_bar_in_ms"] <= 900_000


async def test_record_then_replay_round_trip(tmp_path):
    path = tmp_path / "rec.jsonl"
    feed = SimulatedFeed(seed=9, speed=1200, tick_wall_s=0.02, trades_per_sec=1.0)
    rec = Recorder(path, levels=10, throttle_ms=500)
    rec.open({"venue": "simulator", "symbol": "BTCUSDT", "interval": "15m",
              "instrument": feed.instrument.to_dict(), "real": False})
    rec.attach(feed)
    await feed.start()
    await asyncio.sleep(3)
    await feed.stop()
    rec.close()

    info = recording_info(path)
    assert info["trades"] > 10 and info["books"] > 1
    assert info["end"] > info["start"]

    seen = {"trades": 0, "books": 0}
    replay = ReplayFeed(path=path, speed=0.0, symbol="BTCUSDT")
    replay.on_trade(lambda t: seen.__setitem__("trades", seen["trades"] + 1))
    replay.on_book(lambda b: seen.__setitem__("books", seen["books"] + 1))
    done = asyncio.Event()
    replay.on_finish(done.set)
    await replay.start()
    await asyncio.wait_for(done.wait(), timeout=60)
    await replay.stop()

    assert seen["trades"] == info["trades"]
    assert seen["books"] == info["books"]
    assert replay.instrument.tick_size == 0.1


async def test_a_blocked_entry_does_not_wedge_the_bot(tmp_path):
    """An entry that never fills must release the bot to try again.

    Regression: an unfillable market order left `_entry_ctx` set, so every
    later signal was silently skipped for the rest of the session.
    """
    cfg = AppConfig()
    cfg.state_dir = str(tmp_path / "state")
    cfg.data.venue = "simulator"
    cfg.data.sim_speed = 1800          # coarse enough to strain the clock
    cfg.data.sim_seed = 13
    cfg.data.backfill_bars = 200
    cfg.risk.cooldown_bars = 0
    cfg.execution.market_timeout_s = 5

    feed = build_feed(cfg.data)
    trader = Trader(cfg, feed, EventBus(), store=None)
    await trader.start()
    await asyncio.sleep(12)
    snap = trader.snapshot()
    await trader.stop()

    # Whether a signal fired in this window is up to the market, so the
    # assertion is on the invariant, not on activity: the entry slot is only
    # ever held by an order that is actually working.
    exec_stats = snap["execution"]
    assert snap["bars_seen"] > 200
    assert exec_stats["pending"] == 0, "no order may sit pending indefinitely"
    assert trader._entry_ctx is None or snap["portfolio"]["position"] is not None

    # And the clock the fill engine sees must track the data, not drift away
    # from it - a stale-looking book silently blocks every fill.
    assert snap["book"] is not None
    assert snap["venue_now"] - snap["book"]["ts"] < cfg.execution.book_stale_ms
