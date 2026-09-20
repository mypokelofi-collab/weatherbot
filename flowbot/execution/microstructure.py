"""Liquidity analytics read straight off the real book.

Used in two places:
  * the risk layer, to cap position size at what the book can actually absorb
    without the entry costing more than the edge is worth;
  * the dashboard, to show what a trade would cost *right now*.

All of it is derived from resting depth, so it is only as good as the book -
which is exactly why the feed refuses to serve a book it knows is stale.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.types import BookSnapshot, Side


@dataclass
class CostEstimate:
    qty: float
    filled_qty: float
    avg_price: float
    slippage_bps: float
    levels: int
    complete: bool
    notional: float

    def to_dict(self) -> dict:
        return {
            "qty": self.qty,
            "filled_qty": self.filled_qty,
            "avg_price": self.avg_price,
            "slippage_bps": round(self.slippage_bps, 3),
            "levels": self.levels,
            "complete": self.complete,
            "notional": self.notional,
        }


def walk_book(book: BookSnapshot, side: Side, qty: float, max_levels: int = 50) -> CostEstimate:
    """What a market order for `qty` would actually pay against this book."""
    levels = book.asks[:max_levels] if side is Side.BUY else book.bids[:max_levels]
    mid = book.mid
    remaining = qty
    notional = 0.0
    used = 0
    for lv in levels:
        if remaining <= 1e-12:
            break
        take = min(remaining, lv.qty)
        notional += take * lv.price
        remaining -= take
        used += 1
    filled = qty - remaining
    avg = (notional / filled) if filled > 0 else 0.0
    slip = ((avg - mid) / mid * 1e4 * side.sign) if (mid and filled > 0) else 0.0
    return CostEstimate(
        qty=qty, filled_qty=filled, avg_price=avg, slippage_bps=slip,
        levels=used, complete=remaining <= 1e-12, notional=notional,
    )


def max_qty_within_slippage(
    book: BookSnapshot, side: Side, budget_bps: float, max_levels: int = 50
) -> float:
    """Largest order that still fills inside the slippage budget.

    Walks level by level and stops at the last level where the running VWAP is
    still inside budget - the honest answer to "how big can I go right now".
    """
    levels = book.asks[:max_levels] if side is Side.BUY else book.bids[:max_levels]
    mid = book.mid
    if not mid or not levels:
        return 0.0
    qty = 0.0
    notional = 0.0
    best = 0.0
    for lv in levels:
        qty += lv.qty
        notional += lv.qty * lv.price
        avg = notional / qty
        slip = (avg - mid) / mid * 1e4 * side.sign
        if slip <= budget_bps:
            best = qty
        else:
            # Partially consume this level: solve for the qty that lands
            # exactly on the budget.
            limit_avg = mid * (1 + budget_bps / 1e4 * side.sign)
            prev_qty = qty - lv.qty
            prev_notional = notional - lv.qty * lv.price
            denom = lv.price - limit_avg
            if abs(denom) > 1e-12:
                extra = (limit_avg * prev_qty - prev_notional) / denom
                if extra > 0:
                    best = max(best, prev_qty + min(extra, lv.qty))
            break
    return max(0.0, best)


def liquidity_score(book: BookSnapshot, ref_notional: float = 250_000.0) -> float:
    """0..1 summary of how tradable the book is: depth first, spread second."""
    if not book.bids or not book.asks:
        return 0.0
    bid_usd, ask_usd = book.depth_notional(10.0)
    depth = min(bid_usd, ask_usd) / ref_notional
    depth_score = min(1.0, depth)
    spread_score = max(0.0, 1.0 - book.spread_bps / 5.0)
    return round(0.7 * depth_score + 0.3 * spread_score, 4)


def book_pressure(book: BookSnapshot) -> dict:
    """Snapshot of the pressure metrics the dashboard renders."""
    bid10, ask10 = book.depth_notional(10.0)
    bid25, ask25 = book.depth_notional(25.0)
    return {
        "imbalance_10bps": round(book.imbalance(10.0), 4),
        "imbalance_25bps": round(book.imbalance(25.0), 4),
        "depth_bid_10bps": round(bid10, 2),
        "depth_ask_10bps": round(ask10, 2),
        "depth_bid_25bps": round(bid25, 2),
        "depth_ask_25bps": round(ask25, 2),
        "spread_bps": round(book.spread_bps, 4),
        "mid": book.mid,
        "microprice": round(book.microprice, 4),
        "liquidity_score": liquidity_score(book),
    }
