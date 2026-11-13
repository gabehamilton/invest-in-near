/* NEAR, tested: the desk simulation, live. The twin of pipeline/models/portfolio.py on the same doubles
 * (site/data/sim.json); tests/test_sim_parity.py holds them equal. No figure is typed here. */
(function (root, make) {
  const api = make();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else { root.NearSim = api; api.boot(); }
})(typeof self !== 'undefined' ? self : this, function () {
'use strict';

/* prng: xoshiro128** seeded by splitmix32, with the published constants */
const P32 = 0x100000000, rotl = (x, k) => (x << k) | (x >>> (32 - k));
function seedState(seed) {
  const s = new Uint32Array(4);
  let a = seed >>> 0;
  for (let i = 0; i < 4; i++) {
    a = (a + 0x9e3779b9) >>> 0;
    let t = a ^ (a >>> 16);
    t = Math.imul(t, 0x21f0aaad); t ^= t >>> 15;
    t = Math.imul(t, 0x735a2d97); t ^= t >>> 15;
    s[i] = t;
  }
  return s;
}
function next(s) {
  const r = Math.imul(rotl(Math.imul(s[1], 5), 7), 9) >>> 0, t = s[1] << 9;
  s[2] ^= s[0]; s[3] ^= s[1]; s[1] ^= s[2]; s[0] ^= s[3]; s[2] ^= t; s[3] = rotl(s[3], 11);
  return r;
}
/* /prng */
function prng(seed) { const s = seedState(seed); return () => next(s); }

function mean(xs) { let s = 0; for (let i = 0; i < xs.length; i++) s += xs[i]; return s / xs.length; }
function q7(S, p) {
  const n = S.length, h = (n - 1) * p, f = Math.floor(h);
  return f + 1 >= n ? S[n - 1] : S[f] + (h - f) * (S[f + 1] - S[f]);
}

// F-sim-path: one u32 a day for every window, path-major, day-minor; G and X moment-matched
function simulate(P) {
  const W = P.windows, N = P.paths, nw = W.length, s = seedState(P.seed);
  const out = W.map(() => ({ G: new Float64Array(N), X: new Float64Array(N), H: new Uint8Array(N) }));
  const g = new Float64Array(nw), x = new Float64Array(nw), h = new Uint8Array(nw);
  for (let i = 0; i < N; i++) {
    g.fill(1); x.fill(1); h.fill(0);
    for (let day = 0; day < P.horizon; day++) {
      const r = next(s);
      for (let w = 0; w < nw; w++) {
        const j = Math.floor(r * W[w].a.length / P32);
        g[w] *= W[w].a[j];
        x[w] *= W[w].b[j];
        if (g[w] <= P.half) h[w] = 1;
      }
    }
    for (let w = 0; w < nw; w++) { out[w].G[i] = g[w]; out[w].X[i] = x[w]; out[w].H[i] = h[w]; }
  }
  return out.map((o) => {
    const mg = mean(o.G), mx = mean(o.X);
    for (let i = 0; i < N; i++) { o.G[i] = o.G[i] / mg; o.X[i] = o.X[i] / mx; }
    o.Gs = Float64Array.from(o.G).sort();
    return o;
  });
}

function legs(st, R) {
  const ru = { effr: R.RU, dgs1: R.RT, ffcomp: R.RF }[st.usd];
  return [ru, st.eur === 'parity' ? 1 + ru : 1 + R.rE, { y: R.y, y90: R.y90, ys60: R.ys60 }[st.y]];
}
// portfolio.closed_form
function closed(st, R, V) {
  const [ru, eurF, y] = legs(st, R), ky = st.k * y, nearF = (1 + st.mu) * (1 + ky);
  const rbar = st.s * ru + (1 - st.s) * (eurF - 1), part = st.w * V;
  return {
    nearF, part, mean: part * (nearF - 1 - rbar - st.c), carry: part * (ky - rbar),
    euro: part * ((1 - st.s) * (ru - (eurF - 1))), price: part * (st.mu * (1 + ky)), cost: part * (0 - st.c),
    beMean: (1 + rbar + st.c) / (1 + ky) - 1, beCost: nearF - 1 - rbar, interest: V * rbar,
  };
}
// portfolio.units: F-sim-unit and per-path break-even drifts, sorted
function units(sim, st, R) {
  const [ru, eurF, y] = legs(st, R), ky = st.k * y, nearF = (1 + st.mu) * (1 + ky);
  const sRu = st.s * ru, oms = 1 - st.s, c = st.c, G = sim.G, X = sim.X, n = G.length;
  const u = new Float64Array(n), m = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const rb = sRu + oms * (eurF * X[i] - 1);
    u[i] = nearF * G[i] - 1 - rb - c;
    m[i] = (1 + rb + c) / ((1 + ky) * G[i]) - 1;
  }
  return [u.sort(), m.sort()];
}
function distribution(S, M, T) {
  const n = S.length, worst = Math.floor(n / T.es_every), deep = Math.floor(n / T.es_deep_every), dots = [];
  for (let k = 1; k <= T.dots; k++) dots.push(S[Math.floor((2 * k - 1) * n / (2 * T.dots))]);
  let behind = 0, ahead = 0, sw = 0, sd = 0;
  while (behind < n && S[behind] <= 0) behind++;
  for (const v of dots) if (v > 0) ahead++;
  for (let i = 0; i < worst; i++) sw += S[i];
  for (let i = 0; i < deep; i++) sd += S[i];
  return {
    p05: q7(S, T.lo), median: q7(S, T.mid), p95: q7(S, T.hi), es95: 0 - sw / worst, es975: 0 - sd / deep,
    pBeat: T.pct * (n - behind) / n, dots, ahead, beMedian: q7(M, T.mid),
  };
}

// the registry's display rules: portfolio.shown and display, numbers.fmt
function roundDp(v, dp, ten) {
  let k = 1;
  for (let i = 0; i < dp; i++) k *= ten;
  const n = Math.floor(Math.abs(v) * k + 1 / 2), out = (v < 0 ? -n : n) / k;
  return out === 0 ? 0 : out;
}
function roundStep(v, step) {
  const n = Math.floor(Math.abs(v) / step + 1 / 2), out = (v < 0 ? -n : n) * step;
  return out === 0 ? 0 : out;
}
function makeFmt(F) {
  const ten = F.ten, G = F.group;
  function fixed(v, dp) {   // Python's format: an exact half-way value rounds to even
    const a = Math.abs(v), e = a.toFixed(dp + F.tie), p = e.indexOf('.') + (dp && dp + 1);
    const t = /^\.?50*$/.test(e.slice(p)) && !(e[p - 1] & 1) ? e.slice(0, p) : a.toFixed(dp);
    const i = t.indexOf('.'), int = i < 0 ? t : t.slice(0, i);
    let g = '';
    for (let k = int.length; k > 0; k -= G) g = int.slice(Math.max(0, k - G), k) + (g ? ',' + g : '');
    return (v < 0 ? '-' : '') + g + (i < 0 ? '' : t.slice(i));
  }
  function fmt(v, unit, dp, abs) {
    if (abs) v = Math.abs(v);
    if (unit === 'USD') {
      const sign = v < 0 ? '-' : '', a = Math.abs(v);
      for (const [sc, word] of F.scales) {
        const m = a / sc;
        if (a >= sc) return sign + '$' + fixed(m, dp != null ? dp : m < ten ? 2 : m < ten * ten ? 1 : 0) + ' ' + word;
      }
      return sign + '$' + fixed(a, dp != null ? dp : 0);
    }
    if (unit === '%') {
      const t = fixed(v, dp != null ? dp : Math.abs(v) < ten ? 2 : 1);
      return (/^-?[0.,]*$/.test(t) ? t.replace(/^-/, '') : t) + '%';
    }
    const t = fixed(v, dp != null ? dp : Number.isInteger(v) ? 0 : 1);
    return unit === 'days' ? (t === '1' ? t + ' day' : t + ' days') : t;
  }
  function shown(v, [kind, arg]) {
    if (kind === 'usd') {
      const r = roundStep(v, arg);
      return [r, Math.abs(r) >= F.scales[F.scales.length - 1][0] ? F.usd_dp.find(([lo]) => arg >= lo)[1] : null];
    }
    return kind === 'pctstep' || kind === 'countstep' ? [roundStep(v, arg), 0] : [roundDp(v, arg, ten), arg];
  }
  const display = (v, spec, abs) => { const [r, dp] = shown(v, spec); return fmt(r, F.units[spec[0]], dp, abs); };
  return { fmt, shown, display };
}
const direction = (v) => (v > 0 ? 'ahead' : v < 0 ? 'behind' : 'level');
const rise = (v) => (v > 0 ? 'rise' : v < 0 ? 'fall' : 'level');
const CONTROLS = ['w', 's', 'mu', 'k', 'win', 'venue', 'p', 'rt', 'cu'];
const DYN = { 'usd*': ['usd', 'usd'], 'pct*': ['pctstep', 'pp'], 'count*': ['countstep', 'count'] };

// portfolio.outcomes, state_of and modes
function outcomes(sims, P, ctrl) {
  const pct = P.fmt.pct, T = Object.assign({ pct }, P.tail), R = P.rates, V = P.V, L = P.liq;
  const st = { w: ctrl.w, s: ctrl.s, mu: ctrl.mu === 'own' ? ctrl.own : P.mu[ctrl.mu], k: ctrl.k,
    c: (ctrl.rt + ctrl.cu) / P.bp, p: ctrl.p, win: ctrl.win, venue: ctrl.venue, usd: 'effr', eur: 'flat', y: 'y' };
  const sim = sims[st.win], cf = closed(st, R, V), [S, M] = units(sim, st, R);
  const dist = distribution(S, M, T), part = cf.part, day = P.windows[st.win].day;
  const vol = st.venue === 'all' ? L.cb + L.bn : L.cb, volShort = st.venue === 'all' ? L.cbs + L.bns : L.cbs;
  let half = 0;
  for (let i = 0; i < sim.H.length; i++) half += sim.H[i];
  const raw = {
    sim_share: pct * st.w, sim_near_usd: part, sim_cash_interest_usd: cf.interest,
    sim_split_usd: pct * st.s, sim_split_eur: pct * (1 - st.s), sim_mu: pct * st.mu, sim_participation: pct * st.p,
    sim_mean_usd: cf.mean, sim_carry_usd: cf.carry, sim_carry_euro_usd: cf.euro, sim_price_usd: cf.price,
    sim_cost_usd: cf.cost, sim_median_usd: part * dist.median, sim_p05_usd: part * dist.p05,
    sim_p95_usd: part * dist.p95, sim_es95_usd: part * dist.es95,
    sim_median_price: pct * ((1 + st.mu) * q7(sim.Gs, T.mid) - 1),
    sim_p_beat_n: dist.ahead, sim_p_behind_n: T.dots - dist.ahead, sim_p_beat_r: dist.pBeat,
    sim_p_half: pct * half / S.length, sim_be_mu_mean: pct * cf.beMean, sim_be_mu_median: pct * dist.beMedian,
    sim_be_cost: pct * cf.beCost, sim_es_interest_years: cf.interest > 0 ? part * dist.es95 / cf.interest : 0,
    sim_es_desk_pct: pct * st.w * dist.es95, sim_exit_days: part / vol, sim_exit_days_p: part / (st.p * vol),
    sim_exit_days_short: part / volShort, sim_nslp_cover: part > 0 ? pct * L.nslp / part : 0,
    sim_lst_share: pct * part / L.lst, sim_day_swing_usd: part * P.sigma_d, sim_var1d99_usd: part * day.var99,
    sim_es1d975_usd: part * day.es975,
  };
  for (const [key, muKey, usd, eur, s] of P.bracket) {
    raw[key] = closed(Object.assign({}, st, { mu: muKey ? P.mu[muKey] : st.mu, usd: usd || st.usd,
      eur: eur || st.eur, s: s == null ? st.s : s }), R, V).mean;
  }
  const sp = P.spread[st.win], scale = Math.abs(part * cf.nearF);
  const need = { median: scale * sp.median, p05: scale * sp.p05, p95: scale * sp.p95, es95: scale * sp.es95,
    be_median: sp.be_median, price: Math.abs(1 + st.mu) * sp.price, p_beat: sp.p_beat, p_half: sp.p_half };
  const X = makeFmt(P.fmt), spec = {}, d = {}, val = {};
  for (const [k, [kind, arg]] of Object.entries(P.specs)) {
    const dyn = DYN[kind], lad = dyn && P.steps[dyn[1]];
    spec[k] = dyn ? [dyn[0], lad.find((s) => s > need[arg]) || lad[lad.length - 1]] : [kind, arg];
  }
  for (const k in raw) { val[k] = X.shown(raw[k], spec[k])[0]; d[k] = X.display(raw[k], spec[k]); }
  for (const [k, v] of Object.entries(P.dirs)) d[k] = direction(val[v]);
  for (const [k, v] of Object.entries(P.rise)) d[k] = rise(val[v]);
  const changed = CONTROLS.some((k) => ctrl[k] !== P.default[k]) || (ctrl.mu === 'own' && ctrl.own !== P.default.own);
  Object.assign(d, {
    sim_mu_mode: ctrl.mu, sim_stake_mode: st.k ? 'staked' : 'unstaked', sim_cost_mode: st.c === 0 ? 'none' : 'entered',
    sim_window_mode: st.win === 0 ? 'year' : 'short', sim_venue_mode: st.venue === 'cb' ? 'coinbase' : 'both',
    sim_changed: changed ? 'yes' : 'no', sim_split_basis: P.basis.split, sim_estr_basis: P.basis.estr,
  });
  return { raw, spec, d, S, dots: dist.dots.map((v) => part * v), X };
}

// copy: the `when` grammar of thesis/copy.toml; a figure prints a leading minus as U+2212
const minus = (t) => String(t).replace(/^-(?=[0-9$.])/, '−');
const norm = (t) => String(t).replace(/[\s_-]/g, '').toLowerCase();
function pick(P, name, d) {
  for (const v of P.copy[name] || []) {
    if (v.when.every((c) => {
      const [, key, op, vals] = /^\s*([A-Za-z0-9_.-]+)\s*(!=|=)\s*(.+?)\s*$/.exec(c) || [];
      const hit = vals != null && vals.split('|').map(norm).includes(norm(key in d ? d[key] : ''));
      return op === '=' ? hit : !hit;
    })) return v.text;
  }
  return '';
}
// [text, figure, ..., text]; `over` replaces named figures
function parts(P, O, name, over) {
  const out = [], text = pick(P, name, O.d), re = /\{([a-z_][a-z0-9_]*)(?::([^}]*))?\}/g;
  let last = 0;
  for (let m; (m = re.exec(text));) {
    const [all, key, spec] = m;
    const fig = over && key in over ? over[key]
      : key in O.raw ? (/abs/.test(spec || '') ? O.X.display(O.raw[key], O.spec[key], true) : O.d[key])
        : key in O.d ? O.d[key] : (P.fixed[key] || {})[spec || ''] || '';
    out.push(text.slice(last, m.index), minus(fig));
    last = m.index + all.length;
  }
  out.push(text.slice(last));
  return out;
}

/* the page: adopts the chapter baked in #sim (the plot, the tiles) and adds the controls */
function mount(host, P, sims) {
  const d = document, pct = P.fmt.pct, U = P.ui, PL = P.plot, ctrl = Object.assign({}, P.default), live = [], nums = [];
  let O = outcomes(sims, P, ctrl), cur = -1, redraw = () => {}, readDot = () => {};
  const bad = Object.keys(P.expect).filter((k) => O.d[k] !== P.expect[k]);
  if (bad.length) { host.dataset.sim = 'mismatch'; return console.warn('sim.js: the live twin differs on', bad.join(', ')); }
  const $ = (s, n) => (n || host).querySelector(s), $$ = (s, n) => [...(n || host).querySelectorAll(s)];
  const on = (n, type, f) => n.addEventListener(type, f);
  const attrs = (n, a) => { for (const k in a) n.setAttribute(k, a[k]); return n; };
  const el = (tag, cls, parent, text) => {
    const n = d.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return parent ? parent.appendChild(n) : n;
  };
  const say = (name, over) => parts(P, O, name, over).join('');
  const fill = (n, name, over) => {
    n.textContent = '';
    parts(P, O, name, over).forEach((t, i) => (i % 2 ? el('span', 'num', n, t) : t && n.append(t)));
    n.hidden = !n.textContent.trim();
  };
  const put = (parent, tag, cls, name) => { const n = el(tag, cls, parent); live.push([n, name]); return n; };
  const txt = (parent, tag, cls, name) => el(tag, cls, parent, say(name));
  const chip = (parent, key) => {   // the claim card behind a figure
    const id = P.chips[key];
    if (!id || !d.getElementById('claim-' + id)) return;
    const a = attrs(el('a', 'tag'), { href: '#claim-' + id, 'data-claim': id });
    a.append(say('sim_chip') + ' ');
    el('span', 'id', a, id);
    parent.append(' ', a);
  };

  // the plot: c30's baked geometry (portfolio.dot_layout), redrawn in place
  const box = $('.sim-plot-box'), svg = box && $('svg', box), circ = svg ? $$('.sim-col circle', svg) : [];
  if (svg && circ.length === P.tail.dots) {
    const vb = svg.viewBox.baseVal, top = vb.y, W = vb.width, axis = $('.sim-axis', svg), rows = $('.sim-labs', box);
    const below = top + vb.height - axis.getAttribute('y1'), lines = {}, labs = {}, n = P.tail.dots;
    for (const l of $$('.sim-mark', svg)) lines[l.dataset.mark] = l;
    for (const s of $$('.sim-lab[data-mark]', box)) labs[s.dataset.mark] = s;
    const counts = $$('.sim-lab[data-for]', box);
    const sub = put(null, 'p', 'sim-sub', 'sim_mu_label'), stamp = el('p', 'sim-stamp'), read = el('p', 'sim-read');
    box.before(sub, stamp);
    box.after(read);
    box.tabIndex = 0;
    attrs(read, { 'aria-live': 'polite' });
    attrs(box, { role: 'group', 'aria-label': say('sim_plot_label') + ' ' + say('sim_plot_keys') });
    const ns = svg.namespaceURI, brace = attrs(svg.appendChild(d.createElementNS(ns, 'path')), { class: 'sim-br' });
    const tip = brace.appendChild(d.createElementNS(ns, 'title'));
    let flat = null;
    const layout = () => {
      const v = O.dots, r = PL.r, pad = PL.pad, lo = Math.min(v[0], 0), bins = {};
      let hi = Math.max(v[v.length - 1], 0), rows = 0;
      if (hi === lo) hi = lo + 1;
      const x = (q) => pad + (q - lo) / (hi - lo) * (PL.w - 2 * pad);
      const at = v.map((q) => {
        const b = Math.floor((x(q) - pad) / (2 * r)), k = bins[b] || 0;
        bins[b] = k + 1;
        rows = Math.max(rows, k + 1);
        return [pad + (2 * b + 1) * r, k, q > 0];
      });
      const base = PL.top + rows * 2 * r;
      return { x, base, dots: at.map(([cx, k, up]) => [cx, base - (2 * k + 1) * r, up]) };
    };
    redraw = (all) => {
      const g = layout(), H = g.base + below - top, nT = Math.floor(n / P.tail.es_every), r = PL.r;
      if (all) {
        const m = { p05: O.raw.sim_p05_usd, median: O.raw.sim_median_usd, zero: 0, mean: O.raw.sim_mean_usd, p95: O.raw.sim_p95_usd };
        attrs(svg, { viewBox: [vb.x, top, W, H].join(' ') });
        box.style.setProperty('--r', W / H);
        attrs(axis, { y1: g.base, y2: g.base });
        attrs($('.sim-zero', svg), { x1: g.x(0), x2: g.x(0), y2: g.base });
        // label rows down from the axis: right-hand marks left to right, then the rest right to left (no leader
        // crosses a label)
        const R = W * PL.flip, key = (x) => x > R ? x - 2 * W : -x;
        Object.keys(m).map((k) => [k, g.x(m[k])])
          .sort((a, b) => key(a[1]) - key(b[1]) || (a[0] < b[0] ? -1 : 1)).forEach(([k, x]) => {
            const s = labs[k];
            if (lines[k]) attrs(lines[k], { x1: x, x2: x, y2: g.base });
            if (!s) return;
            s.style.setProperty('--x', pct * x / W + '%');
            s.dataset.anchor = x > R ? 'end' : 'start';
            rows.append(s);
            fill(s.firstElementChild || s, 'film_plot_' + k);
          });
        for (const s of counts) fill(s, 'film_plot_' + s.dataset.for);
        if (!flat) {   // out of the drop-in stacks once, so later changes slide
          flat = attrs(d.createElementNS(ns, 'g'), { class: 'sim-live' });
          svg.insertBefore(flat, $('.sim-mark', svg));
          for (const c of circ) flat.appendChild(c);
        }
        g.dots.forEach(([cx, cy, up], i) => { attrs(circ[i], { cx, cy }).classList.toggle('is-ahead', up); });
      }
      let yb = g.base;
      for (let i = 0; i < nT; i++) yb = Math.min(yb, g.dots[i][1] - r - r);
      attrs(brace, { d: ['M', g.dots[0][0] - r, yb + r, 'V', yb, 'H', g.dots[nT - 1][0] + r, 'V', yb + r].join(' ') });
      tip.textContent = say('sim_plot_tail');
      fill(stamp, 'sim_changed_note');
      const chg = CONTROLS.concat('own').filter((k) => ctrl[k] !== P.default[k] && (k !== 'own' || ctrl.mu === 'own'))
        .map((k) => say('sim_chg_' + ({ own: 'mu', rt: 'c', cu: 'c' }[k] || k)));
      if (chg.length) stamp.append(' ' + [...new Set(chg)].join(', '));
      sub.classList.toggle('own', ctrl.mu === 'own');
    };
    readDot = (i) => {
      circ.forEach((c, j) => c.classList.toggle('cur', j === i));
      box.classList.toggle('sel', i >= 0);
      cur = i;
      const v = O.dots[i], sp = O.spec.sim_median_usd;
      read.textContent = i < 0 ? '' : [say('sim_dot_year'), i + 1, say('sim_dot_of'), n].join(' ') + ': ' +
        O.X.display(v, sp, true) + ' ' + direction(O.X.shown(v, sp)[0]);
    };
    const mid = Math.floor(n / 2);
    on(box, 'keydown', (e) => {
      const mv = { ArrowRight: 1, ArrowUp: 1, ArrowLeft: -1, ArrowDown: -1 }[e.key];
      const to = mv ? (cur < 0 ? mid : cur + mv) : { Home: 0, End: n - 1, Escape: -1 }[e.key];
      if (to === undefined) return;
      e.preventDefault();
      readDot(to < 0 ? -1 : Math.max(0, Math.min(n - 1, to)));
    });
    on(box, 'focus', () => cur < 0 && readDot(mid));
    on(box, 'blur', () => readDot(-1));
    on(svg, 'pointerover', (e) => { const i = circ.indexOf(e.target); if (i >= 0) readDot(i); });
    on(svg, 'pointerleave', () => d.activeElement !== box && readDot(-1));
  }

  // the rest of the chapter, before its closing note
  const rest = $('.sim-rest') || el('div', 'sim-rest', host), end = $(':scope > .sim-note', rest);
  const block = (tag, cls) => rest.insertBefore(el(tag, cls), end);
  const head = attrs(el('p', 'lead sim-headline'), { 'aria-live': 'polite' });
  rest.prepend(head);
  for (const [key, label, text, sub] of P.page.tiles) {
    const li = $('.sim-tile[data-key="' + key + '"]', rest);
    if (!li) continue;
    const v = $('.num[data-key="' + key + '"]', li), l = $('.sim-tile-l', li);
    if (v) nums.push([v, key]);
    if (l) chip(l, label);
    put(li, 'p', 'sim-tile-t', text);
    if (sub) put(li, 'p', 'sim-tile-s', sub);
  }
  const strip = block('div', 'sim-strip');   // the bracket strip (closed form)
  put(strip, 'p', null, 'sim_strip_intro');
  const cells = el('ul', null, strip);
  const marks = P.page.strip.map(([key, name, mode]) => {
    const li = el('li', mode ? null : 'mut', cells);
    nums.push([el('b', 'num', li), key]);
    put(li, 'span', null, name);
    return [li, mode];
  });
  const hurdle = block('div', 'sim-hurdle');
  put(hurdle, 'p', null, 'sim_hurdle');
  put(hurdle, 'p', 'note', 'sim_hurdle_cost');

  // the controls
  const form = block('form', 'sim-ctl');
  on(form, 'submit', (e) => e.preventDefault());
  txt(form, 'h3', null, 'sim_ctl_h');
  let uid = 0;
  const input = (f, type, min, max, step) => Object.assign(el('input', null, f), { type, min, max, step });
  const field = (parent, name, ...a) => {
    const f = el('div', 'sim-f', parent), l = txt(f, 'label', null, name), i = input(f, ...a);
    i.id = l.htmlFor = 'sim-c' + ++uid;
    return [f, i];
  };
  const radios = (parent, name, opts) => {
    const fs = el('fieldset', null, parent);
    txt(fs, 'legend', null, 'sim_ctl_' + name);
    return opts.map(([v, label]) => {
      const l = el('label', 'r', fs), i = Object.assign(el('input', null, l), { type: 'radio', name: 'sim-' + name, value: v });
      l.append(say('sim_ctl_' + label));
      chip(l, v);
      return i;
    });
  };
  const bp = (w) => Math.round(w * P.bp), bpPct = (b) => b * pct / P.bp;
  const [fw, wR] = field(form, 'sim_ctl_share', 'range', 0, P.share.max_bp, P.share.step_bp);
  const ticks = el('datalist', null, fw);
  ticks.id = wR.id + 't';
  for (const v of [P.share.w_b, P.share.w_one_day]) el('option', null, ticks).value = bp(v);
  wR.setAttribute('list', ticks.id);
  const wN = attrs(input(fw, 'number', 0, bpPct(P.share.max_bp), bpPct(P.share.step_bp)), { 'aria-label': say('sim_ctl_share') });
  const wO = el('output', null, fw), wZ = txt(fw, 'small', null, 'sim_ctl_share_zero');
  txt(fw, 'small', null, 'sim_ctl_tick_budget');
  txt(fw, 'small', null, 'sim_ctl_tick_oneday');
  const [fs, sR] = field(form, 'sim_ctl_split', 'range', 0, pct, P.split_step);
  const sO = put(fs, 'output', null, 'sim_ctl_split_out');
  txt(fs, 'small', null, 'sim_ctl_split_hint');
  put(fs, 'small', null, 'sim_split_note');
  const muR = radios(form, 'mu', ['value', 'flat', 'thesis', 'own'].map((v) => [v, 'mu_' + v]));
  const muF = muR[0].parentNode.parentNode;
  const own = attrs(input(muF, 'number', P.mu.lo * pct, P.mu.hi * pct, U.mu_step), { 'aria-label': say('sim_ctl_mu_own') });
  const muM = el('small', null, muF);
  put(muF, 'small', null, 'sim_mu_note');
  const kR = radios(form, 'stake', [['1', 'staked'], ['0', 'unstaked']]);
  const winR = radios(form, 'window', [['0', 'window_year'], ['1', 'window_short']]);
  const more = el('details', null, form);
  txt(more, 'summary', null, 'sim_ctl_more');
  const mg = el('div', null, more);
  const [frt, rt] = field(mg, 'sim_ctl_rt', 'number', 0, U.cost_max, 1);
  const empty = txt(frt, 'small', null, 'sim_ctl_costs_empty');
  const [fcu, cu] = field(mg, 'sim_ctl_cu', 'number', 0, U.cost_max, 1);
  txt(fcu, 'small', null, 'sim_ctl_costs_note');
  const vR = radios(mg, 'venue', [['cb', 'venue_cb'], ['all', 'venue_all']]);
  const [fp, pR] = field(mg, 'sim_ctl_part', 'range', ...U.part);
  const pO = el('output', null, fp), reset = txt(form, 'button', null, 'sim_ctl_reset');
  reset.type = 'button';

  // getting out, the notes, and what is left out
  const liq = block('div', 'sim-liq');
  txt(liq, 'h3', 'sub', 'sim_liq_h');
  put(liq, 'p', 'note', 'sim_liq_where');
  const dl = el('dl', null, liq);
  for (const [k, v] of P.page.liq) {
    put(dl, 'dt', null, k);
    if (P.copy[v]) put(dl, 'dd', 'num', v); else nums.push([el('dd', 'num', dl), v]);
  }
  const notes = block('div', 'sim-notes');
  txt(notes, 'h3', 'sub', 'sim_notes_h');
  for (const k of P.page.notes) put(notes, 'p', 'note', k);
  const left = block('div', 'sim-left');
  txt(left, 'h3', 'sub', 'sim_left_h');
  const ul = el('ul', null, left);
  for (const k of P.page.left) txt(ul, 'li', null, k);

  // state and controls
  const num = (i, dflt) => { const v = parseFloat(i.value); return Number.isFinite(v) ? v : dflt; };
  const one = (rs) => (rs.find((r) => r.checked) || rs[0]).value;
  const toControls = (src) => {
    const set = (i, v) => { if (i !== src) i.value = v; };
    set(wR, bp(ctrl.w));
    set(wN, bpPct(bp(ctrl.w)));
    set(sR, Math.round(ctrl.s * pct));
    set(own, ctrl.mu === 'own' ? ctrl.own * pct : '');
    set(rt, ctrl.rt || '');
    set(cu, ctrl.cu || '');
    set(pR, Math.round(ctrl.p * pct));
    for (const [rs, v] of [[muR, ctrl.mu], [kR, ctrl.k], [winR, ctrl.win], [vR, ctrl.venue]]) {
      for (const r of rs) r.checked = r.value === String(v);
    }
  };
  const fromControls = (src) => {
    ctrl.w = Math.min(P.share.max_bp, Math.max(0, src === wN ? Math.round(num(wN, 0) * P.bp / pct) : num(wR, 0))) / P.bp;
    ctrl.s = num(sR, 0) / pct;
    ctrl.mu = src !== own ? one(muR) : own.value === '' ? P.default.mu : 'own';
    if (ctrl.mu === 'own') ctrl.own = Math.min(P.mu.hi, Math.max(P.mu.lo, num(own, 0) / pct));
    ctrl.k = +one(kR);
    ctrl.win = +one(winR);
    ctrl.venue = one(vR);
    ctrl.rt = Math.max(0, num(rt, 0));
    ctrl.cu = Math.max(0, num(cu, 0));
    ctrl.p = Math.max(U.part[0], num(pR, U.part[0])) / pct;
  };
  let settle = 0;
  const headline = () => {   // a reader's own drift is stamped into it
    fill(head, 'sim_headline');
    if (ctrl.mu === 'own') head.prepend(el('b', 'sim-own', null, say('sim_mu_label')), ' ');
  };
  const render = (first) => {
    if (!first) O = outcomes(sims, P, ctrl);
    for (const [n, name] of live) fill(n, name);
    for (const [n, key] of nums) n.textContent = minus(O.d[key]);
    for (const [li, mode] of marks) li.classList.toggle('on', mode === ctrl.mu);
    wO.value = O.d.sim_share + ' · ' + minus(O.d.sim_near_usd);
    pO.value = O.d.sim_participation;
    for (const [i, o] of [[wR, wO], [sR, sO], [pR, pO]]) i.setAttribute('aria-valuetext', o.textContent);
    wZ.hidden = ctrl.w !== 0;
    empty.hidden = ctrl.rt + ctrl.cu !== 0;
    const over = {};   // marks beyond the own-drift range
    for (const [k, v] of [['sim_mu_thesis_m2', P.mu.thesis], ['sim_be_mu_mean', O.raw.sim_be_mu_mean / pct],
      ['sim_be_mu_median', O.raw.sim_be_mu_median / pct]]) if (v < P.mu.lo || v > P.mu.hi) over[k] = say('sim_ctl_mu_off');
    fill(muM, 'sim_ctl_mu_marks', over);
    redraw(!first);
    readDot(cur);
    clearTimeout(settle);
    if (first) headline(); else settle = setTimeout(headline, U.settle_ms);
  };
  const changed = () => { render(); host.dispatchEvent(new CustomEvent('sim:input', { bubbles: true })); };
  on(form, 'input', (e) => { fromControls(e.target); toControls(e.target); changed(); });
  on(reset, 'click', () => { Object.assign(ctrl, P.default); toControls(); changed(); });
  toControls();
  render(true);
  host.dataset.sim = 'live';
}

function boot() {
  const d = document, host = d.getElementById('sim');
  if (!host || host.dataset.sim) return;
  host.dataset.sim = 'loading';
  if (!d.querySelector('link[href$="sim.css"]')) {
    d.head.appendChild(Object.assign(d.createElement('link'), { rel: 'stylesheet',
      href: (host.dataset.src || 'assets/sim.js').replace(/js$/, 'css') }));
  }
  const fail = (e) => { host.dataset.sim = 'failed'; console.warn('sim.js', e); };
  fetch(host.dataset.payload || 'data/sim.json')
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.statusText))))
    .then((P) => setTimeout(() => { try { mount(host, P, simulate(P)); } catch (e) { fail(e); } }), fail);
}

return { prng, seedState, simulate, outcomes, makeFmt, roundDp, roundStep, pick, parts, boot, mount };
});
