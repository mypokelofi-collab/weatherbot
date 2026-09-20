# The Polymarket pipeline — analysis and plan

**Question:** can the 15-minute BTC momentum signal be carried into
Polymarket's BTC up/down markets, and if so, how?

**Answer, in one line:** yes, but not the way it looks. The momentum tilt is
worth a fraction of a probability point on short-dated markets — far less than
the spread. The thing worth trading is **repricing lag**: quotes that have not
caught up with a BTC move. The momentum signal's real job is to decide whether
that move is going to stick, and to veto the ones that will not.

This document is the analysis behind that conclusion, the pipeline design, the
phasing, and the reasons the code ships read-only.

---

## 1. What is actually being traded

A Polymarket binary market is not a symbol. It is a **question with a deadline
and a settlement rule**:

> "Bitcoin Up or Down on July 4?" — resolves *Up* if the Binance 1-minute
> candle close for BTCUSDT at 12:00 PM ET is above $62,500.

Three things follow, and all three are load-bearing:

1. **The strike is fixed.** Once the window opens, the comparison level stops
   moving. Everything after that is a race between spot and the clock.
2. **The payout is binary.** A share costs between $0.00 and $1.00 and pays
   exactly $1 or $0. Price *is* probability, so the whole trade reduces to:
   is this probability wrong, and by more than it costs to correct it.
3. **The settlement source is specific.** "BTC went up" measured on Binance
   1m closes at 12:00 ET is a *different question* from the same phrase on a
   Coinbase index at 00:00 UTC. Getting this wrong is not noise — it is a
   systematically wrong bet, and it is the single largest risk in the
   pipeline, larger than any modelling error.

`flowbot/polymarket/market.py` therefore makes `ResolutionSpec` a first-class
object, and `EdgeAssessment` refuses to trade any market whose rule the code
could not parse and match to a price series we actually receive.

---

## 2. Pricing: from momentum score to probability

Under lognormal dynamics, the chance of finishing above a fixed strike is

```
P(up) = Φ( [ ln(S/K) + (μ − σ²/2)·τ ] / (σ·√τ) )
```

with `S` = spot now, `K` = the strike, `τ` = time to resolution in years,
`σ` = annualised volatility (from the bot's own realised-vol feature), and
`μ` = the drift our momentum signal claims.

The momentum score enters as drift, expressed in units of σ:

```
μ = shrink × tilt × score × σ
```

Writing it this way makes the parameter interpretable: `tilt = 1.0` claims the
signal is worth one unit of Sharpe at full strength — already an aggressive
claim — and `shrink` (default 0.35) is the honesty discount that stays in
place until out-of-sample calibration earns its removal.

σ is deliberately **inflated** by 15% (`vol_haircut`). Over-estimating
volatility pulls every probability toward 0.5, which shrinks our edge. That is
the direction we want to be wrong in.

### 2.1 The measurement that changes the strategy

How much is each input actually worth, in probability points, on an
at-the-money BTC market at 55% annualised vol?

| Horizon | Momentum tilt at score = 1.0 | Spot +0.25% | Spot +0.5% | Spot +1% |
|---|---|---|---|---|
| 1 hour | **0.12 pt** | 14.4 pt | 27.0 pt | 43.0 pt |
| 6 hours | **0.29 pt** | 6.0 pt | 11.9 pt | 22.7 pt |
| 24 hours | **0.58 pt** | 3.0 pt | 6.0 pt | 11.8 pt |
| 7 days | **1.55 pt** | 1.1 pt | 2.3 pt | 4.5 pt |
| 30 days | **3.20 pt** | 0.5 pt | 1.1 pt | 2.2 pt |

(Reproduce with `model_probability()` in `flowbot/polymarket/pricing.py`.)

Read the first column against the others. On a six-hour market, a full-strength
momentum signal moves fair value by **0.29 points** while a half-percent move
in BTC moves it by **11.9 points** — forty times more. Polymarket spreads on
these markets are routinely 2–4 points. So:

* **A pure momentum-tilt strategy on short-dated markets is dead on arrival.**
  The edge is an order of magnitude below the spread.
* **A repricing strategy is arithmetically live.** BTC moves 0.5% in minutes
  several times a day. If a resting quote has not moved with it, fair value
  and the quote can be 10+ points apart.
* **The tilt only becomes material at multi-week horizons** — which is also
  where a 15-minute signal has the least claim to predictive power. That
  combination is a good reason not to chase the long end.

This is why the pipeline is built as a **stale-quote hunter with a momentum
filter**, not as a momentum bet.

### 2.2 Where the momentum signal still earns its place

1. **Direction filter.** Lift a stale *up* quote only when flow and trend
   agree the move is real. A 0.5% spike that CVD does not support is the kind
   that retraces before resolution.
2. **Vol regime input.** σ comes from the bot's realised-vol feature, not a
   constant. Getting σ wrong moves fair value far more than the tilt does.
3. **Exit thinking.** Positions can be sold back into the book before
   resolution; the momentum score is the input to that decision.

---

## 3. Execution: the part that decides whether the edge survives

The edge above is an *observation*. Capturing it requires beating everyone
else to a quote that is about to be pulled. Four design choices follow.

**Edge is measured against the ask we would lift, not the mid.** Plus one tick
of slippage. On a market with a 3-point spread, an edge measured against the
mid is an edge that does not exist. (`pricing.assess`)

**Latency is modelled by actually waiting.** The pipeline sends the order,
sleeps the round trip (500ms — Polymarket's matcher is an HTTPS service, not a
colocated socket), then **re-reads the real book** and fills against what is
there on arrival. A quote that was pulled produces no fill, which is the
correct and frequent outcome. This is the most faithful thing possible on a
venue we can read but not trade. (`engine._paper_buy`)

**Size is capped by the book.** At most half the shares resting inside our
price limit — the other half is the exit.

**Fills walk the ladder.** The same matching engine that walks a Binance book
walks a Polymarket book unchanged: prices are probabilities, sizes are shares,
everything else is identical.

### 3.1 Sizing

For a $1 binary bought at cost `c` with true probability `p`, edge per share
is `p − c` and the Kelly fraction is `(p − c)/(1 − c)`. We take 25% of Kelly,
capped at 2% of bankroll per market, because Kelly assumes you know `p` and we
do not.

### 3.2 Gates before any paper trade

* resolution rule parsed **and** matched to a price series we receive;
* ≥ 5 minutes to resolution (the last minutes are a latency race we lose);
* spread ≤ 4 points;
* edge ≥ 4 points after slippage;
* size ≥ the venue minimum after rounding;
* market open and active.

---

## 4. Settlement

Positions settle at $1 or $0. Two sources, and the disagreement between them
is the most valuable log line this pipeline produces:

1. **The venue** is authoritative — but UMA resolution can lag by hours.
2. **Our own copy of the reference series** answers immediately, because the
   bot already consumes the Binance feed these markets settle against.

If our reference says *Up* and the venue resolves *Down*, one of two things is
true: we parsed the rule wrong, or the market resolved on a source we do not
model. Both are pipeline-stopping bugs, and both are invisible without the
cross-check.

---

## 5. Calibration — the gate before any real capital

The pipeline logs `(forecast probability, realised outcome)` for every settled
position and reports:

* **Brier score** and **Brier skill score** versus always predicting the base
  rate — a skill score at or below zero means the model adds nothing;
* **log loss**;
* a **reliability table**: does the 70% bucket actually resolve *up* 70% of
  the time?

If the 70% bucket resolves 50% of the time, the model is not a probability,
it is an opinion, and no amount of Kelly maths makes an opinion tradable.

**The rule: no phase advance without a positive Brier skill score and a
reliability table whose gaps are within sampling error, over at least 100
resolved markets.**

---

## 6. Phasing

| Phase | What runs | Exit criteria |
|---|---|---|
| **0 — done** | The perp bot: real data, real book, paper money, full audit trail. | Stable on a VPS, trades reproducible from the ledger. |
| **1 — read-only** | Market discovery, real CLOB books, fair value vs quote logged every 30s. No orders. Record everything alongside the BTC feed. | ≥ 2 weeks of paired data; resolution rules verified against our own series on every market traded. |
| **2 — paper** *(shipped, off by default)* | Everything in phase 1 plus paper positions filled against the real book after a real round trip, settled and scored. | ≥ 100 settled markets, positive Brier skill, reliability gaps within sampling error, positive PnL *after* modelled slippage. |
| **3 — gated** | Live execution. Out of scope for this repository. | Requires a funded wallet, EIP-712 order signing, key management, position limits across venues, and a legal review of who may trade there. Not a code change — a different project. |

Phase 2 is enabled with `polymarket.enabled: true`. It cannot place a real
order: there is no wallet, no key and no signing code in the package.

---

## 7. Risks, ranked by how much they will cost you

1. **Resolution mismatch.** Wrong source, wrong timestamp, wrong timezone
   (these markets quote ET; the bot thinks in UTC). Mitigation: parse and
   verify, refuse the unverified, cross-check every settlement.
2. **The market already knows.** BTC prediction markets are priced by people
   watching the same chart. Assume the obvious edge is arbitraged and
   measure, do not assume.
3. **Adverse selection on the fill.** The quotes that are stale enough to be
   worth lifting are the ones most likely to vanish first — and the ones that
   *do* fill may fill because someone knows something. The re-read-after-
   latency model captures the first half of this; the second half shows up as
   a calibration gap.
4. **Thin books.** Quoted depth is not fillable depth. Half-the-book sizing
   plus per-market caps.
5. **Double-counting BTC risk.** A long perp and a *Yes* on "BTC up today"
   are the same bet twice. Before phase 3, positions must be converted to a
   common BTC delta and capped at the portfolio level.
6. **Settlement and platform friction.** USDC on Polygon, withdrawal delays,
   UMA disputes, and jurisdictional restrictions on who may trade at all.
   These do not affect a paper run and dominate a real one.
7. **Model risk in σ.** Fair value is far more sensitive to the volatility
   input than to the momentum tilt. The 15% haircut is a floor, not a fix;
   phase 1 data should be used to fit a term structure.

---

## 8. Code map

```
flowbot/polymarket/
  market.py    PredictionMarket, Outcome, ResolutionSpec, EdgeAssessment
  client.py    Gamma (questions) + CLOB (books). Read-only; nothing can spend.
  pricing.py   lognormal fair value, momentum tilt, Kelly, edge assessment,
               Brier / log-loss / reliability calibration
  engine.py    poll → price → assess → paper-fill-after-latency → settle
```

Wired into the bot through `Trader.attach_polymarket()`, which gives the
pipeline read access to spot, volatility and the current momentum score, and
no access at all to the perp position or its equity. Surfaced at
`GET /api/polymarket` and as a dashboard card when enabled.

---

## 9. Honest summary

The pipeline is a well-instrumented way to find out whether an edge exists.
The measurement in §2.1 already says the obvious version of the idea — "bet
the momentum signal on BTC up/down markets" — does not clear costs on short
horizons. The version that might work is narrow, latency-sensitive, and
competes with people who do this for a living.

That is worth testing with paper money and a calibration scorecard. It is not
worth funding a wallet for until the scorecard says so.

---

## 10. What shipping this against the live API actually found

Phase 2 sat behind `enabled: false` untested. Turning it on against the real
Gamma/CLOB endpoints surfaced three bugs that would have silently zeroed
this pipeline forever, plus one deliberate policy decision:

1. **`market_slug_contains` pointed at the wrong family, and the discovery
   call itself was broken regardless.** `"bitcoin-up-or-down"` matches the
   hourly/daily fixed-strike markets, not the recurring 15-minute one this
   is meant to trade. Worse, Gamma's `slug` query parameter is an *exact*
   match, not a substring - passing any family prefix there returned `[]`
   from the server every time, before the (correct) client-side filtering
   ever ran. Both are fixed: `client.search_markets` no longer sends `slug`
   as a filter, and the 15m family is found deterministically instead of
   searched for - `btc-updown-15m-<epoch>` encodes its own window-open time,
   aligned to `window_seconds`, so `PolymarketPipeline._discover_windows`
   computes the slug for the current and next window and fetches each by
   exact match (`client.get_market_by_slug`).
2. **The recurring family has no fixed strike.** It compares a Chainlink
   60s-TWAP close to *the price when its own window opened* - there is no
   "$X" in the text for `infer_resolution`'s regex to find, and there
   shouldn't be one. `ResolutionSpec.strike_mode = "window_open"` marks this,
   and `PolymarketPipeline._resolve_strike` fills in the real number once the
   window has actually started, from flowbot's own bar history
   (`CandleAggregator.open_at`) - the two grids are UTC-aligned to the same
   boundaries, so no second price feed is needed. Before the window opens,
   `strike_known` stays `False` and the market correctly refuses to trade,
   exactly per its existing contract.
3. **A lot-size minimum is not a notional minimum.** `assess()` checked
   `shares >= market.min_order_size` but not `shares * cost >= min_notional`;
   on a low-priced outcome (a 0.12 ask, say) the venue's own share-count
   minimum can still be worth under Polymarket's $1 minimum order value, and
   the order is fine on our side and rejected on theirs. Sizing now rounds up
   to the smallest lot multiple that clears both.
4. **`force_min_trades` (policy, not a bug fix).** Per §2.1, genuine edge on
   a 15-minute window does not show up every window - most windows should
   trade zero times, by design. Guaranteeing activity for a bankroll that
   wants to see the pipeline actually run therefore means overriding the
   edge/spread/size/stake gates once a window is close enough to expiry that
   an organic signal was never coming (`force_trade_before_close_s`, default
   120s before close). A forced trade is sized to the smallest fillable lot
   (never Kelly-sized - Kelly on ~zero edge sizes to zero, which is the
   whole problem), tagged `forced` on the position, and excluded from
   `state()["calibration"]` entirely - the phase-3 gate in §5 is measuring
   whether the *model* is right, and a trade explicitly told to ignore what
   the model said is not a data point about that.
