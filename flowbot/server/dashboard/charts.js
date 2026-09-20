/* Canvas chart primitives for the flowbot dashboard.
 *
 * Hand-rolled rather than pulled from a CDN: the dashboard has to work on a
 * VPS with no outbound internet and inside a container with no npm install.
 *
 * Conventions (kept identical across every chart here):
 *   - 2px lines, >=8px markers, hairline grid one step off the surface
 *   - marks carry colour; all text uses the ink tokens
 *   - every plot has a crosshair + tooltip; nothing is hover-only
 *   - up/down is a polarity pair, and it is never the only cue: signs,
 *     arrows and side labels carry the same information as the colour
 */

const CSS = (name, fallback) => {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
};

export const theme = () => ({
  surface: CSS('--surface-1', '#15161a'),
  grid: CSS('--grid', '#26272c'),
  axis: CSS('--axis', '#383a40'),
  ink: CSS('--text-primary', '#ffffff'),
  ink2: CSS('--text-secondary', '#c3c2b7'),
  muted: CSS('--muted', '#898781'),
  up: CSS('--up', '#199e70'),
  down: CSS('--down', '#d03b3b'),
  series1: CSS('--series-1', '#3987e5'),
  series2: CSS('--series-2', '#d95926'),
  series3: CSS('--series-3', '#c98500'),
  accentSoft: CSS('--accent-soft', 'rgba(57,135,229,0.14)'),
});

export function fitCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const w = Math.max(1, Math.floor(rect.width));
  const h = Math.max(1, Math.floor(rect.height));
  if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
    canvas.width = w * dpr;
    canvas.height = h * dpr;
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

export const fmtPrice = (v) =>
  v >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 1 })
            : v.toLocaleString(undefined, { maximumFractionDigits: 4 });

export const fmtTime = (ts) => {
  const d = new Date(ts);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
};

export const fmtDay = (ts) => {
  const d = new Date(ts);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getUTCDate())}/${p(d.getUTCMonth() + 1)} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
};

export function ema(values, period) {
  const out = new Array(values.length).fill(null);
  if (values.length < period) return out;
  const k = 2 / (period + 1);
  let prev = values.slice(0, period).reduce((a, b) => a + b, 0) / period;
  out[period - 1] = prev;
  for (let i = period; i < values.length; i++) {
    prev = values[i] * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

function niceTicks(min, max, count) {
  if (!isFinite(min) || !isFinite(max) || min === max) return [min];
  const span = max - min;
  const raw = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
  const start = Math.ceil(min / step) * step;
  const out = [];
  for (let v = start; v <= max + 1e-9; v += step) out.push(v);
  return out;
}

/* ------------------------------------------------------------------ */
/* Price chart: candles, moving averages, channel, trade markers.      */
/* ------------------------------------------------------------------ */
export class PriceChart {
  constructor(canvas, tooltip) {
    this.canvas = canvas;
    this.tooltip = tooltip;
    this.data = [];
    this.opts = {};
    this.hover = null;
    this.layout = null;
    canvas.addEventListener('mousemove', (e) => this.onMove(e));
    canvas.addEventListener('mouseleave', () => { this.hover = null; this.draw(); this.hideTip(); });
    canvas.addEventListener('touchstart', (e) => this.onMove(e.touches[0]), { passive: true });
    canvas.addEventListener('touchmove', (e) => this.onMove(e.touches[0]), { passive: true });
  }

  set(data, opts) {
    this.data = data || [];
    this.opts = opts || {};
    this.draw();
  }

  onMove(ev) {
    if (!this.layout || !ev) return;
    const rect = this.canvas.getBoundingClientRect();
    this.hover = { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
    this.draw();
  }

  hideTip() { if (this.tooltip) this.tooltip.style.display = 'none'; }

  showTip(html, x, y) {
    if (!this.tooltip) return;
    const t = this.tooltip;
    t.innerHTML = html;
    t.style.display = 'block';
    const box = this.canvas.getBoundingClientRect();
    const tw = t.offsetWidth || 180;
    let left = x + 14;
    if (left + tw > box.width) left = x - tw - 14;
    t.style.left = `${Math.max(4, left)}px`;
    t.style.top = `${Math.max(4, Math.min(y + 12, box.height - 90))}px`;
  }

  draw() {
    const { ctx, w, h } = fitCanvas(this.canvas);
    const t = theme();
    const d = this.data;
    if (!d.length) {
      ctx.fillStyle = t.muted;
      ctx.font = '13px system-ui, sans-serif';
      ctx.fillText('waiting for bars…', 12, 22);
      return;
    }

    const padL = 8, padR = 62, padT = 10, padB = 22;
    const volH = Math.max(26, Math.round(h * 0.16));
    const plotH = h - padT - padB - volH - 6;
    const plotW = w - padL - padR;

    let lo = Infinity, hi = -Infinity;
    for (const c of d) { lo = Math.min(lo, c.l); hi = Math.max(hi, c.h); }
    const pos = this.opts.position;
    const extraLevels = [];
    if (pos) {
      extraLevels.push(pos.entry_price, pos.effective_stop || pos.stop, pos.target);
    }
    for (const lv of extraLevels) {
      if (lv && isFinite(lv)) { lo = Math.min(lo, lv); hi = Math.max(hi, lv); }
    }
    const pad = (hi - lo) * 0.08 || 1;
    lo -= pad; hi += pad;

    const x = (i) => padL + (i + 0.5) * (plotW / d.length);
    const y = (p) => padT + (1 - (p - lo) / (hi - lo)) * plotH;
    this.layout = { padL, padR, padT, padB, plotW, plotH, volH, x, y, lo, hi };

    // grid + price axis
    ctx.lineWidth = 1;
    ctx.font = '11px system-ui, sans-serif';
    ctx.textBaseline = 'middle';
    for (const p of niceTicks(lo, hi, 5)) {
      const yy = Math.round(y(p)) + 0.5;
      ctx.strokeStyle = t.grid;
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + plotW, yy); ctx.stroke();
      ctx.fillStyle = t.muted;
      ctx.fillText(fmtPrice(p), padL + plotW + 6, yy);
    }

    // time axis
    ctx.textBaseline = 'top';
    const stepX = Math.max(1, Math.floor(d.length / 6));
    for (let i = 0; i < d.length; i += stepX) {
      ctx.fillStyle = t.muted;
      ctx.textAlign = 'center';
      ctx.fillText(fmtTime(d[i].t), x(i), h - padB + 6);
    }
    ctx.textAlign = 'left';

    // Donchian channel as two hairlines - a filled band this wide would
    // out-ink the candles it is supposed to frame.
    if (this.opts.donchian && d.length > this.opts.donchian) {
      const n = this.opts.donchian;
      ctx.save();
      ctx.setLineDash([3, 4]);
      ctx.strokeStyle = t.axis;
      ctx.lineWidth = 1;
      for (const pick of ['h', 'l']) {
        ctx.beginPath();
        let started = false;
        for (let i = n; i < d.length; i++) {
          const win = d.slice(i - n, i);
          const v = pick === 'h'
            ? Math.max(...win.map((c) => c.h))
            : Math.min(...win.map((c) => c.l));
          if (!started) { ctx.moveTo(x(i), y(v)); started = true; } else ctx.lineTo(x(i), y(v));
        }
        ctx.stroke();
      }
      ctx.restore();
    }

    // volume, coloured by aggressor balance of the bar
    let maxV = 0;
    for (const c of d) maxV = Math.max(maxV, c.v || 0);
    const volTop = padT + plotH + 6;
    const bw = Math.max(1, plotW / d.length - 2);
    for (let i = 0; i < d.length; i++) {
      const c = d[i];
      const vh = maxV > 0 ? ((c.v || 0) / maxV) * volH : 0;
      const buyShare = c.v > 0 ? (c.bv || 0) / c.v : 0.5;
      ctx.fillStyle = buyShare >= 0.5 ? t.up : t.down;
      ctx.globalAlpha = 0.45;
      ctx.fillRect(x(i) - bw / 2, volTop + volH - vh, bw, vh);
      ctx.globalAlpha = 1;
    }

    // candles
    for (let i = 0; i < d.length; i++) {
      const c = d[i];
      const up = c.c >= c.o;
      const col = up ? t.up : t.down;
      const cx = x(i);
      ctx.strokeStyle = col;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(cx) + 0.5, y(c.h));
      ctx.lineTo(Math.round(cx) + 0.5, y(c.l));
      ctx.stroke();
      const top = y(Math.max(c.o, c.c));
      const bot = y(Math.min(c.o, c.c));
      ctx.fillStyle = col;
      ctx.fillRect(cx - bw / 2, top, bw, Math.max(1, bot - top));
      if (!c.closed) {                       // the live bar reads as provisional
        ctx.globalAlpha = 0.45;
        ctx.fillRect(cx - bw / 2, top, bw, Math.max(1, bot - top));
        ctx.globalAlpha = 1;
      }
    }

    // moving averages
    const closes = d.map((c) => c.c);
    const lines = [
      { vals: ema(closes, this.opts.emaFast || 21), color: t.series1 },
      { vals: ema(closes, this.opts.emaSlow || 55), color: t.series3 },
    ];
    ctx.lineWidth = 2;
    ctx.lineJoin = 'round';
    for (const ln of lines) {
      ctx.strokeStyle = ln.color;
      ctx.beginPath();
      let started = false;
      for (let i = 0; i < d.length; i++) {
        const v = ln.vals[i];
        if (v == null) continue;
        if (!started) { ctx.moveTo(x(i), y(v)); started = true; } else ctx.lineTo(x(i), y(v));
      }
      ctx.stroke();
    }

    // open-position levels
    if (pos) {
      const levels = [
        { v: pos.entry_price, c: t.ink2, label: 'entry', dash: [4, 4] },
        { v: pos.effective_stop || pos.stop, c: t.down, label: 'stop', dash: [] },
        { v: pos.target, c: t.up, label: 'target', dash: [2, 3] },
      ];
      ctx.font = '10px system-ui, sans-serif';
      for (const l of levels) {
        if (!l.v || !isFinite(l.v)) continue;
        const yy = Math.round(y(l.v)) + 0.5;
        ctx.save();
        ctx.setLineDash(l.dash);
        ctx.strokeStyle = l.c;
        ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + plotW, yy); ctx.stroke();
        ctx.restore();
        ctx.fillStyle = l.c;
        ctx.fillRect(padL + plotW + 2, yy - 7, padR - 4, 14);
        ctx.fillStyle = t.surface;
        ctx.textBaseline = 'middle';
        ctx.fillText(fmtPrice(l.v), padL + plotW + 5, yy);
      }
    }

    // entry / exit markers: triangle + side letter, so the cue is not colour alone
    const markers = this.opts.markers || [];
    const t0 = d[0].t, t1 = d[d.length - 1].T || d[d.length - 1].t;
    for (const m of markers) {
      if (m.ts < t0 || m.ts > t1 + 900000) continue;
      const idx = Math.max(0, Math.min(d.length - 1,
        d.findIndex((c) => m.ts >= c.t && m.ts < (c.T || c.t + 900000))));
      const mx = x(idx === -1 ? d.length - 1 : idx);
      const my = y(m.price);
      const buy = m.side === 'buy';
      const col = m.kind === 'entry' ? (buy ? t.up : t.down) : t.series1;
      ctx.fillStyle = col;
      ctx.strokeStyle = t.surface;
      ctx.lineWidth = 2;
      ctx.beginPath();
      const s = 6;
      if (buy) { ctx.moveTo(mx, my - s); ctx.lineTo(mx - s, my + s); ctx.lineTo(mx + s, my + s); }
      else { ctx.moveTo(mx, my + s); ctx.lineTo(mx - s, my - s); ctx.lineTo(mx + s, my - s); }
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = t.ink2;
      ctx.font = '9px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(m.kind === 'entry' ? (buy ? 'L' : 'S') : 'X', mx, my + (buy ? s + 11 : -s - 13));
      ctx.textAlign = 'left';
    }

    // crosshair + tooltip
    if (this.hover && this.hover.x > padL && this.hover.x < padL + plotW) {
      const i = Math.max(0, Math.min(d.length - 1,
        Math.floor((this.hover.x - padL) / (plotW / d.length))));
      const c = d[i];
      const cx = x(i);
      ctx.save();
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = t.axis;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(Math.round(cx) + 0.5, padT); ctx.lineTo(Math.round(cx) + 0.5, padT + plotH); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(padL, Math.round(this.hover.y) + 0.5); ctx.lineTo(padL + plotW, Math.round(this.hover.y) + 0.5); ctx.stroke();
      ctx.restore();
      const chg = ((c.c / c.o - 1) * 100).toFixed(2);
      const delta = (c.bv || 0) - (c.sv || 0);
      this.showTip(
        `<b>${fmtDay(c.t)} UTC</b>
         <span>O <i>${fmtPrice(c.o)}</i></span>
         <span>H <i>${fmtPrice(c.h)}</i></span>
         <span>L <i>${fmtPrice(c.l)}</i></span>
         <span>C <i>${fmtPrice(c.c)}</i> (${chg > 0 ? '+' : ''}${chg}%)</span>
         <span>vol <i>${(c.v || 0).toFixed(2)}</i></span>
         <span>delta <i>${delta >= 0 ? '+' : ''}${delta.toFixed(2)}</i></span>`,
        cx, this.hover.y
      );
    }
  }
}

/* ------------------------------------------------------------------ */
/* Equity curve with drawdown underlay.                                */
/* ------------------------------------------------------------------ */
export class EquityChart {
  constructor(canvas, tooltip) {
    this.canvas = canvas;
    this.tooltip = tooltip;
    this.points = [];
    this.baseline = 0;
    this.hover = null;
    canvas.addEventListener('mousemove', (e) => {
      const r = canvas.getBoundingClientRect();
      this.hover = { x: e.clientX - r.left, y: e.clientY - r.top };
      this.draw();
    });
    canvas.addEventListener('mouseleave', () => {
      this.hover = null; this.draw();
      if (this.tooltip) this.tooltip.style.display = 'none';
    });
  }

  set(points, baseline) {
    this.points = points || [];
    this.baseline = baseline || 0;
    this.draw();
  }

  draw() {
    const { ctx, w, h } = fitCanvas(this.canvas);
    const t = theme();
    const p = this.points;
    if (p.length < 2) {
      ctx.fillStyle = t.muted;
      ctx.font = '13px system-ui, sans-serif';
      ctx.fillText('equity curve builds as the bot runs…', 12, 22);
      return;
    }
    const padL = 8, padR = 60, padT = 10, padB = 20;
    const plotW = w - padL - padR, plotH = h - padT - padB;
    let lo = Infinity, hi = -Infinity;
    for (const [, v] of p) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    lo = Math.min(lo, this.baseline); hi = Math.max(hi, this.baseline);
    // A brand-new session has a flat curve; without a floor on the range the
    // axis zooms into cents and a straight line looks like a cliff.
    const minSpan = Math.abs(this.baseline || hi) * 0.01;
    if (hi - lo < minSpan) {
      const mid = (hi + lo) / 2;
      lo = mid - minSpan / 2;
      hi = mid + minSpan / 2;
    }
    const pad = (hi - lo) * 0.12 || 1;
    lo -= pad; hi += pad;
    const t0 = p[0][0], t1 = p[p.length - 1][0] || t0 + 1;
    const x = (ts) => padL + ((ts - t0) / Math.max(1, t1 - t0)) * plotW;
    const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * plotH;

    ctx.font = '11px system-ui, sans-serif';
    ctx.textBaseline = 'middle';
    for (const v of niceTicks(lo, hi, 4)) {
      const yy = Math.round(y(v)) + 0.5;
      ctx.strokeStyle = t.grid;
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + plotW, yy); ctx.stroke();
      ctx.fillStyle = t.muted;
      ctx.fillText(fmtPrice(v), padL + plotW + 6, yy);
    }

    // starting equity reference
    const by = Math.round(y(this.baseline)) + 0.5;
    ctx.save();
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = t.axis;
    ctx.beginPath(); ctx.moveTo(padL, by); ctx.lineTo(padL + plotW, by); ctx.stroke();
    ctx.restore();

    // drawdown wash: distance below the running peak
    let peak = -Infinity;
    ctx.beginPath();
    ctx.moveTo(x(p[0][0]), y(p[0][1]));
    const peaks = [];
    for (const [ts, v] of p) { peak = Math.max(peak, v); peaks.push([ts, peak]); }
    for (const [ts, v] of p) ctx.lineTo(x(ts), y(v));
    for (let i = peaks.length - 1; i >= 0; i--) ctx.lineTo(x(peaks[i][0]), y(peaks[i][1]));
    ctx.closePath();
    ctx.fillStyle = 'rgba(208,59,59,0.07)';
    ctx.fill();

    // equity area + line
    ctx.beginPath();
    ctx.moveTo(x(p[0][0]), y(p[0][1]));
    for (const [ts, v] of p) ctx.lineTo(x(ts), y(v));
    ctx.lineTo(x(p[p.length - 1][0]), padT + plotH);
    ctx.lineTo(x(p[0][0]), padT + plotH);
    ctx.closePath();
    ctx.fillStyle = t.accentSoft;
    ctx.fill();

    ctx.beginPath();
    ctx.lineWidth = 2;
    ctx.lineJoin = 'round';
    ctx.strokeStyle = t.series1;
    p.forEach(([ts, v], i) => (i ? ctx.lineTo(x(ts), y(v)) : ctx.moveTo(x(ts), y(v))));
    ctx.stroke();

    const last = p[p.length - 1];
    ctx.beginPath();
    ctx.arc(x(last[0]), y(last[1]), 4, 0, Math.PI * 2);
    ctx.fillStyle = t.series1;
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = t.surface;
    ctx.stroke();

    if (this.hover && this.hover.x > padL && this.hover.x < padL + plotW) {
      const ts = t0 + ((this.hover.x - padL) / plotW) * (t1 - t0);
      let best = p[0], bestD = Infinity;
      for (const pt of p) {
        const d = Math.abs(pt[0] - ts);
        if (d < bestD) { bestD = d; best = pt; }
      }
      const bx = x(best[0]);
      ctx.save();
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = t.axis;
      ctx.beginPath(); ctx.moveTo(bx, padT); ctx.lineTo(bx, padT + plotH); ctx.stroke();
      ctx.restore();
      const pnl = best[1] - this.baseline;
      if (this.tooltip) {
        this.tooltip.innerHTML =
          `<b>${fmtDay(best[0])} UTC</b>
           <span>equity <i>${fmtPrice(best[1])}</i></span>
           <span>pnl <i>${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}</i></span>`;
        this.tooltip.style.display = 'block';
        const box = this.canvas.getBoundingClientRect();
        const tw = this.tooltip.offsetWidth || 150;
        this.tooltip.style.left = `${Math.max(4, Math.min(bx + 12, box.width - tw - 6))}px`;
        this.tooltip.style.top = '8px';
      }
    }
  }
}

/* ------------------------------------------------------------------ */
/* Sparkline - used for cumulative delta (CVD).                        */
/* ------------------------------------------------------------------ */
export function sparkline(canvas, values, color) {
  const { ctx, w, h } = fitCanvas(canvas);
  const t = theme();
  if (!values || values.length < 2) return;
  let lo = Math.min(...values), hi = Math.max(...values);
  if (lo === hi) { lo -= 1; hi += 1; }
  const x = (i) => (i / (values.length - 1)) * (w - 4) + 2;
  const y = (v) => h - 3 - ((v - lo) / (hi - lo)) * (h - 6);
  // zero line, so the sign of cumulative delta is readable at a glance
  if (lo < 0 && hi > 0) {
    ctx.strokeStyle = t.grid;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, Math.round(y(0)) + 0.5); ctx.lineTo(w, Math.round(y(0)) + 0.5); ctx.stroke();
  }
  ctx.beginPath();
  values.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
  ctx.strokeStyle = color || t.series1;
  ctx.lineWidth = 2;
  ctx.lineJoin = 'round';
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(x(values.length - 1), y(values[values.length - 1]), 3.5, 0, Math.PI * 2);
  ctx.fillStyle = color || t.series1;
  ctx.fill();
}
