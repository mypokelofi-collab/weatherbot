# pocketbot: research and design notes

`pocketbot/` is a fixed-time ("binary options") trading bot for Pocket Option,
which also runs under the **Pocket Broker** name (`m.pocket-broker.com`, and the
"Pocket Broker" Android app). This page covers what the research found, why
the bot is built the way it is, and how to run it without losing money you
didn't mean to risk.

---

## 1. What the platform is

| | |
|---|---|
| Operator | Gembell Limited, Marshall Islands |
| Regulation | None from a major regulator (not FCA, CySEC, ASIC or CFTC). Retail binary options are banned in the EU/EEA (ESMA, 2018), the UK (FCA, 2019) and Australia (ASIC, 2021). Check what your own country allows. |
| Product | Fixed-time trades: pick CALL (up) or PUT (down) and an expiry from 5 s to hours. A win pays the displayed payout (typically 70–92%). A loss costs the whole stake. |
| Counterparty | The broker itself. Your losses are its revenue. |
| OTC assets (`*_otc`) | Quoted by the broker, not by an interbank market, and available on weekends. Most bots trade these because they are always open and pay the highest payouts. Reddit and review threads say repeatedly that nobody can independently check these prices. |
| Official API | **None.** Pocket Option has no public trading API and no API keys. |

`m.pocket-broker.com` was blocked by this environment's network proxy, so I
couldn't inspect it directly. It is listed and used as a Pocket Option
mirror/brand domain, and the web terminal talks to the same socket.io backend
(`api-*.po.market`). If your account's websocket host is different, set `ws_url`
in the config.

## 2. Ways to automate it

| Approach | How | Verdict |
|---|---|---|
| **Unofficial websocket client** | Log in with a browser, copy the session string (SSID) and drive the same socket.io protocol the web terminal uses. [BinaryOptionsToolsV2](https://github.com/ChipaDevTeam/BinaryOptionsTools-v2) (Rust core, Python bindings, on PyPI, maintained in 2026) handles auth, reconnects, candles, orders and results. | **Chosen.** Fast, gives trade IDs and results, and doesn't break on UI redesigns. |
| Older Python clients | `PocketOptionAPI` (ChipaDevTeam) is archived and points to BinaryOptionsToolsV2. `A11ksa/API-Pocket-Option` and other forks do the same job with less upkeep. | Superseded. |
| Browser automation | Selenium/Playwright clicks the web UI (e.g. `VitalySvyatyuk/pocket_option_trading_bot`). | Fragile and slow, and it breaks whenever the UI changes. Only worth it if the websocket route stops working. |
| MetaTrader 5 | Pocket Option offers MT5 for forex/CFDs. | Not fixed-time trades, so it's a different product. The official `MetaTrader5` Python package works there if you ever want real CFD execution. |
| Signal bots | Telegram bots that post signals for a human to click. | Most of what Reddit and Gumroad sell. Claims of 90%+ win rates, usually with martingale. Treat them as marketing. |

**Terms of service.** Automated trading through an unofficial API is not
sanctioned by the broker. Users report blocked accounts and withheld
withdrawals, though bot use may not be the cause in every case. That is a real
risk to your balance separate from market risk, which is why the bot defaults
to paper and then demo.

## 3. The maths that decides everything

Stake 1. A win returns `+p` (the payout) and a loss returns `−1`. Expected value
per trade at win rate `w` is `w·p − (1 − w)`, so **the breakeven win rate is
`1 / (1 + p)`**:

| Payout | Breakeven win rate |
|---|---|
| 70% | 58.8% |
| 80% | 55.6% |
| 85% | 54.1% |
| 88% | 53.2% |
| 92% | 52.1% |

A strategy with no edge wins about 50%, which means a steady loss of 4–9% of
turnover. Put another way, the house edge is paid on every trade. Consequences:

- **Payout is a first-class input.** The bot refuses to trade below
  `min_payout` (default 85%) and logs the breakeven rate on every order.
- **Martingale is refused.** Doubling after a loss is in nearly every bot for
  sale. It doesn't change the expected value of any single trade; it turns
  many small wins into a rare total wipe-out. Four straight losses at 50/50
  happen about once every 16 attempts. Putting `martingale` in the config is
  an error.
- **Small samples lie.** 12 wins from 20 trades (60%) has a 95% interval of
  roughly 39–78%. The scorecard reports the Wilson lower bound and only says
  "edge evidenced" when that bound clears breakeven.

## 4. How the bot is built

```
             ┌────────────── Feed ──────────────┐
             │ SyntheticFeed   (offline, no edge)│
             │ ListFeed        (CSV backtest)    │
             │ PocketOptionFeed(live websocket)  │
             └───────────────┬───────────────────┘
                             │ closed candles only
                             ▼
  settle expired trades ◀─ Engine.run ─▶ strategy.evaluate ─▶ RiskManager.check ─▶ Broker.place
                             │                                                     │
                             ▼                                        PaperBroker (simulated)
                    Scorecard + Ledger (JSONL)                        PocketOptionBroker (demo/real)
```

Backtest, synthetic demo, paper on live data and real orders all run through
the same `Engine.run`. Only the feed and broker change, so a backtest number
says something about the live bot.

| File | What it does |
|---|---|
| `pocketbot/strategy.py` | `reversion` fades closes outside a 2.2σ Bollinger band when RSI(7) agrees and the slow EMA is flat. `momentum` trades continuation when the fast EMA is above the slow EMA, the slope agrees, and RSI is on the trend side of 50 but not exhausted. Reuses `flowbot`'s indicator code. |
| `pocketbot/risk.py` | Fixed-fraction stake (1% of balance), payout gate, 5% daily loss limit, daily trade cap, pause after 4 straight losses, one open trade at a time. |
| `pocketbot/broker.py` | Feeds and brokers. Settles at the close of the expiry candle; a draw refunds the stake, as on the platform. |
| `pocketbot/stats.py` | Trade ledger and the Wilson-interval scorecard. |
| `pocketbot/__main__.py` | CLI plus account guards: demo mode refuses a real-account SSID, and live mode needs a second explicit opt-in. |

## 5. Running it

```bash
make venv
.venv/bin/pip install -r requirements-pocket.txt # only needed for live data / orders

make pocket-sim                                  # offline, synthetic market, no SSID
```

### Getting your SSID

1. Log in to the web terminal in a desktop browser and switch to the **demo** account.
2. Open DevTools (F12), go to **Network**, filter **WS**, then reload.
3. Click the websocket connection, open **Messages**, and find the frame that starts `42["auth",`.
4. Copy the whole frame, e.g. `42["auth",{"session":"…","isDemo":1,"uid":12345,"platform":2}]`.

```bash
export POCKETBOT_SSID='42["auth",{"session":"…","isDemo":1,"uid":12345,"platform":2}]'
```

The SSID is a login credential. pocketbot only reads it from the environment,
never from the config file, so it can't end up in git. Anyone who has it can
trade on your account; log out of the browser session to invalidate it.

### The path from zero to real money

1. **`make pocket-sim`**: check it runs. The synthetic market has no edge, so
   this should lose at about the rate the payout predicts. If it "wins", that's
   luck or a bug.
2. **`python -m pocketbot assets`**: see which assets are open and what they pay.
3. **`python -m pocketbot fetch --hours 48`**, then
   **`python -m pocketbot backtest data/pocketbot/EURUSD_otc_60s.csv`**. Try both
   strategies and a few expiries. Assume anything you tune on one file is
   overfit until another file agrees.
4. **`mode: paper` with the SSID set**: live candles and live payouts, no orders.
5. **`mode: demo`**: real orders on the demo account. Run it for **several hundred
   trades** and read `python -m pocketbot stats`.
6. **`mode: live`** only if step 5's scorecard says *edge evidenced*. It also needs
   `POCKETBOT_REAL_MONEY=yes-i-accept-the-risk` and an SSID with `isDemo: 0`, and it
   will refuse any mismatch between the two.

## 6. Dashboard and VPS

`python -m pocketbot serve` (or `make pocket-serve`) runs the bot under a
supervisor with a web dashboard on port 8040. The dashboard shows:

- the connection state and which account is live (PAPER, DEMO or a red REAL MONEY badge)
- balance, P&L, win rate against the breakeven rate with its 95% range, and the verdict
- the price chart with Bollinger bands and every entry marked as won, lost or open
- the latest signal and why it did or didn't trade, and today's risk limits
- open trades, recent trades and the bot's activity log

It has one control: **Pause entries**. Pausing stops new trades and lets open
ones settle. Nothing on the page can place or close a trade.

The supervisor never exits because of the broker. A dropped connection, an
expired SSID or a refused account check shows up as a red banner saying why,
and it retries with backoff. Trade ledgers are kept per mode in
`data/pocketbot/trades-<mode>.jsonl`, and stats and the paper balance survive
restarts.

### Deploying next to flowbot

pocketbot has its own directory (`/opt/pocketbot`), compose project
(`pocketbot`), container and port (8040), so it never touches a flowbot
deployment on the same server.

**From GitHub (no SSH needed on your side):** Actions → *Deploy to VPS* →
Run workflow → `app: pocketbot`. It uses the same `VPS_HOST`, `VPS_USER` and
`VPS_SSH_KEY` secrets as flowbot. Optional extra secrets:

| Secret | Purpose |
|---|---|
| `POCKETBOT_SSID` | Your Pocket Option session string. Without it the bot runs a labelled synthetic market. |
| `POCKETBOT_DASHBOARD_TOKEN` | Dashboard password. Defaults to `DASHBOARD_TOKEN`. |

Choose `pocket_mode: paper` (live prices, no orders) or `demo` (orders on the
demo account). Real-money mode can't be set from a deploy.

**On the server itself (simplest):** log in to the VPS and run

```bash
curl -fsSL https://raw.githubusercontent.com/mypokelofi-collab/weatherbot/claude/pocket-broker-trading-bot-nzszf3/scripts/install-pocketbot.sh | sudo bash
```

It asks for your SSID (press Enter to skip), builds and starts the
container, and prints the dashboard link with its token. Re-run it to
update. Settings and ledgers are kept. Add `POCKETBOT_MODE=demo` before `sudo bash`
(as `sudo POCKETBOT_MODE=demo bash`) to trade the demo account.

**From a machine with SSH access:**

```bash
POCKETBOT_SSID='42["auth",{...}]' ./scripts/deploy-pocketbot.sh root@your-vps --mode demo
```

Then open `http://your-vps:8040/?token=<token>`.

**Replacing an expired SSID:** update the `POCKETBOT_SSID` secret and re-run
the workflow, or edit `/opt/pocketbot/.env` on the server and run
`docker compose up -d` there.

## 7. Honest expectations

Nothing found in this research (GitHub bots, PyPI clients, Reddit threads,
TradingView posts, Trustpilot reviews) showed a verified, independently audited
automated edge on this platform. The public bots mostly sell signals or
martingale. The strategies here are reasonable, well-known starting points
wrapped in strict risk control and honest measurement. They are not a promise
of profit. The most likely result of running any fixed-time bot on real money
is a slow loss equal to the payout gap.

## Sources

- [BinaryOptionsTools-v2 (ChipaDevTeam)](https://github.com/ChipaDevTeam/BinaryOptionsTools-v2) · [PyPI](https://pypi.org/project/binaryoptionstoolsv2/) · [docs](https://chipatrade.com/bo2-docs/getting-started)
- [PocketOptionAPI (archived, points to V2)](https://github.com/ChipaDevTeam/PocketOptionAPI)
- [A11ksa/API-Pocket-Option](https://github.com/A11ksa/API-Pocket-Option)
- [VitalySvyatyuk/pocket_option_trading_bot (browser automation)](https://github.com/VitalySvyatyuk/pocket_option_trading_bot)
- [pocket-option SDK on PyPI](https://pypi.org/project/pocket-option/)
- [Traders Union review of Pocket Option (operator, jurisdiction)](https://tradersunion.com/brokers/binary/view/pocketoption/)
- [Pocket Option's own "trading bot" maths article](https://pocketoption.com/blog/en/knowledge-base/trading/pocket-option-trading-bot/)
- [TradingView: "is 98% win-rate possible in binary options?"](https://de.tradingview.com/chart/GBPAUD/KWY3cNBV-What-s-PowerBot-is-98-Win-rate-possible-in-Binary-option)
