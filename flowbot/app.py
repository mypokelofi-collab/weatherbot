"""Application wiring: build the feed, the trader and the dashboard server."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .bot.store import Store
from .bot.trader import Trader
from .core.bus import EventBus
from .core.config import AppConfig
from .data.factory import build_feed
from .data.recorder import Recorder

log = logging.getLogger(__name__)


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def build_trader(cfg: AppConfig) -> tuple[Trader, Recorder | None]:
    feed = build_feed(cfg.data)
    bus = EventBus()
    state_dir = Path(cfg.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    store = Store(state_dir / "flowbot.sqlite")
    trader = Trader(cfg, feed, bus, store)

    recorder = None
    if cfg.data.record:
        path = cfg.data.record_path.format(
            venue=feed.venue,
            symbol=cfg.data.symbol,
            date=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M"),
        )
        recorder = Recorder(
            path, levels=cfg.data.record_levels,
            throttle_ms=cfg.data.record_throttle_ms,
        )
        recorder.open({
            "venue": feed.venue,
            "symbol": cfg.data.symbol,
            "interval": cfg.data.interval,
            "started": int(datetime.now(timezone.utc).timestamp() * 1000),
            "instrument": feed.instrument.to_dict() if hasattr(feed, "instrument") else None,
            "real": feed.real,
        })
        recorder.attach(feed)
        log.info("recording market data to %s", path)

    return trader, recorder


def build_server(cfg: AppConfig, trader: Trader):
    from .server.app import create_app

    return create_app(trader, cfg)


def resolve_config(path: str | None) -> AppConfig:
    from .core.config import load_config

    candidate = path or os.environ.get("FLOWBOT_CONFIG")
    if candidate:
        return load_config(candidate)
    for default in ("config/flowbot.yml", "flowbot.yml"):
        if Path(default).exists():
            return load_config(default)
    return load_config(None)
