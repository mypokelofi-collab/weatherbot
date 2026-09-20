"""Prediction-market pipeline: parsing, pricing, gating, fills and settlement."""

from __future__ import annotations

import math

import pytest

from flowbot.core.config import PolymarketConfig
from flowbot.core.types import BookLevel, BookSnapshot
from flowbot.polymarket.client import infer_resolution, parse_market
from flowbot.polymarket.engine import PolymarketPipeline
from flowbot.polymarket.market import Outcome, PredictionMarket, ResolutionSpec, parse_iso
from flowbot.polymarket.pricing import (
    assess, brier_score, calibration_report, gbm_probability, kelly_fraction,
    model_probability, norm_cdf, reliability_table,
)

RAW = {
    "id": "512",
    "slug": "bitcoin-up-or-down-july-4",
    "question": "Bitcoin Up or Down on July 4?",
    "conditionId": "0xabc",
    "clobTokenIds": '["111","222"]',
    "outcomes": '["Up","Down"]',
    "outcomePrices": '["0.53","0.47"]',
    "endDate": "2026-07-05T16:00:00Z",
    "startDate": "2026-07-04T16:00:00Z",
    "volumeNum": 128000,
    "liquidityNum": 42000,
    "orderPriceMinTickSize": 0.01,
    "description": ("This market resolves Up if the Binance 1 minute candle close for "
                    "BTCUSDT at 12:00 PM ET on July 5 is above $62,500."),
}


def market_fixture(**kw) -> PredictionMarket:
    m = PredictionMarket(
        id="9", slug="bitcoin-up-or-down-today", question="q", condition_id="c",
        outcomes=[Outcome("Up", "t1"), Outcome("Down", "t2")],
        end_ts=6 * 3_600_000, tick_size=0.01, min_order_size=5,
    )
    m.resolution = ResolutionSpec(
        reference="binance:BTCUSDT:1m-close", strike=64_000, strike_known=True
    )
    for k, v in kw.items():
        setattr(m, k, v)
    return m


def books(yes_ask=0.51, no_ask=0.50, size=600):
    yes = BookSnapshot(0, [BookLevel(yes_ask - 0.02, size)],
                       [BookLevel(yes_ask, size), BookLevel(yes_ask + 0.02, size * 2)])
    no = BookSnapshot(0, [BookLevel(no_ask - 0.03, size)], [BookLevel(no_ask, size)])
    return yes, no


# ------------------------------------------------------------------ parsing

def test_market_parsing_pulls_tokens_and_dates():
    m = parse_market(RAW)
    assert m.slug == "bitcoin-up-or-down-july-4"
    assert m.yes.name == "Up" and m.yes.token_id == "111"
    assert m.no.token_id == "222"
    assert m.end_ts == parse_iso("2026-07-05T16:00:00Z")
    assert m.volume == 128000


def test_resolution_inference_finds_source_strike_and_timezone():
    spec = infer_resolution(RAW)
    assert spec.reference == "binance:BTCUSDT:1m-close"
    assert spec.strike == 62_500 and spec.strike_known
    assert "Eastern" in spec.timezone_note
    assert spec.verified


def test_unparseable_resolution_is_not_verified():
    spec = infer_resolution({"description": "Resolves according to the vibes."})
    assert spec.reference == "unknown"
    assert not spec.verified


# ------------------------------------------------------------------ pricing

def test_norm_cdf_and_gbm_sanity():
    assert norm_cdf(0) == pytest.approx(0.5)
    assert gbm_probability(100, 100, 0.5, 0) == 0.0            # expired, not above
    assert gbm_probability(101, 100, 0.5, 0) == 1.0
    # At the money with a long horizon the drag pulls it just under a half.
    p = gbm_probability(100, 100, 0.5, 86400 * 30)
    assert 0.4 < p < 0.5


def test_in_the_money_probability_rises_as_time_runs_out():
    far = gbm_probability(64_800, 64_000, 0.55, 86_400)
    near = gbm_probability(64_800, 64_000, 0.55, 600)
    assert near > far > 0.5


def test_momentum_tilt_moves_the_probability_the_right_way_but_only_a_little():
    flat, _ = model_probability(64_000, 64_000, 0.55, 3_600, score=0.0)
    bull, info = model_probability(64_000, 64_000, 0.55, 3_600, score=0.9)
    bear, _ = model_probability(64_000, 64_000, 0.55, 3_600, score=-0.9)
    assert bull > flat > bear
    # The honest result: over an hour the tilt is worth a fraction of a point,
    # which is the whole reason the pipeline hunts stale quotes instead.
    assert abs(info["tilt_contribution"]) < 0.01


def test_probability_never_claims_certainty():
    p, _ = model_probability(90_000, 64_000, 0.55, 60, score=1.0)
    assert p <= 0.98


def test_vol_haircut_pulls_probability_toward_a_coin_flip():
    sharp, _ = model_probability(64_800, 64_000, 0.55, 86_400, vol_haircut=1.0)
    hedged, _ = model_probability(64_800, 64_000, 0.55, 86_400, vol_haircut=1.5)
    assert 0.5 < hedged < sharp


def test_kelly_fraction_math():
    assert kelly_fraction(0.6, 0.5) == pytest.approx(0.2)
    assert kelly_fraction(0.5, 0.5) == 0.0
    assert kelly_fraction(0.4, 0.5) == 0.0                     # never bet a negative edge
    assert kelly_fraction(0.9, 0.1) == pytest.approx(0.888, abs=0.001)


# -------------------------------------------------------------------- edges

def test_stale_quote_against_a_moved_spot_is_the_edge():
    m = market_fixture()
    yes, no = books(yes_ask=0.51)
    p, _ = model_probability(64_800, 64_000, 0.55, 6 * 3600, score=0.6)
    a = assess(m, yes, no, p, equity=10_000, now_ms=0)
    assert a.side == "yes"
    assert a.edge > 0.15
    assert a.tradable
    assert a.shares >= m.min_order_size
    assert a.stake <= 10_000 * 0.02 + 1e-6                     # stake cap honoured


def test_fair_quote_produces_no_trade():
    m = market_fixture()
    p, _ = model_probability(64_000, 64_000, 0.55, 6 * 3600, score=0.1)
    yes, no = books(yes_ask=round(p, 2), no_ask=round(1 - p, 2))
    a = assess(m, yes, no, p, equity=10_000, now_ms=0)
    assert not a.tradable
    assert any("edge" in b for b in a.blockers)


def test_unverified_resolution_blocks_the_trade():
    m = market_fixture()
    m.resolution = ResolutionSpec()                            # unknown rule
    yes, no = books(yes_ask=0.30)
    a = assess(m, yes, no, 0.80, equity=10_000, now_ms=0)
    assert not a.tradable
    assert any("resolution rule" in b for b in a.blockers)


def test_imminent_resolution_blocks_the_trade():
    m = market_fixture(end_ts=60_000)
    yes, no = books(yes_ask=0.30)
    a = assess(m, yes, no, 0.80, equity=10_000, now_ms=0, min_seconds=300)
    assert any("resolution" in b for b in a.blockers)


def test_wide_spread_blocks_the_trade():
    m = market_fixture()
    yes = BookSnapshot(0, [BookLevel(0.30, 500)], [BookLevel(0.45, 500)])
    no = BookSnapshot(0, [BookLevel(0.40, 500)], [BookLevel(0.60, 500)])
    a = assess(m, yes, no, 0.80, equity=10_000, now_ms=0, max_spread=0.04)
    assert any("spread" in b for b in a.blockers)


def test_edge_is_measured_against_the_ask_not_the_mid():
    m = market_fixture()
    yes, no = books(yes_ask=0.60)
    a = assess(m, yes, no, 0.63, equity=10_000, now_ms=0, min_edge=0.01)
    # mid is 0.59, ask is 0.60, and we add a tick of slippage: cost 0.61.
    assert a.cost == pytest.approx(0.61)
    assert a.edge == pytest.approx(0.02)


def test_size_is_capped_by_the_book():
    m = market_fixture()
    yes, no = books(yes_ask=0.30, size=20)
    a = assess(m, yes, no, 0.80, equity=1_000_000, now_ms=0)
    assert a.shares <= 10                                       # half of 20 resting


# ------------------------------------------------------------------ pipeline

async def test_pipeline_fills_when_the_quote_survives_the_round_trip():
    pipe = PolymarketPipeline(PolymarketConfig(), equity=10_000)
    pipe.latency_ms = 1
    pipe.bind(lambda: (64_800.0, 0.55), lambda: 0.6)
    m = market_fixture()
    yes, no = books(yes_ask=0.51)
    p, _ = model_probability(64_800, 64_000, 0.55, 6 * 3600, score=0.6)
    a = assess(m, yes, no, p, 10_000, 0)

    async def still_there(_token):
        return yes

    pipe.client.get_book = still_there
    pos = await pipe._paper_buy(m, a, "t1", 0)
    assert pos is not None
    assert pos.shares > 0
    assert pos.cost == pytest.approx(0.51)
    assert pipe.equity == pytest.approx(10_000 - pos.stake)


async def test_pipeline_misses_when_the_quote_is_pulled():
    pipe = PolymarketPipeline(PolymarketConfig(), equity=10_000)
    pipe.latency_ms = 1
    m = market_fixture()
    yes, no = books(yes_ask=0.51)
    a = assess(m, yes, no, 0.77, 10_000, 0)

    async def repriced(_token):
        return BookSnapshot(0, [BookLevel(0.70, 500)], [BookLevel(0.80, 500)])

    pipe.client.get_book = repriced
    assert await pipe._paper_buy(m, a, "t1", 0) is None
    assert pipe.equity == 10_000
    assert "no fill" in pipe.events[-1]["message"]


async def test_settlement_pays_one_dollar_a_share_and_records_calibration():
    pipe = PolymarketPipeline(PolymarketConfig(), equity=10_000)
    pipe.latency_ms = 1
    pipe.bind(lambda: (64_800.0, 0.55), lambda: 0.6)
    m = market_fixture(end_ts=0)
    yes, _no = books(yes_ask=0.51)
    a = assess(market_fixture(), yes, _no, 0.77, 10_000, 0)

    async def still_there(_token):
        return yes

    async def no_market(_id):
        return None

    pipe.client.get_book = still_there
    pipe.client.get_market = no_market
    pos = await pipe._paper_buy(m, a, "t1", 0)
    await pipe._settle_due(1)

    assert pos.settled and pos.outcome == 1
    assert pos.pnl == pytest.approx(pos.shares * (1 - pos.cost))
    assert pipe.equity == pytest.approx(10_000 + pos.pnl)
    assert pipe.forecasts and pipe.forecasts[0][1] == 1
    state = pipe.state()
    assert state["stats"]["wins"] == 1
    assert state["calibration"]["n"] == 1


async def test_losing_settlement_costs_the_stake():
    pipe = PolymarketPipeline(PolymarketConfig(), equity=10_000)
    pipe.latency_ms = 1
    pipe.bind(lambda: (63_000.0, 0.55), lambda: -0.6)      # BTC below the strike
    m = market_fixture(end_ts=0)
    yes, no = books(yes_ask=0.51)
    a = assess(market_fixture(), yes, no, 0.77, 10_000, 0)

    async def still_there(_token):
        return yes

    async def no_market(_id):
        return None

    pipe.client.get_book = still_there
    pipe.client.get_market = no_market
    pos = await pipe._paper_buy(m, a, "t1", 0)
    await pipe._settle_due(1)
    assert pos.outcome == 0
    assert pos.pnl == pytest.approx(-pos.stake)
    assert pipe.equity == pytest.approx(10_000 - pos.stake)


# --------------------------------------------------------------- calibration

def test_brier_and_skill():
    perfect = [(1.0, 1), (0.0, 0)]
    assert brier_score(perfect) == 0.0
    coin = [(0.5, 1), (0.5, 0)]
    assert brier_score(coin) == pytest.approx(0.25)
    report = calibration_report([(0.6, 1)] * 60 + [(0.6, 0)] * 40)
    assert report["n"] == 100
    assert report["base_rate"] == pytest.approx(0.6)


def test_reliability_table_buckets_forecasts():
    table = reliability_table([(0.65, 1)] * 70 + [(0.65, 0)] * 30, bins=10)
    row = [r for r in table if r["n"]][0]
    assert row["forecast"] == pytest.approx(0.65)
    assert row["observed"] == pytest.approx(0.70)
    assert row["gap"] == pytest.approx(0.05)


def test_overconfident_model_shows_a_negative_gap():
    table = reliability_table([(0.9, 1)] * 50 + [(0.9, 0)] * 50, bins=10)
    row = [r for r in table if r["n"]][0]
    assert row["gap"] < -0.3          # says 90%, happens 50% of the time
