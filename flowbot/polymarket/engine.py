"""The Polymarket pipeline: signal -> probability -> edge -> paper position.

This is phase 2 of the plan in docs/POLYMARKET_PIPELINE.md. It runs beside
the perp bot, reads the same signal, and paper-trades prediction markets
against their real CLOB book using the same matching engine. It cannot place
a real order: there is no wallet, no key, and no signing code in this package.

What it is actually hunting for is worth stating plainly, because it is not
what it looks like. The momentum tilt moves a short-dated probability by a
fraction of a point - far less than the spread. The exploitable thing is
*repricing lag*: when BTC moves 1% against a fixed strike, fair value for a
daily up/down market moves tens of points, and a quote that has not moved yet
is a real, large edge for as long as it survives. The momentum score's job is
to say whether that move is likely to stick, and to veto the ones that are
noise.

Positions settle against our own copy of the reference price series - the
same Binance feed the bot already consumes - and are cross-checked against
the venue's reported resolution when it is available.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field

from ..core.config import ExecConfig, PolymarketConfig
from ..core.instrument import Instrument
from ..core.types import BookSnapshot, OrderType, Side, TimeInForce
from ..execution.simulator import MatchingEngine
from .client import PolymarketClient
from .market import EdgeAssessment, PredictionMarket
from .pricing import assess, calibration_report, model_probability

log = logging.getLogger(__name__)


@dataclass
class PaperPosition:
    market_id: str
    slug: str
    side: str                       # "yes" | "no"
    token_id: str
    shares: float
    cost: float                     # average price paid per share
    opened_ts: int
    end_ts: int
    strike: float
    model_p_at_entry: float
    edge_at_entry: float
    mark: float = 0.0
    settled: bool = False
    outcome: int | None = None      # 1 = our side won
    pnl: float = 0.0
    settle_note: str = ""
    # True when `force_min_trades` opened this position with no organic edge.
    # Kept out of the calibration/Brier stats entirely (see `_settle_due`) -
    # a forecast this pipeline was told to ignore the disagreement on is not
    # a data point about whether the model is any good.
    forced: bool = False

    @property
    def stake(self) -> float:
        return self.shares * self.cost

    def unrealized(self, mark: float) -> float:
        return (mark - self.cost) * self.shares

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "slug": self.slug,
            "side": self.side,
            "shares": round(self.shares, 2),
            "cost": round(self.cost, 4),
            "stake": round(self.stake, 2),
            "mark": round(self.mark, 4),
            "unrealized": round(self.unrealized(self.mark), 2),
            "opened_ts": self.opened_ts,
            "end_ts": self.end_ts,
            "strike": self.strike,
            "model_p_at_entry": round(self.model_p_at_entry, 4),
            "edge_at_entry": round(self.edge_at_entry, 4),
            "settled": self.settled,
            "outcome": self.outcome,
            "pnl": round(self.pnl, 2),
            "settle_note": self.settle_note,
            "forced": self.forced,
        }


class PolymarketPipeline:
    def __init__(
        self,
        cfg: PolymarketConfig,
        client: PolymarketClient | None = None,
        equity: float = 10_000.0,
    ) -> None:
        self.cfg = cfg
        self.client = client or PolymarketClient(cfg.gamma_url, cfg.clob_url)
        self.equity = equity
        self.start_equity = equity

        # Prediction-market contracts: price is a probability, size is shares.
        self.instrument = Instrument(
            symbol="POLY", base="SHARES", quote="USDC",
            tick_size=0.01, step_size=1.0, min_qty=5.0, min_notional=1.0,
        )
        # Latency is not modelled as a number here: the pipeline actually
        # waits and then re-reads the real book, so the engine itself needs no
        # artificial delay (see _paper_buy).
        exec_cfg = ExecConfig(
            latency_ms=0,
            taker_fee_bps=cfg.taker_fee_bps,
            maker_fee_bps=0.0,
            max_slippage_bps=10_000,      # the edge test already prices slippage
            book_stale_ms=60_000,
        )
        self.latency_ms = ROUND_TRIP_MS
        self.engine = MatchingEngine(exec_cfg, self.instrument)

        self.markets: list[PredictionMarket] = []
        self.assessments: list[EdgeAssessment] = []
        self.positions: list[PaperPosition] = []
        self.forecasts: list[tuple[float, int]] = []    # for calibration
        self.events: list[dict] = []
        self.last_poll = 0
        self.last_error = ""
        self.running = False
        self._task: asyncio.Task | None = None
        self._spot_provider = None
        self._signal_provider = None
        self._window_open_provider = None

    # -- wiring ------------------------------------------------------------
    def bind(self, spot_provider, signal_provider, window_open_provider=None) -> None:
        """`spot_provider() -> (price, sigma_annual)`, `signal_provider() -> score`,
        `window_open_provider(ts_ms) -> float | None` for window-relative markets
        (the recurring 5m/15m/4h family - see `_resolve_strike`)."""
        self._spot_provider = spot_provider
        self._signal_provider = signal_provider
        self._window_open_provider = window_open_provider

    def _event(self, kind: str, message: str) -> None:
        entry = {"ts": int(time.time() * 1000), "kind": kind, "message": message}
        self.events.append(entry)
        self.events = self.events[-200:]
        log.info("[polymarket:%s] %s", kind, message)

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._loop(), name="polymarket-pipeline")
        self._event("start", f"watching markets matching '{self.cfg.market_slug_contains}'")

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        await self.client.close()

    async def _loop(self) -> None:
        while self.running:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("polymarket poll failed")
            await asyncio.sleep(max(5, self.cfg.poll_seconds))

    # -- the cycle ---------------------------------------------------------
    async def poll(self) -> list[EdgeAssessment]:
        now = int(time.time() * 1000)
        self.last_poll = now

        markets = await self._discover_windows(now)
        if not markets:
            self.last_error = self.client.last_error or "no markets matched"
            return []
        self.markets = markets

        spot, sigma = self._spot()
        score = self._score()
        out: list[EdgeAssessment] = []

        for market in markets:
            yes, no = market.yes, market.no
            if not yes or not no:
                continue
            self._resolve_strike(market, now)
            strike = market.resolution.strike or spot
            seconds = market.seconds_to_resolution(now)
            model_p, inputs = model_probability(
                spot=spot, strike=strike, sigma_annual=sigma,
                seconds_left=seconds, score=score,
                tilt=self.cfg.calibration_a * 0.35, shrink=1.0,
            )
            yes_book = await self.client.get_book(yes.token_id)
            no_book = await self.client.get_book(no.token_id)
            a = assess(
                market, yes_book, no_book, model_p, self.equity, now,
                min_edge=self.cfg.min_edge,
                kelly_cap=self.cfg.kelly_fraction,
                max_stake_pct=self.cfg.max_stake_pct,
                min_seconds=self.cfg.min_seconds_to_resolution,
            )
            a.inputs.update(inputs)

            forced = False
            if (
                not a.tradable
                and self.cfg.force_min_trades
                and not self._has_position(market.id)
            ):
                forced = self._force_if_due(market, a)

            out.append(a)
            if (a.tradable or forced) and not self._has_position(market.id):
                token = market.yes if a.side == "yes" else market.no
                await self._paper_buy(
                    market, a, token.token_id if token else "", now, forced=forced
                )

        self.assessments = out
        await self._settle_due(now)
        return out

    async def _discover_windows(self, now_ms: int) -> list[PredictionMarket]:
        """The recurring BTC window family is created on a fixed clock, so
        rather than search for it, compute its slug directly: the epoch in
        `btc-updown-15m-<epoch>` is the window's own open time, aligned to
        `window_seconds`. Exact-slug lookup is also the only Gamma filter
        that actually narrows server-side - see `client.get_market_by_slug`.

        Fetches the current window and the next one, so a position can be
        sized up before the current window's book thins out into close.
        """
        step_ms = self.cfg.window_seconds * 1000
        if step_ms <= 0:
            return []
        aligned = (now_ms // step_ms) * step_ms
        out: list[PredictionMarket] = []
        for open_ts in (aligned, aligned + step_ms):
            slug = f"{self.cfg.market_slug_contains}-{open_ts // 1000}"
            market = await self.client.get_market_by_slug(slug)
            if market:
                out.append(market)
        return out

    def _resolve_strike(self, market: PredictionMarket, now_ms: int) -> None:
        """Fill in the strike for a window-relative market from our own price
        series - only once the window has actually started, which is exactly
        what `strike_known`'s existing contract already means ("False until
        the open price is fixed"). Nothing to do for a fixed-strike market;
        its strike came straight out of the text in `infer_resolution`.
        """
        spec = market.resolution
        if spec.strike_mode != "window_open" or spec.strike_known:
            return
        if now_ms < market.window_open_ts or not self._window_open_provider:
            return
        try:
            price = self._window_open_provider(market.window_open_ts)
        except Exception:  # noqa: BLE001
            price = None
        if price:
            spec.strike = price
            spec.strike_known = True
            spec.open_ts = market.window_open_ts

    def _force_if_due(self, market: PredictionMarket, a: EdgeAssessment) -> bool:
        """`force_min_trades`: guarantee a paper trade in this window even
        with no organic edge, once close enough to resolution that an
        organic signal was never going to arrive in time.

        Still refuses anything that is not an economic judgement call - an
        unverified resolution rule, a closed market, an empty book. Forcing a
        trade this pipeline could not even score would corrupt the ledger,
        not just the calibration stats. `edge`, `spread`, `size` and `stake`
        are the blockers this is allowed to override - a weak or negative
        edge already zeroes Kelly sizing on its own, which is exactly the
        situation this mode exists to override (sizing is replaced with the
        smallest fillable lot below, not left at zero).
        """
        overridable = {"edge", "spread", "size", "stake"}
        hard_blockers = [b for b in a.blockers if b.split()[0] not in overridable]
        if hard_blockers:
            return False
        if a.seconds_left > self.cfg.force_trade_before_close_s:
            return False              # still time for an organic signal
        if a.seconds_left < self.cfg.min_seconds_to_resolution:
            return False              # too late even for a forced entry
        if a.cost <= 0 or a.cost >= 1:
            return False              # no real quote to transact against

        # Sizing here is deliberately not Kelly - Kelly on a ~zero or negative
        # edge sizes to zero, which is correct for a real bet and useless for
        # a forced one. Take the smallest lot that also clears the venue's
        # minimum order value; at a low price the lot-size minimum alone can
        # still be worth under that.
        lot = max(1.0, market.min_order_size)
        min_notional = market.min_notional or 1.0
        shares = lot
        if shares * a.cost < min_notional:
            shares = math.ceil(min_notional / a.cost / lot) * lot
        if shares > a.depth_shares:
            return False              # book can't fill even the minimum viable size

        a.shares = shares
        a.stake = a.shares * a.cost
        a.kelly_fraction = 0.0
        a.blockers = [f"forced: {b}" for b in a.blockers]
        a.tradable = True
        return True

    def _spot(self) -> tuple[float, float]:
        if self._spot_provider:
            try:
                return self._spot_provider()
            except Exception:  # noqa: BLE001
                pass
        return (0.0, 0.5)

    def _score(self) -> float:
        if self._signal_provider:
            try:
                return float(self._signal_provider())
            except Exception:  # noqa: BLE001
                pass
        return 0.0

    def _has_position(self, market_id: str) -> bool:
        return any(p.market_id == market_id and not p.settled for p in self.positions)

    # -- paper execution ---------------------------------------------------
    async def _paper_buy(
        self,
        market: PredictionMarket,
        a: EdgeAssessment,
        token_id: str,
        now: int,
        forced: bool = False,
    ) -> PaperPosition | None:
        """Send the order, wait out the round trip, fill on the book that is
        there *when it arrives*.

        This is the honest way to model a latency race on a venue we can read
        but not trade. The edge this pipeline hunts lives in quotes that have
        not repriced yet - and those are exactly the quotes most likely to be
        pulled in the half second our order is in flight. So rather than
        assuming the offer is still there, we wait and look again. A vanished
        quote produces no fill, which is the correct outcome.
        """
        await asyncio.sleep(self.latency_ms / 1000.0)
        arrival = now + self.latency_ms
        book = await self.client.get_book(token_id)
        if book is None or not book.asks:
            self._event("miss", f"{market.slug}: book gone by the time the order landed")
            return None

        fresh = BookSnapshot(ts=arrival, bids=book.bids, asks=book.asks, seq=book.seq)
        self.engine.instrument = Instrument(
            symbol=market.slug[:20] or "POLY", base="SHARES", quote="USDC",
            tick_size=market.tick_size or 0.01, step_size=1.0,
            min_qty=market.min_order_size or 5.0,
            min_notional=market.min_notional or 1.0,
        )
        self.engine.set_book(fresh)
        order = self.engine.submit(
            side=Side.BUY, qty=a.shares, order_type=OrderType.LIMIT,
            price=a.cost, tif=TimeInForce.IOC, tag=f"poly:{a.side}:{market.slug}",
        )
        if order.filled_qty <= 0:
            self._event(
                "miss",
                f"{market.slug}: no fill at {a.cost:.2f} after the round trip "
                f"({order.reject_reason or 'quote moved away'})",
            )
            return None

        pos = PaperPosition(
            market_id=market.id, slug=market.slug, side=a.side,
            token_id=token_id,
            shares=order.filled_qty, cost=order.avg_price,
            opened_ts=arrival, end_ts=market.end_ts,
            strike=market.resolution.strike, model_p_at_entry=a.model_p,
            edge_at_entry=a.edge, mark=order.avg_price, forced=forced,
        )
        self.positions.append(pos)
        self.equity -= pos.stake + order.fees
        filled_pct = order.filled_qty / a.shares * 100 if a.shares else 0
        self._event(
            "entry",
            f"{market.slug}: {'FORCED ' if forced else ''}bought {pos.shares:g} "
            f"{a.side.upper()} at {pos.cost:.2f} ({filled_pct:.0f}% of intended) · "
            f"model {a.model_p:.0%} · edge {a.edge * 100:+.1f}pts · "
            f"stake ${pos.stake:.2f}",
        )
        return pos

    # -- settlement --------------------------------------------------------
    async def _settle_due(self, now: int) -> None:
        for pos in self.positions:
            if pos.settled or now < pos.end_ts:
                continue
            outcome, note = await self._resolve(pos, now)
            if outcome is None:
                continue
            pos.settled = True
            pos.outcome = outcome
            payout = pos.shares * (1.0 if outcome else 0.0)
            pos.pnl = payout - pos.stake
            pos.mark = 1.0 if outcome else 0.0
            pos.settle_note = note
            self.equity += payout
            if not pos.forced:
                # A forced trade was told to ignore whatever the model's own
                # edge said; scoring it as a forecast would flatter or damage
                # the Brier/reliability numbers for a "prediction" that was
                # never really one. Its PnL still counts in equity above, and
                # it is fully visible in `stats["forced"]`.
                up_happened = outcome if pos.side == "yes" else 1 - outcome
                self.forecasts.append((pos.model_p_at_entry, int(up_happened)))
            self._event(
                "settle",
                f"{pos.slug}: {'FORCED ' if pos.forced else ''}"
                f"{'WON' if outcome else 'LOST'} · "
                f"${pos.pnl:+.2f} on a ${pos.stake:.2f} stake · {note}",
            )

    async def _resolve(self, pos: PaperPosition, now: int) -> tuple[int | None, str]:
        """Decide the outcome from the venue, falling back to our own series.

        The venue is authoritative, but UMA resolution can lag by hours. Our
        own copy of the reference price answers the same question immediately
        - and when the two disagree, that disagreement is the most important
        log line this pipeline can produce.
        """
        market = await self.client.get_market(pos.market_id)
        if market and market.closed and market.resolved_outcome:
            status = market.resolved_outcome.lower()
            if "resolved" in status:
                yes_price = market.yes.last_price if market.yes else 0.0
                venue_up = 1 if yes_price >= 0.5 else 0
                ours = 1 if venue_up else 0
                outcome = ours if pos.side == "yes" else 1 - ours
                return outcome, "venue resolution"

        spot, _sigma = self._spot()
        if spot and pos.strike:
            up = 1 if spot > pos.strike else 0
            outcome = up if pos.side == "yes" else 1 - up
            return outcome, f"our reference series ({spot:.2f} vs strike {pos.strike:.2f})"
        return None, ""

    # -- reporting ---------------------------------------------------------
    def state(self) -> dict:
        open_positions = [p for p in self.positions if not p.settled]
        settled = [p for p in self.positions if p.settled]
        forced_settled = [p for p in settled if p.forced]
        wins = [p for p in settled if p.pnl > 0]
        forced_wins = [p for p in forced_settled if p.pnl > 0]
        return {
            "enabled": True,
            "running": self.running,
            "last_poll": self.last_poll,
            "last_error": self.last_error or self.client.last_error,
            "equity": round(self.equity, 2),
            "start_equity": self.start_equity,
            "pnl": round(self.equity - self.start_equity, 2),
            "markets": [m.to_dict(self.last_poll) for m in self.markets[:8]],
            "assessments": [a.to_dict() for a in self.assessments],
            "positions": [p.to_dict() for p in open_positions],
            "settled": [p.to_dict() for p in settled[-20:]],
            # All settled trades, forced or not - matches `equity`/`pnl` above.
            "stats": {
                "open": len(open_positions),
                "settled": len(settled),
                "wins": len(wins),
                "win_rate": round(len(wins) / len(settled) * 100, 2) if settled else 0.0,
                "staked": round(sum(p.stake for p in settled), 2),
                "pnl": round(sum(p.pnl for p in settled), 2),
            },
            # The volume-mode subset, broken out so it is never mistaken for
            # organic edge performance. Excluded from `calibration` entirely.
            "forced_stats": {
                "enabled": self.cfg.force_min_trades,
                "settled": len(forced_settled),
                "wins": len(forced_wins),
                "win_rate": (
                    round(len(forced_wins) / len(forced_settled) * 100, 2)
                    if forced_settled else 0.0
                ),
                "staked": round(sum(p.stake for p in forced_settled), 2),
                "pnl": round(sum(p.pnl for p in forced_settled), 2),
            },
            "calibration": calibration_report(self.forecasts) if self.forecasts else None,
            "events": self.events[-40:],
            "config": self.cfg.model_dump(),
        }


# Polymarket's matcher is an off-chain service reached over HTTPS from
# wherever the bot runs. Half a second is a realistic round trip, and it is
# an eternity next to the quotes this pipeline is trying to lift.
ROUND_TRIP_MS = 500
