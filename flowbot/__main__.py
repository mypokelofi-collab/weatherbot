"""flowbot command line.

    python -m flowbot run                 # trade paper on the configured venue
    python -m flowbot run --sim           # offline demo, synthetic market
    python -m flowbot record --minutes 60 # capture a real feed for replay
    python -m flowbot backtest FILE       # replay a recording through the bot
    python -m flowbot info FILE           # what is in a recording
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .app import build_server, build_trader, resolve_config, setup_logging

log = logging.getLogger("flowbot")


def _parser() -> argparse.ArgumentParser:
    # Shared flags live on a parent parser so they work on either side of the
    # subcommand: `flowbot -c cfg.yml run` and `flowbot run -c cfg.yml` both work.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS,
                        help="path to a YAML config file")
    common.add_argument("--log-level", default=argparse.SUPPRESS)

    p = argparse.ArgumentParser(prog="flowbot", description=__doc__, parents=[common],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the bot and the dashboard", parents=[common])
    run.add_argument("--sim", action="store_true", help="use the offline simulator")
    run.add_argument("--venue", help="override the venue")
    run.add_argument("--symbol", help="override the symbol")
    run.add_argument("--port", type=int, help="dashboard port")
    run.add_argument("--host", help="dashboard bind address")
    run.add_argument("--no-server", action="store_true", help="headless")
    run.add_argument("--record", action="store_true", help="also record the feed")

    rec = sub.add_parser("record", parents=[common], help="record a live feed to disk")
    rec.add_argument("--minutes", type=float, default=60.0)
    rec.add_argument("--out", help="output path")

    bt = sub.add_parser("backtest", parents=[common], help="replay a recording through the full bot")
    bt.add_argument("recording")
    bt.add_argument("--speed", type=float, default=0.0)
    bt.add_argument("--json", action="store_true", help="emit machine-readable results")
    bt.add_argument("--equity", type=float, default=None)

    info = sub.add_parser("info", parents=[common], help="summarise a recording")
    info.add_argument("recording")

    sub.add_parser("config", parents=[common], help="print the effective configuration")
    return p


async def cmd_run(args, cfg) -> int:
    if args.sim:
        cfg.data.venue = "simulator"
    if args.venue:
        cfg.data.venue = args.venue
    if args.symbol:
        cfg.data.symbol = args.symbol
    if args.port:
        cfg.server.port = args.port
    if args.host:
        cfg.server.host = args.host
    if args.record:
        cfg.data.record = True

    trader, recorder = build_trader(cfg)
    await trader.start()

    if args.no_server:
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await trader.stop()
            if recorder:
                recorder.close()
        return 0

    import uvicorn

    app = build_server(cfg, trader)
    server = uvicorn.Server(uvicorn.Config(
        app, host=cfg.server.host, port=cfg.server.port,
        log_level=cfg.log_level.lower(), access_log=False,
    ))
    log.info("dashboard on http://%s:%d", cfg.server.host, cfg.server.port)
    try:
        await server.serve()
    finally:
        await trader.stop()
        if recorder:
            recorder.close()
            log.info("recording: %s", recorder.stats())
    return 0


async def cmd_record(args, cfg) -> int:
    cfg.data.record = True
    if args.out:
        cfg.data.record_path = args.out
    trader, recorder = build_trader(cfg)
    await trader.feed.start()
    log.info("recording %s for %.1f minutes…", trader.feed.venue, args.minutes)
    try:
        await asyncio.sleep(args.minutes * 60)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await trader.feed.stop()
        if recorder:
            recorder.close()
            print(json.dumps(recorder.stats(), indent=2))
    return 0


async def cmd_backtest(args, cfg) -> int:
    from .backtest.runner import run_backtest

    cfg.data.venue = "replay"
    cfg.data.replay_path = args.recording
    cfg.data.replay_speed = args.speed
    if args.equity:
        cfg.risk.start_equity = args.equity
    result = await run_backtest(cfg)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        from .backtest.report import print_report

        print_report(result)
    return 0


def cmd_info(args) -> int:
    from .data.recorder import recording_info

    print(json.dumps(recording_info(args.recording), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cfg = resolve_config(getattr(args, "config", None))
    setup_logging(getattr(args, "log_level", None) or cfg.log_level)

    command = args.command or "run"
    if command == "info":
        return cmd_info(args)
    if command == "config":
        print(json.dumps(cfg.to_dict(), indent=2))
        return 0

    runner = {
        "run": cmd_run,
        "record": cmd_record,
        "backtest": cmd_backtest,
    }[command]
    try:
        return asyncio.run(runner(args, cfg))
    except KeyboardInterrupt:
        log.info("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
