"""Prediction-market domain types.

A Polymarket binary market is not a symbol - it is a *question with a
deadline and a resolution rule*. Getting the rule wrong is the single
largest source of loss in this pipeline, larger than any modelling error, so
the resolution spec is a first-class object here rather than a string in a
description field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def parse_iso(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(
            datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            .astimezone(timezone.utc).timestamp() * 1000
        )
    except (ValueError, TypeError):
        return 0


@dataclass
class ResolutionSpec:
    """How the market decides the answer.

    `reference` is the price series the market settles against, and it must
    match the series the bot forecasts. "BTC went up" measured on Binance
    1-minute closes at 12:00 ET is a different question from the same phrase
    measured on a Coinbase index at 00:00 UTC, and the difference is not
    noise - it is a systematically wrong bet.
    """

    reference: str = "unknown"          # e.g. "binance:BTCUSDT:1m-close"
    open_ts: int = 0                    # when the comparison window opens
    close_ts: int = 0                   # when it closes (resolution time)
    strike: float = 0.0                 # the level being compared against
    strike_known: bool = False          # False until the open price is fixed
    timezone_note: str = ""
    description: str = ""

    @property
    def verified(self) -> bool:
        """True only when we can reproduce the settlement from our own data."""
        return self.strike_known and self.reference != "unknown"

    def to_dict(self) -> dict:
        return {
            "reference": self.reference,
            "open_ts": self.open_ts,
            "close_ts": self.close_ts,
            "strike": self.strike,
            "strike_known": self.strike_known,
            "verified": self.verified,
            "timezone_note": self.timezone_note,
            "description": self.description[:400],
        }


@dataclass
class Outcome:
    name: str                            # "Up" / "Down" / "Yes" / "No"
    token_id: str
    last_price: float = 0.0

    def to_dict(self) -> dict:
        return {"name": self.name, "token_id": self.token_id, "last_price": self.last_price}


@dataclass
class PredictionMarket:
    id: str
    slug: str
    question: str
    condition_id: str
    outcomes: list[Outcome] = field(default_factory=list)
    end_ts: int = 0
    start_ts: int = 0
    volume: float = 0.0
    liquidity: float = 0.0
    tick_size: float = 0.01
    min_order_size: float = 5.0          # shares
    neg_risk: bool = False
    active: bool = True
    closed: bool = False
    resolved_outcome: str = ""
    resolution: ResolutionSpec = field(default_factory=ResolutionSpec)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def yes(self) -> Outcome | None:
        for o in self.outcomes:
            if o.name.lower() in ("yes", "up"):
                return o
        return self.outcomes[0] if self.outcomes else None

    @property
    def no(self) -> Outcome | None:
        for o in self.outcomes:
            if o.name.lower() in ("no", "down"):
                return o
        return self.outcomes[1] if len(self.outcomes) > 1 else None

    def seconds_to_resolution(self, now_ms: int) -> float:
        return max(0.0, (self.end_ts - now_ms) / 1000.0)

    def to_dict(self, now_ms: int = 0) -> dict:
        return {
            "id": self.id,
            "slug": self.slug,
            "question": self.question,
            "condition_id": self.condition_id,
            "outcomes": [o.to_dict() for o in self.outcomes],
            "end_ts": self.end_ts,
            "start_ts": self.start_ts,
            "seconds_left": self.seconds_to_resolution(now_ms) if now_ms else None,
            "volume": self.volume,
            "liquidity": self.liquidity,
            "tick_size": self.tick_size,
            "min_order_size": self.min_order_size,
            "neg_risk": self.neg_risk,
            "active": self.active,
            "closed": self.closed,
            "resolved_outcome": self.resolved_outcome,
            "resolution": self.resolution.to_dict(),
        }


@dataclass
class EdgeAssessment:
    """One market, one moment: what we think versus what it costs."""

    market_id: str
    slug: str
    side: str                            # "yes" | "no" | "none"
    model_p: float = 0.0
    market_p: float = 0.0                # mid, in probability
    cost: float = 0.0                    # the ask we would actually pay
    edge: float = 0.0                    # model_p - cost, in probability points
    kelly_fraction: float = 0.0
    stake: float = 0.0
    shares: float = 0.0
    spread: float = 0.0
    depth_shares: float = 0.0
    seconds_left: float = 0.0
    tradable: bool = False
    blockers: list[str] = field(default_factory=list)
    inputs: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "slug": self.slug,
            "side": self.side,
            "model_p": round(self.model_p, 4),
            "market_p": round(self.market_p, 4),
            "cost": round(self.cost, 4),
            "edge": round(self.edge, 4),
            "kelly_fraction": round(self.kelly_fraction, 4),
            "stake": round(self.stake, 2),
            "shares": round(self.shares, 2),
            "spread": round(self.spread, 4),
            "depth_shares": round(self.depth_shares, 2),
            "seconds_left": round(self.seconds_left, 1),
            "tradable": self.tradable,
            "blockers": self.blockers,
            "inputs": {k: round(v, 6) for k, v in self.inputs.items()},
        }
