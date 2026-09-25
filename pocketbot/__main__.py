"""pocketbot command line.

    python -m pocketbot sim                     # offline paper demo on a synthetic market
    python -m pocketbot run                     # paper/demo/live, per config `mode`
    python -m pocketbot assets                  # open assets and their payouts (needs SSID)
    python -m pocketbot fetch --hours 24        # save live candles to CSV (needs SSID)
    python -m pocketbot backtest FILE.csv       # run the bot over a CSV of candles
    python -m pocketbot stats                   # scorecard from the trade ledger
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import yaml

from .broker import (ListFeed, PaperBroker, PocketOptionBroker, PocketOptionFeed,
                     SyntheticFeed, connect, ssid_is_demo)
from .engine import Engine
from .market import load_csv, save_csv, Candle
from .risk import RiskConfig, RiskManager
from .stats import Ledger, Scorecard
from .strategy import StrategyConfig

log = logging.getLogger("pocketbot")

REAL_MONEY_ACK = "yes-i-accept-the-risk"
DEFAULT_CONFIG = "config/pocketbot.yml"


def load_config(path: str | None) -> dict:
    p = Path(path or DEFAULT_CONFIG)
    cfg = yaml.safe_load(p.read_text()) if p.exists() else {}
    cfg.setdefault("mode", "paper")
    cfg.setdefault("asset", "EURUSD_otc")
    cfg.setdefault("period", 60)
    cfg.setdefault("expiry_candles", 3)
    cfg.setdefault("history_hours", 3)
    cfg.setdefault("paper", {})
    if cfg["mode"] not in ("paper", "demo", "live"):
        raise SystemExit(f"mode must be paper, demo or live, not {cfg['mode']!r}")
    return cfg


def _engine(cfg: dict, feed, broker, ledger: Ledger, **kw) -> Engine:
    return Engine(feed=feed, broker=broker,
                  risk=RiskManager(RiskConfig.from_dict(cfg.get("risk"))),
                  strategy=StrategyConfig.from_dict(cfg.get("strategy")),
                  asset=cfg["asset"], expiry_candles=int(cfg["expiry_candles"]),
                  ledger=ledger, **kw)


def _ssid(required: bool) -> str | None:
    ssid = os.environ.get("POCKETBOT_SSID", "").strip()
    if not ssid and required:
        raise SystemExit("set POCKETBOT_SSID (see docs/POCKETBOT.md, 'Getting your SSID')")
    return ssid or None


def _guard_account(mode: str, ssid: str, client) -> str:
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


async def cmd_run(cfg: dict, args) -> dict:
    mode = cfg["mode"]
    ssid = _ssid(required=mode != "paper")
    ledger = Ledger(cfg.get("ledger"))
    paper = cfg["paper"]
    if not ssid:
        log.info("paper mode on a SYNTHETIC market (no POCKETBOT_SSID): results mean nothing")
        feed = SyntheticFeed(int(cfg["period"]), delay=args.delay, seed=args.seed)
        broker = PaperBroker(float(paper.get("balance", 1000)), float(paper.get("payout", 0.85)))
        return await _engine(cfg, feed, broker, ledger).run()

    client = await connect(ssid, cfg.get("ws_url"))
    try:
        feed = PocketOptionFeed(client, cfg["asset"], int(cfg["period"]),
                                history_hours=float(cfg["history_hours"]))
        if mode == "paper":
            live = PocketOptionBroker(client, "paper")
            broker = PaperBroker(float(paper.get("balance", 1000)), live.payout)
            log.info("paper mode on LIVE %s candles; no orders will be sent", cfg["asset"])
        else:
            account = _guard_account(mode, ssid, client)
            broker = PocketOptionBroker(client, account)
            log.info("%s account, balance %.2f", account.upper(), await broker.balance())
        return await _engine(cfg, feed, broker, ledger).run()
    finally:
        await client.shutdown()


async def cmd_sim(cfg: dict, args) -> dict:
    feed = SyntheticFeed(int(cfg["period"]), n=args.candles, seed=args.seed, delay=args.delay)
    paper = cfg["paper"]
    broker = PaperBroker(float(paper.get("balance", 1000)), float(paper.get("payout", 0.85)))
    return await _engine(cfg, feed, broker, Ledger(None)).run()


async def cmd_backtest(cfg: dict, args) -> dict:
    candles = load_csv(args.file)
    period = _infer_period(candles) or int(cfg["period"])
    payout = args.payout if args.payout is not None else float(cfg["paper"].get("payout", 0.85))
    broker = PaperBroker(float(cfg["paper"].get("balance", 1000)), payout)
    eng = _engine(cfg, ListFeed(candles, period), broker, Ledger(None), quiet=args.json)
    # A backtest must not be throttled by wall-clock limits meant for live trading.
    eng.risk.cfg.max_trades_per_day = 10**9
    return await eng.run()


async def cmd_fetch(cfg: dict, args) -> None:
    client = await connect(_ssid(required=True), cfg.get("ws_url"))
    try:
        period = int(cfg["period"])
        rows = int(args.hours * 3600 / period)
        gen = client.get_candles_live(cfg["asset"], period, hours=args.hours, max_rows=rows)
        closed, _ = await gen.__anext__()
        await gen.aclose()
        out = args.out or f"data/pocketbot/{cfg['asset']}_{period}s.csv"
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        n = save_csv(out, (Candle.from_dict(c) for c in closed))
        print(f"wrote {n} candles to {out}")
    finally:
        await client.shutdown()


async def cmd_assets(cfg: dict, args) -> None:
    client = await connect(_ssid(required=True), cfg.get("ws_url"))
    try:
        assets = [a for a in await client.active_assets() if a.get("is_active", True)]
        assets.sort(key=lambda a: a.get("payout") or 0, reverse=True)
        for a in assets[: args.top]:
            p = (a.get("payout") or 0) / 100
            be = f"{1 / (1 + p):.1%}" if p else "-"
            print(f"{a.get('symbol', '?'):<22} payout {p:>5.0%}  breakeven {be}")
    finally:
        await client.shutdown()


def cmd_stats(cfg: dict, args) -> dict:
    sc = Scorecard(Ledger(cfg.get("ledger")).load())
    return sc.summary()


def _infer_period(candles: list[Candle]) -> int | None:
    gaps = sorted(b.time - a.time for a, b in zip(candles, candles[1:]) if b.time > a.time)
    return gaps[len(gaps) // 2] if gaps else None


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="pocketbot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default=None)
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="trade per the config mode")
    run.add_argument("--delay", type=float, default=0.2, help="synthetic feed only: s per candle")
    run.add_argument("--seed", type=int, default=None)

    sim = sub.add_parser("sim", help="offline paper demo")
    sim.add_argument("--candles", type=int, default=3000)
    sim.add_argument("--seed", type=int, default=None)
    sim.add_argument("--delay", type=float, default=0.0)

    bt = sub.add_parser("backtest", help="run the bot over a candle CSV")
    bt.add_argument("file")
    bt.add_argument("--payout", type=float, default=None, help="e.g. 0.85")
    bt.add_argument("--json", action="store_true")

    fe = sub.add_parser("fetch", help="download candles to CSV")
    fe.add_argument("--hours", type=float, default=24.0)
    fe.add_argument("--out")

    a = sub.add_parser("assets", help="list open assets by payout")
    a.add_argument("--top", type=int, default=30)

    sub.add_parser("stats", help="scorecard from the ledger")

    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)

    if args.command == "stats":
        result = cmd_stats(cfg, args)
    else:
        fn = {"run": cmd_run, "sim": cmd_sim, "backtest": cmd_backtest,
              "fetch": cmd_fetch, "assets": cmd_assets}[args.command]
        try:
            result = asyncio.run(fn(cfg, args))
        except KeyboardInterrupt:
            return
    if isinstance(result, dict):
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    sys.exit(main())
