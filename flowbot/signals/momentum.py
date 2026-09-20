"""The momentum model.

Eight components, each scored independently into [-1, 1] and then blended by
weight into one composite score. Nothing here is a black box: every component
exposes its raw input, its normalised score and a plain-English note, so the
dashboard can show *why* the bot is long, and so a bad trade can be traced to
the component that drove it.

Design notes:
  * Everything that has price units is divided by ATR before scoring. A $400
    EMA spread means something completely different at 0.2% ATR than at 1.5%.
  * The four "where has price been" components (trend, slope, macd, breakout)
    carry most of the weight; flow and book are confirmation, not the thesis.
    Microstructure leads by seconds, and we hold for hours.
  * Scores saturate via tanh rather than clipping, so a violent move does not
    let one component dominate the blend.
"""

from __future__ import annotations

from ..core.config import SignalConfig
from ..core.types import Regime, SignalComponent
from . import indicators as ind
from .features import Features


def _c(name: str, raw: float, score: float, weight: float, note: str) -> SignalComponent:
    return SignalComponent(name=name, raw=raw, score=ind.clamp(score), weight=weight, note=note)


def score_components(f: Features, cfg: SignalConfig) -> list[SignalComponent]:
    w = cfg.weights
    out: list[SignalComponent] = []

    # 1. Trend: EMA separation in ATR units.
    spread = f.ema_spread_atr
    out.append(_c(
        "trend", spread, ind.squash(spread, 1.5), w.get("trend", 0.0),
        f"EMA{cfg.ema_fast} is {abs(spread):.2f} ATR "
        f"{'above' if spread >= 0 else 'below'} EMA{cfg.ema_slow}",
    ))

    # 2. Trend slope: is the slow EMA itself still moving?
    sl = f.ema_slow_slope_atr
    out.append(_c(
        "trend_slope", sl, ind.squash(sl / 0.15), w.get("trend_slope", 0.0),
        f"EMA{cfg.ema_slow} slope {sl:+.3f} ATR/bar",
    ))

    # 3. MACD histogram, ATR-normalised, with its own slope as a tiebreak.
    hist_atr = (f.macd_hist / f.atr) if f.atr else 0.0
    hist_slope_atr = (f.macd_hist_slope / f.atr) if f.atr else 0.0
    macd_score = 0.7 * ind.squash(hist_atr / 0.30) + 0.3 * ind.squash(hist_slope_atr / 0.08)
    out.append(_c(
        "macd", hist_atr, macd_score, w.get("macd", 0.0),
        f"MACD histogram {hist_atr:+.2f} ATR, {'expanding' if hist_slope_atr * hist_atr > 0 else 'fading'}",
    ))

    # 4. Breakout: outside the Donchian channel is the real momentum tell;
    #    inside it we only lean with where price sits in the range.
    if f.breakout_atr != 0.0:
        bo_score = ind.squash(f.breakout_atr / 0.5)
        bo_note = (
            f"closed {abs(f.breakout_atr):.2f} ATR "
            f"{'above' if f.breakout_atr > 0 else 'below'} the "
            f"{cfg.donchian_period}-bar {'high' if f.breakout_atr > 0 else 'low'}"
        )
    else:
        bo_score = (f.channel_pos - 0.5) * 2 * 0.55
        bo_note = f"inside the {cfg.donchian_period}-bar range at {f.channel_pos * 100:.0f}%"
    out.append(_c("breakout", f.breakout_atr or f.channel_pos, bo_score,
                  w.get("breakout", 0.0), bo_note))

    # 5. RSI as a momentum reading (not a mean-reversion one): 50 is neutral,
    #    70/30 is a full-strength trend, and we do not fade extremes.
    rsi_score = (f.rsi - 50.0) / 20.0
    out.append(_c("rsi", f.rsi, rsi_score, w.get("rsi", 0.0),
                  f"RSI {f.rsi:.0f}"))

    # 6. Rate-of-change z-score: is this move big relative to recent history?
    out.append(_c("momentum_z", f.roc_z, ind.squash(f.roc_z / 1.5),
                  w.get("momentum_z", 0.0),
                  f"{cfg.roc_period}-bar ROC {f.roc:+.2f}% ({f.roc_z:+.1f}σ)"))

    # 7. Order flow: who is hitting whom, on the bar and on the live tape.
    flow_score = (
        0.45 * ind.clamp(f.bar_delta_ratio * 2.5)
        + 0.35 * ind.squash(f.cvd_slope_atr / 0.5)
        + 0.20 * ind.clamp(f.tape_imbalance * 2.0)
    )
    out.append(_c(
        "flow", f.bar_delta_ratio, flow_score, w.get("flow", 0.0),
        f"bar delta {f.bar_delta_ratio * 100:+.0f}%, CVD slope {f.cvd_slope_atr:+.2f}, "
        f"tape {f.tape_imbalance * 100:+.0f}%",
    ))

    # 8. Resting book: imbalance plus the microprice tilt inside the spread.
    book_score = 0.6 * ind.clamp(f.book_imbalance * 2.5) + 0.4 * ind.clamp(f.micro_tilt)
    out.append(_c(
        "book", f.book_imbalance, book_score, w.get("book", 0.0),
        f"book {f.book_imbalance * 100:+.0f}% "
        f"{'bid' if f.book_imbalance >= 0 else 'ask'}-heavy within 10bps",
    ))

    return out


def composite_score(components: list[SignalComponent]) -> float:
    return ind.clamp(sum(c.contribution for c in components))


def classify_regime(f: Features, cfg: SignalConfig) -> tuple[Regime, list[str]]:
    """Regime plus the list of things blocking a new entry right now."""
    blockers: list[str] = []

    if f.bars < cfg.warmup_bars:
        blockers.append(f"warming up ({f.bars}/{cfg.warmup_bars} bars)")

    illiquid = False
    if f.book_levels and f.book_levels < cfg.min_book_levels:
        blockers.append(f"book only {f.book_levels} levels deep")
        illiquid = True
    if f.spread_bps > cfg.max_spread_bps:
        blockers.append(f"spread {f.spread_bps:.1f}bps > {cfg.max_spread_bps}bps")
        illiquid = True
    thin_side = min(f.depth_bid_usd, f.depth_ask_usd)
    if f.depth_bid_usd or f.depth_ask_usd:
        if thin_side < cfg.min_depth_usd:
            blockers.append(
                f"only ${thin_side / 1000:.0f}k resting within 10bps "
                f"(need ${cfg.min_depth_usd / 1000:.0f}k)"
            )
            illiquid = True

    if f.atr_pct < cfg.min_atr_pct:
        blockers.append(f"ATR {f.atr_pct:.2f}% below {cfg.min_atr_pct}% - move too small to pay costs")
    if f.atr_pct > cfg.max_atr_pct:
        blockers.append(f"ATR {f.atr_pct:.2f}% above {cfg.max_atr_pct}% - too wild")

    trending = f.adx >= cfg.min_adx
    if not trending:
        blockers.append(f"ADX {f.adx:.0f} < {cfg.min_adx:.0f} - chop")

    if illiquid:
        regime = Regime.ILLIQUID
    elif trending and f.ema_spread_atr > 0 and f.plus_di >= f.minus_di:
        regime = Regime.TREND_UP
    elif trending and f.ema_spread_atr < 0 and f.minus_di >= f.plus_di:
        regime = Regime.TREND_DOWN
    else:
        regime = Regime.CHOP
        if trending:
            blockers.append("trend direction and DI disagree")

    return regime, blockers


def top_reasons(components: list[SignalComponent], direction: int, limit: int = 4) -> list[str]:
    """The components actually driving the call, biggest contribution first."""
    aligned = [c for c in components if c.contribution * direction > 0]
    aligned.sort(key=lambda c: abs(c.contribution), reverse=True)
    return [f"{c.name}: {c.note}" for c in aligned[:limit]]


def dissent(components: list[SignalComponent], direction: int, limit: int = 2) -> list[str]:
    """Components arguing against the trade - shown so the call is auditable."""
    against = [c for c in components if c.contribution * direction < -0.01]
    against.sort(key=lambda c: abs(c.contribution), reverse=True)
    return [f"{c.name}: {c.note}" for c in against[:limit]]
