"""Signal engine: bars in, decisions out.

The engine is stateless with respect to money. It knows what the market is
doing and whether the bot currently holds a position, and it answers one
question per bar: enter, exit, or sit still. Everything about *size*, *risk*
and *orders* lives in the bot layer - which is what lets the same engine drive
the live bot, the backtester and the Polymarket pricer.

Exit logic is deliberately asymmetric to entry. Entries need a strong,
confirmed, liquid setup; exits fire as soon as the edge stops being there,
because a momentum trade that is no longer moving is just risk with no thesis.
"""

from __future__ import annotations

from typing import Sequence

from ..core.config import SignalConfig
from ..core.types import (
    BookSnapshot,
    Candle,
    Regime,
    Side,
    Signal,
    SignalAction,
)
from .features import Features, TapeWindow, compute_features
from .momentum import (
    classify_regime,
    composite_score,
    dissent,
    score_components,
    top_reasons,
)


class SignalEngine:
    def __init__(self, cfg: SignalConfig) -> None:
        self.cfg = cfg
        self.last_signal: Signal | None = None
        self.last_features: Features | None = None
        self.history: list[Signal] = []

    def evaluate(
        self,
        candles: Sequence[Candle],
        book: BookSnapshot | None = None,
        tape: TapeWindow | None = None,
        position_side: Side | None = None,
        now_ts: int = 0,
    ) -> Signal:
        cfg = self.cfg
        f = compute_features(candles, cfg, book, tape)
        components = score_components(f, cfg)
        score = composite_score(components)
        regime, blockers = classify_regime(f, cfg)

        direction = 1 if score > 0 else (-1 if score < 0 else 0)
        reasons = top_reasons(components, direction)
        against = dissent(components, direction)

        # Counter-trend entries are the classic way a momentum bot bleeds:
        # a flow spike against an established trend scores well for one bar
        # and then gets run over.
        if cfg.require_trend_alignment and direction != 0:
            if direction > 0 and f.ema_spread_atr < 0:
                blockers.append("score is long but price is below the slow EMA")
            if direction < 0 and f.ema_spread_atr > 0:
                blockers.append("score is short but price is above the slow EMA")

        action = SignalAction.NONE
        if position_side is None:
            if not blockers and abs(score) >= cfg.entry_threshold:
                action = (
                    SignalAction.ENTER_LONG if score > 0 else SignalAction.ENTER_SHORT
                )
            elif abs(score) >= cfg.entry_threshold:
                action = SignalAction.NONE      # setup is there, gates say no
            else:
                action = SignalAction.NONE
        else:
            held = 1 if position_side is Side.BUY else -1
            aligned = score * held
            if aligned <= -cfg.flip_threshold:
                action = SignalAction.EXIT
                reasons = [f"momentum flipped against the position ({score:+.2f})"] + reasons
            elif aligned < cfg.exit_threshold:
                action = SignalAction.EXIT
                reasons = [
                    f"momentum decayed to {aligned:+.2f} "
                    f"(exit below {cfg.exit_threshold:+.2f})"
                ] + reasons
            else:
                action = SignalAction.HOLD

        sig = Signal(
            ts=now_ts or f.ts,
            bar_time=f.bar_time,
            action=action,
            score=score,
            confidence=min(1.0, abs(score) / max(cfg.entry_threshold, 1e-9)),
            regime=regime,
            components=components,
            reasons=reasons,
            blockers=blockers,
            features=f.to_dict(),
            price=f.price,
            atr=f.atr,
        )
        self.last_signal = sig
        self.last_features = f
        self.history.append(sig)
        if len(self.history) > 1000:
            self.history = self.history[-1000:]
        return sig

    # -- helpers used by the dashboard and the Polymarket pricer ----------
    def ready(self, candles: Sequence[Candle]) -> bool:
        return len(candles) >= self.cfg.warmup_bars

    @staticmethod
    def wants_entry(sig: Signal) -> Side | None:
        if sig.action is SignalAction.ENTER_LONG:
            return Side.BUY
        if sig.action is SignalAction.ENTER_SHORT:
            return Side.SELL
        return None

    @staticmethod
    def is_tradeable_regime(sig: Signal) -> bool:
        return sig.regime in (Regime.TREND_UP, Regime.TREND_DOWN)
