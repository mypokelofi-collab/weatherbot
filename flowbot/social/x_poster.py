"""Post momentum signals to X (Twitter).

When the signal engine fires an entry (score past the entry threshold and no
gate blocking), this composes a short post and sends it through the X API v2
`POST /2/tweets` endpoint, signed with OAuth 1.0a user context.

Guard rails, because an account that spams gets throttled or suspended - which
is the opposite of monetisation:

* only real entry signals are posted, never every bar;
* the same direction is not re-posted until it changes or `repeat_after_minutes`
  has passed;
* `min_interval_minutes` between any two posts and `max_posts_per_day` on top
  (the X Free API tier allows roughly 500 posts a month, ~16 a day);
* every post says it is an automated paper-trading signal and not financial
  advice.

`dry_run` (the default) writes each post to `x_posts.jsonl` in the state dir
and the log instead of sending it, so the output can be reviewed before any
keys exist. Credentials come only from the environment, never the config file:

    X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ..core.config import XConfig

log = logging.getLogger(__name__)

TWEET_URL = "https://api.x.com/2/tweets"
MAX_CHARS = 280
DAY_MS = 86_400_000

_COMPONENT_LABELS = {
    "trend": "EMA trend",
    "trend_slope": "trend slope",
    "macd": "MACD",
    "breakout": "breakout",
    "rsi": "RSI",
    "momentum_z": "momentum",
    "flow": "order flow",
    "book": "book imbalance",
}


# -- OAuth 1.0a --------------------------------------------------------------
def _pct(value: str) -> str:
    return quote(str(value), safe="~-._")


def oauth1_header(
    method: str,
    url: str,
    consumer_key: str,
    consumer_secret: str,
    token: str,
    token_secret: str,
    params: dict[str, str] | None = None,
    nonce: str | None = None,
    timestamp: int | None = None,
) -> str:
    """Authorization header for an OAuth 1.0a HMAC-SHA1 signed request.

    `params` are query/form parameters that take part in the signature. A JSON
    body (what /2/tweets takes) does not, so it is normally empty.
    """
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(timestamp if timestamp is not None else int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    all_params = {**(params or {}), **oauth}
    param_str = "&".join(
        f"{k}={v}" for k, v in sorted((_pct(k), _pct(v)) for k, v in all_params.items())
    )
    base = "&".join([method.upper(), _pct(url), _pct(param_str)])
    key = f"{_pct(consumer_secret)}&{_pct(token_secret)}"
    sig = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    oauth["oauth_signature"] = sig
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))


@dataclass(frozen=True)
class XCredentials:
    api_key: str
    api_secret: str
    access_token: str
    access_token_secret: str

    @classmethod
    def from_env(cls) -> "XCredentials | None":
        vals = [os.environ.get(k, "").strip() for k in
                ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")]
        return cls(*vals) if all(vals) else None


# -- composing -----------------------------------------------------------------
def _reason_label(reason: str) -> str:
    name = reason.split(":", 1)[0].strip()
    return _COMPONENT_LABELS.get(name, name.replace("_", " "))


def format_post(signal: dict[str, Any], cfg: XConfig) -> str:
    action = signal.get("action")
    long = action == "enter_long"
    price = float(signal.get("price") or 0.0)
    score = float(signal.get("score") or 0.0)
    adx = float((signal.get("features") or {}).get("adx") or 0.0)
    reasons = [_reason_label(r) for r in signal.get("reasons") or []][:3]

    head = f"{'🟢' if long else '🔴'} {cfg.asset} {cfg.timeframe} momentum: {'LONG' if long else 'SHORT'} signal"
    stats = f"Price ${price:,.0f} · score {score:+.2f}" + (f" · ADX {adx:.0f}" if adx else "")
    why = f"Driven by: {', '.join(reasons)}" if reasons else ""
    footer = cfg.disclaimer
    tags = " ".join(cfg.hashtags)

    lines = [head, stats, why, "", footer + (f" {tags}" if tags else "")]
    text = "\n".join(l for i, l in enumerate(lines) if l or i == 3).strip()
    if len(text) > MAX_CHARS and why:
        lines[2] = ""
        text = "\n".join(l for i, l in enumerate(lines) if l or i == 3).strip()
    return text[:MAX_CHARS]


# -- the poster ----------------------------------------------------------------
class XPoster:
    def __init__(
        self,
        cfg: XConfig,
        state_dir: str | Path = "data/state",
        credentials: XCredentials | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.cfg = cfg
        self.creds = credentials if credentials is not None else XCredentials.from_env()
        self.log_path = Path(state_dir) / "x_posts.jsonl"
        self._client = client
        self._sent: list[int] = []          # ms timestamps of accepted posts
        self._last_action: str | None = None
        self._last_action_ms = 0
        self.last_error: str | None = None
        self.posted = 0
        self._tasks: set[asyncio.Task] = set()

        if not cfg.dry_run and self.creds is None:
            log.warning("x poster: dry_run is off but X_API_* credentials are missing - "
                        "falling back to dry run")
            self.cfg = cfg.model_copy(update={"dry_run": True})

    # The bus hands us every closed bar; decide synchronously, send in the background.
    def on_bar(self, _topic: str, payload: dict) -> None:
        sig = (payload or {}).get("signal") or {}
        now = int(time.time() * 1000)
        if not self.should_post(sig, now):
            return
        text = format_post(sig, self.cfg)
        self._mark(sig["action"], now)
        task = asyncio.create_task(self.send(text, sig))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def should_post(self, sig: dict, now_ms: int) -> bool:
        action = sig.get("action")
        if action not in ("enter_long", "enter_short"):
            return False
        if abs(float(sig.get("score") or 0.0)) < self.cfg.min_score:
            return False
        if action == "enter_short" and not self.cfg.post_shorts:
            return False
        self._sent = [t for t in self._sent if now_ms - t < DAY_MS]
        if len(self._sent) >= self.cfg.max_posts_per_day:
            return False
        if self._sent and now_ms - self._sent[-1] < self.cfg.min_interval_minutes * 60_000:
            return False
        if (action == self._last_action
                and now_ms - self._last_action_ms < self.cfg.repeat_after_minutes * 60_000):
            return False
        return True

    def _mark(self, action: str, now_ms: int) -> None:
        self._sent.append(now_ms)
        self._last_action = action
        self._last_action_ms = now_ms

    async def send(self, text: str, sig: dict | None = None) -> dict:
        record: dict[str, Any] = {"ts": int(time.time() * 1000), "text": text,
                                  "dry_run": self.cfg.dry_run}
        if self.cfg.dry_run:
            log.info("x poster (dry run) would post:\n%s", text)
        else:
            try:
                record["response"] = await self._post(text)
                self.posted += 1
                self.last_error = None
                log.info("x poster: posted %s", record["response"].get("data", {}).get("id"))
            except Exception as exc:  # noqa: BLE001 - posting must never hurt trading
                self.last_error = str(exc)
                record["error"] = str(exc)
                log.warning("x poster: post failed: %s", exc)
        self._append(record)
        return record

    async def _post(self, text: str) -> dict:
        assert self.creds is not None
        c = self.creds
        headers = {
            "Authorization": oauth1_header("POST", TWEET_URL, c.api_key, c.api_secret,
                                           c.access_token, c.access_token_secret),
            "Content-Type": "application/json",
        }
        client = self._client or httpx.AsyncClient(timeout=15)
        try:
            resp = await client.post(TWEET_URL, headers=headers, json={"text": text})
        finally:
            if self._client is None:
                await client.aclose()
        if resp.status_code >= 300:
            raise RuntimeError(f"X API {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def _append(self, record: dict) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:  # pragma: no cover - disk issues
            log.warning("x poster: could not write %s: %s", self.log_path, exc)

    def status(self) -> dict:
        return {
            "enabled": self.cfg.enabled,
            "dry_run": self.cfg.dry_run,
            "credentials": self.creds is not None,
            "posts_last_24h": len(self._sent),
            "posted": self.posted,
            "last_error": self.last_error,
        }
