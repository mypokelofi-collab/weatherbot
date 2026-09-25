"""pocketbot: payout maths, risk gates, settlement, strategy and the live-account guards."""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from pocketbot import __main__ as cli
from pocketbot import broker as broker_mod
from pocketbot.broker import ListFeed, PaperBroker, PocketOptionBroker, ssid_is_demo
from pocketbot.engine import Engine
from pocketbot.market import Candle, load_csv, save_csv, synthetic_candles
from pocketbot.risk import RiskConfig, RiskManager, breakeven_win_rate, expected_value
from pocketbot.stats import Scorecard, Trade, wilson_interval
from pocketbot.strategy import CALL, PUT, StrategyConfig, evaluate, warmup

T0 = 1_700_000_040  # a multiple of 60


def series(closes, period=60):
    return [Candle(T0 + i * period, c, c + 0.0001, c - 0.0001, c) for i, c in enumerate(closes)]


# ------------------------------------------------------------------ maths

def test_breakeven_and_expected_value():
    assert breakeven_win_rate(0.80) == pytest.approx(0.5556, abs=1e-4)
    assert breakeven_win_rate(0.92) == pytest.approx(0.5208, abs=1e-4)
    assert expected_value(breakeven_win_rate(0.85), 0.85) == pytest.approx(0.0)
    assert expected_value(0.5, 0.85) < 0          # a coin flip loses money


def test_wilson_interval_is_wide_on_small_samples():
    lo, hi = wilson_interval(12, 20)
    assert lo < 0.40 and hi > 0.78
    lo, hi = wilson_interval(600, 1000)
    assert 0.56 < lo < 0.60 < hi < 0.64


def test_scorecard_verdicts():
    def trades(w, l, payout=0.85):
        out = []
        for i in range(w + l):
            t = Trade(str(i), "X", CALL, 1.0, payout, 0, 60)
            t.settle("win" if i < w else "loss")
            out.append(t)
        return out
    assert "not enough" in Scorecard(trades(8, 2)).summary()["verdict"]
    assert Scorecard(trades(45, 55)).summary()["verdict"].startswith("no edge")
    assert Scorecard(trades(700, 300)).summary()["verdict"].startswith("edge evidenced")
    s = Scorecard(trades(3, 1)).summary()
    assert s["pnl"] == pytest.approx(3 * 0.85 - 1)


# ------------------------------------------------------------------- risk

def test_martingale_is_refused():
    with pytest.raises(ValueError, match="martingale"):
        RiskConfig.from_dict({"martingale": True})
    with pytest.raises(ValueError, match="unknown"):
        RiskConfig.from_dict({"stake_frac": 0.01})
    with pytest.raises(ValueError):
        RiskConfig.from_dict({"stake_fraction": 0.2})


def test_risk_gates():
    rm = RiskManager(RiskConfig(max_consecutive_losses=2, cooldown_minutes=10,
                                daily_loss_limit=0.05, min_payout=0.8))
    assert not rm.check(1000, 0.70, now=T0).allowed           # payout too low
    assert "break even" in rm.check(1000, 0.70, now=T0).reason
    d = rm.check(1000, 0.85, now=T0)
    assert d.allowed and d.stake == 10.0
    rm.opened()
    assert not rm.check(1000, 0.85, now=T0).allowed           # one at a time
    rm.closed(-10, now=T0)
    rm.opened(); rm.closed(-10, now=T0)                       # 2nd loss in a row
    assert "cooling" in rm.check(980, 0.85, now=T0 + 60).reason
    assert rm.check(980, 0.85, now=T0 + 601).allowed


def test_stake_never_rises_after_losses():
    rm = RiskManager(RiskConfig(max_consecutive_losses=99, daily_loss_limit=1.0))
    stakes, bal = [], 1000.0
    for _ in range(5):
        d = rm.check(bal, 0.85, now=T0)
        stakes.append(d.stake)
        rm.opened(); rm.closed(-d.stake, now=T0)
        bal -= d.stake
    assert stakes == sorted(stakes, reverse=True)


def test_daily_loss_limit_halts_until_next_day():
    rm = RiskManager(RiskConfig(daily_loss_limit=0.02, max_consecutive_losses=99))
    for _ in range(2):
        d = rm.check(1000, 0.85, now=T0)
        assert d.allowed
        rm.opened(); rm.closed(-d.stake, now=T0)
    assert not rm.check(980, 0.85, now=T0).allowed
    assert rm.check(980, 0.85, now=T0 + 86400).allowed


# --------------------------------------------------------------- strategy

def test_strategy_waits_for_warmup_and_is_quiet_on_flat_market():
    cfg = StrategyConfig()
    assert "warming" in evaluate(series([1.1] * 10), cfg).reason
    flat = [1.1 + (0.0001 if i % 2 else -0.0001) for i in range(warmup(cfg) + 5)]
    assert not evaluate(series(flat), cfg)


def test_reversion_fades_a_spike_both_ways():
    cfg = StrategyConfig()
    base = [1.1 + (0.00005 if i % 2 else -0.00005) for i in range(80)]
    down = evaluate(series(base + [1.0985, 1.0970]), cfg)
    up = evaluate(series(base + [1.1015, 1.1030]), cfg)
    assert down.direction == CALL and up.direction == PUT
    assert 0 < down.confidence <= 1


def test_momentum_follows_a_trend():
    cfg = StrategyConfig(name="momentum")
    # A steady climb with pullbacks, so RSI sits on the trend side but is not exhausted.
    closes = [1.1 + i * 0.0001 + (0.0002 if i % 2 else -0.0002) for i in range(80)]
    candles = series(closes)
    last = candles[-1]
    candles[-1] = Candle(last.time, last.close - 0.0002, last.high, last.low, last.close)
    assert evaluate(candles, cfg).direction == CALL
    falling = series([2.2 - c for c in closes])
    last = falling[-1]
    falling[-1] = Candle(last.time, last.close + 0.0002, last.high, last.low, last.close)
    assert evaluate(falling, cfg).direction == PUT


def test_unknown_strategy_settings_rejected():
    with pytest.raises(ValueError):
        StrategyConfig.from_dict({"bb_perod": 20})


# ------------------------------------------------------------- settlement

def run(coro):
    return asyncio.run(coro)


def test_paper_broker_settles_on_expiry_candle():
    b = PaperBroker(100.0, 0.8)
    t = run(b.place("X", CALL, 10, 120, 1.0, now=T0 + 60))
    assert run(b.balance()) == 90
    assert run(b.settle(Candle(T0 + 60, 1, 1, 1, 1.5), 60)) == []    # closes at T0+120: early
    done = run(b.settle(Candle(T0 + 120, 1, 1, 1, 1.5), 60))
    assert done == [t] and t.result == "win" and t.pnl == 8.0
    assert run(b.balance()) == pytest.approx(108.0)

    p = run(b.place("X", PUT, 10, 60, 1.0, now=T0))
    run(b.settle(Candle(T0, 1, 1, 1, 1.0), 60))
    assert p.result == "draw" and run(b.balance()) == pytest.approx(108.0)


def test_engine_on_synthetic_market_has_no_edge():
    # A random walk contains no edge; the scorecard must not invent one.
    cfg = {"risk": {"max_trades_per_day": 10**6}, "asset": "X", "expiry_candles": 3}
    eng = cli._engine(cfg, ListFeed(synthetic_candles(15000, 60, seed=7), 60),
                      PaperBroker(1000, 0.85), cli.Ledger(None), quiet=True)
    s = run(eng.run())
    assert s["trades"] > 100
    assert not s["verdict"].startswith("edge evidenced")
    assert s["pnl"] < 0


def test_csv_roundtrip_and_backtest_command(tmp_path, capsys):
    path = tmp_path / "c.csv"
    save_csv(path, synthetic_candles(1500, 60, seed=1))
    assert len(load_csv(path)) == 1500
    cli.main(["backtest", str(path), "--json", "--payout", "0.9"])
    out = json.loads(capsys.readouterr().out)
    assert out["avg_payout"] in (0.9, 0.0) and "verdict" in out


# ------------------------------------------------------------ live broker

class FakeClient:
    """Stands in for BinaryOptionsToolsV2.PocketOptionAsync."""

    def __init__(self, demo=True, profit=8.5):
        self.demo, self.profit, self.orders = demo, profit, []

    def is_demo(self):
        return self.demo

    async def balance(self):
        return 500.0

    async def payout(self, asset):
        return 85

    async def buy(self, asset, amount, time):
        self.orders.append(("buy", asset, amount, time))
        return "abc-1", {"openPrice": 1.2345}

    async def sell(self, asset, amount, time):
        self.orders.append(("sell", asset, amount, time))
        return "abc-2", {"openPrice": 1.2345}

    async def check_win(self, id, timeout_seconds=None):
        return {"id": id, "profit": self.profit, "result": "win" if self.profit > 0 else "loss",
                "closePrice": 1.25}


def test_pocket_option_broker_places_and_settles():
    async def go():
        c = FakeClient()
        b = PocketOptionBroker(c, "demo")
        t = await b.place("EURUSD_otc", PUT, 5.0, 60, 1.0, now=T0)
        assert c.orders == [("sell", "EURUSD_otc", 5.0, 60)]
        assert t.entry == 1.2345 and t.payout == 0.85
        done = await b.settle(Candle(T0, 1, 1, 1, 1), 60)
        return t, done
    t, done = run(go())
    assert done == [t] and t.result == "win" and t.pnl == 8.5 and t.exit == 1.25


DEMO_SSID = '42["auth",{"session":"abcdefghijklmnop","isDemo":1,"uid":1,"platform":2}]'
REAL_SSID = DEMO_SSID.replace('"isDemo":1', '"isDemo":0')


def test_ssid_demo_flag():
    assert ssid_is_demo(DEMO_SSID) is True
    assert ssid_is_demo(REAL_SSID) is False
    assert ssid_is_demo("garbage") is None


def test_account_guards(monkeypatch):
    monkeypatch.delenv("POCKETBOT_REAL_MONEY", raising=False)
    assert cli._guard_account("demo", DEMO_SSID, FakeClient(demo=True)) == "demo"
    with pytest.raises(SystemExit, match="REAL account"):
        cli._guard_account("demo", REAL_SSID, FakeClient(demo=False))
    with pytest.raises(SystemExit, match="disagrees"):
        cli._guard_account("demo", DEMO_SSID, FakeClient(demo=False))
    with pytest.raises(SystemExit, match="POCKETBOT_REAL_MONEY"):
        cli._guard_account("live", REAL_SSID, FakeClient(demo=False))
    monkeypatch.setenv("POCKETBOT_REAL_MONEY", cli.REAL_MONEY_ACK)
    assert cli._guard_account("live", REAL_SSID, FakeClient(demo=False)) == "real"
    with pytest.raises(SystemExit, match="demo account"):
        cli._guard_account("live", DEMO_SSID, FakeClient(demo=True))


def test_non_paper_modes_need_an_ssid(monkeypatch):
    monkeypatch.delenv("POCKETBOT_SSID", raising=False)
    cfg = cli.load_config(None)
    cfg["mode"] = "demo"
    with pytest.raises(SystemExit, match="POCKETBOT_SSID"):
        run(cli.cmd_run(cfg, types.SimpleNamespace(delay=0, seed=1)))


def test_demo_run_end_to_end_with_fake_client(monkeypatch, tmp_path):
    candles = list(synthetic_candles(400, 60, seed=11))

    class LiveClient(FakeClient):
        shut = False

        async def get_candles_live(self, asset, period, hours=2.0, max_rows=100):
            for i in range(60, len(candles)):
                closed = [c.__dict__ for c in candles[:i]][-max_rows:]
                yield closed, None
                yield closed, None                # a tick inside the same candle

        async def shutdown(self):
            self.shut = True

    client = LiveClient(demo=True)

    async def fake_connect(ssid, ws_url=None, timeout=60.0):
        return client

    monkeypatch.setattr(broker_mod, "connect", fake_connect)
    monkeypatch.setenv("POCKETBOT_SSID", DEMO_SSID)
    cfg = cli.load_config(None)
    cfg.update(mode="demo", ledger=str(tmp_path / "t.jsonl"))
    s = run(cli.cmd_run(cfg, types.SimpleNamespace(delay=0, seed=None)))
    assert client.orders and client.shut
    assert all(o[3] == 180 for o in client.orders)          # 3 x 60s candles
    assert s["trades"] >= 1 and s["wins"] == s["trades"]    # FakeClient always wins
    assert len(cli.Ledger(tmp_path / "t.jsonl").load()) == s["trades"]


# ------------------------------------------------------------- dashboard

from fastapi.testclient import TestClient  # noqa: E402

from pocketbot.monitor import Monitor  # noqa: E402
from pocketbot.server import create_app  # noqa: E402
from pocketbot.service import Service  # noqa: E402


def make_monitor():
    return Monitor("paper", "X", 60, 3, "reversion", Scorecard(), RiskManager(RiskConfig()))


def test_dashboard_api_needs_the_token():
    mon = make_monitor()
    client = TestClient(create_app(mon, "s3cret"))
    assert client.get("/api/health").json()["ok"] is True       # liveness stays open
    assert client.get("/").status_code == 200                   # the page holds no data
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/state?token=wrong").status_code == 401
    assert client.get("/api/state?token=s3cret").json()["asset"] == "X"
    hdr = {"Authorization": "Bearer s3cret"}
    assert client.get("/api/state", headers=hdr).status_code == 200
    assert client.post("/api/control/pause").status_code == 401
    assert client.post("/api/control/pause", headers=hdr).json()["paused"] is True
    assert mon.paused
    client.post("/api/control/resume", headers=hdr)
    assert not mon.paused
    assert client.post("/api/control/flatten", headers=hdr).status_code == 404


def test_paused_monitor_blocks_entries_but_not_settlement():
    mon = make_monitor()
    mon.paused = True
    cfg = {"risk": {"max_trades_per_day": 10**6}, "asset": "X", "expiry_candles": 3}
    eng = cli._engine(cfg, ListFeed(synthetic_candles(3000, 60, seed=7), 60),
                      PaperBroker(1000, 0.85), cli.Ledger(None), quiet=True, monitor=mon)
    s = run(eng.run())
    assert s["trades"] == 0 and mon.last_skip["reason"] == "paused from the dashboard"
    snap = mon.snapshot()
    assert snap["status"] == "running" and len(snap["candles"]) == 150


def test_engine_fills_the_monitor():
    mon = make_monitor()
    cfg = {"risk": {"max_trades_per_day": 10**6}, "asset": "X", "expiry_candles": 3}
    eng = cli._engine(cfg, ListFeed(synthetic_candles(3000, 60, seed=7), 60),
                      PaperBroker(1000, 0.85), cli.Ledger(None), quiet=True, monitor=mon,
                      scorecard=mon.scorecard, risk=mon.risk)
    s = run(eng.run())
    snap = mon.snapshot()
    assert s["trades"] > 0 and snap["summary"]["trades"] == s["trades"]
    assert snap["equity"] and snap["balance"] == snap["equity"][-1][1]
    assert snap["signal"]["reason"]


def test_service_survives_a_bad_session_and_reports_it(monkeypatch):
    # A demo-mode SSID that opens a real account must not crash the service:
    # the dashboard shows the refusal and the supervisor waits to retry.
    monkeypatch.setenv("POCKETBOT_SSID", DEMO_SSID)

    class Shutdownable(FakeClient):
        async def shutdown(self):
            pass

    async def fake_connect(ssid, ws_url=None, timeout=60.0):
        return Shutdownable(demo=False)

    monkeypatch.setattr(broker_mod, "connect", fake_connect)
    cfg = cli.load_config(None)
    cfg.update(mode="demo", ledger=None)
    svc = Service(cfg)

    async def go():
        task = asyncio.create_task(svc.run_forever())
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    run(go())
    assert svc.monitor.connects == 1
    assert "disagrees" in svc.monitor.error or "REAL account" in svc.monitor.error


def test_service_restores_paper_balance_and_stats_from_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("POCKETBOT_SSID", DEMO_SSID)   # a real session, so the ledger is used
    path = tmp_path / "trades-paper.jsonl"
    led = cli.Ledger(path)
    for i, res in enumerate(["win", "loss", "win"]):
        t = Trade(str(i), "X", CALL, 10.0, 0.8, 0, 60, account="paper")
        t.settle(res)
        led.append(t)
    cfg = cli.load_config(None)
    cfg.update(mode="paper", ledger=str(tmp_path / "trades-{mode}.jsonl"))
    svc = Service(cfg)
    assert svc.scorecard.summary()["trades"] == 3
    assert run(svc.paper.balance()) == pytest.approx(1000 + 8 - 10 + 8)
