"""Live state the dashboard reads.

The engine pushes into this as it goes; the web server only ever reads a
snapshot. Nothing here can place or change a trade - the only thing the
dashboard can write is the `paused` flag, which blocks new entries and never
touches a trade that is already open.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import asdict

from .market import Candle
from .risk import RiskManager, breakeven_win_rate
from .stats import Scorecard, Trade
from .strategy import Signal

MAX_CANDLES = 150
MAX_EVENTS = 200
MAX_EQUITY = 2000


class Monitor:
    def __init__(self, mode: str, asset: str, period: int, expiry_candles: int,
                 strategy: str, scorecard: Scorecard, risk: RiskManager):
        self.mode = mode
        self.asset = asset
        self.period = period
        self.expiry_candles = expiry_candles
        self.strategy = strategy
        self.scorecard = scorecard
        self.risk = risk
        self.started_at = time.time()

        self.status = "starting"          # starting | connecting | running | error | stopped
        self.error = ""
        self.feed = ""                    # "pocket-option" | "synthetic"
        self.account = ""                 # paper | demo | real
        self.connects = 0
        self.paused = False

        self.candles: deque[Candle] = deque(maxlen=MAX_CANDLES)
        self.last_candle_wall: float | None = None
        self.signal: dict = {}
        self.last_skip: dict = {}
        self.balance: float | None = None
        self.payout: float | None = None
        self.open: dict[str, Trade] = {}
        self.equity: deque[tuple[float, float]] = deque(maxlen=MAX_EQUITY)
        self.events: deque[dict] = deque(maxlen=MAX_EVENTS)

    # -- engine hooks --------------------------------------------------------
    def on_candles(self, candles: list[Candle], period: int) -> None:
        self.period = period
        self.candles.clear()
        self.candles.extend(candles[-MAX_CANDLES:])
        self.last_candle_wall = time.time()
        if self.status != "running":
            self.status = "running"
            self.error = ""

    def on_signal(self, sig: Signal, now: float) -> None:
        self.signal = {"time": now, "direction": sig.direction, "confidence": sig.confidence,
                       "reason": sig.reason, "features": sig.features}

    def on_skip(self, reason: str) -> None:
        self.last_skip = {"time": self.signal.get("time"), "reason": reason}

    def on_open(self, t: Trade, balance: float) -> None:
        self.open[t.id] = t
        self.balance = round(balance, 2)

    def on_settled(self, trades: list[Trade], balance: float, now: float) -> None:
        for t in trades:
            self.open.pop(t.id, None)
        self.balance = round(balance, 2)
        self.equity.append((now, round(balance, 2)))

    # -- supervisor hooks ----------------------------------------------------
    def set_status(self, status: str, error: str = "") -> None:
        self.status = status
        self.error = error

    def event(self, level: str, message: str) -> None:
        self.events.append({"time": time.time(), "level": level, "message": message})

    # -- read side -----------------------------------------------------------
    def snapshot(self, trade_limit: int = 50) -> dict:
        summary = self.scorecard.summary()
        recent = [asdict(t) for t in self.scorecard.trades[-trade_limit:]][::-1]
        stale = None
        if self.last_candle_wall is not None:
            stale = round(time.time() - self.last_candle_wall, 1)
        r = self.risk
        return {
            "server_time": time.time(),
            "status": self.status, "error": self.error,
            "mode": self.mode, "account": self.account, "feed": self.feed,
            "connects": self.connects, "paused": self.paused,
            "uptime": round(time.time() - self.started_at),
            "asset": self.asset, "period": self.period,
            "expiry_seconds": self.period * self.expiry_candles,
            "strategy": self.strategy,
            "balance": self.balance,
            "payout": self.payout,
            "breakeven": breakeven_win_rate(self.payout) if self.payout else None,
            "seconds_since_candle": stale,
            "summary": summary,
            "risk": {
                "day_pnl": round(r.day_pnl, 2), "day_trades": r.day_trades,
                "max_trades_per_day": r.cfg.max_trades_per_day,
                "daily_loss_limit": round(r.day_start_balance * r.cfg.daily_loss_limit, 2),
                "consecutive_losses": r.consecutive_losses,
                "paused_until": r.paused_until or None, "halted": r.halted,
                "min_payout": r.cfg.min_payout,
            },
            "signal": self.signal, "last_skip": self.last_skip,
            "candles": [asdict(c) for c in self.candles],
            "open_trades": [asdict(t) for t in self.open.values()],
            "trades": recent,
            "equity": list(self.equity),
            "events": list(self.events)[::-1][:80],
        }


class MonitorLogHandler(logging.Handler):
    """Copies the bot's log lines into the dashboard's event feed."""

    def __init__(self, monitor: Monitor):
        super().__init__(logging.INFO)
        self.monitor = monitor

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.monitor.event(record.levelname.lower(), record.getMessage())
        except Exception:  # pragma: no cover - logging must never raise
            pass
