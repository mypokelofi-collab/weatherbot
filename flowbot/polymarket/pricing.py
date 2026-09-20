"""Turning a momentum score into a probability, and a probability into a bet.

Three steps, each of which can be wrong in its own way:

1. **Baseline.** Without a view, the chance BTC is above a level at time T is
   the lognormal probability implied by current price, the level, and
   volatility. At the money with no drift this is ~50% and the market knows
   it; our only possible edge is in the tilt and in the vol estimate.

2. **Tilt.** The composite momentum score becomes a small annualised drift:
   `mu = shrink * tilt * score * sigma`. Writing it in units of sigma makes
   the parameter interpretable - `tilt = 1.0` claims the signal is worth one
   unit of Sharpe at full strength, which is already an aggressive claim, and
   `shrink` is the honesty discount applied until out-of-sample calibration
   says otherwise.

3. **Bet.** On a binary paying $1, buying at cost c with true probability p
   has edge (p - c) per share and Kelly fraction (p - c)/(1 - c). We take a
   fraction of Kelly, because Kelly assumes you know p and we do not.

The vol input is deliberately inflated by `vol_haircut`: over-estimating
volatility pulls every probability toward 0.5, which shrinks our edge. That is
the direction we want to be wrong in.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

from ..core.types import BookSnapshot
from .market import EdgeAssessment, PredictionMarket

SECONDS_PER_YEAR = 365 * 24 * 3600


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def gbm_probability(
    spot: float,
    strike: float,
    sigma_annual: float,
    seconds_left: float,
    drift_annual: float = 0.0,
) -> float:
    """P(S_T > strike) under lognormal dynamics."""
    if spot <= 0 or strike <= 0 or seconds_left <= 0:
        return 1.0 if spot > strike else 0.0
    sigma = max(1e-6, sigma_annual)
    tau = seconds_left / SECONDS_PER_YEAR
    denom = sigma * math.sqrt(tau)
    if denom <= 0:
        return 1.0 if spot > strike else 0.0
    d2 = (math.log(spot / strike) + (drift_annual - 0.5 * sigma * sigma) * tau) / denom
    return norm_cdf(d2)


def momentum_drift(score: float, sigma_annual: float, tilt: float, shrink: float) -> float:
    """Annualised drift implied by the momentum score, in units of sigma."""
    return shrink * tilt * max(-1.0, min(1.0, score)) * sigma_annual


def model_probability(
    spot: float,
    strike: float,
    sigma_annual: float,
    seconds_left: float,
    score: float = 0.0,
    tilt: float = 0.8,
    shrink: float = 0.35,
    vol_haircut: float = 1.15,
    floor: float = 0.02,
) -> tuple[float, dict[str, float]]:
    """Our probability that the market resolves 'up', plus every input used."""
    sigma = max(1e-6, sigma_annual) * max(1.0, vol_haircut)
    drift = momentum_drift(score, sigma, tilt, shrink)
    p = gbm_probability(spot, strike, sigma, seconds_left, drift)
    p = min(1.0 - floor, max(floor, p))      # never claim certainty
    baseline = gbm_probability(spot, strike, sigma, seconds_left, 0.0)
    return p, {
        "spot": spot,
        "strike": strike,
        "sigma_annual": sigma,
        "seconds_left": seconds_left,
        "score": score,
        "drift_annual": drift,
        "baseline_p": baseline,
        "tilt_contribution": p - baseline,
    }


def kelly_fraction(p: float, cost: float) -> float:
    """Kelly stake fraction for a $1 binary bought at `cost`."""
    if cost <= 0 or cost >= 1:
        return 0.0
    f = (p - cost) / (1.0 - cost)
    return max(0.0, min(1.0, f))


def best_ask(book: BookSnapshot | None) -> tuple[float, float]:
    if not book or not book.asks:
        return (0.0, 0.0)
    return (book.asks[0].price, book.asks[0].qty)


def depth_within(book: BookSnapshot | None, limit_price: float, side: str = "ask") -> float:
    """Shares available at or better than `limit_price`."""
    if not book:
        return 0.0
    levels = book.asks if side == "ask" else book.bids
    total = 0.0
    for lv in levels:
        if side == "ask" and lv.price > limit_price:
            break
        if side == "bid" and lv.price < limit_price:
            break
        total += lv.qty
    return total


def assess(
    market: PredictionMarket,
    yes_book: BookSnapshot | None,
    no_book: BookSnapshot | None,
    model_p: float,
    equity: float,
    now_ms: int,
    min_edge: float = 0.04,
    kelly_cap: float = 0.25,
    max_stake_pct: float = 2.0,
    min_seconds: int = 300,
    max_spread: float = 0.04,
    slippage_ticks: int = 1,
) -> EdgeAssessment:
    """Compare our probability with the real book, and size what is left.

    The comparison is against the *ask we would actually lift*, not the mid.
    On a market with a three-cent spread, an edge measured against the mid is
    an edge that does not exist.
    """
    seconds_left = market.seconds_to_resolution(now_ms)
    ass = EdgeAssessment(
        market_id=market.id, slug=market.slug, side="none",
        model_p=model_p, seconds_left=seconds_left,
    )

    yes_ask, yes_size = best_ask(yes_book)
    no_ask, no_size = best_ask(no_book)
    if yes_book and yes_book.bids and yes_book.asks:
        ass.market_p = yes_book.mid
        ass.spread = yes_book.spread
    elif yes_ask:
        ass.market_p = yes_ask

    # Cost includes crossing a tick of slippage; thin books rarely give you
    # the touch for size.
    tick = market.tick_size or 0.01
    yes_cost = yes_ask + slippage_ticks * tick if yes_ask else 0.0
    no_cost = no_ask + slippage_ticks * tick if no_ask else 0.0

    edge_yes = (model_p - yes_cost) if yes_cost else -1.0
    edge_no = ((1.0 - model_p) - no_cost) if no_cost else -1.0

    if edge_yes >= edge_no:
        ass.side, ass.cost, ass.edge = "yes", yes_cost, edge_yes
        book_side, book_p = yes_book, model_p
    else:
        ass.side, ass.cost, ass.edge = "no", no_cost, edge_no
        book_side, book_p = no_book, 1.0 - model_p

    # -- gates -------------------------------------------------------------
    if market.closed or not market.active:
        ass.blockers.append("market is closed")
    if not market.resolution.verified:
        ass.blockers.append(
            "resolution rule not verified against our own price series"
        )
    if seconds_left < min_seconds:
        ass.blockers.append(
            f"only {seconds_left:.0f}s to resolution (min {min_seconds}s)"
        )
    if not yes_ask and not no_ask:
        ass.blockers.append("no resting offers on either side")
    if ass.spread and ass.spread > max_spread:
        ass.blockers.append(
            f"spread {ass.spread * 100:.1f}c wider than {max_spread * 100:.0f}c"
        )
    if ass.edge < min_edge:
        ass.blockers.append(
            f"edge {ass.edge * 100:+.1f} points below the {min_edge * 100:.0f}-point minimum"
        )

    # -- sizing ------------------------------------------------------------
    ass.kelly_fraction = kelly_fraction(book_p, ass.cost) * kelly_cap
    ass.depth_shares = depth_within(book_side, ass.cost, "ask")
    stake_cap = equity * max_stake_pct / 100.0
    stake = min(equity * ass.kelly_fraction, stake_cap)
    lot = max(1.0, market.min_order_size)
    min_notional = market.min_notional or 1.0
    if ass.cost > 0:
        shares = stake / ass.cost
        shares = min(shares, ass.depth_shares * 0.5)     # leave room to exit
        shares = math.floor(shares / lot) * lot
        # The lot-size minimum alone is not enough: at a low price, that many
        # shares can still be worth less than the venue's minimum order
        # value. Round up to the smallest lot multiple that clears it, but
        # only if the stake cap and the book can actually support it -
        # otherwise leave it short and let the blocker below catch it.
        if 0 < shares * ass.cost < min_notional:
            needed = math.ceil(min_notional / ass.cost / lot) * lot
            if needed * ass.cost <= stake_cap and needed <= ass.depth_shares:
                shares = needed
        ass.shares = max(0.0, shares)
        ass.stake = ass.shares * ass.cost

    if ass.shares < market.min_order_size:
        ass.blockers.append(
            f"size rounds below the venue minimum of {market.min_order_size:g} shares"
        )
    elif ass.stake < min_notional:
        ass.blockers.append(
            f"stake ${ass.stake:.2f} below the ${min_notional:.2f} venue minimum order value"
        )

    ass.tradable = not ass.blockers and ass.shares > 0
    ass.inputs = {
        "yes_ask": yes_ask, "no_ask": no_ask,
        "yes_size": yes_size, "no_size": no_size,
        "tick": tick,
    }
    return ass


# ---------------------------------------------------------------- calibration

def brier_score(pairs: Sequence[tuple[float, int]]) -> float:
    """Mean squared error of probability forecasts. Lower is better; 0.25 is
    the score of always saying 50%, so anything above that is worse than
    useless."""
    if not pairs:
        return 0.0
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def log_loss(pairs: Sequence[tuple[float, int]], eps: float = 1e-6) -> float:
    if not pairs:
        return 0.0
    total = 0.0
    for p, o in pairs:
        p = min(1 - eps, max(eps, p))
        total += -(o * math.log(p) + (1 - o) * math.log(1 - p))
    return total / len(pairs)


def reliability_table(
    pairs: Sequence[tuple[float, int]], bins: int = 10
) -> list[dict]:
    """Forecast versus frequency, bucketed.

    This is the table that decides whether the pipeline is allowed to trade:
    if the 70% bucket does not resolve 'up' about 70% of the time, the model
    is not a probability, it is an opinion.
    """
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for p, o in pairs:
        idx = min(bins - 1, max(0, int(p * bins)))
        buckets[idx].append((p, o))
    out = []
    for i, bucket in enumerate(buckets):
        if not bucket:
            out.append({"bin": f"{i / bins:.1f}-{(i + 1) / bins:.1f}", "n": 0,
                        "forecast": 0.0, "observed": 0.0, "gap": 0.0})
            continue
        forecast = sum(p for p, _ in bucket) / len(bucket)
        observed = sum(o for _, o in bucket) / len(bucket)
        out.append({
            "bin": f"{i / bins:.1f}-{(i + 1) / bins:.1f}",
            "n": len(bucket),
            "forecast": round(forecast, 4),
            "observed": round(observed, 4),
            "gap": round(observed - forecast, 4),
        })
    return out


def calibration_report(pairs: Sequence[tuple[float, int]]) -> dict:
    base = sum(o for _, o in pairs) / len(pairs) if pairs else 0.0
    reference = brier_score([(base, o) for _, o in pairs]) if pairs else 0.0
    score = brier_score(pairs)
    return {
        "n": len(pairs),
        "brier": round(score, 5),
        "brier_of_always_base_rate": round(reference, 5),
        "brier_skill_score": round(1 - score / reference, 4) if reference else 0.0,
        "log_loss": round(log_loss(pairs), 5),
        "base_rate": round(base, 4),
        "reliability": reliability_table(pairs),
    }


def outcomes_from_series(
    closes: Iterable[float], horizon_bars: int
) -> list[int]:
    """Label helper: was the close `horizon_bars` later higher?"""
    values = list(closes)
    return [
        1 if values[i + horizon_bars] > values[i] else 0
        for i in range(len(values) - horizon_bars)
    ]
