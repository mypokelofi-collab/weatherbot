"""X poster: OAuth signing, post text, and the anti-spam rules."""

from __future__ import annotations

import json

import httpx
import pytest

from flowbot.core.config import XConfig
from flowbot.social.x_poster import (
    MAX_CHARS,
    XCredentials,
    XPoster,
    format_post,
    oauth1_header,
)

MIN = 60_000


def _sig(action="enter_long", score=0.52, price=97_250.4, adx=27.3):
    return {
        "action": action, "score": score, "price": price,
        "features": {"adx": adx},
        "reasons": ["trend: fast EMA 1.2 ATR above slow", "breakout: new 20-bar high",
                    "flow: buyers lifting the offer", "rsi: 64"],
    }


def test_oauth_signature_matches_the_published_example():
    # The worked example from X's "Creating a signature" documentation.
    header = oauth1_header(
        "POST", "https://api.twitter.com/1.1/statuses/update.json",
        consumer_key="xvz1evFS4wEEPTGEFPHBog",
        consumer_secret="kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
        token="370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
        token_secret="LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
        params={"include_entities": "true",
                "status": "Hello Ladies + Gentlemen, a signed OAuth request!"},
        nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
        timestamp=1318622958,
    )
    assert 'oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"' in header


def test_post_text_is_labelled_and_fits():
    text = format_post(_sig(), XConfig())
    assert text.startswith("🟢 $BTC 15m momentum: LONG signal")
    assert "$97,250" in text and "+0.52" in text and "ADX 27" in text
    assert "EMA trend, breakout, order flow" in text
    assert "Not financial advice" in text and "paper-trading" in text
    assert len(text) <= MAX_CHARS
    assert "SHORT" in format_post(_sig("enter_short", -0.5), XConfig())


def test_long_text_drops_the_reasons_rather_than_the_disclaimer():
    cfg = XConfig(hashtags=["#" + "x" * 60] * 3)
    text = format_post(_sig(), cfg)
    assert len(text) <= MAX_CHARS
    assert "Not financial advice" in text


def test_only_entries_are_posted(tmp_path):
    p = XPoster(XConfig(enabled=True), tmp_path, credentials=None)
    for action in ("none", "hold", "exit"):
        assert not p.should_post(_sig(action), 0)
    assert p.should_post(_sig(), 0)
    assert not XPoster(XConfig(post_shorts=False), tmp_path).should_post(_sig("enter_short"), 0)
    assert not XPoster(XConfig(min_score=0.6), tmp_path).should_post(_sig(score=0.5), 0)


def test_rate_limits(tmp_path):
    cfg = XConfig(min_interval_minutes=60, repeat_after_minutes=240, max_posts_per_day=3)
    p = XPoster(cfg, tmp_path)
    DAY = 86_400_000
    t = 10 * DAY
    p._mark("enter_long", t)
    assert not p.should_post(_sig("enter_short"), t + 30 * MIN)      # too soon
    assert p.should_post(_sig("enter_short"), t + 61 * MIN)          # direction changed
    assert not p.should_post(_sig("enter_long"), t + 61 * MIN)       # same direction
    assert p.should_post(_sig("enter_long"), t + 241 * MIN)
    p._mark("enter_short", t + 61 * MIN)
    p._mark("enter_long", t + 241 * MIN)
    assert not p.should_post(_sig("enter_short"), t + 400 * MIN)     # daily cap
    assert p.should_post(_sig("enter_short"), t + DAY + 1)           # window rolled


async def test_dry_run_logs_instead_of_sending(tmp_path):
    p = XPoster(XConfig(enabled=True, dry_run=True), tmp_path)
    await p.send("hello")
    rec = json.loads((tmp_path / "x_posts.jsonl").read_text())
    assert rec["text"] == "hello" and rec["dry_run"] is True


async def test_missing_keys_force_dry_run(tmp_path, monkeypatch):
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"):
        monkeypatch.delenv(k, raising=False)
    assert XPoster(XConfig(dry_run=False), tmp_path).cfg.dry_run is True


async def test_live_post_hits_v2_tweets_and_errors_are_contained(tmp_path):
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(201, json={"data": {"id": "1", "text": "x"}})
        return httpx.Response(429, json={"title": "Too Many Requests"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    p = XPoster(XConfig(dry_run=False), tmp_path,
                credentials=XCredentials("k", "s", "t", "ts"), client=client)
    ok = await p.send("hello")
    assert ok["response"]["data"]["id"] == "1"
    req = seen[0]
    assert str(req.url) == "https://api.x.com/2/tweets"
    assert req.headers["Authorization"].startswith("OAuth ")
    assert json.loads(req.content) == {"text": "hello"}

    bad = await p.send("again")
    assert "429" in bad["error"] and p.last_error
    await client.aclose()
