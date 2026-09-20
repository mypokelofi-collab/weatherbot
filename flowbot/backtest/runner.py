"""Replay-driven backtest.

The point of this harness is that there is no separate backtest engine. The
recording is replayed through the *same* feed interface, the same signal
engine, the same risk manager and the same fill simulator the live bot uses.
If a backtest result differs from live paper trading, it is because the market
differed - not because two code paths disagreed.

What this does not do is pretend a short recording is a strategy study. The
runner reports how much warmup the signal needed versus how much data it had,
and refuses to dress up a two-hour sample as an edge.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..bot.stats import full_stats
from ..bot.trader import Trader
from ..core.bus import EventBus
from ..core.clock import interval_ms
from ..core.config import AppConfig
from ..core.types import BookSnapshot
from ..data.recorder import recording_info
from ..data.replay import ReplayFeed

log = logging.getLogger(__name__)


async def run_backtest(cfg: AppConfig, quiet: bool = False) -> dict:
    cfg = cfg.model_copy(deep=True)
    cfg.data.venue = "replay"
    cfg.data.record = False

    info = recording_info(cfg.data.replay_path)
    step_ms = interval_ms(cfg.data.interval)
    bars_available = int((info["end"] - info["start"]) / step_ms) if info["end"] else 0

    # The engine refuses to fill against a stale book. That threshold is tuned
    # for a live 100ms feed; a recording with a coarser book cadence would
    # otherwise silently fill nothing, so widen it to match the data and say so.
    cadence = int(info.get("book_gap_ms_p95") or 0)
    stale_warning = ""
    if cadence and cadence * 2 > cfg.execution.book_stale_ms:
        widened = max(cfg.execution.book_stale_ms, cadence * 2)
        stale_warning = (
            f"book was only captured every ~{cadence / 1000:.1f}s (p95), so fills are "
            f"matched against depth up to {widened / 1000:.1f}s old - coarser than live"
        )
        cfg.execution.book_stale_ms = widened

    feed = ReplayFeed(
        path=cfg.data.replay_path,
        speed=cfg.data.replay_speed,
        symbol=cfg.data.symbol,
        loop=False,
    )
    finished = asyncio.Event()
    feed.on_finish(finished.set)

    trader = Trader(cfg, feed, EventBus(), store=None)
    started = time.time()
    await trader.start()

    try:
        await asyncio.wait_for(finished.wait(), timeout=3600)
    except asyncio.TimeoutError:  # pragma: no cover - runaway recording
        log.warning("backtest timed out after an hour of wall clock")

    # Close anything still open at the end of the data. The replay has stopped
    # producing events, so the exit order would sit pending forever; we
    # re-present the final book with the clock advanced past the submit
    # latency. The fill still happens against real captured depth.
    if trader.portfolio.position is not None:
        last_book = trader.last_book
        trader.flatten("end of recording")
        if last_book is not None:
            for step in range(1, 6):
                bumped = BookSnapshot(
                    ts=last_book.ts + step * (cfg.execution.latency_ms + 50),
                    bids=last_book.bids, asks=last_book.asks, seq=last_book.seq,
                )
                trader.broker.set_book(bumped)
                await asyncio.sleep(0)
                if trader.portfolio.position is None:
                    break

    await trader.stop()
    elapsed = time.time() - started

    stats = full_stats(
        trader.portfolio.trades,
        trader.portfolio.equity_curve,
        trader.portfolio.start_equity,
        trader.bars_in_market,
        max(1, trader.bars_seen),
    )
    warmup = cfg.signal.warmup_bars
    warnings: list[str] = []
    if stale_warning:
        warnings.append(stale_warning)
    if bars_available < warmup + 20:
        warnings.append(
            f"recording spans ~{bars_available} {cfg.data.interval} bars but the signal "
            f"needs {warmup} to warm up - the bot could only trade the tail of it"
        )
    if stats["trades"] < 30:
        warnings.append(
            f"{stats['trades']} trades is far too small a sample to infer an edge"
        )

    return {
        "recording": info,
        "config": {
            "interval": cfg.data.interval,
            "symbol": cfg.data.symbol,
            "signal": cfg.signal.model_dump(),
            "risk": cfg.risk.model_dump(),
            "execution": cfg.execution.model_dump(),
        },
        "bars_seen": trader.bars_seen,
        "bars_available": bars_available,
        "stats": stats,
        "portfolio": trader.portfolio.to_dict(),
        "execution": trader.broker.stats(),
        "trades": [t.to_dict() for t in trader.portfolio.trades],
        "equity_curve": trader.portfolio.equity_curve,
        "signals": len(trader.engine.history),
        "wall_seconds": round(elapsed, 2),
        "warnings": warnings,
    }
