"""Dashboard API surface and configuration handling."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from flowbot.bot.trader import Trader
from flowbot.core.bus import EventBus
from flowbot.core.config import AppConfig, SignalConfig, apply_env_overrides, load_config
from flowbot.data.factory import build_feed
from flowbot.server.app import create_app


@pytest.fixture
def running_app(app_config, monkeypatch):
    """A trader on the simulator with a live dashboard in front of it."""
    cfg = app_config
    cfg.data.sim_speed = 1200
    cfg.data.backfill_bars = 120
    feed = build_feed(cfg.data)
    trader = Trader(cfg, feed, EventBus(), store=None)
    app = create_app(trader, cfg)
    with TestClient(app) as client:
        client.portal.call(trader.start)  # type: ignore[attr-defined]
        yield client, trader, cfg
        client.portal.call(trader.stop)   # type: ignore[attr-defined]


def test_health_reports_paper_and_data_provenance(running_app):
    client, trader, _ = running_app
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is True
    assert body["real_data"] is False        # simulator must say so
    assert body["venue"] == "simulator"
    assert body["equity"] > 0


def test_state_contains_everything_the_dashboard_renders(running_app):
    client, _, _ = running_app
    s = client.get("/api/state").json()
    for key in (
        "candles", "signal", "portfolio", "risk", "stats", "book", "pressure",
        "orders", "trades", "events", "tape", "cvd", "execution", "instrument",
        "config", "feed", "markers", "equity_curve", "position_mgmt",
    ):
        assert key in s, f"missing {key}"
    assert s["mode"] == "paper"
    assert s["real_data"] is False
    assert isinstance(s["candles"], list) and s["candles"]
    assert s["config"]["risk"]["risk_per_trade_pct"] > 0


def test_tick_payload_is_small_but_complete(running_app):
    client, _, _ = running_app
    tick = client.get("/api/tick").json()
    assert {"last_price", "book", "portfolio", "feed", "risk"} <= set(tick)
    assert len(json.dumps(tick)) < len(json.dumps(client.get("/api/state").json()))


def test_pause_and_resume_control(running_app):
    client, trader, _ = running_app
    assert client.post("/api/control/pause").status_code == 200
    assert trader.trading_enabled is False
    assert client.post("/api/control/resume").status_code == 200
    assert trader.trading_enabled is True


def test_kill_switch_and_revive(running_app):
    client, trader, _ = running_app
    client.post("/api/control/kill", json={"reason": "test"})
    assert trader.risk.kill_switch is True
    assert "manual" in trader.risk.kill_reason
    client.post("/api/control/revive")
    assert trader.risk.kill_switch is False


def test_unknown_control_action_is_404(running_app):
    client, _, _ = running_app
    assert client.post("/api/control/launch-rocket").status_code == 404


def test_live_parameter_update(running_app):
    client, trader, _ = running_app
    r = client.post("/api/params/signal", json={"entry_threshold": 0.5, "bogus": 1})
    assert r.status_code == 200
    assert r.json()["applied"] == {"entry_threshold": 0.5}
    assert trader.cfg.signal.entry_threshold == 0.5
    assert client.post("/api/params/nope", json={}).status_code == 400


def test_websocket_sends_a_snapshot_then_ticks(running_app):
    client, _, _ = running_app
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot"
        assert first["data"]["symbol"] == "BTCUSDT"
        ws.send_text("ping")
        # The socket may deliver a scheduled tick before the pong.
        for _ in range(6):
            msg = ws.receive_json()
            if msg["type"] == "pong":
                break
            assert msg["type"] in ("tick", "snapshot", "event", "trade", "bar", "feed")
        else:
            pytest.fail("no pong received")


def test_dashboard_page_and_assets_are_served(running_app):
    client, _, _ = running_app
    page = client.get("/")
    assert page.status_code == 200
    assert "flowbot" in page.text
    assert "PAPER MONEY" in page.text
    for asset in ("/static/app.js", "/static/charts.js", "/static/styles.css"):
        assert client.get(asset).status_code == 200


def test_page_and_assets_are_never_cached_blindly(running_app):
    # Regression: a CSS fix went out in a redeploy and a browser with no
    # special configuration kept rendering the old layout - no Cache-Control
    # meant it trusted a disk-cached copy without even revalidating. There is
    # no content hash in these filenames to bust a cache with instead.
    client, _, _ = running_app
    for path in ("/", "/static/app.js", "/static/charts.js", "/static/styles.css"):
        assert client.get(path).headers["cache-control"] == "no-cache", path


def test_auth_token_gates_the_api(app_config):
    cfg = app_config
    cfg.server.auth_token = "s3cret"
    feed = build_feed(cfg.data)
    trader = Trader(cfg, feed, EventBus(), store=None)
    with TestClient(create_app(trader, cfg)) as client:
        assert client.get("/api/state").status_code == 401
        assert client.get("/api/state?token=s3cret").status_code == 200
        assert client.get(
            "/api/state", headers={"authorization": "Bearer s3cret"}
        ).status_code == 200
        assert client.get("/api/health").status_code == 200   # health stays open

        # Every other read endpoint must gate the same way - none of these are
        # allowed to leak trades, orders, signal history, book depth, events,
        # the polymarket state, config or session history to an unauthenticated
        # caller just because /api/state happens to be the one everyone checks.
        gated = [
            "/api/trades", "/api/orders", "/api/signal", "/api/book",
            "/api/events", "/api/polymarket", "/api/config", "/api/sessions",
        ]
        for path in gated:
            assert client.get(path).status_code == 401, path
            assert client.get(f"{path}?token=s3cret").status_code == 200, path


def test_config_endpoint_never_echoes_the_auth_token(app_config):
    # /api/config is public read surface once a caller is authenticated for
    # dashboard use; it must still never hand the secret back, because the
    # moment it does, the token gate on every other endpoint is worthless.
    cfg = app_config
    cfg.server.auth_token = "s3cret"
    feed = build_feed(cfg.data)
    trader = Trader(cfg, feed, EventBus(), store=None)
    with TestClient(create_app(trader, cfg)) as client:
        body = client.get("/api/config?token=s3cret").json()
        assert body["server"]["auth_token"] != "s3cret"


# ------------------------------------------------------------------ config

def test_weights_are_normalised():
    cfg = SignalConfig(weights={"trend": 2.0, "flow": 2.0})
    assert sum(cfg.weights.values()) == pytest.approx(1.0)
    assert cfg.weights["trend"] == pytest.approx(0.5)


def test_env_overrides_coerce_types(monkeypatch):
    monkeypatch.setenv("TESTBOT_A__B", "12")
    monkeypatch.setenv("TESTBOT_A__C", "1.5")
    monkeypatch.setenv("TESTBOT_A__D", "true")
    monkeypatch.setenv("TESTBOT_E", "text")
    raw = apply_env_overrides({}, prefix="TESTBOT_")
    assert raw == {"a": {"b": 12, "c": 1.5, "d": True}, "e": "text"}


def test_env_override_path_and_coercion(monkeypatch):
    monkeypatch.setenv("FLOWBOT_RISK__RISK_PER_TRADE_PCT", "0.25")
    monkeypatch.setenv("FLOWBOT_SERVER__PORT", "9999")
    monkeypatch.setenv("FLOWBOT_RISK__ALLOW_SHORT", "false")
    cfg = load_config(None)
    assert cfg.risk.risk_per_trade_pct == 0.25
    assert cfg.server.port == 9999
    assert cfg.risk.allow_short is False


def test_config_file_round_trip(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text(
        "mode: paper\ndata:\n  venue: simulator\n  symbol: BTCUSDT\n"
        "risk:\n  start_equity: 5000\n"
    )
    cfg = load_config(path)
    assert cfg.risk.start_equity == 5000
    assert cfg.data.venue == "simulator"
    assert cfg.server.port == 8033            # default preserved


def test_missing_config_file_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yml")


def test_default_config_is_paper_only():
    cfg = AppConfig()
    assert cfg.mode == "paper"
    assert cfg.server.port == 8033
    assert cfg.risk.risk_per_trade_pct <= 1.0


def test_live_broker_refuses_to_exist(instrument):
    from flowbot.execution.broker import LiveBroker

    with pytest.raises(NotImplementedError):
        LiveBroker()


# ------------------------------------------------- the ledger is never fatal

def test_a_broken_ledger_never_reaches_the_trading_loop(tmp_path):
    """Regression: a SQLite write error escaped the store and killed the
    market-data feed. Bookkeeping may fail; trading may not stop."""
    from flowbot.bot.store import Store
    from flowbot.core.types import Fill, Liquidity, Side

    store = Store(tmp_path / "ledger.sqlite")
    assert store.start_session({"started_at": 1, "venue": "test"}) == 1

    # Simulate the database going away under a running bot (disk full, volume
    # unmounted, file deleted) - every subsequent write must be swallowed.
    store._db.close()

    fill = Fill(ts=1, order_id="o1", side=Side.BUY, price=100, qty=1,
                fee=0.1, liquidity=Liquidity.TAKER)
    for _ in range(30):
        store.record_fill(fill)          # must not raise
        store.record_event("tick", "still trading", 1)

    assert store.write_failures == 0 or not store.enabled
    assert store.health()["enabled"] is False     # it switched itself off
    assert store.record_equity(1, 1.0, 1.0, 0.0, 100.0, None) is None
    assert store.load_trades() is None or store.load_trades() == []


def test_an_unopenable_ledger_still_lets_the_bot_run(tmp_path):
    from flowbot.bot.store import Store

    blocked = tmp_path / "a-file-not-a-dir"
    blocked.write_text("x")
    store = Store(blocked / "nested" / "ledger.sqlite")
    assert store.enabled is False
    assert store.health()["enabled"] is False
    store.record_event("boot", "no persistence available", 1)   # must not raise
