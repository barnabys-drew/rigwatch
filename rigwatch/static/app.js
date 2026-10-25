'use strict';
// rigwatch dashboard. One SSE feed (/api/stream) drives everything; longer ranges
// come from /api/history and refresh every minute. No libraries: SVG by hand.

const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const store = {
  get(k, d) { try { return localStorage.getItem('rigwatch.' + k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem('rigwatch.' + k, v); } catch { /* private window */ } },
};

const REST = {
  rest_wsl: { label: 'Rest of WSL', color: 'var(--rest-wsl)', note: 'Docker daemon, shells, kernel: Linux work no app claims' },
  wsl_cache: { label: 'WSL cache', color: 'var(--cache)', note: 'What the WSL VM holds on Windows beyond what Linux is using (page cache)' },
  rest_win: { label: 'Rest of PC', color: 'var(--rest-win)', note: 'Windows programs: browsers, Discord, games, the OS' },
};
const LIVE_SPAN = 15 * 60 * 1000;

const state = {
  snap: null,
  live: { t: [], cpu: {}, mem: {} },   // last 15 min from the stream
  hist: null,                           // the selected range
  range: store.get('range', '15m'),
  scope: store.get('scope', 'pc'),
  tables: { cpu: false, mem: false },
  hidden: new Set(),
  expanded: new Set(),
  pointer: {},                          // chart id -> last clientX while hovering
};

// ---------------------------------------------------------------- formatting

const fmtPct = v => v == null || isNaN(v) ? '–' : v <= 0.0001 ? '0%' : v < 0.1 ? '<0.1%' : v < 10 ? v.toFixed(1) + '%' : Math.round(v) + '%';
function fmtBytes(b, digits = 1) {
  if (!b || b <= 0) return '0 MB';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(4, Math.floor(Math.log(b) / Math.log(1024)));
  const v = b / 1024 ** i;
  return (i >= 3 ? v.toFixed(v >= 100 ? 0 : digits) : Math.round(v)) + ' ' + units[i];
}
const split = s => { const i = s.lastIndexOf(' '); return [s.slice(0, i), s.slice(i + 1)]; };
const fmtMbit = b => { const m = b * 8 / 1e6; return m >= 100 ? Math.round(m) + ' Mb/s' : m >= 1 ? m.toFixed(1) + ' Mb/s' : (m * 1000 < 1 ? '0' : Math.round(m * 1000)) + ' kb/s'; };
const fmtRate = b => b < 1024 ? '0 KB/s' : fmtBytes(b) + '/s';
function fmtDur(sec) {
  sec = Math.abs(sec);
  if (sec < 90) return Math.round(sec) + ' s';
  if (sec < 5400) return Math.round(sec / 60) + ' min';
  if (sec < 172800) return Math.round(sec / 3600) + ' h';
  return Math.round(sec / 86400) + ' d';
}
function fmtTime(ms, span) {
  const d = new Date(ms);
  if (span > 2 * 86400 * 1000) return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

// ---------------------------------------------------------------- series

function seriesDefs(metric) {
  const groups = (state.snap?.groups || []).map(g => ({ key: g.id, label: g.name, color: `var(--s${g.slot})`, app: true }));
  if (state.scope === 'apps') return groups;
  const rest = metric === 'cpu' ? ['rest_wsl', 'rest_win'] : ['rest_wsl', 'wsl_cache', 'rest_win'];
  if (!state.snap?.windows_ok) rest.splice(rest.indexOf('rest_win'), 1);
  return [...groups, ...rest.filter(k => metric === 'cpu' || k !== 'wsl_cache' || state.snap?.windows_ok)
    .map(k => ({ key: k, label: REST[k].label, color: REST[k].color, app: false }))];
}

function pushLive(snap) {
  const L = state.live;
  const cpu = { ...snap.pc.cpu, total: snap.pc.cpu_total, gpu: snap.win?.gpu?.util ?? 0 };
  const mem = snap.pc.mem;
  for (const [obj, buf] of [[cpu, L.cpu], [mem, L.mem]]) {
    for (const k of Object.keys(obj)) if (!buf[k]) buf[k] = new Array(L.t.length).fill(0);
    for (const k of Object.keys(buf)) buf[k].push(obj[k] ?? 0);
  }
  L.t.push(snap.t);
  const cut = snap.t - LIVE_SPAN;
  let drop = 0;
  while (drop < L.t.length && L.t[drop] < cut) drop++;
  if (drop) {
    L.t.splice(0, drop);
    for (const buf of [L.cpu, L.mem]) for (const k in buf) buf[k].splice(0, drop);
  }
}

function stats(buf, key) {
  const v = buf?.[key];
  if (!v || !v.length) return { avg: 0, peak: 0 };
  let s = 0, p = 0;
  for (const x of v) { s += x; if (x > p) p = x; }
  return { avg: s / v.length, peak: p };
}
function rangeStats(metric, key) {
  if (state.range === '15m' || !state.hist) return stats(state.live[metric], key);
  const h = state.hist;
  return metric === 'cpu' ? { avg: h.avg?.[key] ?? 0, peak: h.peak?.[key] ?? 0 }
    : { avg: h.mem_avg?.[key] ?? 0, peak: h.mem_peak?.[key] ?? 0 };
}
const viewData = () => state.range === '15m' ? state.live : state.hist;

// ---------------------------------------------------------------- small SVG pieces

function spark(values, color, h = 44, max) {
  const n = values.length;
  if (n < 2) return `<svg class="spark" viewBox="0 0 100 ${h}" preserveAspectRatio="none"></svg>`;
  const top = Math.max(max ?? 0, ...values) * 1.15 || 1;
  const pts = values.map((v, i) => [(i / (n - 1)) * 100, h - 2 - (v / top) * (h - 4)]);
  const line = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(2) + ' ' + p[1].toFixed(2)).join('');
  return `<svg class="spark" viewBox="0 0 100 ${h}" preserveAspectRatio="none" aria-hidden="true">
    <path d="${line}L100 ${h}L0 ${h}Z" style="fill:${color};opacity:.14"/>
    <path d="${line}" fill="none" style="stroke:${color}" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}

const ICON = {
  good: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1a7 7 0 1 0 0 14A7 7 0 0 0 8 1zm3.3 5.2-3.9 4.4a.8.8 0 0 1-1.2 0L4.7 9a.8.8 0 1 1 1.2-1.1l.9 1 3.3-3.8a.8.8 0 1 1 1.2 1.1z"/></svg>',
  warning: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8.9 1.6a1 1 0 0 0-1.8 0L.7 13a1 1 0 0 0 .9 1.5h12.8a1 1 0 0 0 .9-1.5zM8 5.2c.5 0 .8.4.8.8v3.6a.8.8 0 0 1-1.6 0V6c0-.4.4-.8.8-.8zm0 6.2a.9.9 0 1 1 0 1.8.9.9 0 0 1 0-1.8z"/></svg>',
  critical: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1a7 7 0 1 0 0 14A7 7 0 0 0 8 1zm2.9 8.8a.8.8 0 1 1-1.1 1.1L8 9.1l-1.8 1.8a.8.8 0 1 1-1.1-1.1L6.9 8 5.1 6.2a.8.8 0 1 1 1.1-1.1L8 6.9l1.8-1.8a.8.8 0 1 1 1.1 1.1L9.1 8z"/></svg>',
};
ICON.serious = ICON.warning;
const STATUS_TEXT = { good: 'Healthy', info: 'Healthy', warning: 'Warning', serious: 'Degraded', critical: 'Down' };
const pill = level => `<span class="pill ${level === 'info' ? 'good' : level}">${ICON[level] || ICON.good}${STATUS_TEXT[level]}</span>`;

// ---------------------------------------------------------------- stacked time chart

// round tick step (1, 2, 2.5, 5 x 10^k) so four gridlines land on readable values
function niceStep(raw) {
  if (raw <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(raw));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= raw) return m * p;
  return 10 * p;
}
const TIME_STEPS = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800].map(s => s * 1000);

function renderChart(id, metric) {
  const host = $('#chart-' + id);
  const data = viewData();
  const defs = seriesDefs(metric).filter(s => !state.hidden.has(s.key));
  const fmt = metric === 'cpu' ? fmtPct : v => fmtBytes(v);
  host.classList.remove('stale');

  if (state.tables[id]) {
    const all = seriesDefs(metric);
    const rows = all.map(s => {
      const st = rangeStats(metric, s.key);
      const now = state.snap?.pc[metric]?.[s.key] ?? 0;
      return `<tr><td><span class="key"><i class="sw" style="background:${s.color}"></i>${esc(s.label)}</span></td>
        <td class="n">${fmt(now)}</td><td class="n">${fmt(st.avg)}</td><td class="n">${fmt(st.peak)}</td></tr>`;
    }).join('');
    host.innerHTML = `<table class="data"><thead><tr><th>Series</th><th class="n">Now</th><th class="n">Average</th><th class="n">Peak</th></tr></thead><tbody>${rows}</tbody></table>`;
    return;
  }
  if (!data || data.t.length < 2 || !defs.length) {
    host.innerHTML = `<div class="empty">${!defs.length ? 'Every series is hidden. Pick one in the legend.' : state.range === '15m' ? 'Collecting the first samples…' : 'No history for this range yet. It fills in a minute at a time.'}</div>`;
    return;
  }

  const t = data.t, n = t.length;
  const W = Math.max(280, host.clientWidth), H = 260, AXB = 24, L = 48, R = 10, T = 10;
  const pw = W - L - R, ph = H - T;
  const t0 = t[0], t1 = t[n - 1], span = Math.max(1, t1 - t0);
  const cum = [];
  let prev = new Array(n).fill(0);
  for (const s of defs) {
    const vals = data[metric][s.key] || new Array(n).fill(0);
    const next = prev.map((p, i) => p + Math.max(0, vals[i] || 0));
    cum.push({ s, lo: prev, hi: next, vals });
    prev = next;
  }
  let top = Math.max(...prev);
  if (metric === 'cpu') top = Math.min(100, top);
  // memory ticks are stepped in GB, so they read 8 GB, 16 GB rather than 9.3 GB
  const unit = metric === 'cpu' ? 1 : top >= 2 ** 30 ? 2 ** 30 : 2 ** 20;
  const yMax = niceStep((top * 1.05) / unit / 4 || 0.25) * 4 * unit;
  const X = i => L + ((t[i] - t0) / span) * pw;
  const Y = v => T + ph - (Math.min(v, yMax) / yMax) * ph;
  const xs = t.map((_, i) => X(i));

  let grid = '';
  for (let k = 0; k <= 4; k++) {
    const v = (yMax / 4) * k, y = Y(v);
    grid += k ? `<line class="gridline" x1="${L}" x2="${W - R}" y1="${y}" y2="${y}"/>` : '';
    grid += `<text x="${L - 8}" y="${y + 4}" text-anchor="end">${metric === 'cpu' ? (k ? +v.toFixed(2) + '%' : '0') : k ? +(v / unit).toFixed(2) + (unit === 2 ** 30 ? ' GB' : ' MB') : '0'}</text>`;
  }
  const step = TIME_STEPS.find(s => span / s <= 6) || TIME_STEPS[TIME_STEPS.length - 1];
  let xt = '';
  for (let tick = Math.ceil(t0 / step) * step; tick <= t1; tick += step) {
    const x = L + ((tick - t0) / span) * pw;
    if (x < L + 20 || x > W - R - 20) continue;
    xt += `<text x="${x}" y="${H + 17}" text-anchor="middle">${fmtTime(tick, span)}</text>`;
  }

  let areas = '';
  for (const c of cum) {
    let d = 'M' + xs[0].toFixed(1) + ' ' + Y(c.hi[0]).toFixed(1);
    for (let i = 1; i < n; i++) d += 'L' + xs[i].toFixed(1) + ' ' + Y(c.hi[i]).toFixed(1);
    let edge = d;
    for (let i = n - 1; i >= 0; i--) d += 'L' + xs[i].toFixed(1) + ' ' + Y(c.lo[i]).toFixed(1);
    areas += `<path d="${d}Z" style="fill:${c.s.color}"/>`;
    areas += `<path d="${edge}" fill="none" style="stroke:var(--surface)" stroke-width="1.5" stroke-linejoin="round"/>`;
  }

  const label = `${metric === 'cpu' ? 'CPU' : 'Memory'} over the last ${state.range}, stacked by app. Use the arrow keys to read values.`;
  host.innerHTML = `<svg viewBox="0 0 ${W} ${H + AXB}" height="${H + AXB}" tabindex="0" role="img" aria-label="${esc(label)}">
    <g class="axis">${grid}${xt}</g>
    <g>${areas}</g>
    <line class="baseline" x1="${L}" x2="${W - R}" y1="${T + ph}" y2="${T + ph}"/>
    <g class="hover" visibility="hidden"><line class="cross" y1="${T}" y2="${T + ph}"/><g class="dots"></g></g>
    <rect x="${L}" y="${T}" width="${pw}" height="${ph}" fill="transparent" class="hit"/>
  </svg>`;
  host._chart = { metric, xs, t, cum, Y, fmt, span };
  const svg = host.firstElementChild;
  const move = clientX => {
    const r = svg.getBoundingClientRect();
    const x = (clientX - r.left) * (W / r.width);
    let lo = 0, hi = n - 1;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (xs[mid] < x) lo = mid; else hi = mid; }
    showHover(host, Math.abs(xs[lo] - x) < Math.abs(xs[hi] - x) ? lo : hi);
  };
  svg.addEventListener('pointermove', e => { state.pointer[id] = e.clientX; move(e.clientX); });
  svg.addEventListener('pointerleave', () => { delete state.pointer[id]; hideHover(host); });
  svg.addEventListener('keydown', e => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End', 'Escape'].includes(e.key)) return;
    e.preventDefault();
    if (e.key === 'Escape') return hideHover(host);
    let i = host._idx ?? n - 1;
    i = e.key === 'Home' ? 0 : e.key === 'End' ? n - 1 : i + (e.key === 'ArrowLeft' ? -1 : 1) * Math.max(1, Math.round(n / 60));
    showHover(host, Math.max(0, Math.min(n - 1, i)), true);
  });
  svg.addEventListener('blur', () => hideHover(host));
  if (state.pointer[id] != null) move(state.pointer[id]);
  else if (host._kbd && host._idx != null) showHover(host, Math.min(n - 1, host._idx), true);
}

function showHover(host, i, kbd = false) {
  const c = host._chart;
  if (!c) return;
  host._idx = i;
  host._kbd = kbd;
  const g = host.querySelector('.hover');
  const x = c.xs[i];
  g.setAttribute('visibility', 'visible');
  g.querySelector('line').setAttribute('x1', x);
  g.querySelector('line').setAttribute('x2', x);
  g.querySelector('.dots').innerHTML = c.cum.filter(k => k.vals[i] > 0).map(k =>
    `<circle cx="${x}" cy="${c.Y(k.hi[i])}" r="4" style="fill:${k.s.color};stroke:var(--surface)" stroke-width="2"/>`).join('');
  let total = 0;
  const rows = c.cum.slice().reverse().map(k => {
    total += k.vals[i] || 0;
    return `<div class="row"><i class="sw" style="background:${k.s.color}"></i><span>${esc(k.s.label)}</span><b>${c.fmt(k.vals[i] || 0)}</b></div>`;
  }).join('');
  const tip = $('#tip');
  tip.innerHTML = `<div class="tt">${new Date(c.t[i]).toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit', second: c.span < 3600e3 ? '2-digit' : undefined })}</div>${rows}
    <div class="row total"><i class="sw"></i><span>Total shown</span><b>${c.fmt(total)}</b></div>`;
  tip.hidden = false;
  const svgR = host.firstElementChild.getBoundingClientRect();
  const sx = svgR.left + (x / host.firstElementChild.viewBox.baseVal.width) * svgR.width;
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  let left = sx + 16;
  if (left + tw > window.innerWidth - 8) left = sx - tw - 16;
  tip.style.left = Math.max(8, left) + 'px';
  tip.style.top = Math.max(8, Math.min(window.innerHeight - th - 8, svgR.top + 10)) + 'px';
}
function hideHover(host) {
  host._idx = null;
  host._kbd = false;
  host.querySelector('.hover')?.setAttribute('visibility', 'hidden');
  $('#tip').hidden = true;
}

function renderLegend(id, metric) {
  const el = $('#legend-' + id);
  const now = state.snap?.pc[metric] || {};
  const fmt = metric === 'cpu' ? fmtPct : v => fmtBytes(v);
  el.innerHTML = seriesDefs(metric).map(s =>
    `<button class="key" type="button" data-k="${s.key}" aria-pressed="${!state.hidden.has(s.key)}" title="${esc(REST[s.key]?.note || 'Click to show or hide')}">
      <i class="sw" style="background:${s.color}"></i>${esc(s.label)} <b>${fmt(now[s.key] ?? 0)}</b></button>`).join('');
}

// ---------------------------------------------------------------- hero share bars

function renderShare(metric) {
  const el = $('#share-' + metric);
  const s = state.snap;
  const pc = s.pc;
  const total = metric === 'cpu' ? 100 : pc.mem_total;
  const vals = pc[metric];
  const fmt = metric === 'cpu' ? fmtPct : v => fmtBytes(v);
  const apps = metric === 'cpu' ? pc.apps_cpu : pc.apps_mem;
  const restKeys = metric === 'cpu' ? ['rest_wsl', 'rest_win'] : ['rest_wsl', 'wsl_cache', 'rest_win'];
  const rest = restKeys.reduce((a, k) => a + (vals[k] || 0), 0);
  const used = metric === 'cpu' ? pc.cpu_total : apps + rest;
  const segs = [
    ...s.groups.map(g => ({ key: g.id, label: g.name, color: `var(--s${g.slot})`, v: vals[g.id] || 0 })),
    ...restKeys.filter(k => k in vals).map(k => ({ key: k, label: REST[k].label, color: REST[k].color, v: vals[k] || 0 })),
  ];
  const free = Math.max(0, total - used);
  const [bigV, bigU] = metric === 'cpu' ? [fmtPct(apps), ''] : split(fmtBytes(apps));
  const share = used > 0 ? Math.round((apps / used) * 100) : 0;
  el.innerHTML = `
    <div class="eyebrow">${metric === 'cpu' ? 'CPU' : 'Memory'} · whole PC</div>
    <div class="headline">
      <span class="big">${bigV}${bigU ? `<small style="font-size:20px;color:var(--muted);font-weight:600"> ${bigU}</small>` : ''}</span>
      <span class="vs">your apps, <b>${share}%</b> of what's in use.<br>The rest of the PC: <b>${fmt(rest)}</b> · ${metric === 'cpu' ? 'idle' : 'free'} <b>${fmt(free)}</b></span>
    </div>
    <div class="bar" role="img" aria-label="${esc(segs.filter(x => x.v > 0).map(x => `${x.label} ${fmt(x.v)}`).join(', '))}">
      ${segs.map(x => `<span data-k="${x.key}" data-l="${esc(x.label)}" data-v="${esc(fmt(x.v))}" style="flex-basis:${(x.v / total) * 100}%;${x.v > 0 ? 'min-width:3px;' : ''}background:${x.color}"></span>`).join('')}
    </div>
    <div class="scale"><span>0</span><span>${metric === 'cpu' ? '25%' : fmtBytes(total / 4, 0)}</span><span>${metric === 'cpu' ? '50%' : fmtBytes(total / 2, 0)}</span><span>${metric === 'cpu' ? '75%' : fmtBytes(total * 3 / 4, 0)}</span><span>${metric === 'cpu' ? '100%' : fmtBytes(total, 0)}</span></div>
    <div class="keys">${segs.filter(x => x.v > 0 || !REST[x.key]).sort((a, b) => b.v - a.v).slice(0, 7).map(x =>
      `<span class="key"><i class="sw" style="background:${x.color}"></i>${esc(x.label)} <b>${fmt(x.v)}</b></span>`).join('')}</div>`;
  el.querySelectorAll('.bar span').forEach(sp => {
    sp.addEventListener('pointermove', e => {
      const tip = $('#tip');
      tip.innerHTML = `<div class="row"><i class="sw" style="background:${sp.style.background}"></i><span>${sp.dataset.l}</span><b>${sp.dataset.v}</b></div>
        ${REST[sp.dataset.k] ? `<div class="tt" style="margin:6px 0 0">${esc(REST[sp.dataset.k].note)}</div>` : ''}`;
      tip.hidden = false;
      tip.style.left = Math.min(window.innerWidth - tip.offsetWidth - 8, e.clientX + 14) + 'px';
      tip.style.top = (e.clientY + 16) + 'px';
    });
    sp.addEventListener('pointerleave', () => { $('#tip').hidden = true; });
  });
}

// ---------------------------------------------------------------- tiles

function renderTiles() {
  const s = state.snap, w = s.win;
  const tiles = [];
  tiles.push(`<article class="card tile"><div class="label">CPU, whole PC</div>
    <div class="value">${fmtPct(s.pc.cpu_total)}</div>
    <div class="foot">${esc((s.host.cpu_name || '').replace(/\s+\d+-Core Processor/, '').replace('AMD ', '')) || s.host.ncpu + ' threads'}</div>
    ${spark(state.live.cpu.total || [], 'var(--s1)', 28, 10)}</article>`);
  const mt = s.pc.mem_total, mu = w ? w.mem_used : s.wsl.mem_used;
  const [mv, mu2] = split(fmtBytes(mu));
  tiles.push(`<article class="card tile"><div class="label">Memory in use</div>
    <div class="value">${mv}<small>${mu2} / ${fmtBytes(mt, 0)}</small></div>
    <div class="meter"><i style="width:${(mu / mt) * 100}%"></i></div>
    <div class="foot">${Math.round((mu / mt) * 100)}% used</div></article>`);
  if (w?.gpu) {
    const g = w.gpu;
    tiles.push(`<article class="card tile"><div class="label">GPU</div>
      <div class="value">${fmtPct(g.util)}</div>
      <div class="meter"><i style="width:${Math.min(100, g.util)}%;background:var(--s7)"></i></div>
      <div class="foot">VRAM ${fmtBytes(g.vram_used)} / ${fmtBytes(w.vram_total, 0)}</div></article>`);
  } else {
    tiles.push(`<article class="card tile"><div class="label">GPU</div><div class="value">–</div><div class="foot">${w ? 'reading counters…' : 'needs the Windows sampler'}</div></article>`);
  }
  const up = (w?.net || []).filter(n => !n.app);
  const rx = up.reduce((a, n) => a + n.rx, 0), tx = up.reduce((a, n) => a + n.tx, 0);
  tiles.push(`<article class="card tile"><div class="label">Internet (uplink)</div>
    <div class="value">${w ? fmtMbit(rx) : fmtMbit(s.wsl.net)}</div>
    <div class="foot">${w ? `↓ ${fmtMbit(rx)} · ↑ ${fmtMbit(tx)}` : 'WSL interfaces only'}</div></article>`);
  const vm = w ? w.vmmem_ws : s.wsl.mem_used;
  const [vv, vu] = split(fmtBytes(vm));
  tiles.push(`<article class="card tile"><div class="label">WSL VM footprint</div>
    <div class="value">${vv}<small>${vu}</small></div>
    <div class="foot">Linux using ${fmtBytes(s.wsl.mem_used)} of ${fmtBytes(s.wsl.mem_total, 0)}</div></article>`);
  const issues = s.groups.flatMap(g => g.issues.map(i => ({ ...i, app: g.name })));
  const worst = issues.reduce((a, i) => ({ good: 0, warning: 1, serious: 2, critical: 3 }[i.level] > { good: 0, warning: 1, serious: 2, critical: 3 }[a] ? i.level : a), 'good');
  tiles.push(`<article class="card tile"><div class="label">Health</div>
    <div class="value">${issues.length ? issues.length + `<small>${issues.length === 1 ? 'issue' : 'issues'}</small>` : 'All good'}</div>
    <div class="foot">${issues.length ? pill(worst) + ' ' + esc(issues[0].app) : `${s.groups.length} apps checked`}</div></article>`);
  $('#tiles').innerHTML = tiles.join('');
}

// ---------------------------------------------------------------- app cards

function renderApps() {
  const s = state.snap;
  const sumCpu = s.groups.reduce((a, g) => a + g.cpu, 0);
  $('#apps-sub').textContent = `${s.groups.length} groups · ${fmtPct(sumCpu)} CPU and ${fmtBytes(s.pc.apps_mem)} together · sampled every 2 s from cgroups`;
  const label = { '15m': '15 min', '1h': '1 h', '6h': '6 h', '24h': '24 h', '7d': '7 d', '30d': '30 d' }[state.range];
  $('#apps').innerHTML = s.groups.map(g => {
    const st = rangeStats('cpu', g.id);
    const [mv, mu] = split(fmtBytes(g.mem));
    const open = state.expanded.has(g.id);
    const parts = open ? g.parts : g.parts.slice(0, 5);
    const kind = { unit: 'unit', sys: 'system', ctr: 'docker', proc: 'process', win: 'Windows' };
    const timers = g.timers.filter(t => t.next).map(t => `<span><svg class="clock" viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="4.8"/><path d="M6 3.4V6l1.8 1.2"/></svg>${esc(t.name.replace('.timer', ''))} <b>in ${fmtDur(t.next - Date.now() / 1000)}</b></span>`);
    return `<article class="card app" style="--c:var(--s${g.slot})">
      <div class="app-head">
        <div><div class="app-name"><i class="sw" style="background:var(--s${g.slot})"></i>${esc(g.name)}</div><div class="app-blurb">${esc(g.blurb)}</div></div>
        ${pill(g.status)}
      </div>
      <div class="nums">
        <div class="num"><div class="k">CPU now</div><div class="v">${fmtPct(g.cpu)}</div></div>
        <div class="num"><div class="k">Memory</div><div class="v">${mv}<small>${mu}</small></div></div>
        <div class="num"><div class="k">Peak ${label}</div><div class="v">${fmtPct(st.peak)}</div></div>
      </div>
      ${spark(state.live.cpu[g.id] || [], `var(--s${g.slot})`, 44, 0.5)}
      ${g.issues.length ? `<div class="issues">${g.issues.map(i => `<div><span style="fill:var(--${i.level})">${ICON[i.level]}</span><span>${esc(i.text)}</span></div>`).join('')}</div>` : ''}
      <ul class="parts">${parts.map(p => `<li title="${esc(p.state || '')}">
          <span class="dot ${p.level}"></span>
          <span class="nm">${esc(p.label)}<em>${kind[p.kind] || ''}</em></span>
          <span class="c">${fmtPct(p.cpu)}</span><span class="m">${p.mem ? fmtBytes(p.mem) : '–'}</span></li>`).join('') || '<li><span></span><span class="nm">Nothing running</span></li>'}</ul>
      ${g.parts.length > 5 ? `<button class="more" type="button" data-g="${g.id}">${open ? 'Show fewer' : `Show all ${g.parts.length}`}</button>` : ''}
      <div class="meta">
        <span><b>${g.pids}</b> processes</span>
        ${g.io > 1024 ? `<span>disk <b>${fmtRate(g.io)}</b></span>` : ''}
        ${g.nic ? `<span>mirror port <b>${fmtMbit(g.nic.rx)}</b></span>` : ''}
        <span>avg ${label} <b>${fmtPct(st.avg)}</b></span>
        ${timers.join('')}
      </div>
    </article>`;
  }).join('');
}

// ---------------------------------------------------------------- the rest of the PC

function renderWindows() {
  const s = state.snap, w = s.win;
  if (!w) {
    const msg = `<h2>Windows</h2><p class="sub">${esc(s.windows_error || 'Windows data unavailable')}</p>`;
    $('#winprocs').innerHTML = msg;
    $('#gpu').innerHTML = '<h2>GPU</h2><p class="sub">needs the Windows sampler</p>';
    $('#net').innerHTML = '<h2>Network</h2><p class="sub">needs the Windows sampler</p>';
    return;
  }
  $('#pc-sub').textContent = `Windows programs outside WSL and outside your apps: ${fmtPct(s.pc.cpu.rest_win)} CPU, ${fmtBytes(s.pc.mem.rest_win)} memory`;
  const procs = w.procs.slice().sort((a, b) => b.cpu - a.cpu || b.ws - a.ws).slice(0, 14);
  const maxC = Math.max(0.5, ...procs.map(p => p.cpu));
  $('#winprocs').innerHTML = `
    <div class="card-head"><div><h2>Top Windows programs</h2><p class="sub">${w.nproc} programs running · grouped by name</p></div></div>
    <table class="data"><thead><tr><th>Program</th><th class="n">CPU</th><th class="n">Memory</th></tr></thead><tbody>
    <tr><td><span class="key"><i class="sw" style="background:var(--rest-wsl)"></i>WSL VM <span class="tag">vmmemWSL, all of your Linux side</span></span></td>
      <td class="n">${fmtPct(s.wsl.cpu)}</td><td class="n">${fmtBytes(w.vmmem_ws)}</td></tr>
    ${procs.map(p => `<tr><td>${esc(p.name)}${p.n > 1 ? `<span class="tag">×${p.n}</span>` : ''}</td>
      <td class="n"><span class="procbar"><i style="width:${Math.max(2, (p.cpu / maxC) * 70)}px;background:var(--rest-win);box-shadow:0 0 0 1px var(--axis) inset"></i>${fmtPct(p.cpu)}</span></td>
      <td class="n">${fmtBytes(p.ws)}</td></tr>`).join('')}
    </tbody></table>`;

  const g = w.gpu;
  if (g) {
    const r = 40, C = 2 * Math.PI * r, u = Math.min(100, g.util);
    $('#gpu').innerHTML = `<div class="card-head"><div><h2>GPU</h2><p class="sub">${esc(s.host.gpu_name)}</p></div></div>
      <div class="gpu-top">
        <svg class="ring" viewBox="0 0 96 96" role="img" aria-label="GPU ${fmtPct(u)} busy">
          <circle cx="48" cy="48" r="${r}" fill="none" style="stroke:var(--track)" stroke-width="8"/>
          <circle cx="48" cy="48" r="${r}" fill="none" style="stroke:var(--s7);transition:stroke-dasharray .8s" stroke-width="8" stroke-linecap="round"
            stroke-dasharray="${(u / 100) * C} ${C}" transform="rotate(-90 48 48)"/>
          <text x="48" y="50" text-anchor="middle">${fmtPct(u)}</text><text class="lbl" x="48" y="65" text-anchor="middle">busiest engine</text>
        </svg>
        <div style="flex:1;min-width:0">
          <div class="k" style="color:var(--muted);font-size:12px">Video memory</div>
          <div style="font-size:20px;font-weight:680">${fmtBytes(g.vram_used)} <small style="color:var(--muted);font-size:12px;font-weight:500">/ ${fmtBytes(w.vram_total, 0)}</small></div>
          <div class="meter"><i style="width:${(g.vram_used / (w.vram_total || 1)) * 100}%;background:var(--s7)"></i></div>
        </div>
      </div>
      <ul class="mini">${(g.procs || []).slice(0, 5).map(p => `<li><span>${esc(p.name)}<span class="tag">${esc(p.type)}</span></span><b>${fmtPct(p.util)}</b></li>`).join('') || '<li><span>No program is using the GPU</span></li>'}</ul>`;
  }

  $('#net').innerHTML = `<div class="card-head"><div><h2>Network</h2><p class="sub">Windows adapters + WSL</p></div></div>
    <ul class="mini">${w.net.map(n => `<li><span>${esc(n.name)}<span class="tag">${esc(n.app ? n.app + ' mirror port' : n.desc)}</span></span><b>↓ ${fmtMbit(n.rx)} · ↑ ${fmtMbit(n.tx)}</b></li>`).join('')}
    <li><span>WSL<span class="tag">Linux side, all interfaces</span></span><b>${fmtMbit(s.wsl.net)}</b></li></ul>`;
}

function renderHeader() {
  const s = state.snap;
  const m = s.machine || { name: '', peers: [] };
  document.title = m.name ? `rigwatch · ${m.name}` : 'rigwatch';
  const nav = $('#machines');
  nav.hidden = !m.peers.length;
  if (m.peers.length) {
    nav.innerHTML = `<span aria-current="page">${esc(m.name)}</span>` +
      m.peers.map(p => `<a href="${esc(p.url)}">${esc(p.name)}</a>`).join('');
  }
  const host = [s.host.cpu_name?.replace(/\s+\d+-Core Processor/, ''), s.host.gpu_name, fmtBytes(s.pc.mem_total, 0) + ' RAM'].filter(Boolean);
  $('#host').textContent = host.join(' · ');
  const rw = s.groups.find(g => g.id === 'rigwatch');
  $('#foot').innerHTML = `rigwatch itself costs <b>${fmtPct(rw?.cpu)}</b> CPU and <b>${fmtBytes(rw?.mem)}</b> · up ${fmtDur(s.uptime || 0)} · samples every 2 s · ${s.windows_ok ? 'Windows + WSL' : 'WSL only: ' + esc(s.windows_error)}`;
  $('#cpu-sub').textContent = state.scope === 'apps' ? 'just your apps · percent of the whole CPU' : `percent of all ${s.host.ncpu} threads · your apps stacked at the bottom`;
  $('#mem-sub').textContent = state.scope === 'apps' ? 'just your apps' : `of ${fmtBytes(s.pc.mem_total, 0)} · your apps stacked at the bottom`;
}

// ---------------------------------------------------------------- render + wiring

let queued = false;
function render() {
  if (queued) return;
  queued = true;
  requestAnimationFrame(() => {
    queued = false;
    if (!state.snap || document.hidden) return;
    renderHeader();
    renderShare('cpu');
    renderShare('mem');
    renderTiles();
    renderChart('cpu', 'cpu');
    renderChart('mem', 'mem');
    renderLegend('cpu', 'cpu');
    renderLegend('mem', 'mem');
    renderApps();
    renderWindows();
  });
}

async function loadHistory() {
  if (state.range === '15m') { state.hist = null; return render(); }
  $('#chart-cpu').classList.add('stale');
  $('#chart-mem').classList.add('stale');
  try {
    const r = await fetch('api/history?range=' + state.range);
    state.hist = await r.json();
  } catch { /* keep the previous render */ }
  render();
}

function setSeg(id, v) {
  document.querySelectorAll(`#${id} button`).forEach(b => b.setAttribute('aria-checked', String(b.dataset.v === v)));
}

function setLive(on, text) {
  const el = $('#live');
  el.className = 'live ' + (on ? 'on' : 'off');
  el.lastElementChild.textContent = text;
}

function connect() {
  const es = new EventSource('api/stream');
  es.onmessage = e => {
    const snap = JSON.parse(e.data);
    state.snap = snap;
    pushLive(snap);
    setLive(true, 'live · 2 s');
    render();
  };
  es.onerror = () => setLive(false, 'reconnecting…');
}

function applyTheme(t) {
  if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
}

async function init() {
  applyTheme(store.get('theme', ''));
  $('#theme').addEventListener('click', () => {
    const dark = matchMedia('(prefers-color-scheme: dark)').matches;
    const cur = document.documentElement.dataset.theme || (dark ? 'dark' : 'light');
    const next = cur === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    store.set('theme', next);
    render();
  });
  setSeg('range', state.range);
  setSeg('scope', state.scope);
  $('#range').addEventListener('click', e => {
    const v = e.target.closest('button')?.dataset.v;
    if (!v) return;
    state.range = v; store.set('range', v); setSeg('range', v); loadHistory();
  });
  $('#scope').addEventListener('click', e => {
    const v = e.target.closest('button')?.dataset.v;
    if (!v) return;
    state.scope = v; store.set('scope', v); setSeg('scope', v); render();
  });
  document.querySelectorAll('[data-table]').forEach(b => b.addEventListener('click', () => {
    const k = b.dataset.table;
    state.tables[k] = !state.tables[k];
    b.setAttribute('aria-pressed', String(state.tables[k]));
    b.textContent = state.tables[k] ? 'Chart' : 'Table';
    render();
  }));
  document.addEventListener('click', e => {
    const key = e.target.closest('.legend .key');
    if (key) {
      const k = key.dataset.k;
      state.hidden.has(k) ? state.hidden.delete(k) : state.hidden.add(k);
      return render();
    }
    const more = e.target.closest('.more');
    if (more) {
      const g = more.dataset.g;
      state.expanded.has(g) ? state.expanded.delete(g) : state.expanded.add(g);
      render();
    }
  });
  let rt;
  addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(render, 120); });
  document.addEventListener('visibilitychange', render);
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', render);

  try {
    const [now, h] = await Promise.all([fetch('api/now').then(r => r.json()), fetch('api/history?range=15m').then(r => r.json())]);
    if (h?.t?.length) state.live = { t: h.t, cpu: h.cpu, mem: h.mem };
    if (!now.warming_up) state.snap = now;
  } catch { setLive(false, 'offline'); }
  await loadHistory();
  connect();
  setInterval(() => { if (state.range !== '15m' && !document.hidden) loadHistory(); }, 60000);
}

init();
