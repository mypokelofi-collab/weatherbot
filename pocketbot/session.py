"""Config, account guards and opening a trading session.

Shared by the one-shot CLI (`pocketbot run`) and the long-running dashboard
service (`pocketbot serve`), so both apply exactly the same safety checks.
"""

from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

import yaml

from . import broker as _broker
from .broker import PaperBroker, PocketOptionBroker, PocketOptionFeed, SyntheticFeed, ssid_is_demo
from .engine import Engine
from .risk import RiskConfig, RiskManager
from .stats import Ledger
from .strategy import StrategyConfig

log = logging.getLogger("pocketbot")

REAL_MONEY_ACK = "yes-i-accept-the-risk"
DEFAULT_CONFIG = "config/pocketbot.yml"


def load_config(path: str | None) -> dict:
    p = Path(path or os.environ.get("POCKETBOT_CONFIG") or DEFAULT_CONFIG)
    cfg = yaml.safe_load(p.read_text()) if p.exists() else {}
    cfg.setdefault("mode", "paper")
    cfg.setdefault("asset", "EURUSD_otc")
    cfg.setdefault("period", 60)
    cfg.setdefault("expiry_candles", 3)
    cfg.setdefault("history_hours", 3)
    cfg.setdefault("paper", {})
    # Container deploys set these without editing the file.
    for key, env in (("mode", "POCKETBOT_MODE"), ("asset", "POCKETBOT_ASSET")):
        if os.environ.get(env):
            cfg[key] = os.environ[env].strip()
    if cfg["mode"] not in ("paper", "demo", "live"):
        raise SystemExit(f"mode must be paper, demo or live, not {cfg['mode']!r}")
    return cfg


def ledger_path(cfg: dict) -> str | None:
    path = cfg.get("ledger")
    return path.format(mode=cfg["mode"]) if path else None


def make_engine(cfg: dict, feed, broker, ledger: Ledger, **kw) -> Engine:
    kw.setdefault("risk", RiskManager(RiskConfig.from_dict(cfg.get("risk"))))
    return Engine(feed=feed, broker=broker,
                  strategy=StrategyConfig.from_dict(cfg.get("strategy")),
                  asset=cfg["asset"], expiry_candles=int(cfg["expiry_candles"]),
                  ledger=ledger, **kw)


def ssid_from_env(required: bool) -> str | None:
    ssid = os.environ.get("POCKETBOT_SSID", "").strip()
    if not ssid and required:
        raise SystemExit("set POCKETBOT_SSID (see docs/POCKETBOT.md, 'Getting your SSID')")
    return ssid or None


def guard_account(mode: str, ssid: str, client) -> str:
    """Refuse any mismatch between what the config asks for and the account the SSID opens."""
    demo = client.is_demo()
    if ssid_is_demo(ssid) is not None and ssid_is_demo(ssid) != demo:
        raise SystemExit("SSID isDemo flag disagrees with the account the server opened; refusing")
    if mode == "demo" and not demo:
        raise SystemExit("mode is demo but this SSID opens a REAL account; refusing to trade")
    if mode == "live":
        if demo:
            raise SystemExit("mode is live but this SSID opens the demo account")
        if os.environ.get("POCKETBOT_REAL_MONEY") != REAL_MONEY_ACK:
            raise SystemExit(f"real money needs POCKETBOT_REAL_MONEY={REAL_MONEY_ACK}")
        log.warning("REAL MONEY MODE: orders will be placed on a real account")
    return "demo" if demo else "real"


@dataclass
class Session:
    feed: object
    broker: object
    feed_kind: str        # "pocket-option" | "synthetic"
    account: str          # "paper" | "demo" | "real"


@contextlib.asynccontextmanager
async def open_session(cfg: dict, paper: PaperBroker | None = None,
                       synthetic_delay: float = 0.2, seed: int | None = None,
                       synthetic_n: int | None = 5000,
                       synthetic_start: int | None = None) -> AsyncIterator[Session]:
    """Connect (or not), check the account, and hand back a feed and a broker.

    `paper` lets a long-running service keep one paper balance across
    reconnects instead of starting from scratch each time.
    """
    mode = cfg["mode"]
    ssid = ssid_from_env(required=mode != "paper")
    pcfg = cfg["paper"]
    if paper is None:
        paper = PaperBroker(float(pcfg.get("balance", 1000)), float(pcfg.get("payout", 0.85)))

    if not ssid:
        log.info("paper mode on a SYNTHETIC market (no POCKETBOT_SSID): results mean nothing")
        feed = SyntheticFeed(int(cfg["period"]), n=synthetic_n, seed=seed,
                             delay=synthetic_delay, start_time=synthetic_start)
        yield Session(feed, paper, "synthetic", "paper")
        return

    client = await _broker.connect(ssid, cfg.get("ws_url"))
    try:
        feed = PocketOptionFeed(client, cfg["asset"], int(cfg["period"]),
                                history_hours=float(cfg["history_hours"]))
        if mode == "paper":
            paper._payout = PocketOptionBroker(client, "paper").payout
            log.info("paper mode on LIVE %s candles; no orders will be sent", cfg["asset"])
            yield Session(feed, paper, "pocket-option", "paper")
        else:
            account = guard_account(mode, ssid, client)
            live = PocketOptionBroker(client, account)
            log.info("connected to Pocket Option: %s account, balance %.2f",
                     account.upper(), await live.balance())
            yield Session(feed, live, "pocket-option", account)
    finally:
        await client.shutdown()
