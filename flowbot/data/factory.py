"""Feed construction from config - one place that knows every venue."""

from __future__ import annotations

from ..core.config import DataConfig
from .feed import MarketFeed


def build_feed(cfg: DataConfig) -> MarketFeed:
    venue = cfg.venue
    if venue in ("binance-futures", "binance-spot"):
        from .binance import BinanceFeed

        return BinanceFeed(
            symbol=cfg.symbol,
            market="futures" if venue.endswith("futures") else "spot",
            depth_limit=cfg.depth_limit,
            depth_speed=cfg.depth_speed,
        )
    if venue == "coinbase":
        from .coinbase import CoinbaseFeed

        return CoinbaseFeed(symbol=cfg.symbol)
    if venue == "replay":
        from .replay import ReplayFeed

        if not cfg.replay_path:
            raise ValueError("data.replay_path is required for venue=replay")
        return ReplayFeed(
            path=cfg.replay_path, speed=cfg.replay_speed,
            symbol=cfg.symbol, loop=cfg.replay_loop,
        )
    if venue == "simulator":
        from .simulated import SimulatedFeed

        return SimulatedFeed(
            symbol=cfg.symbol, speed=cfg.sim_speed,
            seed=cfg.sim_seed, start_price=cfg.sim_start_price,
        )
    raise ValueError(f"unknown venue: {venue}")
