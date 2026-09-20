"""Dashboard server: REST for the details, websocket for the live view.

Payload strategy matters here. The full state snapshot (240 candles, trade
history, orders, config) is tens of kilobytes; pushing that four times a
second would waste bandwidth and make the page janky. So:

  * a full snapshot on connect, on every bar close, on every trade or event,
    and as a 15s heartbeat;
  * a small tick 4x a second with the things that actually move - price, book,
    position, equity, signal score, tape.

The browser merges ticks into the last snapshot it has. Anything that changes
structurally (a new bar, a fill) arrives as a snapshot, so the two can never
drift apart for long.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..bot.trader import Trader
from ..core.config import AppConfig

log = logging.getLogger(__name__)

DASHBOARD_DIR = Path(__file__).parent / "dashboard"


def tick_payload(trader: Trader) -> dict[str, Any]:
    """The small, high-frequency update."""
    snap_book = trader.last_book
    pos = trader.portfolio.position
    return {
        "ts": trader.venue_now or 0,
        "last_price": trader.last_trade_price,
        "next_bar_in_ms": trader.snapshot_next_bar_ms(),
        "book": snap_book.to_dict(15) if snap_book else None,
        "portfolio": trader.portfolio.to_dict(),
        "position_mgmt": trader.positions.to_dict(pos),
        "feed": trader.feed.health.to_dict(),
        "tape": trader.tape_ring.tail(25),
        "tape_stats": trader.tape.stats(),
        "cvd": trader.cvd_series.tail(3),
        "risk": trader.risk.to_dict(),
        "execution": trader.broker.stats(),
        "trading_enabled": trader.trading_enabled,
        "running": trader.running,
        "open_bar": (
            trader.aggregator.current.to_dict() if trader.aggregator.current else None
        ),
    }


def create_app(trader: Trader, cfg: AppConfig) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.push_task = asyncio.create_task(push_loop(application), name="ws-push")
        application.state.bus_task = asyncio.create_task(bus_loop(application), name="ws-bus")
        try:
            yield
        finally:
            for name in ("push_task", "bus_task"):
                task = getattr(application.state, name, None)
                if task:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task

    app = FastAPI(title=cfg.server.title, docs_url="/api/docs", redoc_url=None,
                  lifespan=lifespan)
    app.state.trader = trader
    app.state.cfg = cfg
    app.state.clients: set[WebSocket] = set()

    token = cfg.server.auth_token

    def check_auth(request_token: str | None, header: str | None) -> None:
        if not token:
            return
        supplied = request_token or ""
        if not supplied and header and header.lower().startswith("bearer "):
            supplied = header[7:]
        if supplied != token:
            raise HTTPException(status_code=401, detail="bad or missing token")

    # Every deploy can change the page, the CSS or the JS, and this app has
    # no content-hashed filenames to bust a cache with. Without an explicit
    # Cache-Control, browsers may reuse a stale disk-cached copy on a plain
    # navigation even after Last-Modified/ETag have changed - a real
    # redeploy went out with a CSS ordering fix and a fresh tab still
    # rendered the old layout until this was added. `no-cache` still lets
    # the browser keep a local copy; it just forces a conditional GET (cheap
    # 304 on a hit) instead of trusting the cache blindly.
    @app.middleware("http")
    async def no_cache_for_the_page_and_its_assets(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # -- pages -------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(DASHBOARD_DIR / "index.html")

    if DASHBOARD_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(DASHBOARD_DIR)), name="static")

    # -- read API ----------------------------------------------------------
    @app.get("/api/health")
    async def health() -> dict:
        feed = trader.feed.health
        healthy = trader.running and feed.connected
        return {
            "ok": healthy,
            "running": trader.running,
            "feed_connected": feed.connected,
            "real_data": trader.feed.real,
            "venue": trader.feed.venue,
            "symbol": cfg.data.symbol,
            "equity": round(trader.portfolio.equity, 2),
            "position": trader.portfolio.position.side.value if trader.portfolio.position else None,
            "last_trade_ts": feed.last_trade_ts,
        }

    @app.get("/api/state")
    async def state(request: Request, token_q: str | None = Query(None, alias="token")) -> JSONResponse:
        check_auth(token_q, request.headers.get("authorization"))
        return JSONResponse(trader.snapshot())

    @app.get("/api/tick")
    async def tick(request: Request, token_q: str | None = Query(None, alias="token")) -> JSONResponse:
        check_auth(token_q, request.headers.get("authorization"))
        return JSONResponse(tick_payload(trader))

    @app.get("/api/trades")
    async def trades(
        request: Request, limit: int = 200, token_q: str | None = Query(None, alias="token")
    ) -> JSONResponse:
        check_auth(token_q, request.headers.get("authorization"))
        return JSONResponse([t.to_dict() for t in trader.portfolio.trades[-limit:]])

    @app.get("/api/orders")
    async def orders(
        request: Request, limit: int = 100, token_q: str | None = Query(None, alias="token")
    ) -> JSONResponse:
        check_auth(token_q, request.headers.get("authorization"))
        return JSONResponse([o.to_dict() for o in trader.broker.recent_orders(limit)])

    @app.get("/api/signal")
    async def signal(
        request: Request, token_q: str | None = Query(None, alias="token")
    ) -> JSONResponse:
        check_auth(token_q, request.headers.get("authorization"))
        sig = trader.last_signal
        history = [s.to_dict() for s in trader.engine.history[-80:]]
        return JSONResponse({"current": sig.to_dict() if sig else None, "history": history})

    @app.get("/api/book")
    async def book(
        request: Request, levels: int = 25, token_q: str | None = Query(None, alias="token")
    ) -> JSONResponse:
        check_auth(token_q, request.headers.get("authorization"))
        b = trader.last_book
        return JSONResponse(b.to_dict(levels) if b else {})

    @app.get("/api/events")
    async def events(
        request: Request, limit: int = 100, token_q: str | None = Query(None, alias="token")
    ) -> JSONResponse:
        check_auth(token_q, request.headers.get("authorization"))
        return JSONResponse(trader.events.tail(limit))

    @app.get("/api/polymarket")
    async def polymarket(
        request: Request, token_q: str | None = Query(None, alias="token")
    ) -> JSONResponse:
        check_auth(token_q, request.headers.get("authorization"))
        pipe = trader.polymarket
        return JSONResponse(pipe.state() if pipe else {"enabled": False})

    @app.get("/api/config")
    async def config(
        request: Request, token_q: str | None = Query(None, alias="token")
    ) -> JSONResponse:
        check_auth(token_q, request.headers.get("authorization"))
        payload = cfg.to_dict()
        # Never echo the dashboard secret back over the wire, authenticated or not -
        # the UI has no use for it and it would defeat the token gate on every
        # other endpoint the moment anyone reads this one.
        server_section = payload.get("server")
        if isinstance(server_section, dict) and server_section.get("auth_token"):
            server_section["auth_token"] = "***"
        return JSONResponse(payload)

    @app.get("/api/sessions")
    async def sessions(
        request: Request, token_q: str | None = Query(None, alias="token")
    ) -> JSONResponse:
        check_auth(token_q, request.headers.get("authorization"))
        if not trader.store:
            return JSONResponse([])
        return JSONResponse(trader.store.sessions())

    # -- control API -------------------------------------------------------
    @app.post("/api/control/{action}")
    async def control(
        action: str,
        request: Request,
        token_q: str | None = Query(None, alias="token"),
    ) -> JSONResponse:
        check_auth(token_q, request.headers.get("authorization"))
        body: dict = {}
        with contextlib.suppress(Exception):
            body = await request.json()

        if action == "pause":
            trader.set_trading_enabled(False)
        elif action == "resume":
            trader.set_trading_enabled(True)
        elif action == "flatten":
            trader.flatten(body.get("reason", "manual flatten from dashboard"))
        elif action == "kill":
            trader.kill(body.get("reason", "dashboard"))
        elif action == "revive":
            trader.revive()
        elif action == "start":
            await trader.start()
        elif action == "stop":
            await trader.stop()
        else:
            raise HTTPException(status_code=404, detail=f"unknown action: {action}")
        await broadcast_snapshot(app)
        return JSONResponse({"ok": True, "action": action})

    @app.post("/api/params/{section}")
    async def params(
        section: str,
        request: Request,
        token_q: str | None = Query(None, alias="token"),
    ) -> JSONResponse:
        check_auth(token_q, request.headers.get("authorization"))
        body = await request.json()
        try:
            applied = trader.update_params(section, body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await broadcast_snapshot(app)
        return JSONResponse({"ok": True, "applied": applied})

    # -- websocket ---------------------------------------------------------
    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        if token:
            supplied = socket.query_params.get("token", "")
            if supplied != token:
                await socket.close(code=4401)
                return
        await socket.accept()
        app.state.clients.add(socket)
        try:
            await socket.send_text(json.dumps({"type": "snapshot", "data": trader.snapshot()}))
            while True:
                # The client does not need to say anything; this keeps the
                # connection alive and lets us notice a dead peer.
                msg = await socket.receive_text()
                if msg == "ping":
                    await socket.send_text(json.dumps({"type": "pong"}))
                elif msg == "snapshot":
                    await socket.send_text(
                        json.dumps({"type": "snapshot", "data": trader.snapshot()})
                    )
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            app.state.clients.discard(socket)

    return app


async def send_all(app: FastAPI, payload: dict) -> None:
    dead = []
    text = json.dumps(payload, default=str)
    for client in list(app.state.clients):
        try:
            await client.send_text(text)
        except Exception:  # noqa: BLE001 - a dropped browser is routine
            dead.append(client)
    for client in dead:
        app.state.clients.discard(client)


async def broadcast_snapshot(app: FastAPI) -> None:
    if not app.state.clients:
        return
    await send_all(app, {"type": "snapshot", "data": app.state.trader.snapshot()})


async def push_loop(app: FastAPI) -> None:
    """High-frequency ticks plus a periodic full snapshot."""
    cfg: AppConfig = app.state.cfg
    trader: Trader = app.state.trader
    interval = 1.0 / max(0.5, cfg.server.broadcast_hz)
    last_full = 0.0
    while True:
        try:
            await asyncio.sleep(interval)
            if not app.state.clients:
                continue
            await send_all(app, {"type": "tick", "data": tick_payload(trader)})
            loop_now = asyncio.get_event_loop().time()
            if loop_now - last_full > 15:
                last_full = loop_now
                await broadcast_snapshot(app)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("push loop error")


async def bus_loop(app: FastAPI) -> None:
    """Anything structural (bar close, trade, event) forces a fresh snapshot."""
    trader: Trader = app.state.trader
    queue = trader.bus.subscribe({"bar", "trade", "event", "feed"})
    while True:
        try:
            topic, payload = await queue.get()
            if not app.state.clients:
                continue
            if topic in ("bar", "trade"):
                await broadcast_snapshot(app)
            else:
                await send_all(app, {"type": topic, "data": payload})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("bus loop error")
