/* flowbot dashboard client.
 *
 * Two payload kinds arrive on the websocket: a full `snapshot` (on connect,
 * on every bar close, on every trade, and as a heartbeat) and a small `tick`
 * four times a second. Ticks are merged into the last snapshot, so the page
 * always renders from one coherent state object.
 */

import { PriceChart, EquityChart, sparkline, fmtPrice, fmtTime, fmtDay, theme } from './charts.js';

const $ = (id) => document.getElementById(id);
const state = { snap: null, connected: false, lastTickAt: 0 };

const priceChart = new PriceChart($('priceChart'), $('priceTip'));
const equityChart = new EquityChart($('equityChart'), $('equityTip'));

/* ---------------------------------------------------------------- utils */
const fmtUsd = (v, dp = 2) =>
  (v < 0 ? '-' : '') + '$' + Math.abs(v).toLocaleString(undefined,
    { minimumFractionDigits: dp, maximumFractionDigits: dp });

const fmtSigned = (v, dp = 2) => `${v >= 0 ? '+' : ''}${v.toFixed(dp)}`;
const pct = (v, dp = 2) => `${v >= 0 ? '+' : ''}${v.toFixed(dp)}%`;
const cls = (v) => (v > 0 ? 'pos' : v < 0 ? 'neg' : 'flat');

function duration(ms) {
  if (!isFinite(ms) || ms < 0) ms = 0;
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  if (h > 0) return `${h}h ${m % 60}m`;
  if (m > 0) return `${m}m ${String(s % 60).padStart(2, '0')}s`;
  return `${s}s`;
}

function el(tag, className, html) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (html !== undefined) e.innerHTML = html;
  return e;
}

/* ------------------------------------------------------------ websocket */
// A rejected handshake (bad or missing token) and a genuine network drop
// both surface to this code as a plain close event - the browser hides the
// HTTP status of a failed WebSocket upgrade. Retrying a bad token forever,
// silently, every 2s is how "reconnecting" ends up looking indistinguishable
// from a dead server. What we *can* check ourselves is whether the URL even
// has a token, which is the actual cause almost every time this has come up
// (a stale tab, a bookmark, or the link pasted without its query string).
let consecutiveCloses = 0;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const token = new URLSearchParams(location.search).get('token');
  if (!token) {
    setConnState('no access token in this URL - reopen the link with ?token=…');
    return;   // nothing a retry loop can fix; don't spam the server for it
  }
  const url = `${proto}://${location.host}/ws?token=${encodeURIComponent(token)}`;
  const ws = new WebSocket(url);

  ws.onopen = () => {
    state.connected = true;
    consecutiveCloses = 0;
    setConnState('live');
  };
  ws.onclose = () => {
    state.connected = false;
    consecutiveCloses += 1;
    const hint = consecutiveCloses >= 3 ? ' - check the token in the URL is still correct' : '';
    setConnState(`reconnecting…${hint}`);
    const backoffMs = Math.min(2000 * 2 ** Math.min(consecutiveCloses - 1, 4), 20000);
    setTimeout(connect, backoffMs);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'snapshot') {
      state.snap = msg.data;
      renderAll();
    } else if (msg.type === 'tick' && state.snap) {
      Object.assign(state.snap, msg.data);
      state.lastTickAt = Date.now();
      renderLive();
    } else if (msg.type === 'event' && state.snap) {
      state.snap.events = [...(state.snap.events || []), msg.data].slice(-60);
      renderLog();
    }
  };
  window.__ws = ws;
  setInterval(() => { if (ws.readyState === 1) ws.send('ping'); }, 20000);
}

function setConnState(text) {
  const chip = $('chipState');
  chip.textContent = text;
  chip.classList.toggle('danger', text !== 'live');
}

/* -------------------------------------------------------------- actions */
async function post(path, body) {
  const token = new URLSearchParams(location.search).get('token');
  const qs = token ? `?token=${encodeURIComponent(token)}` : '';
  const res = await fetch(`${path}${qs}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) alert(`${path} failed: ${res.status} ${await res.text()}`);
  return res.ok;
}

$('btnPause').onclick = async () => {
  const enabled = state.snap?.trading_enabled;
  await post(`/api/control/${enabled ? 'pause' : 'resume'}`);
};
$('btnFlatten').onclick = async () => {
  if (!state.snap?.portfolio?.position) return alert('No open position.');
  if (confirm('Close the open position now at market?')) await post('/api/control/flatten');
};
$('btnKill').onclick = async () => {
  const killed = state.snap?.risk?.kill_switch;
  if (killed) { await post('/api/control/revive'); return; }
  if (confirm('Kill switch: flatten and stop all new entries?')) await post('/api/control/kill');
};
$('btnTheme').onclick = () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('flowbot.theme', next);
  renderAll();
};
$('btnCvd').onclick = () => {
  const on = document.documentElement.dataset.cvd === 'safe';
  if (on) delete document.documentElement.dataset.cvd;
  else document.documentElement.dataset.cvd = 'safe';
  localStorage.setItem('flowbot.cvd', on ? 'normal' : 'safe');
  renderAll();
};
$('btnApply').onclick = async () => {
  const groups = { signal: {}, risk: {} };
  document.querySelectorAll('#params input').forEach((input) => {
    const [section, key] = input.dataset.path.split('.');
    const v = parseFloat(input.value);
    if (!isNaN(v)) groups[section][key] = v;
  });
  for (const [section, values] of Object.entries(groups)) {
    if (Object.keys(values).length) await post(`/api/params/${section}`, values);
  }
};

/* --------------------------------------------------------------- render */
function renderAll() {
  if (!state.snap) return;
  renderTop();
  renderTiles();
  renderPrice();
  renderEquity();
  renderSignal();
  renderPosition();
  renderBook();
  renderTape();
  renderTrades();
  renderOrders();
  renderLog();
  renderParams();
  renderPolymarket();
  renderDataPane();
}

/* Only the parts that move at tick rate. */
function renderLive() {
  renderTop();
  renderTiles();
  renderPosition();
  renderBook();
  renderTape();
  renderPolymarket();
  renderDataPane();
}

function renderTop() {
  const s = state.snap;
  $('chipVenue').textContent = `${s.venue} · ${s.symbol} · ${s.interval}`;
  $('symbolLabel').textContent = s.symbol;
  const dot = $('feedDot');
  dot.className = `dot ${s.feed?.connected ? 'on' : 'off'}`;
  $('chipLatency').textContent = s.feed?.latency_ms != null ? `${Math.round(s.feed.latency_ms)}ms` : '–';
  $('chipPrice').textContent = s.last_price ? fmtPrice(s.last_price) : '–';

  const dataChip = $('chipData');
  dataChip.textContent = s.real_data ? 'REAL market data' : 'SIMULATED data';
  dataChip.className = `chip ${s.real_data ? 'real' : 'sim'}`;

  const banner = $('banner');
  const messages = [];
  if (!s.real_data) {
    messages.push('⚠ Synthetic market data — this venue is a simulator. Nothing here reflects the real market.');
  }
  if (s.risk?.kill_switch) messages.push(`⛔ Kill switch engaged — ${s.risk.kill_reason}`);
  if (!s.trading_enabled) messages.push('⏸ New entries are paused; an open position is still managed.');
  if (s.feed && !s.feed.connected) messages.push('⚠ Market feed disconnected — reconnecting.');
  banner.textContent = messages.join('   ·   ');
  banner.classList.toggle('hidden', messages.length === 0);

  $('btnPause').textContent = s.trading_enabled ? 'Pause entries' : 'Resume entries';
  $('btnKill').textContent = s.risk?.kill_switch ? 'Release kill switch' : 'Kill switch';
  $('footerMeta').textContent =
    `· ${s.venue} · started ${s.started_at ? fmtDay(s.started_at) : '–'} UTC · ${s.bars_seen} bars seen`;
}

function renderTiles() {
  const s = state.snap;
  const p = s.portfolio || {};
  const st = s.stats || {};

  $('tEquity').textContent = fmtUsd(p.equity || 0);
  const pnl = p.pnl || 0;
  const d = $('tEquityDelta');
  d.textContent = `${fmtUsd(pnl)} (${pct(p.pnl_pct || 0)}) from ${fmtUsd(p.start_equity || 0, 0)}`;
  d.className = `delta ${cls(pnl)}`;

  const pos = p.position;
  const tp = $('tPosition');
  if (pos) {
    tp.textContent = `${pos.side === 'buy' ? '▲ LONG' : '▼ SHORT'} ${pos.qty}`;
    tp.className = `value ${pos.side === 'buy' ? 'pos' : 'neg'}`;
    const upnl = pos.unrealized || 0;
    const dd = $('tPositionDelta');
    dd.textContent = `${fmtUsd(upnl)} · ${fmtSigned(pos.unrealized_r || 0)}R · ${fmtUsd(pos.notional, 0)} notional`;
    dd.className = `delta ${cls(upnl)}`;
  } else {
    tp.textContent = 'flat';
    tp.className = 'value flat';
    const blocked = (s.risk?.blocked || [])[0];
    $('tPositionDelta').textContent = blocked ? blocked : 'waiting for the next signal';
    $('tPositionDelta').className = 'delta';
  }

  const day = s.risk?.day || {};
  $('tToday').textContent = fmtUsd(day.realized_pnl || 0);
  $('tToday').className = `value ${cls(day.realized_pnl || 0)}`;
  $('tTodayDelta').textContent =
    `${pct(day.pnl_pct || 0)} · ${day.trades || 0} trade(s) · ${day.wins || 0}W/${day.losses || 0}L`;

  $('tWin').textContent = st.trades ? `${st.win_rate}%` : '–';
  $('tWinDelta').textContent = st.trades
    ? `${st.wins}W / ${st.losses}L of ${st.trades} · PF ${st.profit_factor}`
    : 'no closed trades yet';

  $('tExpect').textContent = st.trades ? `${fmtSigned(st.expectancy_r)}R` : '–';
  $('tExpect').className = `value ${cls(st.expectancy_r || 0)}`;
  $('tExpectDelta').textContent = st.trades
    ? `avg win ${fmtSigned(st.avg_win_r)}R · avg loss ${fmtSigned(st.avg_loss_r)}R`
    : 'per trade, in R';

  $('tDD').textContent = `${(st.max_drawdown_pct || 0).toFixed(2)}%`;
  $('tDD').className = 'value neg';
  $('tDDDelta').textContent = `now ${(p.drawdown_pct || 0).toFixed(2)}% · peak ${fmtUsd(p.peak_equity || 0, 0)}`;

  const exec = s.execution || {};
  $('tFees').textContent = fmtUsd(p.fees_paid || 0);
  $('tFeesDelta').textContent =
    `avg slip ${(exec.avg_slippage_bps ?? 0).toFixed(2)}bps · ${exec.maker_fills || 0} maker / ${exec.taker_fills || 0} taker`;

  $('tExposure').textContent = `${(st.time_in_market_pct || 0).toFixed(0)}%`;
  $('tExposureDelta').textContent =
    `${st.bars_in_market || 0} of ${st.total_bars || 0} bars · lev ${(p.leverage || 0).toFixed(2)}x`;
}

function renderPrice() {
  const s = state.snap;
  const candles = (s.candles || []).slice(-160);
  const pos = s.portfolio?.position;
  priceChart.set(candles, {
    emaFast: s.config?.signal?.ema_fast || 21,
    emaSlow: s.config?.signal?.ema_slow || 55,
    donchian: s.config?.signal?.donchian_period || 20,
    markers: s.markers || [],
    position: pos ? { ...pos, effective_stop: s.position_mgmt?.effective_stop } : null,
  });
}

function renderEquity() {
  const s = state.snap;
  equityChart.set(s.equity_curve || [], s.portfolio?.start_equity || 0);
  const st = s.stats || {};
  // Annualising a Sharpe from twenty minutes of 5s samples produces a number
  // that looks authoritative and means nothing, so it stays hidden until
  // there is at least a day of curve behind it.
  const sharpe = (st.days || 0) >= 1 ? `Sharpe ${st.sharpe}` : 'Sharpe — (needs 1d)';
  $('equityHint').textContent =
    `${st.days ? `${st.days.toFixed(2)}d` : 'warming up'} · ${sharpe} · return ${pct(st.return_pct || 0)}`;
}

function renderSignal() {
  const s = state.snap;
  const sig = s.signal;
  const cfg = s.config?.signal || {};
  const entry = cfg.entry_threshold ?? 0.35;

  $('threshPos').style.left = `${50 + entry * 50}%`;
  $('threshNeg').style.left = `${50 - entry * 50}%`;

  if (!sig) {
    $('scoreVal').textContent = '–';
    $('scoreAction').textContent = 'waiting for the first closed bar';
    return;
  }

  const score = sig.score || 0;
  const sv = $('scoreVal');
  sv.textContent = fmtSigned(score);
  sv.className = `score-val ${cls(score)}`;

  const fill = $('scoreFill');
  const w = Math.min(50, Math.abs(score) * 50);
  fill.style.width = `${w}%`;
  fill.style.left = score >= 0 ? '50%' : `${50 - w}%`;
  fill.style.background = score >= 0 ? 'var(--up)' : 'var(--down)';

  const action = {
    enter_long: '▲ enter long', enter_short: '▼ enter short',
    exit: '✕ exit', hold: '● hold position', none: '· no trade',
  }[sig.action] || sig.action;
  $('scoreAction').textContent =
    `${action} · confidence ${(sig.confidence * 100).toFixed(0)}% of threshold`;

  const badge = $('regimeBadge');
  badge.textContent = (sig.regime || '').replace('_', ' ');
  badge.className = 'badge ' + ({
    trend_up: 'up', trend_down: 'down', chop: 'chop', illiquid: 'illiquid',
  }[sig.regime] || '');

  const box = $('components');
  box.replaceChildren();
  for (const c of sig.components || []) {
    const row = el('div', 'comp');
    row.appendChild(el('span', 'name', c.name.replace('_', ' ')));
    const bar = el('div', 'bar', '<span class="mid"></span>');
    const i = document.createElement('i');
    const width = Math.min(50, Math.abs(c.score) * 50);
    i.style.width = `${width}%`;
    i.style.left = c.score >= 0 ? '50%' : `${50 - width}%`;
    i.style.background = c.score >= 0 ? 'var(--up)' : 'var(--down)';
    i.style.opacity = String(0.35 + 0.65 * Math.min(1, c.weight / 0.22));
    bar.appendChild(i);
    row.appendChild(bar);
    row.appendChild(el('span', 'num', fmtSigned(c.contribution, 3)));
    row.title = `${c.name}: ${c.note} (score ${fmtSigned(c.score)}, weight ${c.weight.toFixed(2)})`;
    box.appendChild(row);
  }

  const reasons = $('reasons');
  reasons.replaceChildren();
  for (const r of (sig.reasons || []).slice(0, 5)) reasons.appendChild(el('li', '', r));

  const blockers = $('blockers');
  blockers.replaceChildren();
  for (const b of sig.blockers || []) blockers.appendChild(el('li', '', b));
  for (const b of (s.risk?.blocked || [])) blockers.appendChild(el('li', '', b));
}

function renderPosition() {
  const s = state.snap;
  const pos = s.portfolio?.position;
  const kv = $('positionKv');
  kv.replaceChildren();
  const add = (k, v, klass) => {
    kv.appendChild(el('dt', '', k));
    kv.appendChild(el('dd', klass || '', v));
  };

  if (!pos) {
    $('posAge').textContent = '';
    add('state', 'flat — waiting for a signal');
    add('next bar in', duration(s.next_bar_in_ms || 0));
    add('entry threshold', `±${(s.config?.signal?.entry_threshold ?? 0).toFixed(2)}`);
    add('risk target', `${s.config?.risk?.risk_per_trade_pct}% (${fmtUsd((s.portfolio?.equity || 0) * (s.config?.risk?.risk_per_trade_pct || 0) / 100)})`);

    // What the next entry would actually be, sized against the live book -
    // including the risk the venue's minimum order forces on a small account.
    const sp = s.sizing_preview;
    if (sp) {
      if (sp.ok) {
        add('next size', `${sp.qty} ${s.instrument?.base || 'BTC'} · ${fmtUsd(sp.notional, 0)} · ${sp.leverage.toFixed(1)}x`);
        add('would risk', `${sp.actual_risk_pct.toFixed(2)}% (${fmtUsd(sp.risk_amount)})`,
            sp.actual_risk_pct > (s.config?.risk?.risk_per_trade_pct || 0) * 1.5 ? 'neg' : '');
        if (sp.cap_applied) add('size capped by', sp.cap_applied);
        add('entry would cost', `${sp.expected_slippage_bps.toFixed(2)}bps`);
      } else {
        add('next size', sp.reason, 'neg');
      }
    }
    add('trades today', `${s.risk?.day?.trades || 0} / ${s.config?.risk?.max_trades_per_day}`);
    return;
  }

  const mgmt = s.position_mgmt || {};
  const age = (s.ts || Date.now()) - pos.entry_ts;
  $('posAge').textContent = `${duration(age)} · ${pos.bars_held} bars`;

  add('side', pos.side === 'buy' ? '▲ LONG' : '▼ SHORT', pos.side === 'buy' ? 'pos' : 'neg');
  add('size', `${pos.qty} ${s.instrument?.base || 'BTC'} · ${fmtUsd(pos.notional, 0)}`);
  add('entry', fmtPrice(pos.entry_price));
  add('mark', fmtPrice(pos.mark || 0));
  add('unrealised', `${fmtUsd(pos.unrealized)} (${fmtSigned(pos.unrealized_r)}R)`, cls(pos.unrealized));
  add('stop', fmtPrice(mgmt.effective_stop || pos.stop), 'neg');
  add('hard stop', fmtPrice(pos.stop));
  add('trail', pos.trail ? fmtPrice(pos.trail) : '—');
  add('target', `${fmtPrice(pos.target)} (${s.config?.risk?.take_profit_r}R)`);
  add('partial out', pos.scaled_out ? 'yes' : 'no');
  add('breakeven', pos.breakeven_armed ? 'armed' : 'no');
  add('MFE / MAE', `${fmtSigned(pos.max_favorable_r)}R / ${fmtSigned(pos.max_adverse_r)}R`);
  add('1R', fmtUsd(pos.risk_per_unit * pos.qty));
  add('entry score', fmtSigned(pos.entry_signal_score));
  if (s.exit_estimate) {
    add('exit cost now',
      `${fmtPrice(s.exit_estimate.avg_price)} · ${s.exit_estimate.slippage_bps.toFixed(2)}bps`);
  }
  add('why', pos.tag || '—');
}

function renderBook() {
  const s = state.snap;
  const book = s.book;
  const ladder = $('ladder');
  ladder.replaceChildren();
  if (!book || !book.bids?.length) {
    ladder.appendChild(el('div', 'empty', 'waiting for the order book…'));
    return;
  }

  const levels = 8;
  const asks = book.asks.slice(0, levels).reverse();
  const bids = book.bids.slice(0, levels);
  const maxSz = Math.max(
    ...asks.map((a) => a[1]), ...bids.map((b) => b[1]), 1e-9
  );

  let cum = 0;
  const askRows = [];
  for (const [px, sz] of book.asks.slice(0, levels)) {
    cum += sz;
    askRows.push([px, sz, cum]);
  }
  askRows.reverse();
  for (const [px, sz, c] of askRows) {
    const row = el('div', 'row ask');
    row.appendChild(el('span', 'side', 'ask'));
    const fillEl = el('div', 'depthfill');
    fillEl.style.width = `${(sz / maxSz) * 100}%`;
    row.appendChild(fillEl);
    row.appendChild(el('span', 'px', fmtPrice(px)));
    row.appendChild(el('span', 'sz', sz.toFixed(3)));
    row.appendChild(el('span', 'cum', c.toFixed(2)));
    ladder.appendChild(row);
  }

  const spread = el('div', 'spread');
  spread.appendChild(el('span', '', `spread ${book.spread_bps.toFixed(2)} bps`));
  spread.appendChild(el('span', '', `mid ${fmtPrice(book.mid)}`));
  spread.appendChild(el('span', '', `micro ${fmtPrice(book.microprice)}`));
  ladder.appendChild(spread);

  cum = 0;
  for (const [px, sz] of bids) {
    cum += sz;
    const row = el('div', 'row bid');
    row.appendChild(el('span', 'side', 'bid'));
    const fillEl = el('div', 'depthfill');
    fillEl.style.width = `${(sz / maxSz) * 100}%`;
    row.appendChild(fillEl);
    row.appendChild(el('span', 'px', fmtPrice(px)));
    row.appendChild(el('span', 'sz', sz.toFixed(3)));
    row.appendChild(el('span', 'cum', cum.toFixed(2)));
    ladder.appendChild(row);
  }

  const pr = s.pressure || {};
  const imb = pr.imbalance_10bps ?? book.imbalance ?? 0;
  const fill = $('imbFill');
  const w = Math.min(50, Math.abs(imb) * 50);
  fill.style.width = `${w}%`;
  fill.style.left = imb >= 0 ? '50%' : `${50 - w}%`;
  fill.style.background = imb >= 0 ? 'var(--up)' : 'var(--down)';
  $('imbLabel').textContent = `${imb >= 0 ? 'bid' : 'ask'}-heavy ${(Math.abs(imb) * 100).toFixed(0)}%`;
  $('imbBid').textContent = `bid ${fmtUsd(pr.depth_bid_10bps || 0, 0)}`;
  $('imbAsk').textContent = `${fmtUsd(pr.depth_ask_10bps || 0, 0)} ask`;
  $('bookHint').textContent =
    `liquidity ${(pr.liquidity_score ?? 0).toFixed(2)} · ${book.bids.length}×${book.asks.length} levels`;
}

function renderTape() {
  const s = state.snap;
  const tape = $('tape');
  tape.replaceChildren();
  const rows = (s.tape || []).slice(-40).reverse();
  for (const t of rows) {
    const row = el('div', `t ${t.s}`);
    row.appendChild(el('span', 'ts', fmtTime(t.ts)));
    row.appendChild(el('span', 'p', fmtPrice(t.p)));
    row.appendChild(el('span', 'q', t.q.toFixed(4)));
    row.appendChild(el('span', 'arrow', t.s === 'buy' ? '▲' : '▼'));
    tape.appendChild(row);
  }
  const ts = s.tape_stats || {};
  $('tapeHint').textContent =
    `${(ts.trades_per_min || 0).toFixed(0)} trades/min · avg ${fmtUsd(ts.avg_trade_usd || 0, 0)}`;

  const cvd = (s.cvd || []).map((c) => c.cvd);
  sparkline($('cvdSpark'), cvd, theme().series1);
  const last = cvd.length ? cvd[cvd.length - 1] : 0;
  $('cvdVal').textContent =
    `${fmtSigned(last, 2)} BTC · 5m imbalance ${((ts.imbalance || 0) * 100).toFixed(0)}%`;
}

function renderTrades() {
  const s = state.snap;
  const body = $('tradesTable').querySelector('tbody');
  body.replaceChildren();
  const trades = (s.trades || []).slice().reverse();
  if (!trades.length) {
    const tr = el('tr');
    tr.appendChild(el('td', 'empty', 'No closed trades yet — the bot is waiting for a signal.'))
      .setAttribute('colspan', '10');
    body.appendChild(tr);
  }
  for (const t of trades) {
    const tr = el('tr');
    const cells = [
      [`#${t.id}`, ''],
      [t.side === 'buy' ? '▲ long' : '▼ short', t.side === 'buy' ? 'pos' : 'neg'],
      [t.qty, ''],
      [fmtPrice(t.entry_price), ''],
      [fmtPrice(t.exit_price), ''],
      [fmtUsd(t.pnl), cls(t.pnl) + ' strong'],
      [`${fmtSigned(t.r_multiple)}R`, cls(t.r_multiple)],
      [`${fmtSigned(t.max_favorable_r)}R`, ''],
      [t.bars_held, ''],
      [t.exit_reason, ''],
    ];
    for (const [text, klass] of cells) tr.appendChild(el('td', klass, String(text)));
    tr.title = `entry: ${t.entry_reason}\nfees ${fmtUsd(t.fees)} · entry slip ${t.entry_slippage_bps.toFixed(2)}bps · exit slip ${t.exit_slippage_bps.toFixed(2)}bps`;
    body.appendChild(tr);
  }
  const st = s.stats || {};
  $('tradesHint').textContent = st.trades
    ? `${st.trades} trades · net ${fmtUsd(st.net_pnl)} · fees ${fmtUsd(st.fees)} · PF ${st.profit_factor} · longs ${st.longs} / shorts ${st.shorts}`
    : '';
}

function renderOrders() {
  const s = state.snap;
  const body = $('ordersTable').querySelector('tbody');
  body.replaceChildren();
  const orders = (s.orders || []).slice().reverse();
  if (!orders.length) {
    const tr = el('tr');
    const td = el('td', 'empty', 'No orders yet.');
    td.setAttribute('colspan', '11');
    tr.appendChild(td);
    body.appendChild(tr);
  }
  for (const o of orders) {
    const tr = el('tr');
    const statusKlass = { filled: 'pos', rejected: 'neg', canceled: 'flat' }[o.status] || '';
    const cells = [
      [o.id, ''],
      [fmtTime(o.ts), ''],
      [o.side === 'buy' ? 'buy' : 'sell', o.side === 'buy' ? 'pos' : 'neg'],
      [`${o.type}${o.tif === 'post' ? ' (post)' : ''}`, ''],
      [o.qty, ''],
      [o.price ? fmtPrice(o.price) : '—', ''],
      [o.filled_qty, ''],
      [o.avg_price ? fmtPrice(o.avg_price) : '—', ''],
      [o.fees ? fmtUsd(o.fees) : '—', ''],
      [o.status + (o.reject_reason ? ' ⓘ' : ''), statusKlass],
      [o.tag, ''],
    ];
    for (const [text, klass] of cells) tr.appendChild(el('td', klass, String(text)));
    tr.title = [o.tag, o.reject_reason, `queue ahead ${o.queue_ahead ?? 0}`]
      .filter(Boolean).join('\n');
    body.appendChild(tr);
  }
  const e = s.execution || {};
  $('execHint').textContent =
    `${e.submitted || 0} sent · ${e.filled || 0} filled · ${e.rejected || 0} rejected · ${e.canceled || 0} cancelled · avg slip ${(e.avg_slippage_bps ?? 0).toFixed(2)}bps`;
}

function renderLog() {
  const s = state.snap;
  const log = $('log');
  log.replaceChildren();
  for (const e of (s.events || []).slice(-60)) {
    const row = el('div', `row ${e.kind}`);
    row.appendChild(el('span', 'ts', fmtTime(e.ts)));
    row.appendChild(el('span', 'kind', e.kind));
    row.appendChild(el('span', 'msg', e.message));
    log.appendChild(row);
  }
}

function renderPolymarket() {
  const pm = state.snap.polymarket;
  const card = $('polyCard');
  if (!pm || !pm.enabled) { card.hidden = true; return; }
  card.hidden = false;

  const st = pm.stats || {};
  const fst = pm.forced_stats || {};
  const forcedNote = fst.enabled ? ` · ${fst.settled || 0} forced` : '';
  $('polyHint').textContent =
    `${st.open || 0} open · ${st.settled || 0} settled${forcedNote} · ${fmtUsd(pm.pnl || 0)}`;

  const kv = $('polyKv');
  kv.replaceChildren();
  const add = (k, v, klass) => {
    kv.appendChild(el('dt', '', k));
    kv.appendChild(el('dd', klass || '', v));
  };
  add('paper bankroll', fmtUsd(pm.equity || 0));
  add('settled pnl', fmtUsd(st.pnl || 0), cls(st.pnl || 0));
  add('win rate', st.settled ? `${st.win_rate}% of ${st.settled}` : '—');
  if (pm.calibration) {
    add('brier', `${pm.calibration.brier} (skill ${pm.calibration.brier_skill_score})`);
  }
  if (pm.last_error) add('last error', pm.last_error, 'neg');

  const body = $('polyTable').querySelector('tbody');
  body.replaceChildren();
  const rows = [
    ...(pm.positions || []).map((p) => ({
      slug: p.slug, model: p.model_p_at_entry, book: p.mark, edge: p.edge_at_entry,
      left: (p.end_ts - (state.snap.ts || Date.now())),
      status: `${p.forced ? 'FORCED · ' : ''}holding ${p.shares} ${p.side}`,
    })),
    ...(pm.assessments || []).map((a) => ({
      slug: a.slug, model: a.model_p, book: a.market_p, edge: a.edge,
      left: a.seconds_left * 1000,
      status: a.tradable ? `ready · ${a.shares} ${a.side}` : (a.blockers[0] || 'no edge'),
    })),
  ];
  if (!rows.length) {
    const tr = el('tr');
    const td = el('td', 'empty', 'no matching markets yet');
    td.setAttribute('colspan', '6');
    tr.appendChild(td);
    body.appendChild(tr);
  }
  for (const r of rows.slice(0, 10)) {
    const tr = el('tr');
    tr.appendChild(el('td', 'strong', r.slug.replace(/-/g, ' ').slice(0, 34)));
    tr.appendChild(el('td', '', `${(r.model * 100).toFixed(0)}%`));
    tr.appendChild(el('td', '', `${(r.book * 100).toFixed(0)}%`));
    tr.appendChild(el('td', cls(r.edge), `${(r.edge * 100) >= 0 ? '+' : ''}${(r.edge * 100).toFixed(1)}pt`));
    tr.appendChild(el('td', '', duration(r.left)));
    tr.appendChild(el('td', '', r.status));
    body.appendChild(tr);
  }
}

const PARAM_FIELDS = [
  ['signal.entry_threshold', 'Entry threshold |score|'],
  ['signal.exit_threshold', 'Exit threshold'],
  ['signal.flip_threshold', 'Flip threshold'],
  ['signal.min_adx', 'Min ADX (trend gate)'],
  ['signal.max_spread_bps', 'Max spread (bps)'],
  ['risk.risk_per_trade_pct', 'Risk per trade (%)'],
  ['risk.stop_atr_mult', 'Stop (× ATR)'],
  ['risk.trail_atr_mult', 'Trail (× ATR)'],
  ['risk.take_profit_r', 'Partial target (R)'],
  ['risk.max_bars_in_trade', 'Time stop (bars)'],
  ['risk.daily_loss_limit_pct', 'Daily loss limit (%)'],
  ['risk.max_trades_per_day', 'Max trades / day'],
];

function renderParams() {
  const s = state.snap;
  const box = $('params');
  if (box.dataset.ready === '1') return;      // don't fight the user's typing
  box.replaceChildren();
  for (const [path, label] of PARAM_FIELDS) {
    const [section, key] = path.split('.');
    const value = s.config?.[section]?.[key];
    if (value === undefined) continue;
    const lbl = el('label', '', label);
    const input = document.createElement('input');
    input.type = 'number';
    input.step = 'any';
    input.value = value;
    input.dataset.path = path;
    box.appendChild(lbl);
    box.appendChild(input);
  }
  box.dataset.ready = '1';
}

let activeTab = 'features';
$('dataTabs').addEventListener('click', (e) => {
  const btn = e.target.closest('button');
  if (!btn) return;
  activeTab = btn.dataset.tab;
  [...$('dataTabs').children].forEach((b) =>
    b.setAttribute('aria-selected', String(b.dataset.tab === activeTab)));
  renderDataPane();
});

function renderDataPane() {
  const s = state.snap;
  const pane = $('dataPane');
  let entries = [];
  if (activeTab === 'features') {
    entries = Object.entries(s.signal?.features || {});
  } else if (activeTab === 'stats') {
    entries = Object.entries(s.stats || {});
  } else if (activeTab === 'exec') {
    entries = [
      ...Object.entries(s.execution || {}),
      ...Object.entries(s.instrument || {}).map(([k, v]) => [`instrument.${k}`, v]),
      ...Object.entries(s.config?.execution || {}).map(([k, v]) => [`cfg.${k}`, v]),
    ];
  } else {
    entries = [
      ...Object.entries(s.feed || {}),
      ...Object.entries(s.pressure || {}),
      ['venue_clock', s.venue_now ? fmtDay(s.venue_now) : '–'],
      ['next_bar_in', duration(s.next_bar_in_ms || 0)],
    ];
  }
  pane.replaceChildren();
  for (const [k, v] of entries) {
    const row = el('div');
    row.appendChild(el('span', '', k));
    const val = typeof v === 'number'
      ? (Number.isInteger(v) ? v.toLocaleString() : v.toFixed(Math.abs(v) < 1 ? 4 : 2))
      : String(v);
    row.appendChild(el('span', '', val));
    pane.appendChild(row);
  }
  if (!entries.length) pane.appendChild(el('div', 'empty', 'nothing here yet'));
}

/* --------------------------------------------------- local countdown UI */
setInterval(() => {
  if (!state.snap) return;
  const drift = Date.now() - (state.lastTickAt || Date.now());
  const remaining = Math.max(0, (state.snap.next_bar_in_ms || 0) - drift);
  $('chipNextBar').textContent = duration(remaining);
}, 250);

window.addEventListener('resize', () => {
  if (!state.snap) return;
  renderPrice();
  renderEquity();
  renderTape();
});

/* restore preferences */
document.documentElement.dataset.theme = localStorage.getItem('flowbot.theme') || 'dark';
if (localStorage.getItem('flowbot.cvd') === 'safe') document.documentElement.dataset.cvd = 'safe';

connect();
