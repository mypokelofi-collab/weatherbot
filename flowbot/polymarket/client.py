"""Polymarket read-only client: Gamma for the questions, CLOB for the book.

Read-only on purpose. Placing a real order on Polymarket means signing EIP-712
messages with a wallet key and holding USDC on Polygon; that is a different
project with a different risk review (and its own legal constraints on who may
trade). This client fetches, parses and normalises - nothing here can spend.

The two endpoints answer different questions:
  * Gamma  - which markets exist, what exactly do they resolve on, when.
  * CLOB   - what is resting on the book for a given outcome token, right now.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from ..core.types import BookLevel, BookSnapshot, now_ms
from .market import Outcome, PredictionMarket, ResolutionSpec, parse_iso

log = logging.getLogger(__name__)


def _as_list(value: Any) -> list:
    """Gamma returns several fields as JSON-encoded strings."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


def infer_resolution(raw: dict) -> ResolutionSpec:
    """Best-effort parse of the settlement rule from the market description.

    Deliberately conservative: anything it cannot pin down stays `unknown`,
    and an unknown rule blocks trading rather than getting a guess. The
    patterns here cover the BTC up/down family; every new market family needs
    its own reading before it is allowed through.
    """
    text = " ".join(
        str(raw.get(k, "")) for k in ("description", "question", "slug", "resolutionSource")
    )
    spec = ResolutionSpec(description=str(raw.get("description", "")))

    lowered = text.lower()
    if "binance" in lowered:
        spec.reference = "binance:BTCUSDT:1m-close"
    elif "coinbase" in lowered:
        spec.reference = "coinbase:BTC-USD:1m-close"
    elif "chainlink" in lowered:
        spec.reference = "chainlink:BTC-USD"
    elif "pyth" in lowered:
        spec.reference = "pyth:BTC-USD"

    if re.search(r"\bet\b|eastern", lowered):
        spec.timezone_note = "market clock is US Eastern - convert, do not assume UTC"
    elif "utc" in lowered:
        spec.timezone_note = "UTC"

    spec.open_ts = parse_iso(raw.get("startDate") or raw.get("start_date_iso"))
    spec.close_ts = parse_iso(raw.get("endDate") or raw.get("end_date_iso"))

    # The recurring 5m/15m/4h/daily "crypto market" family (Gamma tags these
    # with a structured `cryptoMarketConfig`, which is far more reliable than
    # parsing "time-weighted average price" out of the description) compares
    # the close of the window to its own start, not to a printed dollar
    # level. There is no "$X" in the text for these at all - the regex below
    # would never match them, which is correct: the strike genuinely is not
    # known until the window opens, and the engine fills it in from our own
    # price series once it does (see `PolymarketPipeline._resolve_strike`).
    crypto_cfg = raw.get("cryptoMarketConfig") or {}
    if crypto_cfg.get("twapEnabled"):
        asset = str(crypto_cfg.get("asset") or "btc").upper()
        lookback = crypto_cfg.get("twapLookbackSeconds", 60)
        spec.reference = f"chainlink:{asset}-USD:twap{lookback}s-vs-window-open"
        spec.strike_mode = "window_open"
        spec.timezone_note = spec.timezone_note or (
            "UTC - window boundaries are UTC-aligned regardless of the ET wording"
        )
        # `open_ts` above is when the market opened for TRADING, which for
        # this family can be a day before its comparison window starts.
        # `eventStartTime` is the window itself; PredictionMarket carries it
        # separately as `window_open_ts` since ResolutionSpec has no market
        # context of its own.
        return spec

    # "above $70,000" style strikes (the daily/hourly fixed-strike family)
    m = re.search(r"\$([0-9][0-9,]{2,})", text)
    if m:
        try:
            spec.strike = float(m.group(1).replace(",", ""))
            spec.strike_known = True
        except ValueError:
            pass
    return spec


def parse_market(raw: dict) -> PredictionMarket:
    token_ids = [str(t) for t in _as_list(raw.get("clobTokenIds"))]
    names = [str(n) for n in _as_list(raw.get("outcomes"))] or ["Yes", "No"]
    prices = [float(p) for p in _as_list(raw.get("outcomePrices")) or []]

    outcomes: list[Outcome] = []
    for i, name in enumerate(names):
        outcomes.append(Outcome(
            name=name,
            token_id=token_ids[i] if i < len(token_ids) else "",
            last_price=prices[i] if i < len(prices) else 0.0,
        ))

    return PredictionMarket(
        id=str(raw.get("id", "")),
        slug=str(raw.get("slug", "")),
        question=str(raw.get("question", "")),
        condition_id=str(raw.get("conditionId", "")),
        outcomes=outcomes,
        end_ts=parse_iso(raw.get("endDate") or raw.get("end_date_iso")),
        start_ts=parse_iso(raw.get("startDate") or raw.get("start_date_iso")),
        window_open_ts=(
            parse_iso(raw.get("eventStartTime"))
            or parse_iso(raw.get("startDate") or raw.get("start_date_iso"))
        ),
        volume=float(raw.get("volumeNum") or raw.get("volume") or 0) or 0.0,
        liquidity=float(raw.get("liquidityNum") or raw.get("liquidity") or 0) or 0.0,
        tick_size=float(raw.get("orderPriceMinTickSize") or 0.01),
        min_order_size=float(raw.get("orderMinSize") or 5.0),
        neg_risk=bool(raw.get("negRisk", False)),
        active=bool(raw.get("active", True)),
        closed=bool(raw.get("closed", False)),
        resolved_outcome=str(raw.get("umaResolutionStatus") or ""),
        resolution=infer_resolution(raw),
        raw=raw,
    )


class PolymarketClient:
    def __init__(
        self,
        gamma_url: str = "https://gamma-api.polymarket.com",
        clob_url: str = "https://clob.polymarket.com",
        timeout: float = 12.0,
    ) -> None:
        self.gamma_url = gamma_url.rstrip("/")
        self.clob_url = clob_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self.last_error = ""

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                headers={"User-Agent": "flowbot/0.1 (read-only)"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- Gamma -------------------------------------------------------------
    async def search_markets(
        self, slug_contains: str = "bitcoin-up-or-down", limit: int = 40
    ) -> list[PredictionMarket]:
        """Broad, volume-sorted market list, filtered client-side.

        Gamma's `slug` query param is an *exact* match, not a substring one -
        passing a family prefix there (as this used to do) silently returns
        an empty list every time, on every family. The filtering has to
        happen after the fetch. Note this still will not surface a
        low-volume recurring market buried outside the top `limit` by
        overall volume; for the BTC 5m/15m/4h family, prefer
        `get_market_by_slug` with a computed slug instead (see
        `PolymarketPipeline._discover_windows`).
        """
        client = await self._http()
        params = {
            "closed": "false", "active": "true", "limit": str(limit),
            "order": "volumeNum", "ascending": "false",
        }
        try:
            r = await client.get(f"{self.gamma_url}/markets", params=params)
            r.raise_for_status()
            rows = r.json()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("gamma market search failed: %s", self.last_error)
            return []

        markets = [parse_market(row) for row in rows if isinstance(row, dict)]
        if slug_contains:
            needle = slug_contains.lower()
            markets = [
                m for m in markets
                if needle in m.slug.lower() or needle.replace("-", " ") in m.question.lower()
            ]
        return markets

    async def get_market_by_slug(self, slug: str) -> PredictionMarket | None:
        """Exact-slug lookup - the one Gamma filter that actually narrows on
        the server. `slug=` as a substring filter (what `search_markets` used
        to rely on for this family) silently returns an empty list; only an
        exact match works. The recurring BTC windows encode their own open
        time in the slug (`btc-updown-15m-<epoch>`, aligned to the window
        size), so the caller can compute the slug it wants instead of
        searching for it - see `PolymarketPipeline._discover_windows`.
        """
        client = await self._http()
        try:
            r = await client.get(f"{self.gamma_url}/markets", params={"slug": slug})
            r.raise_for_status()
            rows = r.json()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
        if not rows or not isinstance(rows[0], dict):
            return None
        return parse_market(rows[0])

    async def get_market(self, market_id: str) -> PredictionMarket | None:
        client = await self._http()
        try:
            r = await client.get(f"{self.gamma_url}/markets/{market_id}")
            r.raise_for_status()
            return parse_market(r.json())
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

    # -- CLOB --------------------------------------------------------------
    async def get_book(self, token_id: str) -> BookSnapshot | None:
        """The real resting book for one outcome token, as a BookSnapshot.

        Prices are probabilities in [0, 1] and sizes are shares (each share
        pays $1 if the outcome happens), so the same fill engine that walks a
        BTC book walks this one unchanged.
        """
        client = await self._http()
        try:
            r = await client.get(f"{self.clob_url}/book", params={"token_id": token_id})
            r.raise_for_status()
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

        bids = [
            BookLevel(float(lv["price"]), float(lv["size"]))
            for lv in data.get("bids", []) if float(lv.get("size", 0)) > 0
        ]
        asks = [
            BookLevel(float(lv["price"]), float(lv["size"]))
            for lv in data.get("asks", []) if float(lv.get("size", 0)) > 0
        ]
        bids.sort(key=lambda lv: -lv.price)
        asks.sort(key=lambda lv: lv.price)
        ts = int(data.get("timestamp") or 0) or now_ms()
        if ts < 1e12:                      # some responses use seconds
            ts *= 1000
        return BookSnapshot(ts=ts, bids=bids, asks=asks, seq=int(data.get("hash_seq", 0) or 0))

    async def get_midpoint(self, token_id: str) -> float | None:
        client = await self._http()
        try:
            r = await client.get(f"{self.clob_url}/midpoint", params={"token_id": token_id})
            r.raise_for_status()
            return float(r.json()["mid"])
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

    async def health(self) -> dict:
        client = await self._http()
        out = {"gamma": False, "clob": False, "error": ""}
        try:
            r = await client.get(f"{self.gamma_url}/markets", params={"limit": "1"})
            out["gamma"] = r.status_code == 200
        except Exception as exc:  # noqa: BLE001
            out["error"] = str(exc)
        try:
            r = await client.get(f"{self.clob_url}/ok")
            out["clob"] = r.status_code == 200
        except Exception as exc:  # noqa: BLE001
            out["error"] = out["error"] or str(exc)
        return out
