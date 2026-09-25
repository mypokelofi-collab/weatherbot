"""Trade ledger and the honest scorecard.

A 60% win rate over 20 trades means almost nothing: the 95% interval runs
from roughly 39% to 78%. The scorecard reports the Wilson lower bound next to
the breakeven rate, and only calls an edge "evidenced" when the lower bound
clears breakeven.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .risk import breakeven_win_rate


@dataclass
class Trade:
    id: str
    asset: str
    direction: str
    stake: float
    payout: float                  # fraction, e.g. 0.85
    opened_at: float
    expires_at: float
    entry: float | None = None
    exit: float | None = None
    result: str = "open"           # open | win | loss | draw | error
    pnl: float = 0.0
    reason: str = ""
    account: str = "paper"         # paper | demo | real

    def settle(self, result: str, pnl: float | None = None, exit_price: float | None = None) -> None:
        self.result = result
        if exit_price is not None:
            self.exit = exit_price
        if pnl is None:
            pnl = {"win": self.stake * self.payout, "loss": -self.stake}.get(result, 0.0)
        self.pnl = round(pnl, 2)


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass
class Scorecard:
    trades: list[Trade] = field(default_factory=list)

    def add(self, t: Trade) -> None:
        self.trades.append(t)

    def summary(self) -> dict:
        done = [t for t in self.trades if t.result in ("win", "loss", "draw")]
        wins = sum(t.result == "win" for t in done)
        losses = sum(t.result == "loss" for t in done)
        draws = sum(t.result == "draw" for t in done)
        decided = wins + losses
        pnl = round(sum(t.pnl for t in done), 2)
        staked = sum(t.stake for t in done)
        avg_payout = (sum(t.payout for t in done) / len(done)) if done else 0.0
        be = breakeven_win_rate(avg_payout) if done else 0.0
        lo, hi = wilson_interval(wins, decided)
        win_rate = wins / decided if decided else 0.0
        if decided < 30:
            verdict = f"not enough trades ({decided}) to say anything"
        elif lo > be:
            verdict = "edge evidenced: 95% lower bound is above breakeven"
        elif win_rate > be:
            verdict = "above breakeven but not significant yet; keep testing on demo"
        else:
            verdict = "no edge: win rate is below breakeven"
        return {
            "trades": len(done), "wins": wins, "losses": losses, "draws": draws,
            "win_rate": round(win_rate, 4), "win_rate_95": [round(lo, 4), round(hi, 4)],
            "breakeven": round(be, 4), "avg_payout": round(avg_payout, 4),
            "pnl": pnl, "roi": round(pnl / staked, 4) if staked else 0.0,
            "verdict": verdict,
        }


class Ledger:
    """Append-only JSONL of settled trades, so stats survive restarts."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, t: Trade) -> None:
        if not self.path:
            return
        with open(self.path, "a") as fh:
            fh.write(json.dumps(asdict(t)) + "\n")

    def load(self) -> list[Trade]:
        if not self.path or not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            if line.strip():
                out.append(Trade(**json.loads(line)))
        return out
