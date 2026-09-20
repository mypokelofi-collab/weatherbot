"""Configuration.

One YAML file describes an entire run: which venue, which signal parameters,
how much risk, how orders are worked, and where the dashboard listens. Every
field can be overridden by an environment variable (FLOWBOT_<SECTION>__<FIELD>)
so the same image can be deployed with different settings.

Defaults are deliberately conservative: paper mode, 0.5% equity at risk per
trade, one position at a time, shorts enabled (the perp can express them).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class DataConfig(BaseModel):
    venue: Literal[
        "binance-futures", "binance-spot", "coinbase", "simulator", "replay"
    ] = "binance-futures"
    symbol: str = "BTCUSDT"
    interval: str = "15m"
    depth_limit: int = 1000
    depth_speed: str = "100ms"
    backfill_bars: int = 400
    # Replay / simulator
    replay_path: str = ""
    replay_speed: float = 0.0        # 0 = as fast as possible
    replay_loop: bool = False
    sim_speed: float = 60.0          # simulated seconds per real second
    sim_seed: int | None = 7
    sim_start_price: float = 64000.0
    # Recording the live feed for later replay/backtests
    record: bool = False
    record_path: str = "data/recordings/{venue}-{symbol}-{date}.jsonl"
    record_levels: int = 25
    record_throttle_ms: int = 250
    # Cross-venue guard
    stale_feed_seconds: int = 90


class SignalConfig(BaseModel):
    ema_fast: int = 21
    ema_slow: int = 55
    atr_period: int = 14
    rsi_period: int = 14
    adx_period: int = 14
    donchian_period: int = 20
    roc_period: int = 8
    z_period: int = 40
    vol_lookback: int = 200
    flow_bars: int = 6              # bars of tape used for the CVD slope
    warmup_bars: int = 80

    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "trend": 0.22,
            "trend_slope": 0.12,
            "macd": 0.12,
            "breakout": 0.15,
            "rsi": 0.08,
            "momentum_z": 0.10,
            "flow": 0.13,
            "book": 0.08,
        }
    )

    entry_threshold: float = 0.35    # |composite| needed to open
    exit_threshold: float = 0.10     # composite decay that closes a winner
    flip_threshold: float = 0.30     # opposite-side score that forces a reversal

    # Regime gates
    min_adx: float = 18.0
    require_trend_alignment: bool = True
    min_atr_pct: float = 0.06        # % of price; below this a 15m move cannot pay fees
    max_atr_pct: float = 3.0
    max_spread_bps: float = 4.0
    min_depth_usd: float = 150_000.0  # resting notional within 10bps of mid
    min_book_levels: int = 5

    @model_validator(mode="after")
    def _normalise_weights(self) -> "SignalConfig":
        total = sum(abs(w) for w in self.weights.values())
        if total > 0 and abs(total - 1.0) > 1e-6:
            self.weights = {k: v / total for k, v in self.weights.items()}
        return self


class RiskConfig(BaseModel):
    start_equity: float = 10_000.0
    risk_per_trade_pct: float = 0.5      # % of equity lost if the stop is hit
    max_position_pct: float = 100.0      # notional cap as % of equity
    leverage_cap: float = 3.0
    allow_short: bool = True

    # On a small bankroll the venue's minimum order can be larger than
    # risk-based sizing wants. Rather than silently never trading, take the
    # venue minimum and report the risk it actually implies - but never above
    # `max_risk_per_trade_pct`, which is the real circuit breaker for a small
    # account.
    min_lot_fallback: bool = True
    max_risk_per_trade_pct: float = 2.0

    stop_atr_mult: float = 1.6
    trail_atr_mult: float = 2.2
    breakeven_at_r: float = 1.0
    take_profit_r: float = 1.8           # first target, partial exit
    partial_exit_pct: float = 50.0       # % of the position taken off at target
    runner_exit_r: float = 4.0           # hard take-profit for the remainder
    max_bars_in_trade: int = 24          # 6h on 15m bars
    min_bars_in_trade: int = 1

    # Circuit breakers
    daily_loss_limit_pct: float = 3.0
    daily_profit_target_pct: float = 0.0  # 0 = disabled
    max_consecutive_losses: int = 4
    cooldown_bars: int = 1               # bars to wait after any exit
    loss_cooldown_bars: int = 2          # extra patience after a losing trade
    max_trades_per_day: int = 12
    # A momentum flip is the one exit worth re-entering on immediately: the
    # signal has not gone quiet, it has changed sides.
    reverse_on_flip: bool = True


class ExecConfig(BaseModel):
    entry_order: Literal["market", "limit", "post_only"] = "limit"
    exit_order: Literal["market", "limit", "post_only"] = "market"
    limit_offset_ticks: int = 1          # how far inside the spread we post
    limit_timeout_s: float = 45.0        # then convert to market
    # A market order that has not filled in this long is not going to: the
    # book is desynced or the feed stalled. Cancel it rather than leave the
    # bot holding an entry slot it can never use.
    market_timeout_s: float = 30.0
    taker_fee_bps: float = 4.5           # Binance USDⓈ-M taker, no VIP
    maker_fee_bps: float = 1.8
    latency_ms: int = 120                # submit -> venue ack round trip
    max_slippage_bps: float = 12.0       # abort an entry that would cost more
    allow_partial_fills: bool = True
    queue_model: Literal["fifo", "optimistic"] = "fifo"
    max_book_levels_to_eat: int = 40
    # Refuse to fill against a book older than this. Live feeds update every
    # 100ms; a backtest over a sparsely recorded book raises it (see the
    # backtest runner) rather than filling against stale depth.
    book_stale_ms: int = 5_000


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8033
    title: str = "flowbot · BTC 15m momentum"
    broadcast_hz: float = 4.0            # dashboard state pushes per second
    tape_size: int = 300
    auth_token: str = ""                 # optional ?token= / Bearer gate


class PolymarketConfig(BaseModel):
    enabled: bool = False
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    # The recurring window family's slug prefix - the epoch suffix is
    # *computed* from window_seconds, not searched for (Gamma's `slug` filter
    # is exact-match only, so a prefix can never be found by search anyway).
    # "btc-updown-15m" is the 15-minute family; "btc-updown-5m"/"-4h" exist
    # too if window_seconds is changed to match.
    market_slug_contains: str = "btc-updown-15m"
    window_seconds: int = 900            # 15m - must match the family above
    poll_seconds: int = 30
    min_edge: float = 0.04               # probability points over the market
    max_stake_pct: float = 2.0           # % of equity per market
    kelly_fraction: float = 0.25
    taker_fee_bps: float = 0.0           # Polymarket CLOB charges no taker fee today
    # 5 minutes made sense as a floor on an hourly/daily market; on a 15m
    # window it would refuse the last third of every window. 90s still clears
    # the ~0.5s round-trip this pipeline actually waits out by two orders of
    # magnitude.
    min_seconds_to_resolution: int = 90
    # Guarantee at least one paper trade per window even when no organic edge
    # clears `min_edge` - which, per docs/POLYMARKET_PIPELINE.md §2.1, is most
    # windows by design. A forced trade is tagged and excluded from the
    # calibration/Brier stats in state()["calibration"], so turning this on
    # buys trading volume without corrupting the number that gates real
    # capital in phase 3.
    force_min_trades: bool = False
    # Wide on purpose: a forced attempt can still miss its fill (the quote
    # gets pulled during the round trip, or a further price move drops the
    # notional below the venue minimum by the time it lands - both observed
    # live). At poll_seconds=30, a 30s-wide window gives one attempt and no
    # recovery if it misses; this gives ~7, so one bad fill doesn't cost the
    # window its only guaranteed trade.
    force_trade_before_close_s: int = 300
    calibration_a: float = 2.4           # logistic slope on the momentum score
    calibration_b: float = 0.0


class AppConfig(BaseModel):
    mode: Literal["paper"] = "paper"     # live trading is intentionally absent
    log_level: str = "INFO"
    state_dir: str = "data/state"
    autostart: bool = True
    data: DataConfig = Field(default_factory=DataConfig)
    signal: SignalConfig = Field(default_factory=SignalConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecConfig = Field(default_factory=ExecConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    polymarket: PolymarketConfig = Field(default_factory=PolymarketConfig)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


def _coerce(value: str) -> Any:
    low = value.strip().lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", ""):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def apply_env_overrides(raw: dict[str, Any], prefix: str = "FLOWBOT_") -> dict[str, Any]:
    """FLOWBOT_RISK__RISK_PER_TRADE_PCT=0.25 -> raw['risk']['risk_per_trade_pct']."""
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        path = key[len(prefix):].lower().split("__")
        node = raw
        for part in path[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                break
        else:
            node[path[-1]] = _coerce(value)
    return raw


def load_config(path: str | Path | None = None) -> AppConfig:
    raw: dict[str, Any] = {}
    if path:
        p = Path(path)
        if p.exists():
            raw = yaml.safe_load(p.read_text()) or {}
        else:
            raise FileNotFoundError(f"config not found: {p}")
    raw = apply_env_overrides(raw)
    return AppConfig(**raw)
