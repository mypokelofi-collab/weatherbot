"""Durable ledger.

A trading bot that loses its history on restart cannot be audited, and on a
VPS restarts happen - deploys, OOM kills, reboots. Everything that matters
goes into SQLite: orders, fills, closed trades, equity samples, signals and
lifecycle events. The dashboard reads the live objects; this is the record.

Writes are small and synchronous. At our event rate (a handful of orders per
hour, an equity sample every few seconds) that costs nothing and removes any
question about what was flushed when the process died.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from ..core.types import ClosedTrade, Fill, Order, Side, Signal

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at INTEGER, venue TEXT, symbol TEXT, interval TEXT,
    mode TEXT, start_equity REAL, config TEXT, real_data INTEGER
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, session_id INTEGER, ts INTEGER, side TEXT, type TEXT,
    qty REAL, price REAL, tif TEXT, status TEXT, filled_qty REAL, avg_price REAL,
    fees REAL, tag TEXT, reject_reason TEXT, payload TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, ts INTEGER,
    order_id TEXT, side TEXT, price REAL, qty REAL, fee REAL, liquidity TEXT,
    slippage_bps REAL, levels INTEGER
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, trade_no INTEGER,
    side TEXT, qty REAL, entry_ts INTEGER, entry_price REAL, exit_ts INTEGER,
    exit_price REAL, gross_pnl REAL, fees REAL, pnl REAL, r_multiple REAL,
    bars_held INTEGER, entry_reason TEXT, exit_reason TEXT,
    mfe_r REAL, mae_r REAL, entry_slip_bps REAL, exit_slip_bps REAL
);
CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, ts INTEGER,
    equity REAL, realized REAL, unrealized REAL, mark REAL, position TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, ts INTEGER,
    bar_time INTEGER, action TEXT, score REAL, regime TEXT, price REAL,
    atr REAL, payload TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, ts INTEGER,
    kind TEXT, message TEXT, payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts);
CREATE INDEX IF NOT EXISTS idx_trades_exit ON trades(exit_ts);
CREATE INDEX IF NOT EXISTS idx_signals_bar ON signals(bar_time);
"""


class Store:
    def __init__(self, path: str | Path, enabled: bool = True) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self.session_id = 0
        self._db: sqlite3.Connection | None = None
        if enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self.path), check_same_thread=False)
            self._db.executescript(SCHEMA)
            self._db.commit()

    # -- lifecycle ---------------------------------------------------------
    def start_session(self, meta: dict[str, Any]) -> int:
        if not self._db:
            return 0
        cur = self._db.execute(
            "INSERT INTO sessions (started_at, venue, symbol, interval, mode,"
            " start_equity, config, real_data) VALUES (?,?,?,?,?,?,?,?)",
            (
                meta.get("started_at", 0), meta.get("venue", ""), meta.get("symbol", ""),
                meta.get("interval", ""), meta.get("mode", "paper"),
                meta.get("start_equity", 0.0), json.dumps(meta.get("config", {})),
                1 if meta.get("real_data", True) else 0,
            ),
        )
        self._db.commit()
        self.session_id = int(cur.lastrowid or 0)
        return self.session_id

    def close(self) -> None:
        if self._db:
            self._db.commit()
            self._db.close()
            self._db = None

    # -- writes ------------------------------------------------------------
    def record_order(self, order: Order) -> None:
        if not self._db:
            return
        self._db.execute(
            "INSERT INTO orders (id, session_id, ts, side, type, qty, price, tif,"
            " status, filled_qty, avg_price, fees, tag, reject_reason, payload)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET status=excluded.status,"
            " filled_qty=excluded.filled_qty, avg_price=excluded.avg_price,"
            " fees=excluded.fees, reject_reason=excluded.reject_reason,"
            " payload=excluded.payload",
            (
                order.id, self.session_id, order.ts, order.side.value, order.type.value,
                order.qty, order.price, order.tif.value, order.status.value,
                order.filled_qty, order.avg_price, order.fees, order.tag,
                order.reject_reason, json.dumps(order.to_dict()),
            ),
        )
        self._db.commit()

    def record_fill(self, fill: Fill) -> None:
        if not self._db:
            return
        self._db.execute(
            "INSERT INTO fills (session_id, ts, order_id, side, price, qty, fee,"
            " liquidity, slippage_bps, levels) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                self.session_id, fill.ts, fill.order_id, fill.side.value, fill.price,
                fill.qty, fill.fee, fill.liquidity.value, fill.slippage_bps,
                fill.level_depth,
            ),
        )
        self._db.commit()

    def record_trade(self, t: ClosedTrade) -> None:
        if not self._db:
            return
        self._db.execute(
            "INSERT INTO trades (session_id, trade_no, side, qty, entry_ts, entry_price,"
            " exit_ts, exit_price, gross_pnl, fees, pnl, r_multiple, bars_held,"
            " entry_reason, exit_reason, mfe_r, mae_r, entry_slip_bps, exit_slip_bps)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.session_id, t.id, t.side.value, t.qty, t.entry_ts, t.entry_price,
                t.exit_ts, t.exit_price, t.gross_pnl, t.fees, t.pnl, t.r_multiple,
                t.bars_held, t.entry_reason, t.exit_reason, t.max_favorable_r,
                t.max_adverse_r, t.entry_slippage_bps, t.exit_slippage_bps,
            ),
        )
        self._db.commit()

    def record_equity(self, ts: int, equity: float, realized: float,
                      unrealized: float, mark: float, position: dict | None) -> None:
        if not self._db:
            return
        self._db.execute(
            "INSERT INTO equity (session_id, ts, equity, realized, unrealized, mark, position)"
            " VALUES (?,?,?,?,?,?,?)",
            (self.session_id, ts, equity, realized, unrealized, mark,
             json.dumps(position) if position else None),
        )
        self._db.commit()

    def record_signal(self, sig: Signal) -> None:
        if not self._db:
            return
        self._db.execute(
            "INSERT INTO signals (session_id, ts, bar_time, action, score, regime,"
            " price, atr, payload) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                self.session_id, sig.ts, sig.bar_time, sig.action.value, sig.score,
                sig.regime.value, sig.price, sig.atr, json.dumps(sig.to_dict()),
            ),
        )
        self._db.commit()

    def record_event(self, kind: str, message: str, ts: int, payload: dict | None = None) -> None:
        if not self._db:
            return
        self._db.execute(
            "INSERT INTO events (session_id, ts, kind, message, payload) VALUES (?,?,?,?,?)",
            (self.session_id, ts, kind, message, json.dumps(payload or {})),
        )
        self._db.commit()

    # -- reads -------------------------------------------------------------
    def load_trades(self, session_id: int | None = None) -> list[ClosedTrade]:
        if not self._db:
            return []
        sid = self.session_id if session_id is None else session_id
        rows = self._db.execute(
            "SELECT trade_no, side, qty, entry_ts, entry_price, exit_ts, exit_price,"
            " gross_pnl, fees, pnl, r_multiple, bars_held, entry_reason, exit_reason,"
            " mfe_r, mae_r, entry_slip_bps, exit_slip_bps FROM trades"
            " WHERE session_id=? ORDER BY trade_no", (sid,),
        ).fetchall()
        return [
            ClosedTrade(
                id=r[0], side=Side(r[1]), qty=r[2], entry_ts=r[3], entry_price=r[4],
                exit_ts=r[5], exit_price=r[6], gross_pnl=r[7], fees=r[8], pnl=r[9],
                r_multiple=r[10], bars_held=r[11], entry_reason=r[12], exit_reason=r[13],
                max_favorable_r=r[14], max_adverse_r=r[15],
                entry_slippage_bps=r[16], exit_slippage_bps=r[17],
            )
            for r in rows
        ]

    def load_equity(self, session_id: int | None = None, limit: int = 5000) -> list[tuple[int, float]]:
        if not self._db:
            return []
        sid = self.session_id if session_id is None else session_id
        rows = self._db.execute(
            "SELECT ts, equity FROM equity WHERE session_id=? ORDER BY ts DESC LIMIT ?",
            (sid, limit),
        ).fetchall()
        return list(reversed([(int(r[0]), float(r[1])) for r in rows]))

    def last_session(self) -> dict | None:
        if not self._db:
            return None
        row = self._db.execute(
            "SELECT id, started_at, venue, symbol, start_equity FROM sessions"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return {"id": row[0], "started_at": row[1], "venue": row[2],
                "symbol": row[3], "start_equity": row[4]}

    def sessions(self, limit: int = 20) -> list[dict]:
        if not self._db:
            return []
        rows = self._db.execute(
            "SELECT id, started_at, venue, symbol, interval, mode, start_equity, real_data"
            " FROM sessions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            {"id": r[0], "started_at": r[1], "venue": r[2], "symbol": r[3],
             "interval": r[4], "mode": r[5], "start_equity": r[6], "real_data": bool(r[7])}
            for r in rows
        ]

    def recent_events(self, limit: int = 100) -> list[dict]:
        if not self._db:
            return []
        rows = self._db.execute(
            "SELECT ts, kind, message FROM events WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (self.session_id, limit),
        ).fetchall()
        return [{"ts": r[0], "kind": r[1], "message": r[2]} for r in rows]
