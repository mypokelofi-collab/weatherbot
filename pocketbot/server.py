"""The dashboard's HTTP side: one page, a read-only state API, pause/resume.

Every endpoint except /api/health needs the token when one is configured
(?token=... or `Authorization: Bearer ...`). The page reads the token from
its own URL and sends it along.
"""

from __future__ import annotations

import hmac
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from .monitor import Monitor

DASHBOARD = Path(__file__).parent / "dashboard" / "index.html"


def create_app(monitor: Monitor, token: str | None) -> FastAPI:
    app = FastAPI(title="pocketbot", docs_url=None, redoc_url=None)

    def check(request: Request, token_q: str | None) -> None:
        if not token:
            return
        supplied = token_q or ""
        header = request.headers.get("authorization", "")
        if not supplied and header.lower().startswith("bearer "):
            supplied = header[7:]
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(status_code=401, detail="bad or missing token")

    @app.middleware("http")
    async def no_store(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        # The page itself holds no data; it asks for the token before loading any.
        return FileResponse(DASHBOARD)

    @app.get("/api/health")
    async def health() -> dict:
        # Liveness: 200 while the process is up, even when the broker is down,
        # so docker does not restart the bot out from under its own retry loop.
        return {"ok": True, "status": monitor.status, "mode": monitor.mode,
                "feed": monitor.feed, "account": monitor.account}

    @app.get("/api/state")
    async def state(request: Request, token_q: str | None = Query(None, alias="token")):
        check(request, token_q)
        return JSONResponse(monitor.snapshot())

    @app.post("/api/control/{action}")
    async def control(action: str, request: Request,
                      token_q: str | None = Query(None, alias="token")):
        check(request, token_q)
        if action == "pause":
            monitor.paused = True
        elif action == "resume":
            monitor.paused = False
        else:
            raise HTTPException(status_code=404, detail=f"unknown action: {action}")
        monitor.event("info", f"new entries {'paused' if monitor.paused else 'resumed'} from the dashboard")
        return {"ok": True, "paused": monitor.paused}

    return app
