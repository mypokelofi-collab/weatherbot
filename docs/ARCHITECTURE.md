# flowbot — system analysis

A 15-minute BTC momentum bot that goes with the flow: it enters when momentum
is real and confirmed, manages the trade tick by tick, takes the money when
the move pays, and then goes back to waiting for the next signal.

Everything about the market is real — the trades, the order book, the depth
the orders eat, the fees, the latency. **Only the money is paper.** There is
no exchange key anywhere in this repository and no code path that can sign a
real order.

---

## 1. The shape of the system

```
          ┌────────────────────────── REAL ───────────────────────────┐
          │                                                           │
   Binance USDⓈ-M  ──ws──►  aggTrade  ─────────────┐                  │
   (or spot /              depth@100ms ──┐         │                  │
    Coinbase)                            │         │                  │
                                         ▼         ▼                  │
                                   ┌──────────┐ ┌──────────┐          │
                                   │ OrderBook│ │  Tape    │          │
                                   │ replica  │ │  window  │          │
                                   └────┬─────┘ └────┬─────┘          │
                                        │            │                │
                                        │       ┌────▼──────┐         │
                                        │       │  Candle   │ 15m     │
                                        │       │aggregator │ bars    │
                                        │       └────┬──────┘         │
          └─────────────────────────────┼────────────┼────────────────┘
                                        │            │
                                   ┌────▼────────────▼────┐
                                   │    SIGNAL SYSTEM     │  on bar close
                                   │  features → 8 scored │
                                   │  components → score  │
                                   │  + regime + gates    │
                                   └──────────┬───────────┘
                                              │ enter / hold / exit
                                   ┌──────────▼───────────┐
                                   │     TRADING BOT      │
                                   │  risk sizing, stops, │
                                   │  partials, trails,   │
                                   │  circuit breakers    │
                                   └──────────┬───────────┘
                                              │ intents
                                   ┌──────────▼───────────┐
                                   │   PAPER EXECUTION    │  ← real book
                                   │ queue, latency, fees │  ← real tape
                                   │ partial fills, impact│
                                   └──────────┬───────────┘
                                              │ fills
                    ┌─────────────────────────┼─────────────────────┐
                    ▼                         ▼                     ▼
              Portfolio /              SQLite ledger          Dashboard
              closed trades            (audit trail)          (:8033, ws)
```

Two systems, deliberately separate, exactly as asked:

* **The signal system** (`flowbot/signals/`) knows nothing about money. It
  answers one question per closed bar: is there momentum worth trading, in
  which direction, and how confident are we.
* **The trading bot system** (`flowbot/bot/`) knows nothing about
  indicators. It takes a signal and turns it into a sized, risk-managed,
  worked position, and it manages that position continuously until it is
  flat again.

The seam matters: the same signal engine drives the live bot, the backtester
and the Polymarket pricer, and the same bot code runs on live data, a replay,
or the offline simulator.

---

## 2. What "real" means here, line by line

| Thing | How it is real | Where |
|---|---|---|
| Prices | Every print from the venue's public trade tape, with the aggressor side | `data/binance.py` |
| Order book | Local L2 replica from a REST snapshot plus the diff stream, with sequence-gap detection and automatic resync | `data/book.py` |
| Bars | Built from the prints themselves, so the buy/sell split inside each bar is real | `data/candles.py` |
| Fills | Market orders walk the real ladder level by level and pay each level's price | `execution/simulator.py` |
| Partial fills | If the book cannot fill the size, it fills what is there and cancels the rest | `execution/simulator.py` |
| Queue position | A resting order joins behind the real resting size and only fills when the real tape trades through that queue | `execution/simulator.py` |
| Latency | An order exists at the venue only after `latency_ms`; it matches the book as it is *then* | `execution/simulator.py` |
| Impact | Liquidity our own order consumed stays consumed for ~1.5s | `execution/simulator.py` |
| Fees | Venue maker/taker schedule, charged per fill | `core/config.py` |
| Venue rules | Real tick size, lot size and minimum notional from `exchangeInfo` | `core/instrument.py` |
| Liquidity limits | Position size is capped by what the book can absorb inside the slippage budget | `bot/risk.py` |
| **Money** | **Not real.** Paper equity, paper PnL. | `bot/portfolio.py` |

Things that are *modelled*, and therefore approximations, are listed in §7.

---

## 3. The signal system

### 3.1 Inputs

Three sources, three different questions:

* **Bars** — where has price been going? (trend, breakout, volatility)
* **Tape** — who is being aggressive *right now*? (CVD, aggressor imbalance)
* **Book** — what is resting in front of us? (imbalance, spread, depth)

All of it lands in one `Features` snapshot per bar, so a signal, an order and
a dashboard row always reference the same market state.

### 3.2 The eight components

Each is scored into `[-1, +1]`, blended by weight into one composite score.
Anything with price units is divided by ATR first — a $400 EMA spread means
something very different at 0.2% ATR than at 1.5%.

| Component | Weight | What it measures |
|---|---|---|
| `trend` | 0.22 | EMA21 − EMA55, in ATRs |
| `breakout` | 0.15 | Close beyond the 20-bar Donchian channel, in ATRs |
| `flow` | 0.13 | Bar aggressor delta + CVD slope + live tape imbalance |
| `trend_slope` | 0.12 | Slope of EMA55 per bar, in ATRs |
| `macd` | 0.12 | MACD histogram in ATRs, plus whether it is expanding |
| `momentum_z` | 0.10 | 8-bar rate of change as a z-score |
| `rsi` | 0.08 | RSI as a momentum reading, not a fade signal |
| `book` | 0.08 | Resting book imbalance within 10bps + microprice tilt |

Scores saturate through `tanh` rather than clipping, so one violent input
cannot dominate the blend. Price-history components carry ~61% of the weight;
flow and book are confirmation, not the thesis — microstructure leads by
seconds and we hold for hours.

### 3.3 Regime and gates

A score is not a trade. Before an entry is allowed:

* **ADX ≥ 18** — otherwise the tape is chop and momentum is noise.
* **Trend alignment** — a long score with price below the slow EMA is
  refused. Counter-trend flow spikes are how momentum bots bleed.
* **Volatility band** — ATR between 0.06% and 3% of price. Below that a 15m
  move cannot pay the round-trip cost; above it, sizing collapses anyway.
* **Spread ≤ 4bps** and **≥ $150k resting within 10bps** on the thinner side.
* **Warmup** — 80 closed bars before the first trade.

Every gate that blocks is reported by name, to the dashboard and the log.
"Why is it not trading?" is always answerable.

### 3.4 Entry, exit, and waiting

* **Entry** when `|score| ≥ 0.35` with no gate blocking.
* **Hold** while the score stays aligned above the exit threshold.
* **Exit** when the score decays below `0.10` in the direction held — the
  move is over, take what the market gave.
* **Reverse** when the score flips past `−0.30` against the position. A flip
  skips the usual cooldown: the signal did not fade, it changed sides.

After any exit the bot waits — one bar normally, three after a loss — then
looks for the next setup. That is the requested behaviour: in with the flow,
out when it pays, wait, repeat.

---

## 4. The trading bot system

### 4.1 Sizing

Size comes from the **stop distance**, never from a fixed notional:

```
risk_dollars = equity × 0.5%
qty          = risk_dollars / (1.6 × ATR)
```

then clipped, in order, by: the notional cap (100% of equity), the leverage
cap (3×), and **what the real book can absorb inside the 12bps slippage
budget — taking at most half of it, because the exit needs liquidity too.**

A quiet market buys more coin than a volatile one for the same dollar risk,
which is the entire point.

### 4.2 The exit ladder

Checked on every print, not every bar — a stop that only checks at bar close
gets hit tens of basis points worse than it should:

1. **Hard stop** — entry ∓ 1.6 ATR. Never widened.
2. **Trailing stop** — chandelier from the extreme since entry, 2.2 ATR.
3. **Runner target** — a hard ceiling on greed at 4R.
4. **Partial** — half off at 1.8R, and the stop moves to breakeven at 1R, so
   a trade that has paid can no longer lose.
5. **Time stop** — 24 bars (6 hours). Momentum that has not worked is not
   momentum.

The level in force is always the *tighter* of stop and trail, so an early
trail can never loosen a stop that has already moved up.

### 4.3 Circuit breakers

Daily loss limit (3%), max trades per day (12), consecutive-loss kill switch
(4), cooldowns after exits, a manual kill switch on the dashboard, and an
automatic flatten if the market feed goes stale for 90 seconds — a position
you cannot see is a position you cannot manage.

Gates only ever block **entries**. Nothing is allowed to prevent the bot from
closing a position it already holds.

### 4.4 Order working

The broker expresses intent, not orders:

* **passive** (default for entries) — post inside the spread, wait, and only
  cross if the market does not come to us. Saves the taker fee.
* **normal** — post once briefly, then cross.
* **urgent** (all stops and flips) — cross immediately. Getting out beats
  getting a good price.

Escalation is what keeps the backtest honest: an unfilled passive order
becomes a market order that pays the spread, exactly as it would live.

---

## 5. Data, replay and the offline simulator

* `flowbot record` captures the live feed (prints + throttled 25-level book
  snapshots) to JSONL.
* `flowbot backtest FILE` replays it through **the same** trader, signal
  engine and fill simulator. There is no second engine to disagree with the
  first.
* `--sim` runs a regime-switching microstructure simulator for development
  where there is no connectivity. It reports `real_data: false`, and the
  dashboard renders a loud banner. Nothing it produces means anything about
  live profitability, and the code says so wherever it appears.

---

## 6. Dashboard (`:8033`)

One page, dark by default, no CDN dependencies (it has to work on a firewalled
VPS). Websocket pushes a full snapshot on every bar close, fill and event, and
a small tick four times a second.

It shows equity and drawdown, the open position with the stop actually in
force and what exiting right now would cost, the signal score with every
component's contribution and a plain-English reason list, the live depth
ladder with imbalance and liquidity score, the tape with cumulative delta,
every order and fill including the cancelled ones, closed trades with R
multiples and MFE, and the raw feature vector. Controls: pause entries,
flatten, kill switch, and live parameter edits.

Colour note: up/down uses an aqua/red polarity pair that sits in the
colour-vision-deficiency warning band, so **no up/down mark relies on colour
alone** — every one carries a sign, an arrow, a letter or a side label — and a
"CB" toggle swaps to a blue/red pair that clears every contrast gate.

---

## 7. What is modelled, not measured — read this before trusting a number

1. **Our orders do not affect the market.** We consume our own liquidity for
   1.5s, but nobody reacts to us. True for our size on BTC, false in general.
2. **Queue position is FIFO and pessimistic.** Cancels ahead of us are not
   credited (unless `queue_model: optimistic`). Real fills are usually a
   little better.
3. **Latency is a constant.** Real latency has a tail, and the tail arrives
   exactly when the market is moving.
4. **Recorded books are throttled.** A backtest fills against depth up to the
   capture interval old; the runner says so in its report.
5. **No funding, no borrow, no liquidation.** A perp position accrues funding
   every 8 hours; this is not modelled and will matter for long holds.
6. **No outage modelling.** Reconnects are handled; an exchange halt with an
   open position is not simulated.
7. **The simulator is not a market.** It is a plumbing test.

---

## 8. Layout

```
flowbot/
  core/         types, config, instrument rules, bar clock, event bus
  data/         venue feeds, book replica, bar aggregation, record/replay, simulator
  signals/      indicators, features, momentum model, signal engine
  execution/    matching engine (paper), broker with order working, microstructure
  bot/          risk, position ladder, portfolio, stats, SQLite ledger, trader loop
  backtest/     replay runner and terminal report
  polymarket/   prediction-market pipeline (see POLYMARKET_PIPELINE.md)
  server/       FastAPI + dashboard (HTML/CSS/JS, no build step)
config/         flowbot.yml (live paper), sim.yml (offline)
tests/          120 tests, including two end-to-end runs
```
