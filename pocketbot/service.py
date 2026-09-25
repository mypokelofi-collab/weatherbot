"""`pocketbot serve`: the bot, supervised, with its dashboard.

This is what runs on the VPS. The process never exits because of the broker:
a dropped websocket, an expired SSID or a refused account guard puts the
dashboard into an error state that says why, and the supervisor retries with
backoff. Fixing the SSID means editing .env and restarting the container.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from .broker import PaperBroker
from .monitor import Monitor, MonitorLogHandler
from .risk import RiskConfig, RiskManager
from .session import ledger_path, make_engine, open_session
from .stats import Ledger, Scorecard
from .strategy import StrategyConfig

log = logging.getLogger("pocketbot")

MIN_BACKOFF = 5.0
MAX_BACKOFF = 300.0
CONFIG_ERROR_WAIT = 120.0     # a bad SSID or guard refusal will not fix itself quickly


class Service:
    def __init__(self, cfg: dict, synthetic_delay: float | None = None):
        self.cfg = cfg
        self.risk = RiskManager(RiskConfig.from_dict(cfg.get("risk")))
        strategy = StrategyConfig.from_dict(cfg.get("strategy"))
        self.synthetic = not os.environ.get("POCKETBOT_SSID", "").strip()
        # A synthetic market's trades are noise; never mix them into the real ledger.
        self.ledger = Ledger(None if self.synthetic else ledger_path(cfg))
        history = self.ledger.load()
        self.scorecard = Scorecard(history)
        self.monitor = Monitor(cfg["mode"], cfg["asset"], int(cfg["period"]),
                               int(cfg["expiry_candles"]), strategy.name,
                               self.scorecard, self.risk)
        pcfg = cfg["paper"]
        start = float(pcfg.get("balance", 1000))
        restored = start + sum(t.pnl for t in history if t.account == "paper")
        self.paper = PaperBroker(restored, float(pcfg.get("payout", 0.85)))
        if synthetic_delay is None:
            synthetic_delay = float(cfg.get("synthetic_delay", 2.0))
        self.synthetic_delay = synthetic_delay
        if history:
            log.info("loaded %d trades from %s", len(history), self.ledger.path)

    async def run_forever(self) -> None:
        backoff = MIN_BACKOFF
        mon = self.monitor
        while True:
            started = time.time()
            mon.set_status("connecting")
            mon.connects += 1
            try:
                async with open_session(self.cfg, paper=self.paper,
                                        synthetic_delay=self.synthetic_delay,
                                        synthetic_n=None,
                                        synthetic_start=int(time.time())) as s:
                    mon.feed, mon.account = s.feed_kind, s.account
                    mon.balance = await s.broker.balance()
                    if not mon.equity:
                        mon.equity.append((time.time(), round(mon.balance, 2)))
                    engine = make_engine(self.cfg, s.feed, s.broker, self.ledger,
                                         risk=self.risk, scorecard=self.scorecard,
                                         monitor=mon)
                    await engine.run()
                mon.set_status("error", "the price feed ended; reconnecting")
                log.warning("price feed ended; reconnecting")
                wait = backoff
            except asyncio.CancelledError:
                mon.set_status("stopped")
                raise
            except SystemExit as exc:            # configuration / account guard problems
                mon.set_status("error", str(exc))
                log.error("%s", exc)
                wait = CONFIG_ERROR_WAIT
            except Exception as exc:
                mon.set_status("error", f"{type(exc).__name__}: {exc}")
                log.exception("session failed")
                wait = backoff
            # A session that ran for a while was healthy; start the backoff over.
            backoff = MIN_BACKOFF if time.time() - started > 300 else min(backoff * 2, MAX_BACKOFF)
            await asyncio.sleep(wait)


async def serve(cfg: dict, host: str | None = None, port: int | None = None) -> None:
    import uvicorn

    from .server import create_app

    svc = Service(cfg)
    handler = MonitorLogHandler(svc.monitor)
    logging.getLogger("pocketbot").addHandler(handler)

    server_cfg = cfg.get("server") or {}
    host = host or os.environ.get("POCKETBOT_HOST") or server_cfg.get("host", "127.0.0.1")
    port = int(port or os.environ.get("POCKETBOT_PORT") or server_cfg.get("port", 8040))
    token = os.environ.get("POCKETBOT_DASHBOARD_TOKEN", "").strip() or None
    if not token and host not in ("127.0.0.1", "localhost"):
        log.warning("dashboard on %s:%d has NO token; set POCKETBOT_DASHBOARD_TOKEN", host, port)

    app = create_app(svc.monitor, token)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning",
                                           access_log=False))
    bot = asyncio.create_task(svc.run_forever(), name="pocketbot")
    log.info("dashboard on http://%s:%d/%s", host, port, "?token=..." if token else "")
    try:
        await server.serve()
    finally:
        bot.cancel()
        try:
            await bot
        except (asyncio.CancelledError, Exception):
            pass
