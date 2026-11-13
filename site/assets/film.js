/* NEAR, tested: the film player.
 *
 * The page is the film. Scenes are section.scene, shots are div.shot[data-dur] holding one p.cap, and anything that
 * arrives on cue carries data-at (and optionally data-out), in seconds from its shot's start, or from its scene's
 * start when it sits outside every shot. This file reads that markup and draws one frame from one state:
 * render() turns (shot, time, playing, motion) into attributes and custom properties, so playback, scrubbing,
 * deep links, reduced motion and stills all share one path.
 *
 * It holds no copy and no figures: every word and number on screen is in the HTML, baked from the registry. The
 * orbit's geometry is read from data-* and the SVG the page already holds. Nothing is fetched here except, once the
 * desk is near, the page's own simulation script.
 */
(() => {
  'use strict';

  const d = document;
  const root = d.documentElement;
  const film = d.querySelector('.film');
  if (!film || film.dataset.live) return;
  film.dataset.live = 'on';

  /* ------------------------------------------------------------------ helpers */

  const $ = (s, r) => (r || d).querySelector(s);
  const $$ = (s, r) => Array.from((r || d).querySelectorAll(s));
  const clamp = (x, lo, hi) => (x < lo ? lo : x > hi ? hi : x);
  const c01 = (x) => clamp(x, 0, 1);
  const num = (v, dflt) => { const n = parseFloat(v); return Number.isFinite(n) ? n : dflt; };
  const svgNS = 'http://www.w3.org/2000/svg';
  const params = new URLSearchParams(location.search);
  const PHONE = 760;

  function el(tag, cls, parent, txt) {
    const n = d.createElement(tag);
    if (cls) n.className = cls;
    if (txt != null) n.textContent = txt;
    if (parent) parent.appendChild(n);
    return n;
  }

  // the stylesheet's two curves in closed form: slam an exponential ease-out, wipe an in-out quartic
  const inOut = (k) => (x) => { x = c01(x); return x < 0.5 ? Math.pow(2 * x, k) / 2 : 1 - Math.pow(2 - 2 * x, k) / 2; };
  const E = {
    slam: (x) => (x >= 1 ? 1 : 1 - Math.pow(2, -10 * c01(x))),
    wipe: inOut(4),
    camera: inOut(3),
    out: (x) => 1 - Math.pow(1 - c01(x), 3),
    lin: c01,
  };

  /* writes only what changed: a frame touches the DOM where the picture moved, nowhere else */
  function setVar(node, name, v) {
    const c = node.__f || (node.__f = {});
    const s = typeof v === 'number' ? (Math.round(v * 1000) / 1000) : v;
    if (c[name] === s) return;
    c[name] = s;
    node.style.setProperty(name, String(s));
  }
  function setAttr(node, name, v) {
    const c = node.__a || (node.__a = {});
    const s = v == null ? null : (typeof v === 'number' ? String(Math.round(v * 100) / 100) : v);
    if (c[name] === s) return;
    c[name] = s;
    if (s == null) node.removeAttribute(name); else node.setAttribute(name, s);
  }
  function setFlag(node, name, on) {
    if (node.hasAttribute(name) !== !!on) node.toggleAttribute(name, !!on);
  }
  const mmss = (t) => { const s = Math.max(0, Math.floor(t + 1e-6)); const m = Math.floor(s / 60); const r = s % 60; return m + ':' + (r < 10 ? '0' : '') + r; };
  const text = (n) => (n ? n.textContent.replace(/\s+/g, ' ').trim() : '');

  /* ------------------------------------------------------------------ the film, read from the page */

  const KIND = {
    // arrival kinds: duration in seconds and curve; the stylesheet draws each from --p
    ghost: [0.16, E.lin], slam: [0.18, E.slam], stamp: [0.18, E.slam], reveal: [2.4, E.out], grow: [1.1, E.out],
    wipe: [0.48, E.wipe], narrow: [1.6, E.out], widen: [1.6, E.out], fade: [0.3, E.out], drop: [0.6, E.out],
    cut: [0.001, E.lin], line: [0.6, E.wipe],
  };
  function kindOf(n) {
    const k = n.dataset.fx;
    if (k && KIND[k]) return k;
    if (n.classList.contains('stamp')) return 'stamp';
    if (n.classList.contains('rel')) return 'line';
    if (n.matches('figure[data-chart], .film-chart')) return 'reveal';
    if (n.matches('.fig, .hero, .chip, .chips, .lcol, .mega, .tested, .tag, .stamp-word')) return 'slam';
    if (n.matches('.seg, .edge-bar')) return 'grow';
    // inside an svg, or sharing its place with figures that hand over (the title's two readings): no ghost
    if (n.closest('svg, .heroes')) return 'fade';
    // a verdict laid over its exhibit is a smash cut
    if (n.matches('.vframe, .overlay')) return 'cut';
    return 'ghost';
  }

  const scenes = [];
  const shots = [];
  let total = 0;
  const sceneNodes = $$('.scene', film).filter((n) => $('.shot', n));
  for (const node of sceneNodes) {
    const sc = { id: node.dataset.scene || node.id, node, idx: scenes.length, start: total, shots: [], timed: [] };
    const h = $('.scene-title', node) || $('h2, h3', node);
    sc.title = text(h);
    sc.short = node.dataset.short || sc.title;
    for (const sn of $$('.shot', node)) {
      const dur = Math.max(0.5, num(sn.dataset.dur, 4));
      const stamp = $('.stamp', sn);
      const sh = {
        id: sn.dataset.shot || '', node: sn, scene: sc, n: sc.shots.length + 1, dur, start: total, local: total - sc.start,
        cap: $('.cap', sn), stamp, timed: [],
        verdict: !!stamp && !$('.film-chart, figure[data-chart], .split', sn),
        hold: sn.classList.contains('is-hold'), chart: $$('figure[data-chart]', sn),
      };
      sc.shots.push(sh);
      shots.push(sh);
      total += dur;
    }
    sc.end = total;
    sc.dur = sc.end - sc.start;
    for (const sh of sc.shots) sh.node.dataset.kind = sh.verdict ? 'verdict' : sh.chart.length ? 'exhibit' : 'frame';
    scenes.push(sc);
  }
  if (!shots.length) return;
  const shotById = new Map(shots.map((s) => [s.id, s]));

  // timed elements: inside a shot they run on the shot's clock; outside every shot, on the scene's
  for (const sc of scenes) {
    for (const n of $$('[data-at], [data-out]', sc.node)) {
      const host = n.parentElement.closest('.shot');
      const kind = n.hasAttribute('data-at') ? kindOf(n) : 'cut';
      const t = { n, kind, at: num(n.dataset.at, 0), out: n.hasAttribute('data-out') ? num(n.dataset.out, Infinity) : Infinity, dur: KIND[kind][0], ease: KIND[kind][1] };
      n.dataset.fx = kind;
      if (host && host.closest('.scene') === sc.node) {
        const sh = shots.find((s) => s.node === host);
        if (sh) {
          // nothing arrives before the timed element that holds it (a stamp inside an overlay lands with the overlay)
          const outer = n.parentElement.closest('[data-at]');
          if (outer && host.contains(outer)) t.at = Math.max(t.at, num(outer.dataset.at, 0));
          t.shot = sh; sh.timed.push(t); continue;
        }
      }
      t.offset = 0;
      sc.timed.push(t);
    }
  }
  // a verdict holds still for its last STILL seconds: when the page's cues would run later, they are drawn in
  // proportionally (the stamp's rule wipe ends 0.73 s after the stamp)
  const STILL = 2.4;
  for (const sh of shots) {
    if (!sh.verdict) continue;
    const room = sh.dur - STILL - 0.2;
    let k = 1;
    for (const t of sh.timed) if (t.out === Infinity && t.at > 0) k = Math.min(k, Math.max(0, room - t.dur - (t.kind === 'stamp' ? 0.55 : 0)) / t.at);
    if (k < 1) for (const t of sh.timed) if (t.out === Infinity) t.at *= k;
  }
  /* ------------------------------------------------------------------ chrome: caption band, transport, slate */

  const parentOfScenes = sceneNodes[0].parentElement;
  if (parentOfScenes !== film) parentOfScenes.classList.add('film-frame');

  const slateScene = $('.slate-scene', film);

  // the transport and caption band are written by the page (with their words); without them there is no player
  const tp = $('.transport'), band = $('.capband');
  if (!tp || !band) return;
  const bandText = $('p', band);
  bandText.classList.add('cb-text');
  const bandK = el('p', 'cb-k');
  band.insertBefore(bandK, bandText);
  band.setAttribute('aria-live', 'polite');
  film.appendChild(band);
  film.appendChild(tp);
  band.hidden = false;
  tp.hidden = false;
  function button(act, cls, keys, icon) {
    const b = $('[data-act="' + act + '"]', tp) || el('button', null, tp);
    b.type = 'button';
    b.dataset.act = act;
    b.classList.add(...cls.split(' '));
    // the page's word stays as the button's text; icon buttons keep it for assistive tech and draw a glyph
    if (!$('.tp-t', b)) { const t = el('span', 'tp-t'); while (b.firstChild) t.appendChild(b.firstChild); b.appendChild(t); }
    if (icon && !$('.ico', b)) el('i', 'ico', b).setAttribute('aria-hidden', 'true');
    if (keys) b.setAttribute('aria-keyshortcuts', keys);
    b.hidden = false;
    return b;
  }
  const bPrev = button('prev', 'tp-btn tp-prev', 'ArrowLeft', true);
  const bPlay = button('play', 'tp-btn tp-play', 'Space K', true);
  const bNext = button('next', 'tp-btn tp-next', 'ArrowRight', true);
  const bMotion = button('motion', 'tp-pill tp-motion', 'M', false);
  const bEv = button('evidence', 'tp-pill tp-ev', 'E', false);
  bEv.setAttribute('aria-haspopup', 'dialog');
  const playWords = { play: bPlay.getAttribute('aria-label') || text(bPlay), pause: bPlay.dataset.labelPause || '', replay: bPlay.dataset.labelReplay || '' };
  const time = $('.t-time', tp) || el('p', null, tp);
  time.classList.add('tp-time');
  time.textContent = '';
  const timeNow = el('span', 'tp-now', time);
  el('span', 'tp-of', time, ' / ');
  const timeAll = el('span', 'tp-all', time);
  const track = el('div', 'tp-track', tp);
  const labels = el('div', 'tp-labels', track);
  labels.setAttribute('aria-hidden', 'true');
  const segs = el('div', 'tp-segs', track);
  segs.setAttribute('aria-hidden', 'true');
  const scrub = $('input[type="range"]', tp) || el('input', null, track);
  scrub.type = 'range';
  scrub.classList.add('tp-scrub');
  track.appendChild(scrub);
  // order: prev, play, next, time, track, motion, evidence
  for (const n of [bPrev, bPlay, bNext, time, track, bMotion, bEv]) tp.appendChild(n);
  scrub.min = '0';
  scrub.max = String(Math.round(total * 100) / 100);
  scrub.step = String(0.05);
  scrub.setAttribute('aria-keyshortcuts', 'Home End');
  timeAll.textContent = mmss(total);

  // segments: one per scene, widths by duration; a verdict shot leaves its glyph on its scene's segment
  const segEls = scenes.map((sc) => {
    const s = el('span', 'tp-seg', segs);
    s.style.flexGrow = String(sc.dur);
    s.style.flexBasis = '0';
    el('b', 'tp-fill', s);
    for (const sh of sc.shots) {
      if (!sh.stamp) continue;
      const m = el('i', 'tp-mark', s);
      m.dataset.verdict = sh.stamp.dataset.verdict || '';
      m.style.left = (((sh.local + Math.min(sh.dur, 0.5)) / sc.dur) * 100).toFixed(2) + '%';
      const g = $('svg', sh.stamp);
      if (g) { const c = g.cloneNode(true); c.removeAttribute('id'); m.appendChild(c); }
      m.setAttribute('aria-hidden', 'true');
    }
    return s;
  });
  const labelEls = scenes.map((sc) => {
    // a pointer shortcut only: the scrubber and the digit keys reach every scene for everyone else
    const b = el('span', 'tp-scene', labels, sc.title);
    b.dataset.scene = sc.id;
    b.addEventListener('click', () => { userPause(); seek(sc.start); });
    return b;
  });

  /* ------------------------------------------------------------------ state */

  const mqReduce = window.matchMedia ? matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
  let stored = null;
  try { stored = sessionStorage.getItem('film-motion'); } catch (e) { /* private mode */ }
  const S = {
    T: 0,
    playing: false,
    scrubbing: false,
    rm: params.get('rm') === '1' || (stored ? stored === 'off' : mqReduce.matches),
    article: false,
    held: false,
  };
  let cur = null;
  let curScene = null;
  let lastTs = 0;
  let raf = 0;
  let autoPaused = false;
  let inView = true;

  const shotAt = (T) => { for (let i = shots.length - 1; i >= 0; i--) if (T >= shots[i].start - 1e-6) return shots[i]; return shots[0]; };
  const endT = () => total - 0.001;

  /* ------------------------------------------------------------------ timed elements */

  // the time a shot is drawn at: its own time, or its settled last frame when motion is off
  function shotTime(sh, T) { return S.rm ? sh.dur : clamp(T - sh.start, 0, sh.dur); }

  function drawTimed(list, lt, lit) {
    // lights up: an element still to come shows at full strength, unless it replaces one still on screen
    let block = Infinity;
    if (lit) for (const t of list) if (t.out !== Infinity && lt < t.out && lt >= t.at) block = Math.min(block, t.out);
    for (const t of list) {
      let st, p;
      if (lt >= t.out) { st = 'out'; p = 1; }
      else if (lt < t.at) {
        if (lit && t.at < block) { st = 'in'; p = 1; } else { st = 'pre'; p = 0; }
      } else { st = 'in'; p = lit ? 1 : t.ease(c01((lt - t.at) / t.dur)); }
      if (t.n.dataset.st !== st) t.n.dataset.st = st;
      setVar(t.n, '--p', p);
      if (t.kind === 'stamp') {
        const since = lit ? 9 : lt - t.at;
        // one hit: a decaying shake for a fifth of a second, then the rule wipes under the word
        const sh = since > 0 && since < 0.2 ? Math.sin(since * 70) * 6 * (1 - since / 0.2) : 0;
        setVar(t.n, '--sh', sh);
        const host = t.n.closest('.vframe') || t.n.parentElement;
        setVar(host, '--w', st === 'in' ? E.wipe(c01((since - 0.25) / 0.48)) : 0);
      }
    }
  }

  function settle(list) {
    for (const t of list) {
      const st = t.out !== Infinity ? 'out' : 'in';
      if (t.n.dataset.st !== st) t.n.dataset.st = st;
      setVar(t.n, '--p', 1);
      if (t.kind === 'stamp') { setVar(t.n, '--sh', 0); const host = t.n.closest('.vframe') || t.n.parentElement; setVar(host, '--w', 1); }
    }
  }

  /* ------------------------------------------------------------------ the orbit (title, and its two returns) */

  const Orbit = (() => {
    const fig = $('.orbit', film);
    const svg = fig && $('svg', fig);
    if (!fig || !svg) return null;
    const home = fig.parentElement;
    const marker = d.createComment('orbit');
    home.insertBefore(marker, fig);
    const ds = (k) => (svg.dataset[k] != null ? svg.dataset[k] : fig.dataset[k]);
    const vb = (svg.getAttribute('viewBox') || '').split(/[\s,]+/).map(Number);
    const vw = vb[2] || 640, vh = vb[3] || 640;
    const cx = num(ds('cx'), vw / 2), cy = num(ds('cy'), vh / 2), k = num(ds('k'), 0);
    const cam = $('.o-cam', fig) || fig;
    cam.style.transformOrigin = ((cx / vw) * 100).toFixed(3) + '% ' + ((cy / vh) * 100).toFixed(3) + '%';
    // ring scale labels: drawn here from data-scale, so the page's own markup never types a digit. One scale serves
    // both beats (money growth and what money pays), so the ticks never change
    if (k && ds('scale') && !$('.o-ticks', svg)) {
      const g = d.createElementNS(svgNS, 'g');
      g.setAttribute('class', 'o-ticks');
      for (const v of ds('scale').split(/\s+/).map(Number).filter(Number.isFinite)) {
        const t = d.createElementNS(svgNS, 'text');
        t.setAttribute('x', String(cx + 5));
        t.setAttribute('y', String(cy - v * k - 4));
        t.textContent = v + '%';
        g.appendChild(t);
      }
      const grid = $('.o-grid', svg);
      if (grid) grid.after(g); else svg.prepend(g);
    }
    // the ladder's ticks, the same way, centred on their own ticks at any type size; zero's figure hangs just under
    // the zero line, which runs through its place, beside the shorter of the line's two words
    for (const l of $$('.o-rt', svg)) {
      const v = num(l.dataset.tick, 0);
      if (l.nextSibling && l.nextSibling.nodeName === 'text') continue;
      const t = d.createElementNS(svgNS, 'text');
      t.setAttribute('x', String(num(l.getAttribute('x1'), 0) - 8));
      t.setAttribute('y', String(num(l.getAttribute('y1'), 0) + (v ? 0 : 4)));
      t.setAttribute('dominant-baseline', v ? 'central' : 'hanging');
      t.setAttribute('text-anchor', 'end');
      t.textContent = (v > 0 ? '+' : v ? '\u2212' : '') + Math.abs(v) + '%';
      l.after(t);
    }
    const ring = (ent) => { const c = $('.o-ring[data-entity="' + ent + '"]', svg); return c ? { n: c, r: num(c.getAttribute('r'), 0) } : null; };
    const rN = ring('near'), rU = ring('usd');
    const wash = $('.o-wash', svg);
    const core = $('.o-core', svg);
    // the third beat: where each mark goes on the ladder, as an offset from where the page drew it
    const rankOff = (n, x, y) => (n && n.dataset.rankX != null ? [num(n.dataset.rankX, x) - x, num(n.dataset.rankY, y) - y] : null);
    const coreK = rankOff(core, cx, cy);
    const backs = $$(':scope > .o-grid, :scope > .o-ticks', svg);
    const brackets = $$('.o-bracket', svg);
    const bodies = $$('.o-body', svg).map((g) => {
      const dot = $('.o-dot', g) || $('circle', g);
      const x = num(dot && dot.getAttribute('cx'), cx), y = num(dot && dot.getAttribute('cy'), cy);
      const rM = num(g.dataset.rMoney, Math.hypot(x - cx, y - cy));
      const ux = num(g.dataset.ux, rM ? (x - cx) / rM : 0), uy = num(g.dataset.uy, rM ? (y - cy) / rM : 0);
      return {
        g, code: g.dataset.code, usd: g.dataset.entity === 'usd', dx: x - cx, dy: y - cy, ux, uy, rM,
        // no policy rate, no second radius: the body leaves rather than move to a place no figure gives it
        rP: g.dataset.rPay != null && g.dataset.rPay !== '' ? num(g.dataset.rPay, null) : null,
        // hollow is against the thesis in either reading: money that grew more slowly than NEAR's supply (inside the
        // ring), then a policy rate that pays more than staking NEAR (outside it); the values the page baked decide
        hM: g.hasAttribute('data-inside') ? 1 : 0, hP: g.hasAttribute('data-inside-pay') ? 0 : 1,
        marks: $$('circle', g), rk: rankOff(g, x, y),
      };
    });
    const others = bodies.filter((b) => !b.usd);
    const usdBody = bodies.find((b) => b.usd);

    // the second beat: the same picture read for what holding pays. Rings and bodies move from the money radii to
    // the pay radii on the one scale; a currency with no policy rate has no second radius and leaves instead
    const nM = num(ds('ringMoney'), rN ? rN.r : 0);
    const nP = num(ds('ringPay'), NaN);
    const uM = rU ? rU.r : 0;
    const uP = num(ds('usdPay'), usdBody && usdBody.rP != null ? usdBody.rP : NaN);
    // three readings on one picture: money (0), what holding pays (1), both at once as a ladder (2). The state is
    // one number that eases from reading to reading; the ladder's move is the longer one
    const SETS = { pay: 1, rank: 2 };
    const MORPH = [1.5, 1.5, 1.8];
    const title = scenes.find((s) => s.node.contains(fig)) || scenes[0];
    const modeOf = (sh) => sh.node.dataset.orbit || '';
    const keys = Number.isFinite(nP) ? title.shots.filter((sh) => /^(pay|money|rank)$/.test(modeOf(sh)))
      .map((sh) => ({ at: sh.local + 0.4, to: SETS[modeOf(sh)] || 0 })) : [];
    function mAt(s, n) {
      let v = 0;
      for (let i = 0; i < n; i++) {
        const q = keys[i];
        if (s < q.at) break;
        const v0 = i ? mAt(q.at, i) : 0;
        v = v0 + (q.to - v0) * E.camera((s - q.at) / MORPH[q.to]);
      }
      return v;
    }
    // which words are up: the outgoing state's labels leave as the move starts, the new ones arrive once it lands,
    // so no label is ever on screen while the bodies travel
    function phase(s) {
      const o = [1, 0, 0];
      let cur = 0;
      for (const q of keys) {
        if (s < q.at) break;
        const from = cur;
        cur = q.to;
        if (from === cur) continue;
        o[from] = 1 - c01((s - q.at) / 0.2);
        o[cur] = c01((s - q.at - MORPH[cur]) / 0.3);
      }
      return o;
    }
    const setOf = (n) => SETS[n.dataset.when] || 0;
    const labs = $$('.o-lab', fig).filter((n) => !n.hasAttribute('data-at'));
    const hasPayLabels = labs.some((n) => setOf(n) === 1);
    const leaders = $$('.o-leader', svg);
    // the key under the picture arrives with the ten; inside it, and anywhere else on the title, whatever names one
    // reading (data-when) crosses to the other on the labels' cue
    const legend = $$(':scope > :is(.o-legend, .o-scale, .o-norate)', fig);
    const whens = [...new Set([...$$('[data-when]', fig), ...$$('[data-when]', title.node)])]
      .filter((n) => !n.matches('.o-lab, .o-leader, .o-bracket') && !legend.includes(n) && !n.closest('.shot'));
    const ws = (n, ph) => (n.hasAttribute('data-when') ? ph[setOf(n)] : 1);
    // shot boundaries of the title, in scene seconds: the choreography follows the durations the page declares
    const mark = (i) => (title.shots[i] ? title.shots[i].local : title.dur);
    const s1 = mark(1), s2 = mark(2);
    const settled = title.dur + 10;
    let where = 'home';
    let laid = false;
    // the labels are placed for the picture's size: when it changes (fonts arriving, a resize, a slot), again
    if (window.ResizeObserver) new ResizeObserver(() => { if (laid) { laid = false; render(); } }).observe(cam);

    function draw(s, force) {
      if (!laid && fig.offsetWidth) layout();
      const v = force ? SETS[force] || 0 : mAt(s, keys.length);
      const m = c01(v), m2 = c01(v - 1);
      const ph = force ? [0, 1, 2].map((i) => +(i === v)) : phase(s);
      const tr = (k, f) => (k && f > 0 ? 'translate(' + (k[0] * f).toFixed(2) + ' ' + (k[1] * f).toFixed(2) + ')' : null);
      const pull = E.camera((s - s2) / 1.6);
      setVar(cam, '--z', 1.26 - 0.26 * pull);
      const gN = E.out((s - s1) / 1.5), gU = E.out((s - s1) / 2.6);
      const nNow = Number.isFinite(nP) ? nM + (nP - nM) * m : nM;
      const uNow = Number.isFinite(uP) ? uM + (uP - uM) * m : uM;
      const rn = rN ? nNow * gN : 0, ru = rU ? uNow * gU : 0;
      // on the ladder the blue ring closes round NEAR's own mark and travels with it to its place; the scale behind
      // the orbit and the dollar's ring go out
      if (rN) { setAttr(rN.n, 'r', rn + (9 - rn) * m2); setAttr(rN.n, 'transform', tr(coreK, m2)); }
      if (core) setAttr(core, 'transform', tr(coreK, m2));
      for (const n of backs) setVar(n, '--o', 1 - m2);
      // the dollar ring steps back once the other currencies arrive, and comes forward again as fed funds
      const dim = pull * (1 - m);
      if (rU) {
        setAttr(rU.n, 'r', ru * (1 - m2));
        setVar(rU.n, '--o', (1 - 0.62 * dim) * (Number.isFinite(uP) ? 1 : 1 - m) * (1 - m2));
        setFlag(rU.n, 'data-dotted', dim > 0.5);
      }
      const gap = c01((s - s1 - 2.7) / 0.16);
      if (wash) {
        // the band between the two rings: the money gap first, the staking-over-fed-funds spread once the move lands
        const arc = (r) => ['M', cx - r, cy, 'a', r, r, 0, 1, 0, 2 * r, 0, 'a', r, r, 0, 1, 0, -2 * r, 0, 'Z'].join(' ');
        setAttr(wash, 'd', ru > 0 ? arc(ru) + (rn > 0 ? arc(rn) : '') : null);
        setVar(wash, '--o', gap * (1 - pull) * (1 - m) + (Number.isFinite(uP) ? ph[1] : 0));
        setFlag(wash, 'data-pay', m > 0.5);
      }
      for (const n of brackets) setVar(n, '--o', setOf(n) ? ph[1] : gap * (1 - pull) * ph[0]);
      let j = 0;
      for (const b of bodies) {
        const e = b.usd ? gU : E.out((s - s2 - 0.1 - j * 0.1) / 1.1);
        let vis = b.usd ? (gU > 0 ? 1 : 0) : c01((s - s2 - 0.1 - j * 0.1) / 0.15);
        if (!b.usd) j++;
        setAttr(b.g, 'transform', e >= 1 ? null : 'translate(' + (-(1 - e) * b.dx).toFixed(2) + ' ' + (-(1 - e) * b.dy).toFixed(2) + ')');
        // the move itself is on the marks, so lines drawn for either state stay where the page put them
        const rNow = b.rP == null ? b.rM : b.rM + (b.rP - b.rM) * m;
        const off = rNow - b.rM;
        // from its place on the orbit to its place on the ladder, in a straight line
        const k = [b.ux * off, b.uy * off];
        if (b.rk) { k[0] += (b.rk[0] - k[0]) * m2; k[1] += (b.rk[1] - k[1]) * m2; } else vis *= 1 - m2;
        for (const c of b.marks) setAttr(c, 'transform', Math.hypot(k[0], k[1]) < 0.01 ? null : tr(k, 1));
        if (b.rP == null && keys.length) vis *= 1 - m;
        setVar(b.g, '--o', vis);
        // the fill eases from one reading to the other with the move, so only the bodies whose answer changes
        // change, none of them flickers, and every one has the new reading's fill when the labels land; on the
        // ladder every mark is solid (the zero line, not the fill, says which way a share went)
        setVar(b.g, '--h', (b.rP == null ? b.hM : b.hM + (b.hP - b.hM) * m) * (1 - m2));
      }
      for (const n of leaders) setVar(n, '--o', ph[setOf(n)]);
      for (const n of labs) {
        const f = n.dataset.for;
        const set = setOf(n);
        let o;
        if (set) o = ph[set];
        else {
          let at;
          if (f === 'near') at = s1 + 1.4;
          else if (f === 'gap') at = s1 + 2.7;
          else if (n.dataset.entity === 'usd' || f === 'USD') at = s1 + 2.4;
          else { const i = others.findIndex((b) => b.code === f); at = s2 + 1.1 + Math.max(0, i) * 0.05; }
          o = c01((s - at) / 0.2) * (hasPayLabels || !keys.length ? ph[0] : 1 - m);
          if (f === 'gap') o *= 1 - pull;
        }
        setVar(n, '--o', o);
        setFlag(n, 'data-off', o <= 0);
      }
      const key = c01((s - s2 - 1.8) / 0.3);
      for (const n of legend) {
        const o = key * ws(n, ph);
        setVar(n, '--o', o);
        setFlag(n, 'data-off', o <= 0);
      }
      for (const n of whens) { const o = ph[setOf(n)]; setVar(n, '--o', o); setFlag(n, 'data-off', o <= 0); }
    }

    // a slot in another shot (the two match cuts) borrows the same node, settled
    function place(slot) {
      const want = slot ? slot : 'home';
      if (want === where) return;
      if (slot) slot.appendChild(fig); else marker.after(fig);
      where = want;
      laid = false;
    }

    // labels never sit on each other or across the blue ring: checked once per layout, in unscaled pixels, for each
    // state's labels against that state's ring
    function layout() {
      if (!fig.offsetWidth || !rN) return;
      laid = true;
      const box = cam.getBoundingClientRect();
      const z = box.width / (cam.offsetWidth || 1);
      const scale = (cam.offsetWidth || 1) / vw;
      setVar(svg, '--u', (1 / scale).toFixed(3));
      const Ccx = cx * scale, Ccy = cy * scale;
      for (const set of [0, 1]) {
        const R = (set ? nP : nM) * scale;
        if (!Number.isFinite(R)) continue;
        const items = labs.concat($$('.o-lab[data-at]', fig)).filter((n) => setOf(n) === set)
          .map((n) => { n.style.setProperty('--nx', '0px'); n.style.setProperty('--ny', '0px'); return n; })
          .map((n) => {
            const r = n.getBoundingClientRect();
            return { n, x: (r.left - box.left) / z, y: (r.top - box.top) / z, w: r.width / z, h: r.height / z, dx: 0, dy: 0 };
          });
        const fixed = (it) => it.n.dataset.for === 'near' || it.n.dataset.for === 'gap';
        const dist = (it) => {
          const nx = clamp(Ccx, it.x + it.dx, it.x + it.dx + it.w), ny = clamp(Ccy, it.y + it.dy, it.y + it.dy + it.h);
          const near = Math.hypot(nx - Ccx, ny - Ccy);
          let far = 0;
          for (const [px, py] of [[0, 0], [it.w, 0], [0, it.h], [it.w, it.h]]) far = Math.max(far, Math.hypot(it.x + it.dx + px - Ccx, it.y + it.dy + py - Ccy));
          return [near, far];
        };
        // this state's dots, so the centre label never lands on one
        const dots = bodies.map((b) => (set ? b.rP : b.rM)).map((r, i) => (r == null ? null : [(cx + bodies[i].ux * r) * scale, (cy + bodies[i].uy * r) * scale])).filter(Boolean);
        const hits = (it) => dots.some(([px, py]) => px > it.x + it.dx - 6 && px < it.x + it.dx + it.w + 6 && py > it.y + it.dy - 6 && py < it.y + it.dy + it.h + 6);
        for (const it of items) {
          // the centre label wider than the ring goes just under it (centred, then to either side), or just over it,
          // wherever no dot is
          if (it.n.dataset.for !== 'near') continue;
          const [near, far] = dist(it);
          if (!(near < R && far > R)) continue;
          const opts = [];
          for (const y of [Ccy + R + 4 - it.y, Ccy - R - 4 - it.h - it.y]) for (const x of [0, Ccx - it.x, Ccx - it.w - it.x]) opts.push([x, y]);
          [it.dx, it.dy] = opts.find(([x, y]) => { it.dx = x; it.dy = y; return !hits(it); }) || opts[0];
        }
        for (let pass = 0; pass < 10; pass++) {
          let moved = false;
          for (const it of items) {
            if (fixed(it)) continue;
            const [near, far] = dist(it);
            if (near < R + 3 && far > R - 3) {
              // on the ring's stroke: push to the side the label's own centre is on
              const mx = it.x + it.dx + it.w / 2 - Ccx, my = it.y + it.dy + it.h / 2 - Ccy, mm = Math.hypot(mx, my) || 1;
              const step = mm < R ? -(far - R + 4) : (R - near + 4);
              it.dx += (mx / mm) * step; it.dy += (my / mm) * step; moved = true;
            }
          }
          for (let i = 0; i < items.length; i++) {
            for (let q = i + 1; q < items.length; q++) {
              const a = items[i], b = items[q];
              const ox = Math.min(a.x + a.dx + a.w, b.x + b.dx + b.w) - Math.max(a.x + a.dx, b.x + b.dx);
              const oy = Math.min(a.y + a.dy + a.h, b.y + b.dy + b.h) - Math.max(a.y + a.dy, b.y + b.dy);
              if (ox > -2 && oy > -2) {
                // apart along the shorter overlap, the movable one away from the other
                const mv = fixed(b) ? a : b;
                const other = mv === a ? b : a;
                if (ox < oy) mv.dx += (mv.x + mv.dx >= other.x + other.dx ? 1 : -1) * (ox + 3);
                else mv.dy += (mv.y + mv.dy >= other.y + other.dy ? 1 : -1) * (oy + 3);
                moved = true;
              }
            }
          }
          if (!moved) break;
        }
        for (const it of items) { it.n.style.setProperty('--nx', it.dx.toFixed(1) + 'px'); it.n.style.setProperty('--ny', it.dy.toFixed(1) + 'px'); }
      }
    }

    return { fig, title, settled, draw, place, layout, relayout: () => { laid = false; } };
  })();

  /* ------------------------------------------------------------------ charts inside the film */

  const mounted = new WeakSet();
  function mountCharts(sc) {
    if (!sc || !window.NearCharts || !window.NearCharts.start) return;
    for (const f of $$('figure[data-chart]', sc.node)) {
      if (mounted.has(f) || !$('.plot', f)) continue;
      mounted.add(f);
      window.NearCharts.start(f);
    }
  }

  /* ------------------------------------------------------------------ the desk hand-off */

  const deskScene = scenes.find((s) => s.id === 'desk');
  const sim = $('#sim', film);
  let simAsked = false;
  function loadSim() {
    if (simAsked || !sim) return;
    simAsked = true;
    if (d.querySelector('script[data-sim], script[src$="sim.js"]')) return;
    const s = d.createElement('script');
    s.src = sim.dataset.src || 'assets/sim.js';
    s.defer = true;
    s.dataset.sim = '';
    d.head.appendChild(s);
  }

  /* ------------------------------------------------------------------ fit: a frame is never cut */

  // a shot that holds more than the stage is scaled to fit it, measured from layout (not from anything in motion),
  // once per shot, stage size and font load. What a box clips (the checklist's cut-off rows) is not measured: shrinking
  // the shot would not bring it into view
  let refit = true, fitGen = 0;
  const unfit = (n) => { n.removeAttribute('data-fit'); n.style.scale = n.style.translate = n.style.transformOrigin = ''; };
  function fit(n, sc) {
    const key = film.clientWidth + 'x' + sc.clientHeight + 'g' + fitGen;
    if (n.dataset.fit === key || !n.offsetParent) return;
    unfit(n);
    n.dataset.fit = key;
    const cs = getComputedStyle(sc), f0 = parseFloat(cs.paddingTop), H = sc.clientHeight - f0 - parseFloat(cs.paddingBottom);
    let c0 = Infinity, c1 = -Infinity;
    const clips = [];
    for (const e of [n, ...$$('*', n)]) {
      if (!e.offsetParent || !e.offsetHeight || clips.some((c) => c.contains(e))) continue;
      if (e !== n && e.scrollHeight > e.clientHeight + 1 && getComputedStyle(e).overflowY !== 'visible') clips.push(e);
      let y = 0, p = e;
      while (p && p !== sc) { y += p.offsetTop; p = p.offsetParent; }
      if (p) { c0 = Math.min(c0, y); c1 = Math.max(c1, y + e.offsetHeight); }
    }
    if (!(H > 0 && c1 > c0)) return;
    const k = Math.max(0.6, Math.min(1, H / (c1 - c0)));
    const ty = k < 1 || c0 < f0 ? f0 - c0 : Math.min(0, f0 + H - c1);
    if (k === 1 && !ty) return;
    n.style.transformOrigin = '0 ' + (c0 - n.offsetTop) + 'px';
    n.style.scale = String(Math.floor(k * 1000) / 1000);
    n.style.translate = '0 ' + ty + 'px';
  }

  /* ------------------------------------------------------------------ render(S) */

  function render() {
    const sh = S.article ? null : shotAt(S.T);
    if (S.article) return renderArticle();
    const sc = sh.scene;
    const lit = !S.playing && !S.scrubbing && !S.rm;
    film.classList.toggle('lit', lit);
    film.classList.toggle('playing', S.playing);
    film.classList.toggle('rm', S.rm);

    if (sc !== curScene) {
      for (const s of scenes) {
        const on = s === sc;
        s.node.hidden = !on;
        setFlag(s.node, 'inert', !on);
        s.node.classList.toggle('on', on);
      }
      curScene = sc;
      if (slateScene) slateScene.textContent = sc.title;
      labelEls.forEach((b, i) => b.classList.toggle('cur', scenes[i] === sc));
      mountCharts(sc);
      mountCharts(scenes[sc.idx + 1]);
      if (deskScene && sc.idx >= deskScene.idx - 1) loadSim();
      layoutLabels();
    }
    if (sh !== cur) {
      const prev = cur;
      cur = sh;
      const i = sc.shots.indexOf(sh);
      const back = sh.verdict && i > 0 && !sc.shots[i - 1].verdict ? sc.shots[i - 1] : null;
      for (const s of sc.shots) {
        const on = s === sh, behind = s === back;
        s.node.hidden = !(on || behind);
        s.node.dataset.st = on ? 'on' : behind ? 'behind' : s.local < sh.local ? 'past' : 'future';
        setFlag(s.node, 'inert', !on);
        if (behind) { s.node.setAttribute('aria-hidden', 'true'); settle(s.timed); } else s.node.removeAttribute('aria-hidden');
      }
      // the caption band: scene, position, and the shot's own caption (cloned, so the shot keeps its text)
      bandK.textContent = sc.title + ' · ' + sh.n + '/' + sc.shots.length;
      bandText.textContent = '';
      if (sh.cap) for (const c of Array.from(sh.cap.childNodes)) bandText.appendChild(c.cloneNode(true));
      for (const n of $$('[id]', bandText)) n.removeAttribute('id');
      // the orbit's two returns borrow the title's node
      if (Orbit) {
        const slot = $('[data-orbit-slot]', sh.node);
        if (slot) Orbit.place(slot); else if (sc === Orbit.title) Orbit.place(null);
      }
      const open = !!(deskScene && sh.scene === deskScene && sh === deskScene.shots[deskScene.shots.length - 1]);
      setOpen(open);
      if (prev && prev.scene !== sc) film.scrollTop = 0;
      refit = true;
    }
    if (refit) {
      refit = false;
      if (!film.classList.contains('open')) for (const c of sc.node.children) if (c.matches('.shot, .layer')) fit(c, sc.node);
    }

    const lt = shotTime(sh, S.T);
    drawTimed(sh.timed, lt, lit);
    // scene-level elements follow the scene's clock; lights do not pull them ahead of the picture
    const st = S.rm ? sh.local + sh.dur - 0.001 : S.T - sc.start;
    drawTimedScene(sc.timed, st, sh.local + sh.dur);
    if (Orbit) {
      const slot = $('[data-orbit-slot]', sh.node);
      if (sc === Orbit.title) Orbit.draw(st);
      else if (slot) Orbit.draw(Orbit.settled, slot.dataset.orbitSlot);
    }

    // transport
    const T = S.T;
    timeNow.textContent = mmss(T);
    if (!S.scrubbing) scrub.value = String(Math.round(T * 100) / 100);
    segEls.forEach((s, i) => {
      const x = scenes[i];
      setVar(s, '--f', T >= x.end ? 1 : T <= x.start ? 0 : (T - x.start) / x.dur);
    });
    // the position in words already on the page: time, scene, shot, and the verdict word if one is stamped
    setAttr(scrub, 'aria-valuetext', mmss(T) + ', ' + sc.title + ' ' + sh.n + '/' + sc.shots.length + (sh.stamp ? ', ' + (sh.stamp.dataset.claim || '') + ' ' + (sh.stamp.dataset.verdict || '') : ''));
    const atEnd = S.held || (!S.playing && T >= endT() - 0.01);
    tp.classList.toggle('held', atEnd);
    bPlay.dataset.state = S.playing ? 'pause' : atEnd ? 'replay' : 'play';
    setAttr(bPlay, 'aria-label', (S.playing ? playWords.pause : atEnd ? playWords.replay : '') || playWords.play);
    setAttr(bMotion, 'aria-pressed', S.rm ? 'false' : 'true');
  }

  // a scene-level element due in a later shot is not ghosted ahead: it is not there yet ('far')
  function drawTimedScene(list, s, end) {
    for (const t of list) {
      const lt = s - t.offset;
      let st, p;
      if (lt >= t.out) { st = 'out'; p = 1; }
      else if (lt < t.at) { st = t.at + t.offset >= (end || Infinity) - 0.001 ? 'far' : 'pre'; p = 0; }
      else { st = 'in'; p = S.rm ? 1 : t.ease(c01((lt - t.at) / t.dur)); }
      if (t.n.dataset.st !== st) t.n.dataset.st = st;
      setVar(t.n, '--p', p);
    }
  }

  // reduced motion, read as an article: every scene and shot in document order, settled
  function renderArticle() {
    film.classList.remove('lit', 'playing');
    for (const n of $$('[data-fit]', film)) unfit(n);
    for (const s of scenes) {
      s.node.hidden = false;
      setFlag(s.node, 'inert', false);
      for (const x of s.shots) { x.node.hidden = false; setFlag(x.node, 'inert', false); x.node.dataset.st = 'on'; x.node.removeAttribute('aria-hidden'); settle(x.timed); }
      drawTimedScene(s.timed, s.dur + 10);
      mountCharts(s);
    }
    if (Orbit) { Orbit.place(null); Orbit.draw(Orbit.settled, 'money'); }
    curScene = null;
    cur = null;
  }

  /* ------------------------------------------------------------------ the frame opens (the desk's last shot) */

  let isOpen = false;
  function setOpen(on) {
    if (on === isOpen) return;
    isOpen = on;
    film.classList.toggle('open', on);
    if (sim) sim.dataset.film = on ? 'open' : 'stage';
    if (on) loadSim();
  }

  /* ------------------------------------------------------------------ the clock */

  function frame(ts) {
    raf = 0;
    if (S.playing) {
      const dt = lastTs ? Math.min(0.1, (ts - lastTs) / 1000) : 0;
      lastTs = ts;
      const before = shotAt(S.T);
      let T = S.T + dt;
      const holdAt = before.start + before.dur - 0.001;
      if ((before.hold || before === shots[shots.length - 1]) && T >= holdAt) {
        S.T = holdAt;
        S.playing = false;
        S.held = true;
        render();
        return;
      }
      if (T >= endT()) T = endT();
      S.T = T;
    }
    render();
    if (S.playing) raf = requestAnimationFrame(frame);
  }
  function kick() { if (!raf) raf = requestAnimationFrame(frame); }

  function play() {
    if (S.article) exitArticle();
    if (S.held || S.T >= endT() - 0.01) { S.T = 0; S.held = false; if (isOpen) film.scrollIntoView(); }
    S.playing = true;
    autoPaused = false;
    lastTs = 0;
    kick();
  }
  function pause() {
    if (!S.playing) return;
    S.playing = false;
    lastTs = 0;
    kick();
  }
  function userPause() { autoPaused = false; pause(); }
  function toggle() { if (S.playing) userPause(); else play(); }
  function seek(T) {
    S.T = clamp(T, 0, endT());
    S.held = false;
    if (S.article) exitArticle();
    kick();
  }
  function step(dir) {
    userPause();
    const sh = shotAt(S.T), i = shots.indexOf(sh);
    if (dir < 0 && S.T - sh.start > 1.2 && !S.rm) return seek(sh.start);
    if (dir > 0 && !shots[i + 1]) return;
    seek(shots[clamp(i + dir, 0, shots.length - 1)].start);
  }
  function stepScene(dir) {
    userPause();
    const sc = shotAt(S.T).scene;
    seek(scenes[clamp(sc.idx + dir, 0, scenes.length - 1)].start);
  }
  function lastFrame(sh) { return sh.start + sh.dur - 0.001; }

  /* ------------------------------------------------------------------ motion and the article */

  const rmNote = $('.rm-note', film);
  function setMotion(off) {
    S.rm = off;
    try { sessionStorage.setItem('film-motion', off ? 'off' : 'on'); } catch (e) { /* private mode */ }
    if (rmNote) rmNote.hidden = !off;
    kick();
  }
  function enterArticle() {
    userPause();
    S.article = true;
    film.classList.add('article');
    render();
    film.scrollIntoView({ block: 'start' });
  }
  function exitArticle() {
    S.article = false;
    film.classList.remove('article');
    curScene = null;
    cur = null;
  }

  /* ------------------------------------------------------------------ the evidence dialog */

  // the page's dialog: a heading, a close button, an empty body, and one link into the full evidence
  const dlg = $('dialog.ev-dialog') || $('dialog.ev');
  const dlgBody = dlg && ($('.ev-body', dlg) || dlg);
  const dlgH = dlg && ($('h2', dlg) || $('[id]', dlg));
  const dlgFull = dlg && $('[data-act="full"], .ev-full a', dlg);
  let dlgSub = null;
  if (dlg) {
    dlg.classList.add('ev');
    if (dlgH) { dlgH.tabIndex = -1; dlgSub = el('p', 'ev-scene'); dlgH.after(dlgSub); }
  }
  const moved = [];
  let opener = null;

  // jump from the dialog into the page: close, then land on the target in the evidence
  function goTo(id) {
    opener = null;
    closeEvidence();
    const t = id && d.getElementById(id);
    if (!t) return;
    t.scrollIntoView({ block: 'start' });
    if (!t.hasAttribute('tabindex')) t.tabIndex = -1;
    t.focus({ preventScroll: true });
  }
  function openEvidence(claims, charts, heading, home) {
    const ids = [...claims.map((c) => 'claim-' + c), ...charts].filter((id) => d.getElementById(id));
    userPause();
    if (!dlg) { goTo(home || ids[0] || 'evidence'); return; }
    if (dlg.open) closeEvidence();
    // focus goes back where it came from, or to the Evidence button when the dialog opened from the address
    opener = d.activeElement && d.activeElement !== d.body ? d.activeElement : bEv;
    dlgBody.textContent = '';
    for (const id of ids) {
      const n = d.getElementById(id);
      if (film.contains(n) || dlg.contains(n)) continue;
      const m = d.createComment('ev');
      n.before(m);
      moved.push([n, m]);
      el('div', 'ev-item', dlgBody).appendChild(n);
      for (const x of $$('.num[title]', n)) { x.dataset.tip = x.title; x.removeAttribute('title'); }
      if (window.NearCharts) for (const f of n.matches('figure[data-chart]') ? [n] : $$('figure[data-chart]', n)) window.NearCharts.start(f);
    }
    if (dlgSub) dlgSub.textContent = heading || '';
    if (dlgFull) dlgFull.setAttribute('href', '#' + (home || ids[0] || 'evidence'));
    if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
    if (dlgH) dlgH.focus();
    kick();
  }
  function restore() {
    while (moved.length) {
      const [n, m] = moved.pop();
      for (const x of $$('.num[data-tip]', n)) { x.title = x.dataset.tip; delete x.dataset.tip; }
      m.replaceWith(n);
    }
    if (dlgBody) dlgBody.textContent = '';
    const o = opener;
    opener = null;
    if (o && o.focus && d.contains(o)) o.focus({ preventScroll: true });
  }
  function closeEvidence() {
    if (!dlg) return;
    if (dlg.open && typeof dlg.close === 'function') dlg.close(); else dlg.removeAttribute('open');
    restore();
  }
  if (dlg) {
    dlg.addEventListener('close', () => { if (!dlg.open) restore(); });
    dlg.addEventListener('click', (e) => {
      const t = e.target;
      if (t.closest && t.closest('[data-act="close"], .ev-close')) { closeEvidence(); return; }
      const a = t.closest && t.closest('a[href^="#"]');
      if (a) {
        // links to the evidence leave the dialog for the page, unless their target is already in the dialog
        const id = decodeURIComponent(a.getAttribute('href').slice(1));
        const n = d.getElementById(id);
        e.preventDefault();
        if (a !== dlgFull && n && dlg.contains(n)) n.scrollIntoView({ block: 'start' }); else goTo(id);
        return;
      }
      if (t === dlg) {
        // a click on the backdrop (outside the panel's box) closes it
        const r = dlg.getBoundingClientRect();
        if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) closeEvidence();
      }
    });
  }
  const words = (s) => (s || '').split(/\s+/).filter(Boolean);
  function evidenceForScene() {
    const sc = shotAt(S.T).scene;
    openEvidence(words(sc.node.dataset.claims), words(sc.node.dataset.charts), sc.title, d.getElementById('ev-' + sc.id) ? 'ev-' + sc.id : '');
  }
  // a claim's own charts: the chart links in its evidence block
  function chartsOf(cid) {
    const b = d.getElementById('claim-' + cid);
    return b ? $$('a[href^="#"]', b).map((a) => a.getAttribute('href').slice(1)).filter((id) => { const n = d.getElementById(id); return n && n.matches('figure'); }) : [];
  }

  /* ------------------------------------------------------------------ wiring */

  tp.addEventListener('click', (e) => {
    const b = e.target.closest('[data-act]');
    if (!b) return;
    const act = b.dataset.act;
    if (act === 'play') toggle();
    else if (act === 'prev') step(-1);
    else if (act === 'next') step(1);
    else if (act === 'motion') setMotion(!S.rm);
    else if (act === 'evidence') evidenceForScene();
  });

  // the scrubber: focusing or dragging pauses; dragging draws every frame it passes; letting go stays paused
  scrub.addEventListener('pointerdown', () => { userPause(); S.scrubbing = true; });
  scrub.addEventListener('focus', () => userPause());
  scrub.addEventListener('input', () => { S.T = clamp(num(scrub.value, 0), 0, endT()); S.held = false; if (S.article) exitArticle(); kick(); });
  const endScrub = () => { if (S.scrubbing) { S.scrubbing = false; kick(); } };
  scrub.addEventListener('change', endScrub);
  scrub.addEventListener('pointerup', endScrub);
  scrub.addEventListener('pointercancel', endScrub);
  scrub.addEventListener('blur', endScrub);
  scrub.addEventListener('keydown', (e) => {
    // the scrubber keeps its own arrows: a second a press, ten with Page keys
    const by = { ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1, PageDown: -10, PageUp: 10 }[e.key];
    if (by) { e.preventDefault(); userPause(); seek(S.T + by); }
    else if (e.key === 'Home') { e.preventDefault(); seek(0); }
    else if (e.key === 'End') { e.preventDefault(); seek(endT()); }
  });

  // a click on the frame's empty area plays or pauses
  (parentOfScenes === film ? film : parentOfScenes).addEventListener('click', (e) => {
    const t = e.target;
    if (S.article || !t.closest || t.closest('a, button, input, select, textarea, summary, label, [tabindex], .num, .plot, #sim, dialog')) return;
    const sel = window.getSelection && window.getSelection();
    if (sel && String(sel).length) return;
    toggle();
  });

  // chips and tags that name a claim open its evidence
  film.addEventListener('click', (e) => {
    const c = e.target.closest && e.target.closest('button[data-claim], a[data-claim]');
    if (!c || !film.contains(c)) return;
    e.preventDefault();
    const cid = c.dataset.claim;
    openEvidence([cid], chartsOf(cid), text($('.chip-id', c)) || cid, 'claim-' + cid);
  });

  // skip links: the desk jumps to its settled, opened state; the evidence is plain document scroll
  for (const a of $$('a[data-skip], .skip-link', d)) {
    a.addEventListener('click', (e) => {
      const to = a.dataset.skip || (a.getAttribute('href') || '').replace('#', '');
      if (to === 'desk' && deskScene) {
        e.preventDefault();
        userPause();
        S.held = true;
        seek(lastFrame(deskScene.shots[deskScene.shots.length - 1]));
        S.held = true;
        render();
        loadSim();
        (sim || deskScene.node).scrollIntoView({ block: 'start' });
      } else userPause();
    });
  }
  for (const a of $$('[data-article]', film)) a.addEventListener('click', (e) => { e.preventDefault(); enterArticle(); });

  // the desk: touching a control pauses, and the film jumps to the open frame
  if (sim) {
    const take = (e) => {
      if (!deskScene || isOpen) return;
      if (!(e.target.closest && e.target.closest('input, select, button, textarea, [role="slider"], [role="radio"]'))) return;
      userPause();
      seek(lastFrame(deskScene.shots[deskScene.shots.length - 1]));
      S.held = true;
    };
    sim.addEventListener('pointerdown', take);
    sim.addEventListener('focusin', take);
    sim.addEventListener('sim:input', () => userPause());
  }

  d.addEventListener('keydown', (e) => {
    if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
    if (dlg && dlg.open) { if (e.key === 'e' || e.key === 'E') { e.preventDefault(); closeEvidence(); } return; }
    const t = e.target;
    if (t && t.closest && (t.closest('input, select, textarea, [contenteditable=""], [contenteditable="true"]') || (isOpen && t.closest('#sim')))) return;
    // the film's keys belong to the film: in the evidence or the opened desk, Space, Home, End and the arrows scroll
    // the page as usual unless focus is in the film
    if (!(t && t.closest && t.closest('.film')) && (isOpen || !inView)) return;
    const onControl = t && t.closest && t.closest('button, a, summary, [role="button"]');
    const k = e.key;
    if (k === ' ' || k === 'k' || k === 'K') { if (k === ' ' && onControl) return; e.preventDefault(); toggle(); }
    else if (k === 'ArrowRight') { e.preventDefault(); if (e.shiftKey) stepScene(1); else step(1); }
    else if (k === 'ArrowLeft') { e.preventDefault(); if (e.shiftKey) stepScene(-1); else step(-1); }
    else if (k === 'Home') { e.preventDefault(); userPause(); seek(0); }
    else if (k === 'End') { e.preventDefault(); userPause(); seek(endT()); S.held = true; }
    else if (k === 'e' || k === 'E') { e.preventDefault(); evidenceForScene(); }
    else if (k === 'm' || k === 'M') { e.preventDefault(); setMotion(!S.rm); }
    else if (/^[1-9]$/.test(k) && scenes[+k - 1]) { e.preventDefault(); userPause(); seek(scenes[+k - 1].start); }
  });

  // pause rules: a hidden tab, or less than half the stage in view; those pauses lift by themselves
  function autoPause() { if (S.playing) { autoPaused = true; pause(); } }
  function autoResume() { if (autoPaused && inView && !d.hidden && !(dlg && dlg.open)) play(); }
  d.addEventListener('visibilitychange', () => { if (d.hidden) autoPause(); else autoResume(); });
  if ('IntersectionObserver' in window) {
    new IntersectionObserver((en) => {
      for (const x of en) { inView = x.intersectionRatio >= 0.5 || isOpen; if (!inView) autoPause(); else autoResume(); }
    }, { threshold: [0, 0.5, 1] }).observe(film);
  }
  if (mqReduce.addEventListener) mqReduce.addEventListener('change', (e) => { if (!stored) setMotion(e.matches); });

  /* ------------------------------------------------------------------ layout: scene labels that never collide */

  // every scene's name above its segment, none hidden and none touching: a name too long for the room it has
  // takes the scene's short name, the longest first, then letters and gap tighten. On a phone,
  // and on a track too narrow for all of them, only the current scene is named
  function layoutLabels() {
    const tw = segs.clientWidth;
    if (!tw) return;
    const n = labelEls.length, cur = curScene ? curScene.idx : 0;
    let gap = 24;
    const geo = segEls.map((s) => [s.offsetLeft, s.offsetWidth]);
    const short = scenes.map(() => false);
    const place = () => {
      labelEls.forEach((b, i) => { b.hidden = false; b.textContent = short[i] ? scenes[i].short : scenes[i].title; });
      const w = labelEls.map((b) => b.offsetWidth), x = [];
      for (let i = 0; i < n; i++) x[i] = Math.max(geo[i][0], i ? x[i - 1] + w[i - 1] + gap : 0);
      for (let i = n - 1, lim = tw; i >= 0; lim = x[i] - gap, i--) x[i] = Math.min(x[i], lim - w[i]);
      // a name starts within its own segment (a hair before it at most), so it reads as that segment's name
      const miss = x.map((v, i) => Math.max(geo[i][0] - 4 - v, v - geo[i][0] - geo[i][1] / 2, (i ? x[i - 1] + w[i - 1] + gap - v : -v)));
      return { w, x, miss };
    };
    labels.style.cssText = '';
    let r = null;
    if (film.clientWidth > PHONE) {
      for (let k = 0; k <= 2 * n + 1; k++) {
        r = place();
        if (r.miss.every((v) => v <= 0.5)) break;
        // shorten the longest full name in trouble (or beside it), else tighten, else give up
        const cands = labelEls.map((_, i) => i).filter((i) => !short[i] && scenes[i].short !== scenes[i].title
          && Math.max(r.miss[i], r.miss[i + 1] || 0, r.miss[i - 1] || 0) > 0.5);
        if (cands.length) short[cands.sort((a, b) => r.w[b] - r.w[a])[0]] = true;
        else if (!labels.style.cssText) labels.style.cssText = '--ls:0.04em';
        else if (gap > 16) gap = 16;
        else { r = null; break; }
        if (k === 2 * n + 1) r = null;
      }
    }
    labelEls.forEach((b, i) => {
      if (r) { b.style.left = r.x[i] + 'px'; return; }
      b.textContent = scenes[i].title;
      b.hidden = i !== cur;
      if (i === cur) b.style.left = clamp(geo[i][0], 0, Math.max(0, tw - b.offsetWidth)) + 'px';
    });
  }

  let rz = 0;
  function relayout() {
    clearTimeout(rz);
    rz = setTimeout(() => { fitGen++; refit = true; layoutLabels(); if (Orbit) Orbit.relayout(); render(); }, 120);
  }
  window.addEventListener('resize', relayout);

  /* ------------------------------------------------------------------ boot: deep links, autoplay */

  const hash = decodeURIComponent(location.hash.replace('#', ''));
  const named = shotById.get(params.get('shot') || '') || (hash && shotById.get(hash));
  let start = 0;
  if (named) start = named.start + clamp(num(params.get('t'), 0), 0, named.dur - 0.001);
  else if (params.has('t')) start = num(params.get('t'), 0);
  else if (S.rm) start = lastFrame(scenes[0].shots[scenes[0].shots.length - 1]);
  S.T = clamp(start, 0, endT());
  if (rmNote) rmNote.hidden = !S.rm;
  if (hash === 'desk' && deskScene) { S.T = lastFrame(deskScene.shots[deskScene.shots.length - 1]); S.held = true; }
  root.classList.add('film-on');
  render();
  layoutLabels();
  if (Orbit) Orbit.layout();
  if (d.fonts && d.fonts.ready) { d.fonts.ready.then(relayout); d.fonts.addEventListener('loadingdone', relayout); }

  const deep = !!named || params.has('t') || params.get('play') === '0' || (hash && hash !== 'film');
  if (!S.rm && !deep && !d.hidden) {
    const r = film.getBoundingClientRect();
    inView = r.height > 0 && Math.min(r.bottom, window.innerHeight) - Math.max(r.top, 0) >= r.height * 0.5;
    if (inView) play(); else autoPaused = true;
  }
  if (params.get('ev') === '1') {
    if (dlg) dlg.classList.add('still');
    evidenceForScene();
  }
})();
