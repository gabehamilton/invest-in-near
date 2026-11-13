/* NEAR, tested: chart hydration for index.html and data.html.
 *
 * Vanilla JS, no libraries, no CDN. Each <figure data-chart="cNN"> loads site/data/charts/cNN.json
 * when it nears the viewport and draws SVG for its form (chartspec.FORMS). Colors come from the
 * series entity through CSS custom properties, so light/dark themes need no redraw.
 *
 * Every value drawn here is read from the chart JSON; this file computes positions, never figures.
 * If the fetch fails (for example a page opened from file://), the baked table twin stays visible.
 */
(() => {
  'use strict';

  const d = document;
  const root = d.documentElement;
  const NS = 'http://www.w3.org/2000/svg';
  const DAY = 864e5;
  const MERGE_MS = 30 * DAY;
  const THEME_KEY = 'near-theme';
  const MINUS = '−';
  const ELLIPSIS = '…';
  const ENTITIES = ['near', 'usd', 'third', 'context', 'positive', 'negative'];
  const KINDS = {
    line: 'line', 'small-multiples': 'line', column: 'cols', 'diverging-bar': 'cols',
    bar: 'rows', 'dot-range': 'rows', 'table-heatmap': 'heat',
  };
  const UNIT_CAPTION = { pp: 'pp', NEAR: 'NEAR', hours: 'hours', days: 'days', x: '×', blocks: 'blocks' };

  /* ------------------------------------------------------------------ helpers */

  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const val = (v) => (isNum(v) ? v : null);
  const f1 = (v) => Math.round(v * 10) / 10;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const cvar = (entity) => `var(--c-${ENTITIES.includes(entity) ? entity : 'context'})`;

  function svg(tag, attrs, parent) {
    const n = d.createElementNS(NS, tag);
    if (attrs) {
      for (const k of Object.keys(attrs)) {
        const v = attrs[k];
        if (v === null || v === undefined || v === false) continue;
        if (k === 'text') n.textContent = v;
        else if (k === 'style') Object.assign(n.style, v);
        else if (k === 'cls') n.setAttribute('class', v);
        else n.setAttribute(k, typeof v === 'number' ? f1(v) : v);
      }
    }
    if (parent) parent.appendChild(n);
    return n;
  }

  function html(tag, cls, text, parent) {
    const n = d.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    if (parent) parent.appendChild(n);
    return n;
  }

  function titled(node, full) {
    if (full) svg('title', { text: full }, node);
    return node;
  }

  /* text measurement for label layout */
  let fontFamily = 'system-ui, sans-serif';
  const measureCtx = (() => {
    try { return d.createElement('canvas').getContext('2d'); } catch (e) { return null; }
  })();
  function textW(s, size = 11, weight = 400) {
    s = String(s);
    if (!measureCtx) return s.length * size * 0.58;
    measureCtx.font = `${weight} ${size}px ${fontFamily}`;
    return measureCtx.measureText(s).width;
  }
  function fit(s, maxW, size = 11, weight = 400) {
    s = String(s);
    if (textW(s, size, weight) <= maxW) return s;
    let lo = 0;
    let hi = s.length;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (textW(s.slice(0, mid) + ELLIPSIS, size, weight) <= maxW) lo = mid;
      else hi = mid - 1;
    }
    return lo > 0 ? s.slice(0, lo).trimEnd() + ELLIPSIS : '';
  }
  function split2(s, size = 11) {
    const words = String(s).split(/\s+/);
    if (words.length < 2) return [String(s)];
    let best = null;
    for (let i = 1; i < words.length; i++) {
      const a = words.slice(0, i).join(' ');
      const b = words.slice(i).join(' ');
      const w = Math.max(textW(a, size), textW(b, size));
      if (!best || w < best.w) best = { a, b, w };
    }
    return [best.a, best.b];
  }
  /* greedy two-line wrap: as many words as fit on line one, the rest (ellipsized if needed) on line two */
  function wrap2(s, maxW, size = 11) {
    const words = String(s).split(/\s+/);
    let a = '';
    let k = 0;
    while (k < words.length && textW(a ? a + ' ' + words[k] : words[k], size) <= maxW) { a = a ? a + ' ' + words[k] : words[k]; k += 1; }
    if (!a) return [fit(s, maxW, size)];
    if (k >= words.length) return [a];
    return [a, fit(words.slice(k).join(' '), maxW, size)];
  }

  /* ------------------------------------------------------------------ label collision avoidance
   * Each panel keeps an occupancy record: text boxes already placed, mark boxes (bars, dots) and line
   * segments. A label goes to the first candidate spot that covers no other text and, where it can,
   * no mark. Lower-priority labels that find no room are dropped (the value stays in the tooltip and
   * the table) or, for reference lines, move into the legend. Endpoint labels are placed first. */
  const newOcc = () => ({ text: [], marks: [], segs: [] });
  const boxHit = (a, b, pad = 0) => a[0] < b[2] + pad && a[2] > b[0] - pad && a[1] < b[3] + pad && a[3] > b[1] - pad;
  function segHit(b, s) {
    let [xa, ya, xb, yb] = s;
    if (xa > xb) [xa, ya, xb, yb] = [xb, yb, xa, ya];
    if (xb < b[0] || xa > b[2]) return false;
    if (xb - xa < 1e-6) return Math.max(ya, yb) >= b[1] && Math.min(ya, yb) <= b[3];
    const k = (yb - ya) / (xb - xa);
    const y0 = ya + k * (Math.max(xa, b[0]) - xa);
    const y1 = ya + k * (Math.min(xb, b[2]) - xa);
    return Math.max(y0, y1) >= b[1] && Math.min(y0, y1) <= b[3];
  }
  /* the box a text of width w covers when drawn at baseline y with the given anchor */
  function textBox(x, y, w, anchor = 'start', size = 11) {
    const x0 = anchor === 'end' ? x - w : anchor === 'middle' ? x - w / 2 : x;
    return [x0 - 2, y - size * 0.82 - 1, x0 + w + 2, y + size * 0.24 + 1];
  }
  const hitsText = (o, b) => o.text.some((t) => boxHit(b, t, 1));
  const hitsMarks = (o, b) => o.marks.some((m) => boxHit(b, m)) || o.segs.some((sg) => segHit(b, sg));
  const within = (b, lim) => b[0] >= lim[0] - 0.5 && b[1] >= lim[1] - 0.5 && b[2] <= lim[2] + 0.5 && b[3] <= lim[3] + 0.5;
  /* text on a surface-colored plate (a halo that also masks marks underneath) */
  function plated(parent, x, y, text, anchor, full, cls) {
    const b = textBox(x, y, textW(text), anchor);
    svg('rect', { cls: 'plate', x: b[0], y: b[1], width: b[2] - b[0], height: b[3] - b[1], rx: 3 }, parent);
    return titled(svg('text', { cls: cls || null, x, y, 'text-anchor': anchor, text }, parent), full && full !== text ? full : null);
  }
  /* a reference line labelled "zero" says nothing the axis does not */
  const bareZero = (r) => r.v === 0 && /^\s*zero\s*$/i.test(r.label);

  /* ------------------------------------------------------------------ number and date formatting */

  function digits(v) {
    const a = Math.abs(v);
    return a >= 100 ? 0 : a >= 10 ? 1 : 2;
  }
  function plain(v, dp) {
    const s = Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp });
    return (v < 0 && /[1-9]/.test(s) ? MINUS : '') + s;
  }
  /* fixed=true keeps trailing zeros (labels: $4.80B next to $3.99B); axis ticks pass false ($2B, not $2.00B). */
  function compact(v, fixed = false) {
    const a = Math.abs(v);
    const steps = [[1e12, 'T'], [1e9, 'B'], [1e6, 'M'], [1e3, 'K']];
    for (const [k, suffix] of steps) {
      if (a >= k) {
        const q = a / k;
        const dp = q >= 100 ? 0 : q >= 10 ? 1 : 2;
        return (v < 0 ? MINUS : '') + q.toLocaleString('en-US', { minimumFractionDigits: fixed ? dp : 0, maximumFractionDigits: dp }) + suffix;
      }
    }
    return plain(v, digits(v));
  }
  function smart(v) {
    const a = Math.abs(v);
    if (a === 0) return '0';
    if (a < 0.01) return (v < 0 ? MINUS : '') + a.toPrecision(2);
    return plain(v, digits(v));
  }
  /* Same decimals as the registry's display (pipeline/numbers.py fmt), so a chart label and the
     sentence above it print the same figure: rates 2 dp below 10 and 1 dp from 10; counts whole. */
  const rateDp = (v) => (Math.abs(v) < 10 ? 2 : 1);
  const wholeDp = (v) => (Number.isInteger(v) ? 0 : 1);
  function fmt(v, unit) {
    if (v === null || v === undefined) return 'no data';
    if (!isNum(v)) return String(v);
    switch (unit) {
      case '%': return plain(v, rateDp(v)) + '%';
      case 'pp': return (v > 0 ? '+' : '') + plain(v, rateDp(v)) + ' pp';
      case 'x': return plain(v, rateDp(v)) + '×';
      case 'ratio': return plain(v, 2);
      case 'USD': return (v < 0 ? MINUS : '') + '$' + compact(Math.abs(v), true);
      case 'NEAR': return (Math.abs(v) >= 1e4 ? compact(v, true) : smart(v)) + ' NEAR';
      case 'hours': return plain(v, wholeDp(v)) + ' h';
      case 'days': return plain(v, wholeDp(v)) + ' days';
      case 'count': case 'blocks': return plain(v, wholeDp(v));
      default: return smart(v);
    }
  }
  function tickFmt(v, unit, step) {
    const dp = Math.max(0, Math.min(6, -Math.floor(Math.log10(step) + 1e-9)));
    if (v === 0) return unit === '%' ? '0%' : unit === 'USD' ? '$0' : '0';
    switch (unit) {
      case '%': return plain(v, dp) + '%';
      case 'pp': return (v > 0 ? '+' : '') + plain(v, dp);
      case 'USD': return (v < 0 ? MINUS : '') + '$' + compact(Math.abs(v));
      case 'NEAR': return Math.abs(v) >= 1e4 ? compact(v) : plain(v, dp);
      case 'count': return Math.abs(v) >= 1e4 ? compact(v) : plain(v, dp);
      default: return plain(v, dp);
    }
  }

  function toTime(x) {
    if (isNum(x)) return x;
    if (typeof x !== 'string') return NaN;
    let s = x.trim();
    if (/^\d{4}-\d{2}$/.test(s)) s += '-01';
    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return Date.parse(s + 'T00:00:00Z');
    if (/^\d{4}-\d{2}-\d{2}[T ]\d/.test(s)) {
      s = s.replace(' ', 'T');
      if (!/(Z|[+-]\d{2}:?\d{2})$/.test(s)) s += 'Z';
      return Date.parse(s);
    }
    return NaN;
  }
  const looksDate = (x) => typeof x === 'string' && /^\d{4}-\d{2}(-\d{2})?([T ].*)?$/.test(x);
  const fmtD = (t, o) => new Date(t).toLocaleDateString('en-US', Object.assign({ timeZone: 'UTC' }, o));
  const dateDay = (t) => fmtD(t, { year: 'numeric', month: 'short', day: 'numeric' });
  const dateMonth = (t) => fmtD(t, { year: 'numeric', month: 'short' });

  /* ------------------------------------------------------------------ scales and ticks */

  function linear(d0, d1, r0, r1) {
    const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0);
    const f = (v) => r0 + (v - d0) * k;
    f.inv = (p) => (k ? d0 + (p - r0) / k : d0);
    return f;
  }
  function logScale(d0, d1, r0, r1) {
    const a = Math.log10(d0);
    const b = Math.log10(d1);
    const k = b === a ? 0 : (r1 - r0) / (b - a);
    const f = (v) => r0 + (Math.log10(Math.max(v, d0)) - a) * k;
    f.inv = (p) => 10 ** (k ? a + (p - r0) / k : a);
    return f;
  }
  function niceTicks(lo, hi, count, fixed) {
    if (!(hi > lo)) {
      const pad = Math.abs(lo) * 0.1 || 1;
      lo -= pad;
      hi += pad;
    }
    const raw = (hi - lo) / Math.max(1, count);
    const mag = 10 ** Math.floor(Math.log10(raw));
    const err = raw / mag;
    const step = mag * (err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1);
    const a = Math.floor(lo / step + 1e-9);
    const b = Math.ceil(hi / step - 1e-9);
    const ticks = [];
    for (let i = a; i <= b; i++) {
      const t = +(i * step).toFixed(12);
      if (!fixed || (t >= lo - 1e-9 && t <= hi + 1e-9)) ticks.push(t);
    }
    return fixed ? { lo, hi, step, ticks } : { lo: a * step, hi: b * step, step, ticks };
  }
  function logTicks(lo, hi) {
    const a = Math.floor(Math.log10(lo));
    const b = Math.ceil(Math.log10(hi));
    const mult = b - a <= 2 ? [1, 2, 5] : [1];
    const ticks = [];
    for (let e = a; e <= b; e++) for (const m of mult) ticks.push(m * 10 ** e);
    return { lo: 10 ** a, hi: 10 ** b, step: 10 ** a, ticks: ticks.filter((t) => t >= 10 ** a && t <= 10 ** b), log: true };
  }
  function timeTicks(t0, t1, width) {
    const maxN = Math.max(2, Math.floor(width / 76));
    const span = t1 - t0;
    const opts = [['d', 1], ['d', 7], ['m', 1], ['m', 3], ['m', 6], ['y', 1], ['y', 2], ['y', 5], ['y', 10], ['y', 25]];
    const len = (u, n) => (u === 'd' ? n * DAY : u === 'm' ? n * 30.44 * DAY : n * 365.25 * DAY);
    let idx = opts.findIndex(([u, n]) => span / len(u, n) <= maxN);
    if (idx < 0) idx = opts.length - 1;
    const build = ([unit, k]) => {
      const out = [];
      const s = new Date(t0);
      let t;
      if (unit === 'y') {
        let y = Math.ceil(s.getUTCFullYear() / k) * k;
        if (Date.UTC(y, 0, 1) < t0) y += k;
        for (; (t = Date.UTC(y, 0, 1)) <= t1; y += k) out.push({ t, label: String(y) });
      } else if (unit === 'm') {
        let m = s.getUTCFullYear() * 12 + s.getUTCMonth();
        if (Date.UTC(Math.floor(m / 12), m % 12, 1) < t0) m += 1;
        m = Math.ceil(m / k) * k;
        for (; (t = Date.UTC(Math.floor(m / 12), m % 12, 1)) <= t1; m += k) {
          const first = out.length === 0 || m % 12 === 0;
          out.push({ t, label: fmtD(t, first ? { month: 'short', year: 'numeric' } : { month: 'short' }) });
        }
      } else {
        t = Date.UTC(s.getUTCFullYear(), s.getUTCMonth(), s.getUTCDate());
        if (t < t0) t += DAY;
        for (; t <= t1; t += k * DAY) out.push({ t, label: fmtD(t, { month: 'short', day: 'numeric' }) });
      }
      return out;
    };
    let out = build(opts[idx]);
    // a narrow panel can land on a single tick (one January inside the span): step to the next finer
    // interval so the axis keeps at least two; labels that would touch are still dropped by placeXLabels
    while (out.length < 2 && idx > 0) out = build(opts[--idx]);
    return out;
  }

  /* rounded data ends (4px), square at the baseline */
  function vbar(x, w, y0, y1, r) {
    const h = Math.abs(y1 - y0);
    if (h < 0.5 || w <= 0) return '';
    r = Math.min(r, h, w / 2);
    const s = y1 < y0 ? 1 : -1;
    return `M${f1(x)},${f1(y0)}V${f1(y1 + s * r)}Q${f1(x)},${f1(y1)} ${f1(x + r)},${f1(y1)}` +
      `H${f1(x + w - r)}Q${f1(x + w)},${f1(y1)} ${f1(x + w)},${f1(y1 + s * r)}V${f1(y0)}Z`;
  }
  function hbar(y, h, x0, x1, r) {
    const w = Math.abs(x1 - x0);
    if (w < 0.5 || h <= 0) return '';
    r = Math.min(r, w, h / 2);
    const s = x1 > x0 ? -1 : 1;
    return `M${f1(x0)},${f1(y)}H${f1(x1 + s * r)}Q${f1(x1)},${f1(y)} ${f1(x1)},${f1(y + r)}` +
      `V${f1(y + h - r)}Q${f1(x1)},${f1(y + h)} ${f1(x1 + s * r)},${f1(y + h)}H${f1(x0)}Z`;
  }

  function nearestIdx(sorted, t) {
    if (!sorted.length) return -1;
    let lo = 0;
    let hi = sorted.length - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (sorted[mid] <= t) lo = mid;
      else hi = mid;
    }
    return Math.abs(sorted[lo] - t) <= Math.abs(sorted[hi] - t) ? lo : hi;
  }

  /* keeps a list of {y} labels at least `gap` apart inside [lo, hi] */
  function spread(items, gap, lo, hi) {
    items.sort((a, b) => a.y - b.y);
    for (let i = 1; i < items.length; i++) if (items[i].y - items[i - 1].y < gap) items[i].y = items[i - 1].y + gap;
    const over = items.length ? items[items.length - 1].y - hi : 0;
    if (over > 0) for (const it of items) it.y -= over;
    for (let i = items.length - 2; i >= 0; i--) if (items[i + 1].y - items[i].y < gap) items[i].y = items[i + 1].y - gap;
    for (const it of items) it.y = Math.max(lo, it.y);
  }

  /* Annotation labels in lanes above the plot (LANE px each). A label may wrap to two lines. No label
     covers another, and no annotation's hairline runs through another annotation's label. Kinds with
     higher priority are placed first; a label that finds no room within maxLanes is dropped, and its
     hairline keeps the text as a tooltip. */
  const LANE = 13;
  const ANN_PRIORITY = { ceiling_change: 3, fed_turn: 2, data_boundary: 1 };
  function layoutAnns(items, xmin, xmax, maxLanes = 3) {
    const room = Math.max(60, xmax - xmin);
    const maxW = Math.min(250, room * 0.62);
    const placed = [];
    const out = [];
    const order = items.slice().sort((p, q) => ((ANN_PRIORITY[q.kind] || 1) - (ANN_PRIORITY[p.kind] || 1)) || p.x - q.x);
    // a dropped label's hairline starts at the plot top, below the lanes, so it constrains nothing here
    const free = (c) => placed.every((p) => {
      if (p.l1 < 0) return true;
      if (c.l0 <= p.l1 && p.l0 <= c.l1 && c.x0 < p.x1 + 8 && c.x1 > p.x0 - 8) return false;
      if (c.l1 > p.l1 && p.x >= c.x0 - 4 && p.x <= c.x1 + 4) return false; // p's hairline crosses c
      if (p.l1 > c.l1 && c.x >= p.x0 - 4 && c.x <= p.x1 + 4) return false; // c's hairline crosses p
      return true;
    });
    for (const a of order) {
      const variants = [];
      if (textW(a.label) <= maxW) variants.push([a.label]);
      const bal = /\s/.test(a.label) ? split2(a.label) : null;
      if (bal && Math.max(textW(bal[0]), textW(bal[1])) <= maxW) variants.push(bal);
      if (!variants.length) variants.push(wrap2(a.label, maxW));
      const cands = [];
      for (const lines of variants) {
        const w = Math.max(...lines.map((l) => textW(l)));
        for (let l0 = 0; l0 + lines.length <= maxLanes; l0++) {
          for (const anchor of ['start', 'end']) {
            const x0 = anchor === 'start' ? a.x + 3 : a.x - 3 - w;
            const x1 = x0 + w;
            if (x0 < xmin || x1 > xmax) continue;
            cands.push({ lines, anchor, x: a.x, x0, x1, l0, l1: l0 + lines.length - 1 });
          }
        }
      }
      cands.sort((p, q) => p.l1 - q.l1 || p.lines.length - q.lines.length);
      const c = cands.find(free);
      const full = a.also && a.also.length ? [a.label].concat(a.also).join('; ') : a.label;
      const it = c ? Object.assign({ full, kind: a.kind }, c)
        : { x: a.x, x0: a.x, x1: a.x, l0: -1, l1: -1, lines: [], anchor: 'start', full, kind: a.kind };
      placed.push(it);
      out.push(it);
    }
    const lanes = Math.max(0, ...out.map((it) => it.l1 + 1));
    return { rows: lanes, items: out.sort((p, q) => p.x - q.x) };
  }
  /* draws laid-out annotation labels; a label's text box joins the panel's occupancy */
  function drawAnnLabels(ag, anns, annTop, o) {
    for (const a of anns.items) {
      if (!a.lines.length) continue;
      const t = svg('text', { x: a.anchor === 'start' ? a.x + 3 : a.x - 3, y: annTop + a.l0 * LANE + 10, 'text-anchor': a.anchor }, ag);
      a.lines.forEach((ln, k) => svg('tspan', { x: a.anchor === 'start' ? a.x + 3 : a.x - 3, dy: k ? LANE : null, text: ln }, t));
      titled(t, a.lines.join(' ') !== a.full ? a.full : null);
      o.text.push([a.x0 - 2, annTop + a.l0 * LANE, a.x1 + 2, annTop + (a.l1 + 1) * LANE]);
    }
  }

  function mergeAnns(list, time) {
    const items = (list || []).filter((a) => a && a.label !== undefined && a.x !== undefined && a.x !== null)
      .map((a) => {
        // `also`: labels the model merged into this one but left out of the plot text (they stay in the tooltip)
        const also = Array.isArray(a.also) ? a.also.map(String) : [];
        return { x: a.x, t: time ? toTime(a.x) : null, label: String(a.label), also, kind: a.kind || '', panel: a.panel ? String(a.panel) : null };
      })
      .filter((a) => !time || Number.isFinite(a.t));
    const out = [];
    if (!time) {
      for (const a of items) {
        const same = out.find((o) => String(o.x) === String(a.x) && o.panel === a.panel);
        if (same) { if (!same.label.includes(a.label)) same.label += '; ' + a.label; same.also = same.also.concat(a.also); } else out.push(a);
      }
      return out;
    }
    items.sort((a, b) => a.t - b.t);
    for (const a of items) {
      const last = out.filter((o) => o.panel === a.panel).pop();
      if (last && a.t - last.t < MERGE_MS) {
        if (!last.label.includes(a.label)) last.label += '; ' + a.label;
        last.also = last.also.concat(a.also);
        if ((ANN_PRIORITY[a.kind] || 1) > (ANN_PRIORITY[last.kind] || 1)) last.kind = a.kind;
      } else out.push(Object.assign({}, a));
    }
    return out;
  }
  /* annotations for one panel: those without a panel field show on every panel */
  const annsFor = (P, g) => P.ann.filter((a) => !a.panel || a.panel === g.id);

  /* category or tick labels along the x axis, centered under their mark, kept inside [xlo, xhi];
     placed in priority order, and a label that would touch an earlier one is dropped */
  function placeXLabels(ax, items, o, xlo, xhi) {
    const order = items.slice().sort((a, b) => b.pri - a.pri);
    for (const it of order) {
      const x0 = clamp(it.x - it.w / 2, xlo, Math.max(xlo, xhi - it.w));
      const b = [x0 - 3, it.y - 10, x0 + it.w + 3, it.y + (it.lines === 2 ? 15 : 3)];
      if (hitsText(o, b)) continue;
      o.text.push(b);
      const cx = x0 + it.w / 2;
      if (it.lines === 2) {
        const t = svg('text', { x: cx, y: it.y, 'text-anchor': 'middle' }, ax);
        svg('tspan', { x: cx, text: it.a }, t);
        if (it.b) svg('tspan', { x: cx, dy: 12, text: it.b }, t);
        titled(t, it.full);
      } else {
        titled(svg('text', { x: cx, y: it.y, 'text-anchor': 'middle', text: it.text }, ax), it.full && it.full !== it.text ? it.full : null);
      }
    }
  }

  /* ------------------------------------------------------------------ chart model */

  function normSeries(s, i, kind, unit) {
    const o = {
      id: String(s.id), label: String(s.label || s.id), entity: s.entity, role: s.role || 'emphasis',
      unit: s.unit || unit, panel: s.panel || null, dashed: !!s.dashed || s.role === 'model', i,
      underlay: !!s.underlay,
    };
    const pts = Array.isArray(s.points) ? s.points : [];
    if (kind === 'line') {
      o.pts = pts.map((p) => [toTime(p[0]), val(p[1])]).filter((p) => Number.isFinite(p[0])).sort((a, b) => a[0] - b[0]);
      o.nn = o.pts.filter((p) => p[1] !== null);
      o.ts = o.nn.map((p) => p[0]);
      const gaps = [];
      for (let k = 1; k < o.ts.length; k++) gaps.push(o.ts[k] - o.ts[k - 1]);
      gaps.sort((a, b) => a - b);
      o.tol = Math.max(DAY, (gaps.length ? gaps[gaps.length >> 1] : DAY) * 0.6);
    } else {
      o.cats = [];
      o.at = new Map();
      for (const p of pts) {
        if (!Array.isArray(p)) continue;
        const c = String(p[0]);
        if (!o.at.has(c)) o.cats.push(c);
        o.at.set(c, p.slice(1).map(val));
      }
    }
    return o;
  }

  const first = (s, c) => { const v = s.at.get(c); return v ? v[0] : null; };

  function uniq(list) {
    const seen = new Set();
    const out = [];
    for (const x of list) if (!seen.has(x)) { seen.add(x); out.push(x); }
    return out;
  }

  function prepare(fig, plot, chart) {
    const form = plot.dataset.form || chart.form;
    const kind = KINDS[form];
    if (!kind) return null;
    const x = chart.x || {};
    const y = chart.y || {};
    const all0 = Array.isArray(chart.series) ? chart.series : [];
    let unit = form === 'dot-range' ? x.unit : y.unit;
    if (!unit || unit === 'text') unit = (all0[0] && all0[0].unit) || '%';
    const title = ((fig.querySelector('.card-title') || {}).textContent || chart.title || '').replace(/\s+/g, ' ').trim();
    const defs = Array.isArray(chart.panels) ? chart.panels : [];
    const all = all0.map((s, i) => normSeries(s, i, kind, unit));
    const groups = Array.from(plot.querySelectorAll('.panel')).map((el) => {
      const id = el.dataset.panel || null;
      const def = id ? defs.find((p) => p.id === id) || { id } : null;
      const ids = def ? def.series_ids || [] : [];
      const series = def ? all.filter((s) => s.panel === id || ids.includes(s.id)) : all;
      return { el, id, label: def ? String(def.label || '') : '', series };
    });
    const P = {
      chart, form, kind, unit, title, all, groups, x, y,
      xTime: x.type === 'time',
      monthly: x.unit === 'month',
      log: y.scale === 'log',
      refs: (chart.reference_lines || []).filter((r) => r && (isNum(r.y) || isNum(r.x)))
        .map((r) => ({ v: isNum(r.y) ? r.y : r.x, label: String(r.label || ''), axis: isNum(r.y) ? 'y' : 'x' })),
      bands: (chart.bands || []).filter((b) => b && b.from !== undefined && b.to !== undefined),
      ann: mergeAnns(chart.annotations, kind === 'line'),
      dyn: [],
      forceMt: 0,
    };
    // an annotation naming a panel that is not drawn falls back to every panel
    const panelIds = new Set(groups.map((g) => g.id).filter(Boolean));
    for (const a of P.ann) if (a.panel && !panelIds.has(a.panel)) a.panel = null;
    P.yBand = (b) => b.axis === 'y' || isNum(b.from);
    const diverging = all.filter((s) => s.role === 'diverging');
    P.signColor = diverging.length === 1 && diverging[0].entity !== 'negative';
    P.colorFor = (s, v) => (P.signColor && s.role === 'diverging'
      ? (isNum(v) && v < 0 ? 'var(--c-negative)' : 'var(--c-positive)') : cvar(s.entity));
    P.catLabel = (c) => {
      if (P.xTime && looksDate(c)) return (P.monthly || /^\d{4}-\d{2}(-01)?$/.test(c)) ? dateMonth(toTime(c)) : dateDay(toTime(c));
      return String(c);
    };
    P.catsOf = (g) => uniq(g.series.flatMap((s) => s.cats));
    P.keyLabel = (k) => {
      if (kind === 'line') return P.monthly ? dateMonth(k) : dateDay(k);
      if (kind === 'heat') { const [sid, c] = String(k).split('\u0001'); const s = all.find((q) => q.id === sid); return (s ? s.label + ' · ' : '') + P.catLabel(c); }
      return P.catLabel(k);
    };

    const vals = [];
    const plotted = uniq(groups.flatMap((g) => g.series));
    // values one panel draws (for y.scale_per_panel: small multiples of measures with different sizes)
    const valsOf = (list) => {
      const out = [];
      for (const s of list) {
        if (kind === 'line') { for (const p of s.nn) out.push(p[1]); continue; }
        for (const c of s.cats) {
          const arr = s.at.get(c);
          for (const v of (kind === 'rows' ? arr : arr.slice(0, 1))) if (isNum(v)) out.push(v);
        }
      }
      return out;
    };
    if (kind === 'line') {
      let t0 = Infinity;
      let t1 = -Infinity;
      for (const s of plotted) {
        for (const [t, v] of s.nn) { t0 = Math.min(t0, t); t1 = Math.max(t1, t); vals.push(v); }
      }
      for (const b of P.bands) {
        if (b.axis === 'y' || isNum(b.from)) continue;
        const a = toTime(b.from);
        const z = toTime(b.to);
        if (Number.isFinite(a) && Number.isFinite(z) && Number.isFinite(t0)) { t0 = Math.min(t0, a); t1 = Math.max(t1, z); }
      }
      P.xd = [t0, t1];
      P.ann = P.ann.filter((a) => a.t >= t0 && a.t <= t1);
    } else if (kind === 'rows') {
      for (const s of plotted) for (const c of s.cats) for (const v of s.at.get(c)) if (isNum(v)) vals.push(v);
    } else {
      for (const s of plotted) for (const c of s.cats) { const v = first(s, c); if (isNum(v)) vals.push(v); }
    }
    P.hasData = vals.length > 0;
    const extra = [];
    for (const r of P.refs) if (kind === 'rows' || r.axis === 'y') extra.push(r.v);
    for (const b of P.bands) if (b.axis === 'y' || isNum(b.from)) { if (isNum(b.from)) extra.push(b.from); if (isNum(b.to)) extra.push(b.to); }
    // dot-range rows read their value domain from x
    const given = kind === 'rows' && form === 'dot-range' ? x : y;
    const dom = Array.isArray(given.domain) && isNum(given.domain[0]) && isNum(given.domain[1]) ? given.domain : null;
    const clampDom = !!(dom && given.clamp);
    const needsZero = kind === 'cols' || form === 'bar';
    const range = (vs) => {
      let lo = vs.length ? Math.min(...vs) : 0;
      let hi = vs.length ? Math.max(...vs) : 1;
      if (needsZero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
      if (dom) {
        lo = clampDom ? dom[0] : Math.min(dom[0], lo);
        hi = clampDom ? dom[1] : Math.max(dom[1], hi);
      }
      return [lo, hi];
    };
    const [lo, hi] = range(vals.concat(extra));
    if (P.log && lo <= 0) P.log = false;
    P.vd = [lo, hi];
    P.fixed = !!dom;
    P.clamped = clampDom;
    const floor = isNum(given.floor) ? given.floor : null;
    const ticker = (a, z) => (pixels, per) => {
      if (P.log) return logTicks(a, z);
      const r = niceTicks(a, z, Math.max(2, Math.floor(pixels / per)), P.fixed);
      // a value that cannot exist below `floor` never gets axis room below it, at any width
      if (floor !== null && r.lo < floor) {
        r.lo = floor;
        r.ticks = r.ticks.filter((t) => t >= floor - 1e-9);
        if (!r.ticks.length || r.ticks[0] > floor + 1e-9) r.ticks.unshift(floor);
      }
      return r;
    };
    P.ticks = ticker(lo, hi);
    // each panel gets its own y scale only when the chart asks for it (its panels are labelled with their unit)
    const perPanel = kind !== 'heat' && !!y.scale_per_panel && groups.length > 1;
    for (const g of groups) {
      if (!perPanel) { g.ticks = P.ticks; continue; }
      const [a, z] = range(valsOf(g.series).concat(extra));
      g.ticks = ticker(a, z === a ? a + 1 : z);
    }
    P.center = isNum(y.diverging_center) ? y.diverging_center : (P.refs[0] ? P.refs[0].v : 0);
    return P;
  }

  function startSvg(g, P) {
    const el = g.el;
    el.textContent = '';
    const W = Math.max(120, el.clientWidth);
    const H = Math.max(80, el.clientHeight);
    const label = P.title + (g.label ? ': ' + g.label : '');
    const s = svg('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': label, focusable: 'false' }, el);
    return { s, W, H };
  }

  function drawPanelLabel(s, g, W, top) {
    if (!g.label) return top;
    titled(svg('text', { cls: 'panel-label', x: 0, y: top + 11, text: fit(g.label, W, 12, 650) }, s), g.label);
    return top + 20;
  }

  /* A panel's own legend (small multiples): the series this panel draws that the shared legend above
     the chart does not already name. Flows into rows; every panel reserves the same number of rows
     (P.legendRows) so side-by-side panels keep their plots aligned. */
  const PL_ROW = 15;
  function panelLegendRows(items, W) {
    if (!items || !items.length) return 0;
    let x = 0;
    let rows = 1;
    for (const it of items) {
      const w = 21 + Math.min(textW(it.label), W - 22);
      if (x > 0 && x + w > W) { x = 0; rows += 1; }
      x += w + 14;
    }
    return rows;
  }
  function drawPanelLegend(s, P, g, W, top) {
    const items = P.panelLegend ? P.panelLegend.get(g) || [] : [];
    if (!P.legendRows) return top;
    const lg = svg('g', { cls: 'panel-legend', 'aria-hidden': 'true' }, s);
    let x = 0;
    let row = 0;
    for (const it of items) {
      const text = fit(it.label, W - 22);
      const w = 21 + textW(text);
      if (x > 0 && x + w > W) { x = 0; row += 1; }
      const y = top + row * PL_ROW;
      drawKey(svg('g', { transform: `translate(${f1(x)},${f1(y + 2)})` }, lg), it);
      titled(svg('text', { cls: 'legend-text', x: x + 21, y: y + 10.5, text }, lg), it.label);
      x += w + 14;
    }
    return top + P.legendRows * PL_ROW + 4;
  }

  /* y grid and tick labels; tick label boxes join the occupancy so data labels never cover them */
  function drawYGrid(s, yi, sy, x0, x1, unit, o) {
    const grid = svg('g', { cls: 'grid', 'aria-hidden': 'true' }, s);
    const ax = svg('g', { cls: 'ax', 'aria-hidden': 'true' }, s);
    for (const v of yi.ticks) {
      const Y = Math.round(sy(v)) + 0.5;
      if (v !== 0) svg('line', { x1: x0, x2: x1, y1: Y, y2: Y }, grid);
      const t = tickFmt(v, unit, yi.step);
      svg('text', { x: x0 - 6, y: Y + 4, 'text-anchor': 'end', text: t }, ax);
      if (o) o.text.push(textBox(x0 - 6, Y + 4, textW(t), 'end'));
    }
  }

  function yScale(P, yi, bottom, top) {
    return yi.log ? logScale(yi.lo, yi.hi, bottom, top) : linear(yi.lo, yi.hi, bottom, top);
  }

  /* Horizontal reference lines. The label sits inside the plot, as far left as it can, just above or
     below its line, on a surface plate; it may not cover text or marks. With no free spot, it moves
     into the legend as a line key instead of being drawn over the data. */
  function drawRefsY(s, P, sy, x0, x1, mt, bottom, o) {
    const g = svg('g', { cls: 'ref' }, s);
    const lim = [x0 + 1, mt - 12, x1 - 1, bottom - 1];
    const todo = [];
    for (const r of P.refs) {
      if (r.axis !== 'y') continue;
      const Y = sy(r.v);
      if (!Number.isFinite(Y) || Y < mt - 1 || Y > bottom + 1) continue;
      const yy = Math.round(Y) + 0.5;
      svg('line', { x1: x0, x2: x1, y1: yy, y2: yy }, g);
      o.segs.push([x0, yy, x1, yy]);
      if (r.label && !bareZero(r)) todo.push({ r, Y });
    }
    // labels are placed later, once the marks and endpoint labels they must avoid are known
    return () => {
      const lab = svg('g', { cls: 'ref' }, s);
      for (const { r, Y } of todo) {
        const text = fit(r.label, (x1 - x0) * 0.8);
        const w = textW(text);
        let spot = null;
        for (let x = x0 + 4; !spot && x + w <= x1 - 2; x += 6) {
          for (const y of [Y - 4, Y + 12]) {
            const b = textBox(x, y, w);
            if (within(b, lim) && !hitsText(o, b) && !hitsMarks(o, b)) { spot = { x, y, b }; break; }
          }
        }
        if (!spot) { P.dyn.push({ shape: 'ref', color: 'var(--ink-2)', label: r.label }); continue; }
        o.text.push(spot.b);
        plated(lab, spot.x, spot.y, text, 'start', r.label);
      }
    };
  }

  function drawUnit(s, unit, x, top, anchor = 'start') {
    const cap = UNIT_CAPTION[unit];
    if (!cap) return top;
    svg('text', { cls: 'unit', x, y: top + 9, 'text-anchor': anchor, text: cap }, s);
    return top + 14;
  }

  /* ------------------------------------------------------------------ line and small multiples */

  function drawLine(g, P) {
    const { s, W, H } = startSvg(g, P);
    if (!Number.isFinite(P.xd[0])) return emptyPanel(s, W, H, g);
    const o = newOcc();
    const ticksN = Math.max(3, Math.floor((H - 70) / 46));
    const yi = g.ticks(H - 70, (H - 70) / ticksN);
    const tl = yi.ticks.map((v) => tickFmt(v, P.unit, yi.step));
    const ml = Math.ceil(Math.max(20, ...tl.map((t) => textW(t)))) + 10;
    const span = P.xd[1] - P.xd[0];
    const enders = g.series.length <= 4
      ? g.series.filter((se) => se.nn.length && P.xd[1] - se.ts[se.ts.length - 1] <= Math.max(span * 0.08, DAY))
      : [];
    // series that share a color (gray context lines) also get their name at the end, so they never
    // rely on the legend alone; skipped on narrow panels where the name would crowd the plot
    const shared = (se) => enders.filter((q) => q.entity === se.entity).length > 1;
    const endTxt = enders.map((se) => {
      const v = fmt(se.nn[se.nn.length - 1][1], se.unit);
      return shared(se) && W >= 520 ? `${fit(se.label, 110)} ${v}` : v;
    });
    const mr = endTxt.length ? Math.ceil(Math.max(...endTxt.map((t) => textW(t, 11, 600)))) + 12 : 10;
    let top = drawPanelLabel(s, g, W, 2);
    top = drawPanelLegend(s, P, g, W, top);
    const sx = linear(P.xd[0], P.xd[1], ml, W - mr);
    const anns = layoutAnns(annsFor(P, g).map((a) => ({ x: sx(a.t), label: a.label, also: a.also, kind: a.kind })), 0, W - 2, W < 420 ? 2 : 3);
    const annTop = top;
    top += anns.rows ? anns.rows * LANE + 4 : 0;
    top = drawUnit(s, P.unit, 0, top);
    const mt = Math.max(top + 8, P.forceMt || 0);
    const mb = 22;
    const sy = yScale(P, yi, H - mb, mt);

    drawYGrid(s, yi, sy, ml, W - mr, P.unit, o);
    const bg = svg('g', { cls: 'band' }, s);
    const bandLabels = [];
    for (const b of P.bands) {
      if (P.yBand(b)) {
        // a horizontal band is named in the legend, never over the lines
        const y0 = sy(clamp(+b.to, yi.lo, yi.hi));
        const y1 = sy(clamp(+b.from, yi.lo, yi.hi));
        titled(svg('rect', { x: ml, width: W - mr - ml, y: Math.min(y0, y1), height: Math.abs(y1 - y0) }, bg), b.label);
      } else {
        const a = clamp(toTime(b.from), P.xd[0], P.xd[1]);
        const z = clamp(toTime(b.to), P.xd[0], P.xd[1]);
        if (!(z > a)) continue;
        const x0 = sx(a);
        const w = Math.max(1, sx(z) - x0);
        titled(svg('rect', { x: x0, width: w, y: mt, height: H - mb - mt }, bg), b.label);
        if (b.label && w > textW(b.label) + 8) bandLabels.push({ x: x0 + 4, text: String(b.label) });
      }
    }
    if (yi.lo < 0 && yi.hi > 0) {
      const Z = Math.round(sy(0)) + 0.5;
      svg('line', { cls: 'baseline', x1: ml, x2: W - mr, y1: Z, y2: Z }, s);
    } else {
      svg('line', { cls: 'baseline', x1: ml, x2: W - mr, y1: H - mb + 0.5, y2: H - mb + 0.5 }, s);
    }
    const refLabels = drawRefsY(s, P, sy, ml, W - mr, mt, H - mb, o);

    const ag = svg('g', { cls: 'ann' }, s);
    for (const a of anns.items) {
      const X = Math.round(a.x) + 0.5;
      const y1 = a.lines.length ? annTop + (a.l1 + 1) * LANE : mt;
      titled(svg('line', { x1: X, x2: X, y1, y2: H - mb, 'stroke-dasharray': a.kind === 'data_boundary' ? '3 3' : null }, ag), a.lines.length ? null : a.full);
      o.segs.push([X, Math.max(y1, mt), X, H - mb]);
    }
    drawAnnLabels(ag, anns, annTop, o);

    const lines = svg('g', { cls: 'lines' }, s);
    const order = g.series.slice().sort((a, b) => (a.role === 'emphasis' ? 1 : 0) - (b.role === 'emphasis' ? 1 : 0));
    for (const se of order) {
      let path = '';
      let pen = false;
      let px = null;
      let py = null;
      const pts = se.pts;
      for (let k = 0; k < pts.length; k++) {
        const [t, v] = pts[k];
        if (v === null || (P.log && v <= 0)) { pen = false; continue; }
        const X = sx(t);
        const Y = sy(v);
        path += (pen ? 'L' : 'M') + f1(X) + ',' + f1(Y);
        if (pen) o.segs.push([px, py, X, Y]);
        else o.marks.push([X - 2, Y - 2, X + 2, Y + 2]);
        pen = true;
        px = X;
        py = Y;
        const prev = k > 0 ? pts[k - 1][1] : null;
        const next = k < pts.length - 1 ? pts[k + 1][1] : null;
        if (prev === null && next === null) svg('circle', { cx: X, cy: Y, r: 2.5, style: { fill: cvar(se.entity) } }, lines);
      }
      if (!path) continue;
      svg('path', {
        cls: 'line' + (se.role === 'context' ? ' context' : '') + (se.underlay ? ' underlay' : ''), d: path,
        'stroke-dasharray': se.dashed ? '5 4' : null, style: { stroke: cvar(se.entity) },
      }, lines);
    }

    if (enders.length) {
      const lab = svg('g', { cls: 'end-labels', 'aria-hidden': 'true' }, s);
      const items = enders.map((se, k) => ({ y: sy(se.nn[se.nn.length - 1][1]) + 4, text: endTxt[k], strong: se.role === 'emphasis' }));
      spread(items, 13, mt + 4, H - mb);
      for (const it of items) {
        svg('text', { cls: 'label' + (it.strong ? ' strong' : ''), x: W - mr + 6, y: it.y, text: it.text }, lab);
        o.text.push(textBox(W - mr + 6, it.y, textW(it.text, 11, 600)));
      }
    }

    const ax = svg('g', { cls: 'ax', 'aria-hidden': 'true' }, s);
    const xt = timeTicks(P.xd[0], P.xd[1], W - ml - mr);
    placeXLabels(ax, xt.map((t, i) => ({ x: sx(t.t), w: textW(t.label), y: H - mb + 15, text: t.label, pri: i })), o, ml, W);

    for (const bl of bandLabels) {
      const b = textBox(bl.x, mt + 11, textW(bl.text));
      if (hitsText(o, b) || hitsMarks(o, b)) continue;
      o.text.push(b);
      svg('text', { x: bl.x, y: mt + 11, text: bl.text }, bg);
    }
    refLabels();

    const hov = svg('g', { cls: 'hover', 'aria-hidden': 'true' }, s);
    const keys = uniq(g.series.flatMap((se) => se.ts)).sort((a, b) => a - b);
    // an annotation line whose label found no free lane is named in the tooltip at its nearest data point,
    // so every line can be identified (its <title> needs a pointer exactly on a one-pixel line)
    const dropped = new Map();
    if (keys.length) {
      for (const a of anns.items) {
        if (a.lines.length) continue;
        const k = keys[nearestIdx(keys, sx.inv(a.x))];
        dropped.set(k, dropped.has(k) ? dropped.get(k) + '; ' + a.full : a.full);
      }
    }
    return {
      g, keys, mt,
      keyAt(px) {
        if (!keys.length) return null;
        const t = sx.inv(clamp(px, ml, W - mr));
        return keys[nearestIdx(keys, t)];
      },
      show(t) {
        hov.textContent = '';
        if (t < P.xd[0] || t > P.xd[1]) return null;
        const X = sx(t);
        svg('line', { cls: 'crosshair', x1: Math.round(X) + 0.5, x2: Math.round(X) + 0.5, y1: mt, y2: H - mb }, hov);
        const rows = [];
        let ysum = 0;
        let yn = 0;
        for (const se of g.series) {
          const k = nearestIdx(se.ts, t);
          const hit = k >= 0 && Math.abs(se.ts[k] - t) <= se.tol ? se.nn[k] : null;
          if (hit) {
            const Y = sy(hit[1]);
            ysum += Y;
            yn += 1;
            svg('circle', { cls: 'dot-mark', cx: sx(hit[0]), cy: Y, r: 4, style: { fill: cvar(se.entity) } }, hov);
          }
          rows.push({
            color: cvar(se.entity), shape: 'line', dash: se.dashed, label: se.label,
            value: hit ? fmt(hit[1], se.unit) : 'no data',
            note: hit && hit[0] !== t ? (P.monthly ? dateMonth(hit[0]) : dateDay(hit[0])) : '',
          });
        }
        return { rows, x: X, y: yn ? ysum / yn : mt, note: dropped.get(t) || '' };
      },
      clear() { hov.textContent = ''; },
    };
  }

  function emptyPanel(s, W, H, g) {
    svg('text', { cls: 'na-mark', x: W / 2, y: H / 2, 'text-anchor': 'middle', text: 'No values in this build' }, s);
    return { g, keys: [], keyAt: () => null, show: () => null, clear() {} };
  }

  /* ------------------------------------------------------------------ columns and diverging bars */

  function catPositionFor(P, cats, x) {
    const i = cats.indexOf(String(x));
    if (i >= 0) return i;
    if (P.xTime && looksDate(String(x))) {
      const t = toTime(String(x));
      let best = -1;
      cats.forEach((c, k) => { const tc = toTime(c); if (Number.isFinite(tc) && tc <= t) best = k; });
      return best;
    }
    return -1;
  }

  const MIN_BAR = 4; // px: a non-zero column never renders shorter than this (its value is labelled)

  function drawCols(g, P) {
    const { s, W, H } = startSvg(g, P);
    const cats = P.catsOf(g);
    if (!cats.length) return emptyPanel(s, W, H, g);
    const o = newOcc();
    const n = cats.length;
    const ticksN = Math.max(3, Math.floor((H - 80) / 46));
    const yi = g.ticks(H - 80, (H - 80) / ticksN);
    const tl = yi.ticks.map((v) => tickFmt(v, P.unit, yi.step));
    const ml = Math.ceil(Math.max(20, ...tl.map((t) => textW(t)))) + 10;
    const mr = 8;
    const step = (W - ml - mr) / n;
    const labels = cats.map((c) => P.catLabel(c));
    const maxW1 = Math.max(...labels.map((l) => textW(l)));
    let lines = 1;
    let every = 1;
    let rotate = false;
    if (maxW1 + 8 > step) {
      const twoLine = labels.map((l) => split2(l));
      const maxW2 = Math.max(...twoLine.map((ls) => Math.max(...ls.map((x) => textW(x)))));
      if (labels.some((l) => /\s/.test(l)) && maxW2 + 8 <= step) lines = 2;
      // a category label names its column: rotate before dropping any (only an impossibly narrow column thins)
      else if (step >= 14) rotate = true;
      else every = Math.ceil((maxW1 + 10) / step);
    }
    const ROT = 40;
    const rotW = Math.min(maxW1, 96);
    const rotH = rotate ? Math.ceil(rotW * Math.sin(ROT * Math.PI / 180) + 11 * Math.cos(ROT * Math.PI / 180)) : 0;
    let top = drawPanelLabel(s, g, W, 2);
    top = drawPanelLegend(s, P, g, W, top);
    const annItems = annsFor(P, g).map((a) => {
      const i = catPositionFor(P, cats, a.x);
      return i < 0 ? null : { x: ml + step * (i + 0.5), label: a.label, also: a.also, kind: a.kind };
    }).filter(Boolean);
    const anns = layoutAnns(annItems, 0, W - 2, W < 420 ? 2 : 3);
    const annTop = top;
    const lanesBottom = annTop + anns.rows * LANE;
    top += anns.rows ? anns.rows * LANE + 4 : 0;
    if (UNIT_CAPTION[P.unit]) o.text.push(textBox(0, top + 9, textW(UNIT_CAPTION[P.unit])));
    top = drawUnit(s, P.unit, 0, top);

    const hasBars = g.series.some((se) => se.role !== 'context');
    const barSeries = g.series.filter((se) => !(P.form === 'column' && se.role === 'context' && hasBars));
    const dotSeries = g.series.filter((se) => !barSeries.includes(se));
    // dashed (hatched) fill only marks a method change inside one chart; a lone series draws solid
    const mixDash = barSeries.some((se) => se.dashed) && barSeries.some((se) => !se.dashed);
    // with a clamped domain, values past an edge get an arrow and a printed value: reserve room for both
    // (up to two more 12px tiers when several values are off-scale, so neighbours can stagger)
    let offHi = 0;
    let offLo = 0;
    if (P.clamped) {
      for (const se of barSeries) {
        for (const c of cats) {
          const v = first(se, c);
          if (isNum(v) && v > yi.hi) offHi += 1;
          if (isNum(v) && v < yi.lo) offLo += 1;
        }
      }
    }
    const room = (k) => (k ? 22 + 12 * (Math.min(k, 3) - 1) : 0);
    const mt = Math.max(top + 16 + room(offHi), P.forceMt || 0);
    const mb = rotate ? rotH + 12 : lines === 2 ? 34 : 21;
    const bottom = H - mb - room(offLo);
    const sy = yScale(P, yi, bottom, mt);
    drawYGrid(s, yi, sy, ml, W - mr, P.unit, o);

    const bg = svg('g', { cls: 'band' }, s);
    for (const b of P.bands) {
      if (P.yBand(b)) {
        // horizontal bands are named in the legend (they would collide with bar labels in the plot)
        const y0 = sy(clamp(+b.to, yi.lo, yi.hi));
        const y1 = sy(clamp(+b.from, yi.lo, yi.hi));
        titled(svg('rect', { x: ml, width: W - mr - ml, y: Math.min(y0, y1), height: Math.abs(y1 - y0) }, bg), b.label);
      } else {
        const a = catPositionFor(P, cats, b.from);
        const z = catPositionFor(P, cats, b.to);
        if (a < 0 || z < a) continue;
        titled(svg('rect', { x: ml + step * a, width: step * (z - a + 1), y: mt, height: bottom - mt }, bg), b.label);
      }
    }
    const hoverBand = svg('rect', { cls: 'hover-band', x: 0, y: mt, width: 0, height: bottom - mt, visibility: 'hidden' }, s);

    const exclusive = cats.every((c) => barSeries.filter((se) => isNum(first(se, c))).length <= 1);
    // x.pack: a category draws only the series it has a value for, side by side and centred (two methods that
    // never share a column, e.g. an exact and a sampled share, then take one slot instead of leaving a hole)
    const pack = !!(P.x && P.x.pack);
    const present = (c) => barSeries.filter((se) => isNum(first(se, c)));
    const slots = exclusive ? 1 : Math.max(1, pack ? Math.max(...cats.map((c) => present(c).length)) : barSeries.length);
    const gap = 2;
    const bw = Math.max(2, Math.min(24, (step * 0.72 - gap * (slots - 1)) / slots));
    const zero = sy(clamp(0, yi.lo, yi.hi));
    const bars = svg('g', { cls: 'bars' }, s);
    const cands = [];
    cats.forEach((c, i) => {
      const cx = ml + step * (i + 0.5);
      const here = pack ? present(c) : barSeries;
      const k = exclusive ? 1 : Math.max(1, pack ? here.length : slots);
      const gw = k * bw + (k - 1) * gap;
      let any = false;
      barSeries.forEach((se, j) => {
        const v = first(se, c);
        if (!isNum(v)) return;
        any = true;
        const x = cx - gw / 2 + (exclusive ? 0 : pack ? here.indexOf(se) : j) * (bw + gap);
        const off = P.clamped ? (v > yi.hi ? 1 : v < yi.lo ? -1 : 0) : 0;
        let y1 = sy(clamp(v, yi.lo, yi.hi));
        if (v !== 0 && Math.abs(y1 - zero) < MIN_BAR) y1 = zero + (v > 0 ? -MIN_BAR : MIN_BAR);
        const color = P.colorFor(se, v);
        const st = se.dashed && mixDash
          ? { fill: `color-mix(in srgb, ${color} 28%, transparent)`, stroke: color, strokeWidth: '1.5px', strokeDasharray: '4 3' }
          : { fill: color };
        if (se.role === 'context' && P.form !== 'column') st.opacity = '0.75';
        // off-scale: square end at the plot edge, then a 2px gap and an arrow pointing past it
        svg('path', { cls: 'bar', d: vbar(x, bw, zero, y1, off ? 0 : 4), style: st }, bars);
        o.marks.push([x, Math.min(zero, y1), x + bw, Math.max(zero, y1)]);
        let yEnd = y1;
        if (off) {
          const ya = y1 - off * 2;
          const yb = y1 - off * 9;
          svg('path', { cls: 'off-arrow', d: `M${f1(x)},${f1(ya)}L${f1(x + bw)},${f1(ya)}L${f1(x + bw / 2)},${f1(yb)}Z`, style: { fill: color } }, bars);
          o.marks.push([x, Math.min(ya, yb), x + bw, Math.max(ya, yb)]);
          yEnd = yb;
        }
        cands.push({ se, v, x: x + bw / 2, yEnd, up: v >= 0, i, off });
      });
      for (const se of dotSeries) {
        const v = first(se, c);
        if (!isNum(v)) continue;
        any = true;
        const cy = sy(clamp(v, yi.lo, yi.hi));
        svg('circle', { cls: 'dot-mark', cx, cy, r: 4.5, style: { fill: cvar(se.entity) } }, bars);
        o.marks.push([cx - 5, cy - 5, cx + 5, cy + 5]);
      }
      if (!any) {
        svg('text', { cls: 'na-mark', x: cx, y: zero - 5, 'text-anchor': 'middle', text: 'n/a' }, bars);
        o.text.push(textBox(cx, zero - 5, textW('n/a', 10), 'middle', 10));
      }
    });
    if (yi.lo <= 0 && yi.hi >= 0) {
      const Z = Math.round(zero) + 0.5;
      svg('line', { cls: 'baseline', x1: ml, x2: W - mr, y1: Z, y2: Z }, s);
    }
    const refLabels = drawRefsY(s, P, sy, ml, W - mr, mt, bottom, o);

    /* annotation labels in their lanes, each with a dotted leader down to its column and a marker above it */
    const ag = svg('g', { cls: 'ann' }, s);
    for (const a of anns.items) {
      const X = Math.round(a.x) + 0.5;
      const y1 = a.lines.length ? annTop + (a.l1 + 1) * LANE - 1 : lanesBottom - 4;
      // the top of the annotated column (its tallest bar, or the zero line), less room for its value label
      const ci = Math.floor((a.x - ml) / step);
      const tops = cands.filter((c) => c.i === ci).map((c) => Math.min(c.yEnd, zero));
      const colTop = tops.length ? Math.min(...tops) : zero;
      const y2 = Math.max(y1 + 5, Math.min(colTop - 18, bottom - 4));
      titled(svg('line', { x1: X, x2: X, y1, y2, 'stroke-dasharray': '2 2' }, ag), a.lines.length ? null : a.full);
      svg('path', { cls: 'ann-mark', d: `M${f1(X - 3.5)},${f1(y2)}L${f1(X + 3.5)},${f1(y2)}L${f1(X)},${f1(y2 + 4.5)}Z` }, ag);
      o.segs.push([X, y1, X, y2 + 5]);
    }
    drawAnnLabels(ag, anns, annTop, o);

    /* category labels: the latest is kept, then every `every`-th back from it, none touching another */
    const ax = svg('g', { cls: 'ax', 'aria-hidden': 'true' }, s);
    const xItems = [];
    for (let i = n - 1; i >= 0; i -= every) {
      const X = ml + step * (i + 0.5);
      const room = step * every - 6;
      if (rotate) {
        // drawn directly: rotated labels step with their columns, so they never overlap one another
        const t = fit(labels[i], rotW);
        const Y = H - mb + 12;
        titled(svg('text', { x: X + 3, y: Y, 'text-anchor': 'end', transform: `rotate(-${ROT} ${f1(X + 3)} ${f1(Y)})`, text: t }, ax), t !== labels[i] ? labels[i] : null);
        continue;
      }
      if (lines === 2) {
        const [a, b = ''] = split2(labels[i]);
        const fa = fit(a, room);
        const fb = b ? fit(b, room) : '';
        xItems.push({ x: X, w: Math.max(textW(fa), fb ? textW(fb) : 0), y: H - mb + 14, lines: 2, a: fa, b: fb, full: labels[i], pri: i });
      } else {
        const t = fit(labels[i], room);
        xItems.push({ x: X, w: textW(t), y: H - mb + 15, text: t, full: labels[i], pri: i });
      }
    }
    placeXLabels(ax, xItems, o, ml, W);

    /* value labels, by priority: off-scale values (the bar no longer shows them), the latest column,
       the extremes, then (when every column has room) the rest. A label that would cover text or
       another column is dropped, except the latest, which lifts clear of a taller neighbour. */
    const widest = Math.max(0, ...cands.map((l) => textW(fmt(l.v, l.se.unit))));
    const roomy = cands.length <= 12 && step / slots >= widest + 2;
    const pri = new Map();
    const by = new Map();
    for (const l of cands) { if (!by.has(l.se)) by.set(l.se, []); by.get(l.se).push(l); }
    for (const [se, arr] of by) {
      if (roomy) { for (const l of arr) pri.set(l, 1); }
      if (se.role === 'context') continue;
      const last = arr[arr.length - 1];
      const mx = arr.reduce((a, b) => (b.v > a.v ? b : a));
      const mn = arr.reduce((a, b) => (b.v < a.v ? b : a));
      for (const [l, p] of [[mn, 2], [mx, 3], [last, 4]]) pri.set(l, Math.max(pri.get(l) || 0, p));
    }
    for (const l of cands) if (l.off) pri.set(l, 5);
    const lab = svg('g', { cls: 'value-labels', 'aria-hidden': 'true' }, s);
    const lim = [ml, annTop, W, H - mb + 2];
    const order = cands.filter((l) => pri.has(l)).sort((a, b) => pri.get(b) - pri.get(a) || b.i - a.i);
    for (const l of order) {
      const t = fmt(l.v, l.se.unit);
      const endpoint = l.i === n - 1 && l.se.role !== 'context';
      const strong = l.i === n - 1 || !!l.off;
      const w = textW(t, 11, strong ? 600 : 400);
      const cx = clamp(l.x, ml + w / 2 + 2, W - w / 2 - 2);
      let x = cx;
      const at = (y, xx = cx) => { const b = textBox(xx, y, w, 'middle'); return within(b, lim) && !hitsText(o, b) && !hitsMarks(o, b) ? b : null; };
      const base = l.up ? l.yEnd - 5 : l.yEnd + 13;
      const away = l.up ? -1 : 1;
      let y = base;
      let b = null;
      // off-scale values stagger outward in 12px tiers, then sideways, until they clear their neighbours
      const tries = l.off ? [[0, 0], [1, 0], [2, 0], [0, 1], [0, -1], [1, 1], [1, -1], [2, 1], [2, -1], [3, 0]] : [[0, 0]];
      for (const [k, side] of tries) {
        const xx = clamp(cx + side * (w / 2 + 3), ml + w / 2 + 2, W - w / 2 - 2);
        y = base + away * 12 * k;
        b = at(y, xx);
        if (b) { x = xx; break; }
      }
      if (!b && endpoint) {
        const over = o.marks.filter((m) => m[0] < x + w / 2 + 2 && m[2] > x - w / 2 - 2);
        y = l.up ? Math.min(...over.map((m) => m[1])) - 5 : Math.max(...over.map((m) => m[3])) + 13;
        b = at(y);
      }
      // the latest value is never dropped: as a last resort it sits on a surface plate at its column
      let plate = false;
      if (!b && endpoint) {
        y = base;
        b = textBox(x, y, w, 'middle');
        plate = true;
      }
      if (!b) continue;
      o.text.push(b);
      const cls = 'label' + (strong ? ' strong' : '');
      if (plate) plated(lab, x, y, t, 'middle', null, cls);
      else svg('text', { cls, x, y, 'text-anchor': 'middle', text: t }, lab);
    }
    refLabels();

    return {
      g, keys: cats, mt,
      keyAt(px) {
        const i = Math.floor((px - ml) / step);
        return i >= 0 && i < n ? cats[i] : null;
      },
      show(c) {
        const i = cats.indexOf(c);
        if (i < 0) return null;
        hoverBand.setAttribute('x', f1(ml + step * i));
        hoverBand.setAttribute('width', f1(step));
        hoverBand.setAttribute('visibility', 'visible');
        let yTop = bottom;
        const rows = g.series.map((se) => {
          const v = first(se, c);
          if (isNum(v)) yTop = Math.min(yTop, sy(clamp(v, yi.lo, yi.hi)));
          const dot = dotSeries.includes(se);
          return { color: P.colorFor(se, v), shape: dot ? 'dot' : 'sq', dash: se.dashed && mixDash, label: se.label, value: fmt(v, se.unit) };
        });
        // anchored clear of the column and its value label on either side, so the tip never covers the figure
        const cx = ml + step * (i + 0.5);
        const half = Math.max((slots * bw + (slots - 1) * gap) / 2, ...rows.map((r) => textW(r.value, 11, 600) / 2)) + 2;
        const yMid = (Math.max(mt, yTop) + zero) / 2;
        return { rows, x: cx + half - 14, xl: cx - half + 14, y: Math.max(mt + 20, yMid) };
      },
      clear() { hoverBand.setAttribute('visibility', 'hidden'); },
    };
  }

  /* ------------------------------------------------------------------ horizontal rows: bar, dot-range */

  function drawRows(g, P) {
    const { s, W, H } = startSvg(g, P);
    const cats = P.catsOf(g);
    if (!cats.length) return emptyPanel(s, W, H, g);
    const n = cats.length;
    const form = P.form;
    const labelsLeft = form !== 'bar';
    const labels = cats.map((c) => P.catLabel(c));
    const ml = labelsLeft ? Math.ceil(Math.min(W * 0.38, Math.max(...labels.map((l) => textW(l, 12))) + 16)) : 4;
    const seriesAt = (c) => g.series.filter((se) => se.at.has(c));
    const valueTexts = [];
    if (form === 'bar') for (const se of g.series) for (const c of se.cats) valueTexts.push(fmt(first(se, c), se.unit));
    const mr = form === 'bar' ? Math.ceil(Math.max(0, ...valueTexts.map((t) => textW(t, 11, 600)))) + 12 : 14;
    const vi = g.ticks(W - ml - mr, 84);
    let top = drawPanelLabel(s, g, W, 2);
    top = drawUnit(s, P.unit, W - mr, top, 'end');
    const lo = vi.lo;
    const hi = vi.hi;
    // a reference line's label gets a line of its own over the rows, so it never meets a row's dot or value
    const refs = P.refs.filter((r) => r.v !== 0 && r.v >= lo && r.v <= hi);
    const rl = refs.some((r) => r.label) ? 14 : 0;
    const mt = top + 4 + rl;
    const mb = 22;
    const rowH = (H - mt - mb) / n;
    const sv = linear(vi.lo, vi.hi, ml, W - mr);

    const grid = svg('g', { cls: 'grid', 'aria-hidden': 'true' }, s);
    const ax = svg('g', { cls: 'ax', 'aria-hidden': 'true' }, s);
    for (const v of vi.ticks) {
      const X = Math.round(sv(v)) + 0.5;
      if (v !== 0) svg('line', { x1: X, x2: X, y1: mt, y2: H - mb }, grid);
      const t = tickFmt(v, P.unit, vi.step);
      const w = textW(t);
      svg('text', { x: X, y: H - mb + 15, 'text-anchor': X - w / 2 < 0 ? 'start' : X + w / 2 > W ? 'end' : 'middle', text: t }, ax);
    }
    const hoverBand = svg('rect', { cls: 'hover-band', x: 0, y: 0, width: W, height: rowH, visibility: 'hidden' }, s);
    if (lo <= 0 && hi >= 0) {
      const Z = Math.round(sv(0)) + 0.5;
      svg('line', { cls: 'baseline', x1: Z, x2: Z, y1: mt, y2: H - mb }, s);
    }
    const ref = svg('g', { cls: 'ref' }, s);
    for (const r of refs) {
      const X = Math.round(sv(r.v)) + 0.5;
      svg('line', { x1: X, x2: X, y1: mt - rl, y2: H - mb }, ref);
      if (!r.label) continue;
      // right of the line, or left of it where the right has no room for the whole label
      const w = textW(r.label), room = W - X - 6, left = w > room && X - 6 > room;
      titled(svg('text', { x: left ? X - 4 : X + 4, y: mt - 5, 'text-anchor': left ? 'end' : 'start', text: fit(r.label, left ? X - 6 : room) }, ref), r.label);
    }

    const marks = svg('g', { cls: 'marks' }, s);
    const lab = svg('g', { cls: 'value-labels' }, s);
    const rowLabels = svg('g', { cls: 'rows', 'aria-hidden': 'true' }, s);
    const sclamp = (v) => sv(clamp(v, lo, hi));
    // x.highlight: categories the model marks get a heavier row label
    const hl = new Set(Array.isArray(P.x.highlight) ? P.x.highlight.map(String) : []);
    cats.forEach((c, i) => {
      const y0 = mt + rowH * i;
      const yc = y0 + rowH / 2;
      const here = seriesAt(c);
      const rcls = 'row-label' + (hl.has(String(c)) ? ' hl' : '');
      if (labelsLeft) {
        titled(svg('text', { cls: rcls, x: ml - 10, y: yc + 4, 'text-anchor': 'end', text: fit(labels[i], ml - 12, 12) }, rowLabels), labels[i]);
      } else {
        titled(svg('text', { cls: rcls, x: 4, y: y0 + 14, text: fit(labels[i], W - 8, 12) }, rowLabels), labels[i]);
      }
      if (form === 'bar') {
        const k = Math.max(1, here.length);
        const thick = clamp(rowH - 26, 8, 22);
        const each = (thick - (k - 1) * 2) / k;
        here.forEach((se, j) => {
          const v = first(se, c);
          const by = y0 + 20 + j * (each + 2);
          if (!isNum(v)) {
            svg('text', { cls: 'na-mark', x: sv(clamp(0, lo, hi)) + 4, y: by + each / 2 + 4, text: 'n/a' }, marks);
            return;
          }
          const x0 = sv(clamp(0, lo, hi));
          const x1 = sclamp(v);
          const off = P.clamped ? (v > hi ? 1 : v < lo ? -1 : 0) : 0;
          const t = fmt(v, se.unit);
          const bar = svg('path', { cls: 'bar', d: hbar(by, each, x0, x1, off ? 0 : 4), style: { fill: P.colorFor(se, v) } }, marks);
          if (off) {
            /* cut at the axis edge (y.clamp): a break mark, two slanted gaps across the bar near its end; the
               value label still prints the full value, and the bar's title names it with the series label */
            titled(bar, `${se.label}: ${t}`);
            const brk = svg('g', { cls: 'break-mark', 'aria-hidden': 'true' }, marks);
            const bx = x1 - off * 12;
            for (const dx of [-3, 3]) {
              svg('line', { x1: f1(bx + dx - 3), y1: f1(by + each + 2), x2: f1(bx + dx + 3), y2: f1(by - 2) }, brk);
            }
          }
          const right = v >= 0;
          svg('text', {
            cls: 'label' + (se.role === 'emphasis' ? ' strong' : ''), x: right ? Math.max(x1, x0) + 6 : Math.min(x1, x0) - 6,
            y: by + each / 2 + 4, 'text-anchor': right ? 'start' : 'end', text: t,
          }, lab);
        });
      } else {
        for (const se of here) {
          const [v, mn, mx] = se.at.get(c);
          if (isNum(mn) && isNum(mx)) {
            const a = sclamp(Math.min(mn, mx));
            const z = sclamp(Math.max(mn, mx));
            svg('rect', { cls: 'range', x: a, y: yc - 3, width: Math.max(2, z - a), height: 6, rx: 3, style: { fill: 'var(--range)' } }, marks);
          }
          if (!isNum(v)) {
            svg('text', { cls: 'na-mark', x: sv(clamp(0, lo, hi)) + 4, y: yc + 4, text: 'n/a' }, marks);
            continue;
          }
          const off = v < lo ? -1 : v > hi ? 1 : 0;
          const X = sclamp(v);
          if (off) {
            const tip = off > 0 ? X + 8 : X - 8;
            svg('path', { d: `M${f1(tip)},${f1(yc)}L${f1(X)},${f1(yc - 5)}L${f1(X)},${f1(yc + 5)}Z`, style: { fill: cvar(se.entity) } }, marks);
          }
          svg('circle', { cls: 'dot-mark', cx: X, cy: yc, r: 5.5, style: { fill: cvar(se.entity) } }, marks);
          const emph = off || se.role === 'emphasis' || se.entity === 'near' || se.entity === 'usd';
          if (emph) {
            /* the label sits on its own row (vertically centred on the dot), past the end of the range bar, so
               it never floats between rows or covers the bar */
            const t = fmt(v, se.unit);
            const w = textW(t, 11, 600);
            const ranged = isNum(mn) && isNum(mx);
            const rHi = ranged ? Math.max(X, sclamp(Math.max(mn, mx))) : X;
            const rLo = ranged ? Math.min(X, sclamp(Math.min(mn, mx))) : X;
            const pad = off ? 12 : 9;
            const toRight = off > 0 ? false : off < 0 ? true : rHi + pad + w < W - 2 || rLo - pad - w < ml;
            svg('text', {
              cls: 'label' + (off || se.entity === 'near' ? ' strong' : ''), x: toRight ? rHi + pad : rLo - pad,
              y: yc + 4, 'text-anchor': toRight ? 'start' : 'end', text: t,
            }, lab);
          }
        }
      }
    });

    return {
      g, keys: cats,
      keyAt(px, py) {
        const i = Math.floor((py - mt) / rowH);
        return i >= 0 && i < n ? cats[i] : null;
      },
      show(c) {
        const i = cats.indexOf(c);
        if (i < 0) return null;
        hoverBand.setAttribute('y', f1(mt + rowH * i));
        hoverBand.setAttribute('height', f1(rowH));
        hoverBand.setAttribute('visibility', 'visible');
        let xMax = ml;
        const rows = [];
        for (const se of seriesAt(c)) {
          const arr = se.at.get(c) || [];
          const v = arr[0];
          if (isNum(v)) xMax = Math.max(xMax, sclamp(v));
          const range = form === 'dot-range' && isNum(arr[1]) && isNum(arr[2]) ? `range ${fmt(arr[1], se.unit)} to ${fmt(arr[2], se.unit)}` : '';
          rows.push({
            color: form === 'bar' ? P.colorFor(se, v) : cvar(se.entity), shape: form === 'bar' ? 'sq' : 'dot',
            label: form === 'dot-range' || form === 'bar' ? (range || se.label) : se.label, value: fmt(v, se.unit),
          });
        }
        return { rows, x: Math.min(W - 20, xMax + 8), y: mt + rowH * (i + 0.5) };
      },
      clear() { hoverBand.setAttribute('visibility', 'hidden'); },
    };
  }

  /* ------------------------------------------------------------------ table-heatmap */

  const colorMix = typeof CSS !== 'undefined' && CSS.supports && CSS.supports('color', 'color-mix(in oklab, red 50%, blue)');
  /* Diverging tint: the sign picks the pole, the size of the gap picks the strength, and a value on the
     midpoint stays the neutral gray. A non-zero gap never drops below a faint tint, so "slightly above"
     never reads as "exactly at". The legend swatches use the strongest tint a cell can get. */
  const HEAT_MAX = 50;
  const HEAT_MIN = 10;
  const heatTint = (pole, pct) => (colorMix ? `color-mix(in oklab, ${pole} ${pct}%, var(--div-mid))` : pole);
  const signedGap = (gap, unit) => (unit === '%' || unit === 'pp' ? fmt(gap, 'pp') : (gap > 0 ? '+' : '') + fmt(gap, unit));

  function drawHeat(g, P) {
    const el = g.el;
    el.textContent = '';
    const cols = P.catsOf(g);
    const rows = g.series;
    if (!cols.length || !rows.length) {
      html('p', 'na', 'No values in this build', el);
      return { g, keys: [], keyAt: () => null, show: () => null, clear() {} };
    }
    const center = P.center;
    let maxAbs = 0;
    for (const s of rows) for (const c of cols) { const v = first(s, c); if (isNum(v)) maxAbs = Math.max(maxAbs, Math.abs(v - center)); }
    const wrap = html('div', 'heat-wrap', null, el);
    const t = html('table', 'heat', null, wrap);
    t.setAttribute('aria-label', P.title);
    const tr0 = html('tr', null, null, html('thead', null, null, t));
    html('td', 'corner', '', tr0);
    // a header's value (x.cat_values / x.series_values) is data, formatted here next to its label
    const vu = P.x.values_unit || P.unit;
    const withVal = (label, v) => (isNum(v) ? `${label} (${fmt(v, vu)})` : label);
    const catVals = (P.x.cat_values && typeof P.x.cat_values === 'object') ? P.x.cat_values : {};
    const serVals = (P.x.series_values && typeof P.x.series_values === 'object') ? P.x.series_values : {};
    for (const c of cols) { const th = html('th', null, withVal(P.catLabel(c), catVals[c]), tr0); th.scope = 'col'; }
    const tb = html('tbody', null, null, t);
    const keys = [];
    const cells = new Map();
    for (const s of rows) {
      const tr = html('tr', null, null, tb);
      const th = html('th', null, withVal(s.label, serVals[s.id]), tr);
      th.scope = 'row';
      for (const c of cols) {
        const v = first(s, c);
        const k = s.id + '\u0001' + c;
        keys.push(k);
        const td = html('td', null, null, tr);
        td.dataset.k = k;
        html('span', 'heat-v', isNum(v) ? fmt(v, s.unit) : 'n/a', td);
        if (isNum(v)) {
          const gap = v - center;
          const pct = gap === 0 || maxAbs === 0 ? 0 : Math.round(HEAT_MIN + (HEAT_MAX - HEAT_MIN) * Math.abs(gap) / maxAbs);
          td.style.background = pct ? heatTint(gap > 0 ? 'var(--c-positive)' : 'var(--c-negative)', pct) : 'var(--div-mid)';
          td.dataset.side = gap > 0 ? 'above' : gap < 0 ? 'below' : 'at';
        }
        cells.set(k, { td, s, c, v });
      }
    }
    if (P.refs[0]) html('p', 'heat-note', `Tint: gap from ${P.refs[0].label} (${fmt(P.refs[0].v, P.unit)}); stronger tint, bigger gap; gray, on it.`, el);
    let on = null;
    return {
      g, keys,
      keyAt(px, py, e) {
        const td = e && e.target && e.target.closest ? e.target.closest('td[data-k]') : null;
        return td ? td.dataset.k : null;
      },
      show(k) {
        const cell = cells.get(k);
        if (!cell) return null;
        if (on) on.classList.remove('on');
        on = cell.td;
        on.classList.add('on');
        const v = cell.v;
        const gap = isNum(v) ? v - center : null;
        const side = gap === null ? '' : gap > 0 ? 'above' : gap < 0 ? 'below' : 'at';
        const ref = P.refs[0] ? P.refs[0].label : 'the midpoint';
        return {
          rows: [{
            color: gap === null || gap === 0 ? 'var(--div-mid)' : gap > 0 ? 'var(--c-positive)' : 'var(--c-negative)', shape: 'sq',
            label: gap === null ? cell.s.label : `${side} ${ref}`, value: fmt(v, cell.s.unit),
            note: gap === null || gap === 0 ? '' : `${signedGap(gap, cell.s.unit)} from it`,
          }],
          x: cell.td.offsetLeft + cell.td.offsetWidth, y: cell.td.offsetTop + cell.td.offsetHeight / 2,
        };
      },
      clear() { if (on) on.classList.remove('on'); on = null; },
    };
  }

  const DRAW = { line: drawLine, cols: drawCols, rows: drawRows, heat: drawHeat };

  /* ------------------------------------------------------------------ legend */

  function drawKey(k, it) {
    if (it.shape === 'line' && it.underlay) {
      svg('line', { x1: 3, x2: 13, y1: 5, y2: 5, 'stroke-width': 6, 'stroke-linecap': 'round', style: { stroke: it.color, strokeOpacity: '0.45' } }, k);
    } else if (it.shape === 'line') {
      svg('line', { x1: 1, x2: 15, y1: 5, y2: 5, 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-dasharray': it.dash ? '4 3' : null, style: { stroke: it.color } }, k);
    } else if (it.shape === 'ref') {
      svg('line', { x1: 1, x2: 15, y1: 5.5, y2: 5.5, 'stroke-width': 1, style: { stroke: it.color || 'var(--ink-2)' } }, k);
    } else if (it.shape === 'band') {
      svg('rect', { x: 1, y: 1, width: 14, height: 8, rx: 1, style: { fill: 'var(--band)', stroke: 'var(--axis)', strokeWidth: '1px' } }, k);
    } else if (it.shape === 'dot') {
      svg('circle', { cx: 8, cy: 5, r: 4, style: { fill: it.color } }, k);
    } else if (it.shape === 'range') {
      svg('rect', { x: 1, y: 3, width: 14, height: 4, rx: 2, style: { fill: it.color, opacity: '0.4' } }, k);
    } else if (it.split) {
      svg('rect', { x: 2, y: 0, width: 6, height: 10, rx: 1.5, style: { fill: 'var(--c-positive)' } }, k);
      svg('rect', { x: 9, y: 0, width: 6, height: 10, rx: 1.5, style: { fill: 'var(--c-negative)' } }, k);
    } else if (it.dash) {
      svg('rect', { x: 3, y: 0.75, width: 10, height: 8.5, rx: 2, style: { fill: `color-mix(in srgb, ${it.color} 28%, transparent)`, stroke: it.color, strokeWidth: '1.5px', strokeDasharray: '3 2' } }, k);
    } else {
      svg('rect', { x: 3, y: 0, width: 10, height: 10, rx: 2, style: { fill: it.color } }, k);
    }
  }

  /* Legend items. Series keys follow the marks: lines are lines, columns are swatches, and a dashed
     key appears only when the chart mixes dashed and solid marks (as the marks do). Small multiples
     name the series every panel shares once, above the chart, and each panel names its own series
     under its title (P.panelLegend), so a color is never explained by a different panel's title.
     Horizontal bands are always named here, never inside the plot. */
  function buildLegend(fig, P) {
    P.panelLegend = null;
    let items = [];
    if (P.form === 'dot-range') {
      // the range key only when some row draws a range (c21 plots single readings against a reference line)
      const ranged = P.all.some((se) => se.cats.some((c) => { const a = se.at.get(c) || []; return isNum(a[1]) && isNum(a[2]); }));
      items = [{ shape: 'dot', color: 'var(--ink-2)', label: 'Latest reading' }].concat(ranged ? [{ shape: 'range', color: 'var(--c-context)', label: 'Range over the window' }] : []);
    } else if (P.kind === 'heat') {
      if (P.refs[0]) {
        items = [
          { shape: 'sq', color: heatTint('var(--c-positive)', HEAT_MAX), label: 'Above ' + P.refs[0].label },
          { shape: 'sq', color: heatTint('var(--c-negative)', HEAT_MAX), label: 'Below ' + P.refs[0].label },
        ];
      }
    } else if (P.form !== 'bar' || P.all.some((a) => a.cats.some((c) => P.all.some((b) => b !== a && isNum(first(b, c)) && isNum(first(a, c)))))) {
      // (a bar chart needs a legend only when one row carries two series, e.g. latest against a year earlier)
      const plotted = uniq(P.groups.flatMap((g) => g.series));
      const hasBars = plotted.some((s) => s.role !== 'context');
      const mixDash = plotted.some((s) => s.dashed) && plotted.some((s) => !s.dashed);
      const keyOf = (s) => {
        let shape = 'sq';
        if (P.kind === 'line') shape = 'line';
        else if (P.form === 'column' && s.role === 'context' && hasBars) shape = 'dot';
        return { shape, color: cvar(s.entity), dash: P.kind === 'line' ? s.dashed : mixDash && s.dashed, split: P.signColor && s.role === 'diverging', underlay: s.underlay, label: s.label };
      };
      const byLabel = (list) => { const seen = new Set(); return list.filter((s) => !seen.has(s.label) && seen.add(s.label)); };
      const nLabels = new Set(plotted.map((s) => s.label)).size;
      if (P.kind === 'line' && P.groups.length > 1 && nLabels >= 2) {
        const sets = P.groups.map((g) => new Set(g.series.map((s) => s.label)));
        const everywhere = (s) => sets.every((st) => st.has(s.label));
        items = byLabel(plotted.filter(everywhere)).map(keyOf);
        // a panel that draws a single series is named by its own title and needs no key
        P.panelLegend = new Map(P.groups.map((g) => [g, g.series.length < 2 ? [] : byLabel(g.series.filter((s) => !everywhere(s))).map(keyOf)]));
      } else {
        items = byLabel(plotted).map(keyOf);
        if (items.length < 2 && !items.some((i) => i.split)) items = [];
      }
    }
    if (P.kind !== 'heat') for (const b of P.bands) if (P.yBand(b) && b.label) items.push({ shape: 'band', label: String(b.label) });
    P.legendItems = items;
    renderLegend(fig.querySelector('.legend'), items, []);
  }

  /* items: the fixed legend; dyn: reference lines whose label found no free spot in this draw */
  function renderLegend(box, items, dyn) {
    if (!box) return;
    box.textContent = '';
    const seen = new Set();
    for (const it of items.concat(dyn)) {
      if (seen.has(it.shape + '\u0001' + it.label)) continue;
      seen.add(it.shape + '\u0001' + it.label);
      const sp = html('span', 'legend-item' + (it.shape === 'ref' ? ' legend-ref' : ''), null, box);
      drawKey(svg('svg', { cls: 'legend-key', width: 16, height: 10, viewBox: '0 0 16 10', 'aria-hidden': 'true' }, sp), it);
      html('span', null, it.label, sp);
    }
    box.classList.toggle('is-empty', !box.childElementCount);
  }

  /* ------------------------------------------------------------------ hover and keyboard controller */

  function controller(plot, P) {
    const tip = plot.querySelector('.tip') || html('div', 'tip', null, plot);
    let views = [];
    let keys = [];
    let index = new Map();
    let cur = -1;

    function hide() {
      tip.hidden = true;
      for (const v of views) v.clear();
      cur = -1;
    }
    function place(a, v) {
      const pr = plot.getBoundingClientRect();
      const er = v.g.el.getBoundingClientRect();
      const ox = er.left - pr.left;
      const oy = er.top - pr.top;
      const tw = tip.offsetWidth;
      const th = tip.offsetHeight;
      let x = ox + a.x + 14;
      // xl: the left anchor when it differs (a column's tip flips to the column's left side, past its label)
      if (x + tw > pr.width) x = ox + (isNum(a.xl) ? a.xl : a.x) - 14 - tw;
      if (x < 0) x = clamp(ox + a.x - tw / 2, 0, Math.max(0, pr.width - tw));
      const y = clamp(oy + a.y - th / 2, 0, Math.max(0, pr.height - th));
      tip.style.left = f1(x) + 'px';
      tip.style.top = f1(y) + 'px';
    }
    function show(i, via, hint) {
      if (i < 0 || i >= keys.length) return;
      cur = i;
      const key = keys[i];
      tip.setAttribute('aria-live', via === 'key' ? 'polite' : 'off');
      tip.textContent = '';
      html('div', 'tip-h', P.keyLabel(key), tip);
      let anchor = null;
      let anchorView = null;
      for (const v of views) {
        const r = v.show(key);
        if (!r) continue;
        if (views.length > 1 && v.g.label) html('div', 'tip-g', v.g.label, tip);
        for (const row of r.rows) {
          const line = html('div', 'tip-row', null, tip);
          const k = html('span', 'tip-key ' + (row.shape || 'line') + (row.dash ? ' dash' : ''), null, line);
          if ((row.shape || 'line') === 'line') k.style.borderTopColor = row.color;
          else k.style.background = row.color;
          html('span', 'tip-v', row.value, line);
          html('span', 'tip-l', row.label + (row.note ? ' · ' + row.note : ''), line);
        }
        if (r.note) html('div', 'tip-n', 'Marked line: ' + r.note, tip);
        if (!anchor || v === hint) { anchor = r; anchorView = v; }
      }
      if (!anchor) { tip.hidden = true; return; }
      tip.hidden = false;
      place(anchor, anchorView);
    }
    function fromPointer(e) {
      const v = views.find((vv) => vv.g.el.contains(e.target));
      if (!v) return;
      const r = v.g.el.getBoundingClientRect();
      const k = v.keyAt(e.clientX - r.left, e.clientY - r.top, e);
      if (k === null || k === undefined) return;
      const i = index.get(k);
      if (i !== undefined && (i !== cur || tip.hidden)) show(i, 'pointer', v);
    }
    plot.addEventListener('pointermove', fromPointer);
    plot.addEventListener('pointerdown', fromPointer);
    plot.addEventListener('pointerleave', () => { if (d.activeElement !== plot) hide(); });
    plot.addEventListener('blur', hide);
    plot.addEventListener('keydown', (e) => {
      const n = keys.length;
      if (!n) return;
      const page = Math.max(1, Math.ceil(n / 10));
      let i = cur;
      switch (e.key) {
        case 'ArrowRight': case 'ArrowDown': i = cur < 0 ? 0 : Math.min(n - 1, cur + 1); break;
        case 'ArrowLeft': case 'ArrowUp': i = cur < 0 ? n - 1 : Math.max(0, cur - 1); break;
        case 'PageDown': i = cur < 0 ? 0 : Math.min(n - 1, cur + page); break;
        case 'PageUp': i = cur < 0 ? n - 1 : Math.max(0, cur - page); break;
        case 'Home': i = 0; break;
        case 'End': i = n - 1; break;
        case 'Escape': hide(); return;
        default: return;
      }
      e.preventDefault();
      show(i, 'key', null);
    });
    return {
      setViews(v) {
        hide();
        views = v;
        const all = views.flatMap((x) => x.keys);
        keys = P.kind === 'line' ? uniq(all).sort((a, b) => a - b) : uniq(all);
        index = new Map(keys.map((k, i) => [k, i]));
      },
    };
  }

  /* ------------------------------------------------------------------ loading */

  const cache = new Map();
  function load(id) {
    if (!cache.has(id)) {
      cache.set(id, fetch(`data/charts/${encodeURIComponent(id)}.json`, { credentials: 'same-origin' }).then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      }));
    }
    return cache.get(id);
  }

  function fail(fig, reason) {
    fig.dataset.loaded = 'failed';
    const btn = fig.querySelector('.tbl-btn');
    if (btn) btn.hidden = true;
    const note = fig.querySelector('.load-note');
    if (!note) return;
    let text = reason === 'empty'
      ? 'This build has no plottable values for this chart, so the table below shows what there is.'
      : 'The interactive chart did not load, so the table below shows every value behind it.';
    if (reason !== 'empty' && location.protocol === 'file:') {
      text += ' Browsers block data requests from pages opened as files; serve the site folder over HTTP (for example with Python’s http.server module) to see the charts.';
    }
    note.textContent = text;
    note.hidden = false;
  }

  function mount(fig, chart) {
    const plot = fig.querySelector('.plot');
    if (!plot) return;
    const P = prepare(fig, plot, chart);
    if (!P) return fail(fig, 'error');
    if (!P.hasData && P.kind !== 'heat') return fail(fig, 'empty');
    buildLegend(fig, P);
    const box = fig.querySelector('.legend');
    const ctl = controller(plot, P);
    let lastKey = '';
    const render = () => { P.dyn = []; return P.groups.map((g) => DRAW[P.kind](g, P)); };
    // the widths a draw depends on: the plot and every panel (a breakpoint can change panel widths alone)
    const widths = () => [plot.clientWidth, ...P.groups.map((g) => g.el.clientWidth)].join(',');
    const drawOnce = () => {
      // clear every panel before measuring any: an old fixed-width svg in one panel must not size the others
      for (const g of P.groups) g.el.textContent = '';
      // every panel reserves the rows the widest panel legend needs, so plots stay level side by side
      P.legendRows = P.panelLegend
        ? Math.max(0, ...P.groups.map((g) => panelLegendRows(P.panelLegend.get(g), Math.max(120, g.el.clientWidth))))
        : 0;
      plot.style.setProperty('--legend-rows', String(P.legendRows));
      P.forceMt = 0;
      const before = widths();
      let views = render();
      // panels whose annotations need different numbers of lanes are redrawn on the deepest top margin
      const mts = views.map((v) => v.mt || 0).filter((m) => m > 0);
      if (mts.length > 1 && Math.min(...mts) !== Math.max(...mts)) {
        P.forceMt = Math.max(...mts);
        views = render();
      }
      ctl.setViews(views);
      renderLegend(box, P.legendItems || [], P.dyn);
      return before;
    };
    const draw = () => {
      if (!plot.clientWidth || widths() === lastKey) return;
      let used = drawOnce();
      // the legend or a scrollbar can change the widths while drawing: re-measure once, never loop
      if (widths() !== used) used = drawOnce();
      lastKey = widths();
    };
    draw();
    fig.dataset.loaded = 'ok';
    if ('ResizeObserver' in window) {
      let raf = 0;
      new ResizeObserver(() => { cancelAnimationFrame(raf); raf = requestAnimationFrame(draw); }).observe(plot);
    } else {
      window.addEventListener('resize', draw);
    }
  }

  function start(fig) {
    // one mount per figure: the film starts its own figures, and the viewport observer may reach them too
    if (fig.dataset.started) return;
    fig.dataset.started = '1';
    const id = fig.dataset.chart;
    load(id).then((chart) => {
      try { mount(fig, chart); } catch (err) { fail(fig, 'error'); if (window.console) console.error(`chart ${id}:`, err); }
    }).catch(() => fail(fig, 'fetch'));
  }

  /* ------------------------------------------------------------------ sparklines on the KPI tiles */

  function drawSpark(el, chart) {
    const ents = (el.dataset.entity || 'near').split(/\s+/);
    const series = Array.isArray(chart.series) ? chart.series : [];
    let pick = [];
    for (const e of ents) { pick = series.filter((s) => s.entity === e); if (pick.length) break; }
    if (!pick.length) return;
    const W = Math.round(el.getBoundingClientRect().width);
    const H = Math.round(el.getBoundingClientRect().height) || 36;
    if (!W) return;
    el.setAttribute('viewBox', `0 0 ${W} ${H}`);
    el.textContent = '';
    const lineForm = chart.form === 'line' || chart.form === 'small-multiples';
    if (lineForm) {
      const s = normSeries(pick[0], 0, 'line', '%');
      if (s.nn.length < 2) return;
      const vs = s.nn.map((p) => p[1]);
      let lo = Math.min(...vs);
      let hi = Math.max(...vs);
      if (hi === lo) { lo -= 1; hi += 1; }
      const sx = linear(s.ts[0], s.ts[s.ts.length - 1], 2, W - 5);
      const sy = linear(lo, hi, H - 4, 4);
      if (lo < 0 && hi > 0) svg('line', { cls: 'spark-zero', x1: 0, x2: W, y1: Math.round(sy(0)) + 0.5, y2: Math.round(sy(0)) + 0.5 }, el);
      let path = '';
      let pen = false;
      for (const [t, v] of s.pts) {
        if (v === null) { pen = false; continue; }
        path += (pen ? 'L' : 'M') + f1(sx(t)) + ',' + f1(sy(v));
        pen = true;
      }
      svg('path', { d: path, fill: 'none', 'stroke-width': 1.5, 'stroke-linejoin': 'round', 'stroke-linecap': 'round', style: { stroke: cvar(s.entity) } }, el);
      const [lt, lv] = s.nn[s.nn.length - 1];
      svg('circle', { cx: sx(lt), cy: sy(lv), r: 2.5, style: { fill: cvar(s.entity) } }, el);
      return;
    }
    const ns = pick.map((p, i) => normSeries(p, i, 'cols', '%'));
    const cats = uniq(ns.flatMap((s) => s.cats));
    const pts = cats.map((c) => {
      for (const s of ns) { const v = first(s, c); if (isNum(v)) return { s, v }; }
      return null;
    });
    const vs = pts.filter(Boolean).map((p) => p.v);
    if (!vs.length) return;
    const lo = Math.min(0, ...vs);
    const hi = Math.max(0, ...vs);
    const sy = linear(lo, hi === lo ? lo + 1 : hi, H - 2, 2);
    const step = W / cats.length;
    const bw = Math.max(2, Math.min(10, step * 0.66));
    const div = series.filter((s) => s.role === 'diverging');
    const sign = div.length === 1 && div[0].entity !== 'negative';
    pts.forEach((p, i) => {
      if (!p) return;
      const x = step * (i + 0.5) - bw / 2;
      const color = sign && p.s.role === 'diverging' ? (p.v < 0 ? 'var(--c-negative)' : 'var(--c-positive)') : cvar(p.s.entity);
      svg('path', { d: vbar(x, bw, sy(0), sy(p.v), 2), style: { fill: color } }, el);
    });
  }

  function startSpark(el) {
    load(el.dataset.spark).then((chart) => {
      const draw = () => { try { drawSpark(el, chart); } catch (e) { /* the tile reads fine without it */ } };
      draw();
      if ('ResizeObserver' in window) {
        let w = el.getBoundingClientRect().width;
        new ResizeObserver(() => {
          const nw = el.getBoundingClientRect().width;
          if (nw !== w) { w = nw; requestAnimationFrame(draw); }
        }).observe(el);
      }
    }).catch(() => { /* sparklines are decoration; the tile keeps its number */ });
  }

  /* ------------------------------------------------------------------ registry numbers: hover, focus, deep link */

  function initNums() {
    const nums = d.querySelectorAll('.num[data-key]');
    if (!nums.length) return;
    for (const n of nums) {
      // counts inside a link (the source-status pill) belong to the link: no tooltip, no tab stop,
      // and a click follows the link instead of opening the number's provenance
      if (n.closest('a[href]')) { n.removeAttribute('title'); n.removeAttribute('tabindex'); n.dataset.inLink = '1'; continue; }
      if (n.hasAttribute('title')) { n.dataset.tip = n.getAttribute('title'); n.removeAttribute('title'); }
    }
    const onData = !!d.getElementById('numbers');
    let tipEl = null;
    let owner = null;
    const hide = () => {
      if (tipEl) tipEl.hidden = true;
      if (owner) owner.removeAttribute('aria-describedby');
      owner = null;
    };
    const show = (n) => {
      if (owner === n) return;
      hide();
      // inside the film and its evidence dialog a figure keeps its click-through but shows no key or formula
      if (n.closest('.film, dialog')) return;
      if (!tipEl) {
        tipEl = html('div', 'num-tip', null, d.body);
        tipEl.id = 'num-tip';
        tipEl.setAttribute('role', 'tooltip');
      }
      tipEl.textContent = '';
      html('span', 'k', n.dataset.key, tipEl);
      if (n.dataset.tip) tipEl.appendChild(d.createTextNode(n.dataset.tip));
      html('span', 'hint', 'Click for its formula, inputs and source files.', tipEl);
      tipEl.hidden = false;
      owner = n;
      n.setAttribute('aria-describedby', 'num-tip');
      const r = n.getBoundingClientRect();
      const tw = tipEl.offsetWidth;
      const th = tipEl.offsetHeight;
      const vw = root.clientWidth;
      let x = clamp(r.left + r.width / 2 - tw / 2, 8, Math.max(8, vw - tw - 8));
      let y = r.bottom + 8;
      if (y + th > window.innerHeight - 8) y = r.top - th - 8;
      tipEl.style.left = f1(x) + 'px';
      tipEl.style.top = f1(Math.max(8, y)) + 'px';
    };
    const go = (n) => {
      const id = 'n-' + n.dataset.key;
      if (onData) {
        const row = d.getElementById(id);
        if (row) { location.hash = id; return; }
      }
      location.href = 'data.html#' + encodeURIComponent(id);
    };
    const target = (e) => {
      const n = e.target && e.target.closest ? e.target.closest('.num[data-key]') : null;
      return n && !n.dataset.inLink ? n : null;
    };
    d.addEventListener('pointerover', (e) => { const n = target(e); if (n) show(n); else if (owner && !(e.target.closest && e.target.closest('.num-tip'))) hide(); });
    d.addEventListener('focusin', (e) => { const n = target(e); if (n) show(n); else hide(); });
    d.addEventListener('scroll', hide, { passive: true });
    d.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') hide();
      const n = target(e);
      if (n && e.key === 'Enter') { e.preventDefault(); go(n); }
    });
    d.addEventListener('click', (e) => {
      const n = target(e);
      if (!n || n.closest('summary')) return;
      const sel = window.getSelection && window.getSelection();
      if (sel && String(sel).length) return;
      go(n);
    });
  }

  /* ------------------------------------------------------------------ theme, table toggles, copy buttons */

  function initTheme() {
    const btn = d.querySelector('.theme-btn');
    if (!btn) return;
    const label = btn.querySelector('.theme-label');
    const order = ['auto', 'light', 'dark'];
    const names = { auto: 'Auto', light: 'Light', dark: 'Dark' };
    const get = () => root.getAttribute('data-theme') || 'auto';
    const paint = () => {
      const t = get();
      if (label) label.textContent = names[t];
      btn.dataset.theme = t;
      btn.setAttribute('aria-label', `Color theme: ${names[t]}. Activate to switch.`);
    };
    btn.hidden = false;
    paint();
    btn.addEventListener('click', () => {
      const next = order[(order.indexOf(get()) + 1) % order.length];
      try {
        if (next === 'auto') localStorage.removeItem(THEME_KEY);
        else localStorage.setItem(THEME_KEY, next);
      } catch (e) { /* private mode: the choice lasts for this page */ }
      if (next === 'auto') root.removeAttribute('data-theme');
      else root.setAttribute('data-theme', next);
      paint();
    });
  }

  function initButtons() {
    for (const b of d.querySelectorAll('.copy-btn')) b.hidden = false;
    d.addEventListener('click', (e) => {
      const t = e.target && e.target.closest ? e.target : null;
      if (!t) return;
      const tb = t.closest('.tbl-btn');
      if (tb) {
        const fig = tb.closest('.card');
        if (!fig) return;
        const on = !fig.classList.contains('show-table');
        fig.classList.toggle('show-table', on);
        tb.setAttribute('aria-expanded', String(on));
        tb.textContent = on ? 'Hide table' : 'Table';
        return;
      }
      const cb = t.closest('.copy-btn');
      if (cb) {
        const code = cb.parentElement && cb.parentElement.querySelector('pre code');
        if (!code) return;
        const text = code.textContent;
        const done = (ok) => {
          cb.textContent = ok ? 'Copied' : 'Select and copy';
          setTimeout(() => { cb.textContent = 'Copy'; }, 1600);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(() => done(true), () => done(false));
        else {
          const range = d.createRange();
          range.selectNodeContents(code);
          const sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(range);
          let ok = false;
          try { ok = d.execCommand('copy'); } catch (err) { ok = false; }
          done(ok);
        }
      }
    });
  }

  /* The agent module (#agent): copies the prompt, and for the NEAR AI card first fetches this site's own
     THESIS.md and puts it ahead of the prompt. Nothing leaves the page: the reader pastes into the agent. */
  function initAgent() {
    const sec = d.getElementById('agent');
    const code = sec && sec.querySelector('.agent-prompt pre code');
    if (!code) return;
    for (const b of sec.querySelectorAll('.agent-copy')) b.hidden = false;
    const toClipboard = (text) => {
      if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text).then(() => true, () => false);
      const ta = d.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      d.body.appendChild(ta);
      ta.select();
      let ok = false;
      try { ok = d.execCommand('copy'); } catch (err) { ok = false; }
      ta.remove();
      return Promise.resolve(ok);
    };
    sec.addEventListener('click', (e) => {
      const btn = e.target && e.target.closest ? e.target.closest('.agent-copy') : null;
      if (!btn) return;
      const status = btn.parentElement.querySelector('.agent-status');
      const say = (msg) => { if (status) status.textContent = msg; };
      const prompt = code.textContent.trim() + '\n';
      const attach = btn.dataset.attach;
      say(attach ? 'Fetching THESIS.md' : '');
      const body = !attach ? Promise.resolve({ text: prompt, attached: false })
        : fetch(attach).then((r) => { if (!r.ok) throw new Error(String(r.status)); return r.text(); })
          .then((doc) => ({ text: '=== THESIS.md, pasted because you may not be able to open links ===\n\n' + doc.trim() + '\n\n=== end of THESIS.md ===\n\n' + prompt, attached: true }),
            () => ({ text: prompt, attached: false }));
      body.then((r) => toClipboard(r.text).then((ok) => {
        if (!ok) say('Could not reach the clipboard. Select the prompt below and copy it.');
        else if (attach && !r.attached) say('Copied the prompt. THESIS.md could not be fetched here; open it and paste it above the prompt.');
        else say(r.attached ? 'Copied the prompt with THESIS.md. Paste it into the chat.' : 'Copied. Paste it into the chat.');
      }));
    });
  }

  /* ------------------------------------------------------------------ boot */

  function observe(nodes, fn) {
    if (!nodes.length) return;
    if (!('IntersectionObserver' in window)) { nodes.forEach(fn); return; }
    const io = new IntersectionObserver((entries) => {
      for (const en of entries) {
        if (!en.isIntersecting) continue;
        io.unobserve(en.target);
        fn(en.target);
      }
    }, { rootMargin: '320px 0px' });
    nodes.forEach((n) => io.observe(n));
  }

  function boot() {
    try { fontFamily = getComputedStyle(d.body).fontFamily || fontFamily; } catch (e) { /* keep default */ }
    initTheme();
    initButtons();
    initAgent();
    initNums();
    observe(Array.from(d.querySelectorAll('figure[data-chart]')).filter((f) => f.querySelector('.plot')), start);
    observe(Array.from(d.querySelectorAll('svg.spark[data-spark]')), startSpark);
  }

  // the film mounts its own figures (one scene ahead, while hidden) through this one export
  window.NearCharts = { start };

  if (d.readyState === 'loading') d.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
