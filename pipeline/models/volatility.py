"""Can a patient holder capture NEAR's volatility? Three strategies against shuffled histories (chart c25; claim
H10). The third, the perp ladder, is computed in perps.py; this module scores the first two and charts all three.

Formulas:
  F-vol-windows  One-year windows. A start day s ends at e = s + H. Strategies (a) and (b) need a daily close on every
                 day s..e, DFF on every day in [s, e) and liquid-staking samples at s+1 and e+1 (Samples.edge): a
                 close dated d is the 23:59 UTC price and pairs with the 00:00 UTC sample dated d+1. Prices enter only
                 as p_k = P_{s+k} / P_s, read from one log-price line over the windows' span. Staking:
                 u(k) = (1 + y_w)^(k/365.25), y_w the window's realized liquid-staking yield (an outcome no decision
                 reads). Cash: C(k) = prod_{j<k} (1 + DFF_{s+j}/36000), ACT/360 as F-ffcomp.
  F-vol-rally    Strategy (a). tau = the first day in 1..H with p_tau >= 1 + k/100; the sale is at the next close,
                 t = min(tau + 1, H), and the dollars earn fed funds to e: V_a = p_t u(t) C(H)/C(t), else
                 V_a = p_H u(H). adv_a = 100 (V_a - p_H u(H)) and vs_cash_a = 100 (V_a - C(H)), pp of the starting
                 NEAR stake. The decision reads p_1..p_tau only.
  F-vol-rebal    Strategy (b). w in NEAR and 1 - w in dollars, reset to w on days REBAL_DAYS, 2 REBAL_DAYS, ... < H.
                 adv_b = 100 (R_reb - R_bh) / w with R_bh = w p_H u(H) + (1 - w) C(H); log_b = 100 ln(R_reb / R_bh),
                 pp of the book; theory_b = 100 w (1 - w) sigma_w^2 / 2 over the window's own returns, a ceiling that
                 buy-and-hold partly earns too. w is the desk's NEAR share (sim_share), applied to every past window.
  F-vol-call     The value of volatility: today's Black-Scholes price of a one-year at-the-money call at the trailing
                 year's realised volatility (rv_365_near), r = 365 ln(1 + DFF/36000) at the latest DFF and
                 q = ln(1 + y_lst_90). A model price, not a quote.
  F-vol-null     The benchmark the rule reads. The daily log returns over the windows' span are cut into blocks of
                 NULL_BLOCK_DAYS from the start (the last may be shorter) and the blocks reordered by Fisher-Yates
                 (j = (next * (i + 1)) >> 32 on portfolio.Prng(sim_seed)), NULL_DRAWS times from one stream. Every
                 window keeps its real C and u; only p_k changes. Per statistic:
                 rank = 100 x (shuffle means strictly below the real mean) / NULL_DRAWS, band = type-7 quantiles of
                 the shuffle means at RANK_LO and RANK_HI, excess = real mean - shuffled mean.
"""

from __future__ import annotations

import math
import statistics
import weakref
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import accumulate, chain
from operator import add, mul

from pipeline.chartspec import Chart, Series
from pipeline.models import formulas as F
from pipeline.models import load, market, supply
from pipeline.models.macro import empty_state, files_as_of, fred_query, iso, notes_for, ready, rnd
from pipeline.models.params import param
from pipeline.models.portfolio import Prng, q7, run_jobs
from pipeline.numbers import Registry

HORIZON_DAYS = int(param("vol_horizon_days"))
RALLY_PCT = param("vol_rally_pct")
RALLY_LADDER = param("vol_rally_ladder")
REBAL_DAYS = int(param("vol_rebal_days"))
MIN_YEARS = int(param("vol_min_years"))
NULL_BLOCK_DAYS = int(param("vol_null_block_days"))
NULL_DRAWS = int(param("vol_null_draws"))
RANK_HI = param("vol_rank_hi")
RANK_LO = param("vol_rank_lo")
SEED = int(param("sim_seed"))

STAKE_YEAR = 365.25          # u(k): the F-lst day count
SPAN_GAP = "a daily close is missing inside the window span"
NO_SHARE = "sim_share is not computed"
CHUNK = 125                  # shuffles per parallel job (only builds with enough draws fan out)
# p_tau >= 1 + k/100 is read on log prices, whose sums carry rounding noise of about 1e-16: a close at exactly
# the trigger (1.828 to 2.285 is exactly +25%) must count as reached, so the comparison allows this much slack
TIE = 1e-12


def bs_call(s: float, k: float, t: float, sigma: float, r: float, q: float) -> float:
    """Black-Scholes call with continuous r and q (intrinsic value at sigma = 0)."""
    if sigma <= 0 or t <= 0:
        return max(s * math.exp(-q * t) - k * math.exp(-r * t), 0.0)
    d1 = (math.log(s / k) + (r - q + sigma * sigma / 2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    n = statistics.NormalDist()
    return s * math.exp(-q * t) * n.cdf(d1) - k * math.exp(-r * t) * n.cdf(d2)


class LstIndex:
    """The full-day LST samples indexed once. `edge` is Samples.edge's rule (closest within supply.EDGE_DAYS, the
    earlier on a tie)."""

    def __init__(self, smp: supply.Samples) -> None:
        self.rows = smp.with_("lst", full_only=True)
        self.dates = [r["date"] for r in self.rows]

    def edge(self, target: date) -> dict | None:
        best = None
        for i in range(bisect_left(self.dates, target - timedelta(days=supply.EDGE_DAYS)), len(self.dates)):
            gap = (self.dates[i] - target).days
            if gap > supply.EDGE_DAYS:
                break
            if best is None or abs(gap) < abs((self.dates[best] - target).days):
                best = i
        return self.rows[best] if best is not None else None


@dataclass
class Win:
    s: date
    e: date
    y_w: float
    r0: dict
    r1: dict
    sig_w: float | None = None
    o: int = 0


@dataclass
class Ctx:
    """Everything a scan needs that a shuffle leaves alone (plain lists, so it pickles to a worker)."""
    H: int
    w: float | None
    offs: list[int]
    gy: list[float]                  # ln(1 + y_w) / 365.25, per window
    uH: list[float]                  # u(H)
    cashH: list[float]               # C(H)
    LC: list[float]                  # LC[i] = sum of ln(1 + DFF/36000) over the span's first i days
    trigs: list[float]               # ln(1 + k/100) for the rally levels, ascending
    main: int                        # index of RALLY_PCT in the levels
    segs: list[tuple[int, int, int]]                    # strategy (b) periods: (days, first day, how many)
    AB: list[list[tuple[list[float], list[float]]]]     # per window and period group: w u-ratios, (1 - w) C-ratios


STATS = ("a", "a_cash", "b", "b_log")


def _strategies(cx: Ctx, L: list[float], keep: bool = False):
    """One scan of every window on the log-price line L (real or shuffled). Returns the means
    (a, a_cash, b, b_log, *levels) and, with keep, the per-window results in window order.

    Strategy (a) at every level comes from one right-to-left pass: the stack holds the running-maximum records of L
    to the right of the window's start (negated, so it is ascending), and the first close at or above a trigger is
    the topmost record at or above it, found by bisection."""
    H, offs, LC, trigs, gys, main = cx.H, cx.offs, cx.LC, cx.trigs, cx.gy, cx.main
    w, nl, n = cx.w, len(trigs), len(offs)
    exp, log = math.exp, math.log
    X = {ell: [exp(b - a) for a, b in zip(L, L[ell:])] for ell in {g[0] for g in cx.segs}} if w else {}
    sa = [0.0] * nl
    s_cash = s_b = s_blog = 0.0
    sv: list[float] = []
    si: list[int] = []
    pos = len(L) - 1
    per = [] if keep else None
    for wi in range(n - 1, -1, -1):
        o = offs[wi]
        while pos > o:
            v = -L[pos]
            while sv and sv[-1] >= v:
                sv.pop()
                si.pop()
            sv.append(v)
            si.append(pos)
            pos -= 1
        lo_, gy, lch, cash_h = L[o], gys[wi], LC[o + H], cx.cashH[wi]
        p_h = exp(L[o + H] - lo_)
        hold = p_h * cx.uH[wi]
        # (a): the next close after the first close at or above each level (levels ascending)
        v_main, taus, lv = hold, [None] * nl, [0.0] * nl
        for li in range(nl):
            p = bisect_right(sv, -(lo_ + trigs[li] - TIE))
            if not p:
                break
            tau = si[p - 1] - o
            if tau > H:
                break
            t2 = tau + 1 if tau < H else H
            val = exp(L[o + t2] - lo_ + t2 * gy + lch - LC[o + t2])
            sa[li] += val - hold
            taus[li], lv[li] = tau, val - hold
            if li == main:
                v_main = val
        s_cash += v_main - cash_h
        # (b): the book's value is the product over periods of (w * NEAR growth + (1 - w) * cash growth)
        rb = bh = None
        if w:
            rb = 1.0
            for (ell, start, cnt), (A, B) in zip(cx.segs, cx.AB[wi]):
                rb *= math.prod(map(add, map(mul, A, X[ell][o + start:o + start + ell * cnt:ell]), B))
            bh = w * hold + (1 - w) * cash_h
            s_b += rb - bh
            s_blog += log(rb / bh)
        if keep:
            per.append({"p_h": p_h, "hold": hold, "taus": taus, "lv": lv, "v_a": v_main, "cash_h": cash_h, "rb": rb,
                        "bh": bh})
    means = (100 * sa[main] / n, 100 * s_cash / n, 100 * s_b / (w * n) if w else None, 100 * s_blog / n if w else None,
             *(100 * x / n for x in sa))
    if keep:
        per.reverse()
        return means, per
    return means


def block_orders(nblocks: int, draws: int, seed: int) -> list[list[int]]:
    """The block order of each shuffle: Fisher-Yates from the last block down, j = (next * (i + 1)) >> 32, all
    draws from one Prng(seed) stream in order."""
    rng = Prng(seed)
    out = []
    for _ in range(draws):
        order = list(range(nblocks))
        for i in range(nblocks - 1, 0, -1):
            j = (rng.next() * (i + 1)) >> 32
            order[i], order[j] = order[j], order[i]
        out.append(order)
    return out


def blocks_of(ret: list[float], block: int) -> list[list[float]]:
    return [ret[i:i + block] for i in range(0, len(ret), block)]


def shuffled(ret: list[float], block: int, order: list[int]) -> list[float]:
    """The same daily returns, their consecutive blocks of `block` days in the given order."""
    blocks = blocks_of(ret, block)
    return list(chain.from_iterable(blocks[k] for k in order))


def line(ret: list[float]) -> list[float]:
    """L[0] = 0, L[i] = L[i-1] + r_i."""
    return [0.0, *accumulate(ret)]


def null_chunk(cx: Ctx, ret: list[float], block: int, orders: list[list[int]]) -> list[tuple]:
    """The means of every statistic on each shuffled history (one parallel job)."""
    return [_strategies(cx, line(shuffled(ret, block, order))) for order in orders]


def summarize(real: float | None, nulls: list[float] | None) -> dict | None:
    """Real mean against the shuffle means: shuffled mean, band (type-7 at RANK_LO and RANK_HI), excess, rank."""
    if real is None or not nulls:
        return None
    xs = sorted(nulls)
    mu = statistics.fmean(nulls)
    return {"real": real, "null_mean": mu, "lo": q7(xs, RANK_LO / 100), "hi": q7(xs, RANK_HI / 100),
            "excess": real - mu, "rank": 100 * sum(1 for x in nulls if x < real) / len(nulls)}


def band(rank: float | None) -> str | None:
    if rank is None:
        return None
    return "above" if rank >= RANK_HI else "below" if rank <= RANK_LO else "inside"


# =========================================================================== the windows

@dataclass
class Result:
    files: list[str]
    prices: market.Prices
    smp: supply.Samples
    wins: list[Win] = field(default_factory=list)
    reason: str | None = None            # no eligible window, and why
    fail: dict = field(default_factory=dict)
    w: float | None = None
    w_reason: str | None = None
    levels: list[float] = field(default_factory=list)
    real: dict = field(default_factory=dict)
    per: list[dict] = field(default_factory=list)
    nulls: dict[str, list[float]] | None = None
    null_reason: str | None = None
    draws: int = 0

    @property
    def years(self) -> int | None:
        return (self.wins[-1].e - self.wins[0].s).days // 365 if self.wins else None


def _windows(res: Result, close: dict[date, float], lr: list[tuple[date, float]], dff: dict, lst: LstIndex) -> None:
    """Every eligible window (F-vol-windows)."""
    H = HORIZON_DAYS
    if not close:
        res.reason = "no NEAR daily closes in data/raw/market"
        return
    d0, d1 = min(close), max(close)
    days = (d1 - d0).days + 1
    miss_c, miss_d = [0], [0]
    for i in range(days):
        d = d0 + timedelta(days=i)
        miss_c.append(miss_c[-1] + (close.get(d) is None))
        miss_d.append(miss_d[-1] + (dff.get(d) is None))
    lr_dates = [d for d, _ in lr]

    def rv(end_excl: date) -> float | None:
        a, b = bisect_left(lr_dates, end_excl - timedelta(days=365)), bisect_left(lr_dates, end_excl)
        return market.window_rv(lr[a:b], end_excl, 365, 365, strict=True)[0]

    fail = {"closes": 0, "dff": 0, "lst": 0}
    for i in range(days - H):
        s, e = d0 + timedelta(days=i), d0 + timedelta(days=i + H)
        if miss_c[i + H + 1] - miss_c[i]:
            fail["closes"] += 1
            continue
        if miss_d[i + H] - miss_d[i]:
            fail["dff"] += 1
            continue
        # the close of day d pairs with the 00:00 UTC sample of d+1 (market.stnear_usd's convention)
        r0, r1 = lst.edge(s + timedelta(days=1)), lst.edge(e + timedelta(days=1))
        y_w = supply.lst_yield(r0, r1) if r0 and r1 and r0["time"] < r1["time"] else None
        if y_w is None:
            fail["lst"] += 1
            continue
        res.wins.append(Win(s, e, y_w, r0, r1, sig_w=rv(e + timedelta(days=1))))
    res.fail = fail
    if not res.wins:
        n = days - H
        res.reason = ("no NEAR daily closes span a year" if n <= 0 else
                      "no one-year span of NEAR daily closes without a missing day (data/raw/market)"
                      if fail["closes"] == n else
                      "no one-year span with DFF on every day (data/raw/fred/DFF.csv)"
                      if fail["closes"] + fail["dff"] == n else
                      "no one-year window has liquid-staking samples near both ends (data/raw/rpc)")


def _context(res: Result, close: dict[date, float], dff: dict) -> tuple[Ctx, list[float], bool]:
    """The real log-price line over the span, the shuffle-invariant window data, and whether the span has a gap."""
    H, wins = HORIZON_DAYS, res.wins
    s0 = wins[0].s
    N = (wins[-1].e - s0).days
    ret, gap = [], False
    for i in range(1, N + 1):
        a, b = close.get(s0 + timedelta(days=i - 1)), close.get(s0 + timedelta(days=i))
        if a and b:
            ret.append(math.log(b / a))
        else:   # never inside an eligible window; the shuffles need every day
            ret.append(0.0)
            gap = True
    LC = [0.0]
    for j in range(N):
        rate = dff.get(s0 + timedelta(days=j))
        LC.append(LC[-1] + (math.log(1 + rate / 36000) if rate is not None else 0.0))
    levels = sorted(set(RALLY_LADDER) | {RALLY_PCT})
    res.levels = levels
    marks = list(range(REBAL_DAYS, H, REBAL_DAYS)) + [H]
    periods = list(zip([0] + marks[:-1], marks))
    segs: list[tuple[int, int, int]] = []
    for a, b in periods:
        if segs and segs[-1][0] == b - a and segs[-1][1] + segs[-1][0] * segs[-1][2] == a:
            segs[-1] = (segs[-1][0], segs[-1][1], segs[-1][2] + 1)
        else:
            segs.append((b - a, a, 1))
    w = res.w
    offs, gy, uH, cashH, AB = [], [], [], [], []
    for v in wins:
        v.o = o = (v.s - s0).days
        g = math.log(1 + v.y_w) / STAKE_YEAR
        offs.append(o)
        gy.append(g)
        uH.append(math.exp(H * g))
        ch = math.exp(LC[o + H] - LC[o])
        cashH.append(ch)
        groups = []
        if w:
            for ell, start, cnt in segs:
                ks = [start + ell * j for j in range(cnt)]
                groups.append(([w * math.exp(g * ell) for _ in ks],
                               [(1 - w) * math.exp(LC[o + k + ell] - LC[o + k]) for k in ks]))
        AB.append(groups)
    cx = Ctx(H, w, offs, gy, uH, cashH, LC, [math.log(1 + k / 100) for k in levels], levels.index(RALLY_PCT),
             segs, AB)
    return cx, ret, gap


def run(reg: Registry) -> Result:
    prices = market.near_prices()
    smp = supply.load_samples()
    dff_series = load.fred("DFF")
    dff = dff_series.as_dict()
    res = Result(sorted(set(prices.files + dff_series.files + smp.files)), prices, smp)
    share = reg.value("sim_share") if "sim_share" in reg else None
    res.w = share / 100 if share is not None and 0 < share < 100 else None
    res.w_reason = None if res.w is not None else NO_SHARE
    close = {d: v for d, v in prices.points if v}
    _windows(res, close, F.log_returns(prices.points), dff, LstIndex(smp))
    if not res.wins:
        return res
    cx, ret, gap = _context(res, close, dff)
    _, res.per = _strategies(cx, line(ret), keep=True)
    names = STATS + tuple(f"lad{i}" for i in range(len(res.levels)))
    res.real = real_means(res)
    if gap:
        res.null_reason = SPAN_GAP
        return res
    draws, block = NULL_DRAWS, NULL_BLOCK_DAYS
    orders = block_orders(math.ceil(len(ret) / block), draws, SEED)
    chunks = [orders[i:i + CHUNK] for i in range(0, len(orders), CHUNK)]
    jobs = [(null_chunk, cx, ret, block, ch) for ch in chunks]
    done = run_jobs(jobs) if len(jobs) > 1 else [null_chunk(cx, ret, block, ch) for ch in chunks]
    rows = [m for part in done for m in part]
    res.nulls = {k: [r[i] for r in rows] for i, k in enumerate(names)}
    res.draws = draws
    return res


def real_means(res: Result) -> dict[str, float | None]:
    """The real history's mean of every statistic the shuffles are scored on (statistics.fmean over windows)."""
    per, w = res.per, res.w
    out = {"a": _mean([100 * (p["v_a"] - p["hold"]) for p in per]),
           "a_cash": _mean([100 * (p["v_a"] - p["cash_h"]) for p in per]),
           "b": _mean([100 * (p["rb"] - p["bh"]) / w for p in per]) if w else None,
           "b_log": _mean([100 * math.log(p["rb"] / p["bh"]) for p in per]) if w else None}
    for i in range(len(res.levels)):
        out[f"lad{i}"] = _mean([100 * p["lv"][i] for p in per])
    return out


_RESULTS: "weakref.WeakKeyDictionary[Registry, Result]" = weakref.WeakKeyDictionary()


def result(reg: Registry) -> Result:
    if reg not in _RESULTS:
        _RESULTS[reg] = run(reg)
    return _RESULTS[reg]


# =========================================================================== registry

def _share(flags: list[bool]) -> float | None:
    return 100 * sum(1 for f in flags if f) / len(flags) if flags else None


def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def _mean(xs: list[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def compute(reg: Registry) -> None:
    res = result(reg)
    files = res.files

    def put(key, value, unit, fid, inputs=(), reason=None, src=None, decimals=None):
        reg.put(key, value, unit, fid, source_files=files if src is None else src, inputs=inputs,
                null_reason=None if value is not None else (reason or "not computed"), decimals=decimals)

    # the parameters, echoed so pages print them from the registry
    for key, value, unit, dec in (("vol_rally_pct", RALLY_PCT, "%", 0), ("vol_rebal_days", REBAL_DAYS, "days", None),
                                  ("vol_horizon_days", HORIZON_DAYS, "days", None),
                                  ("vol_null_block_days", NULL_BLOCK_DAYS, "days", None),
                                  ("vol_null_draws", NULL_DRAWS, "count", None), ("vol_rank_hi", RANK_HI, "%", 1),
                                  ("vol_rank_lo", RANK_LO, "%", 1)):
        put(key, value, unit, f"P-{key}", [f"thesis/rules.toml [params] {key}"], src=["thesis/rules.toml"],
            decimals=dec)
    for i, k in enumerate(RALLY_LADDER, 1):
        put(f"vol_ladder_{i}_pct", k, "%", "P-vol_rally_ladder", ["thesis/rules.toml [params] vol_rally_ladder"],
            src=["thesis/rules.toml"], decimals=0)

    wins, per = res.wins, res.per
    reason = res.reason
    span = ([f"{len(wins)} start days {iso(wins[0].s)}..{iso(wins[-1].s)}, horizon {HORIZON_DAYS} days",
             "windows failing on closes / DFF / LST edges: "
             f"{res.fail.get('closes', 0)} / {res.fail.get('dff', 0)} / {res.fail.get('lst', 0)}"] if wins else [])
    # general (F-vol-windows)
    put("vol_windows", len(wins) if wins else None, "count", "F-vol-windows", span, reason)
    put("vol_first_start", iso(wins[0].s) if wins else None, "date", "F-vol-windows", span, reason)
    put("vol_last_start", iso(wins[-1].s) if wins else None, "date", "F-vol-windows", span, reason)
    put("vol_last_end", iso(wins[-1].e) if wins else None, "date", "F-vol-windows", span, reason)
    put("vol_years", res.years, "count", "F-vol-windows", span + ["floor((vol_last_end - vol_first_start) / 365)"],
        reason)
    facs = [p["p_h"] for p in per]
    put("vol_trend_mean", 100 * (_mean(facs) - 1) if facs else None, "%", "F-vol-windows",
        span + ["100 (mean p_H - 1)"], reason)
    put("vol_trend_median", 100 * (_median(facs) - 1) if facs else None, "%", "F-vol-windows",
        span + ["100 (median p_H - 1)"], reason)

    nulls = res.nulls or {}
    null_reason = reason or res.null_reason
    shuffle_inputs = ([f"{res.draws:,} shuffles of {NULL_BLOCK_DAYS}-day blocks, Prng(sim_seed = {SEED})"]
                      if res.nulls else [])

    def shuffled_keys(prefix: str, stat: str, real_reason: str | None, *, lohi: bool = True, rank: bool = True,
                      excess: bool = False, with_band: bool = False) -> None:
        s = summarize(res.real.get(stat), nulls.get(stat)) if res.nulls else None
        why = null_reason or real_reason or "not computed"
        put(f"{prefix}_null_mean", s["null_mean"] if s else None, "pp", "F-vol-null", shuffle_inputs, why)
        if lohi:
            put(f"{prefix}_null_lo", s["lo"] if s else None, "pp", "F-vol-null", shuffle_inputs, why)
            put(f"{prefix}_null_hi", s["hi"] if s else None, "pp", "F-vol-null", shuffle_inputs, why)
        if excess:
            put(f"{prefix}_excess", s["excess"] if s else None, "pp", "F-vol-null", shuffle_inputs, why)
        if rank:
            put(f"{prefix}_rank", s["rank"] if s else None, "%", "F-vol-null", shuffle_inputs, why)
        if with_band:
            put(f"{prefix}_band", band(s["rank"]) if s else None, "text", "F-vol-null",
                shuffle_inputs + [f"above if rank >= {RANK_HI:g}, below if rank <= {RANK_LO:g}, else inside"], why)

    # (a) selling into rallies
    main = res.levels.index(RALLY_PCT) if res.levels else 0
    adv_a = [100 * (p["v_a"] - p["hold"]) for p in per]
    cash_a = [100 * (p["v_a"] - p["cash_h"]) for p in per]
    sold = [p["taus"][main] is not None for p in per]
    a_in = span + [f"sell at the next close after the first close at or above +{RALLY_PCT:g}%"]
    put("vol_rally_mean", res.real.get("a"), "pp", "F-vol-rally", a_in, reason)
    put("vol_rally_median", _median(adv_a), "pp", "F-vol-rally", a_in, reason)
    put("vol_rally_sold", _share(sold), "%", "F-vol-rally", a_in, reason)
    put("vol_rally_beat", _share([x > 0 for x in adv_a]), "%", "F-vol-rally", a_in, reason)
    shuffled_keys("vol_rally", "a", None, excess=True, with_band=True)
    put("vol_rally_cash_mean", res.real.get("a_cash"), "pp", "F-vol-rally", a_in + ["against selling at the start"], reason)
    put("vol_rally_cash_median", _median(cash_a), "pp", "F-vol-rally", a_in + ["against selling at the start"],
        reason)
    shuffled_keys("vol_rally_cash", "a_cash", None)

    # (b) rebalancing
    w = res.w
    b_reason = reason or res.w_reason
    b_ok = w is not None and bool(per)
    adv_b = [100 * (p["rb"] - p["bh"]) / w for p in per] if b_ok else []
    log_b = [100 * math.log(p["rb"] / p["bh"]) for p in per] if b_ok else []
    b_in = span + ([f"w = sim_share = {100 * w:g}%, reset every {REBAL_DAYS} days"] if w is not None else [])
    # the share strategy (b) used, echoed under this model's name so c25's note never reads a desk key directly
    share = reg.get("sim_share") if "sim_share" in reg else None
    put("vol_rebal_share", share.value if share is not None and w is not None else None, "%", "F-vol-rebal",
        ["sim_share: the desk's NEAR share"], b_reason, src=list(share.source_files) if share is not None else [],
        decimals=share.decimals if share is not None else None)
    put("vol_rebal_mean", res.real.get("b"), "pp", "F-vol-rebal", b_in, b_reason)
    put("vol_rebal_median", _median(adv_b), "pp", "F-vol-rebal", b_in, b_reason)
    put("vol_rebal_beat", _share([x > 0 for x in adv_b]), "%", "F-vol-rebal", b_in, b_reason)
    shuffled_keys("vol_rebal", "b", res.w_reason, excess=True, with_band=True)
    put("vol_rebal_log", res.real.get("b_log"), "pp", "F-vol-rebal", b_in + ["100 ln(R_reb / R_bh), pp of the book"],
        b_reason)
    put("vol_rebal_log_median", _median(log_b), "pp", "F-vol-rebal", b_in, b_reason)
    theory = [100 * 0.5 * w * (1 - w) * v.sig_w ** 2 for v in wins if v.sig_w is not None] if b_ok else []
    put("vol_rebal_theory", _mean(theory), "pp", "F-vol-rebal",
        b_in + [f"{len(theory)} windows with a full year of their own returns"],
        b_reason or "no window has a full year of its own daily returns")
    shuffled_keys("vol_rebal_log", "b_log", res.w_reason, lohi=False, rank=False)

    # the value of volatility: a one-year call at today's inputs (a model price)
    _put_call_now(reg, res, put)

    # the ladder (data page only; no rule reads it)
    for i, k in enumerate(RALLY_LADDER, 1):
        li = res.levels.index(k) if res.levels else 0
        st = f"lad{li}"
        lad_in = span + [f"strategy (a) at +{k:g}%"]
        put(f"vol_ladder_{i}_mean", res.real.get(st), "pp", "F-vol-rally", lad_in, reason)
        put(f"vol_ladder_{i}_sold", _share([p["taus"][li] is not None for p in per]), "%", "F-vol-rally", lad_in,
            reason)
        s = summarize(res.real.get(st), nulls.get(st)) if res.nulls else None
        put(f"vol_ladder_{i}_null_mean", s["null_mean"] if s else None, "pp", "F-vol-null", shuffle_inputs,
            null_reason)
        put(f"vol_ladder_{i}_rank", s["rank"] if s else None, "%", "F-vol-null", shuffle_inputs, null_reason)


def _put_call_now(reg: Registry, res: Result, put) -> None:
    """An at-the-money call over the horizon at today's inputs: rv_365_near, the latest DFF and y_lst_90."""
    dff = load.fred("DFF").non_null()
    rv = reg.value("rv_365_near") if "rv_365_near" in reg else None
    y90 = reg.value("y_lst_90") if "y_lst_90" in reg else None
    reason = ("rv_365_near is not computed" if rv is None else "y_lst_90 is not computed" if y90 is None else
              "no DFF in data/raw/fred/DFF.csv" if not dff else None)
    value = None
    if reason is None:
        r_last = dff[-1][1]
        value = 100 * bs_call(1.0, 1.0, HORIZON_DAYS / 365, rv / 100, 365 * math.log(1 + r_last / 36000),
                              math.log(1 + y90 / 100))
    inputs = ([f"rv_365_near {rv}%, y_lst_90 {y90}%, DFF {dff[-1][0].isoformat()} {dff[-1][1]}%"]
              if value is not None else [])
    put("vol_call_now", value, "%", "F-vol-call", inputs, reason)


# =========================================================================== chart c25

# row labels; the sentence names each row's comparison
ROWS = (("Selling into rallies", "vol_rally"), ("Rebalancing", "vol_rebal"), ("Ladder · Hyperliquid", "vol_perp_hl"),
        ("Ladder · Binance", "vol_perp_bn"))


def charts(reg: Registry) -> list[Chart]:
    return [strategies_chart(reg)]


def _v(reg: Registry, key: str):
    return reg.value(key) if key in reg else None


def strategies_chart(reg: Registry) -> Chart:
    """c25: each strategy's average gain over holding on NEAR's real history, against the average and middle band of
    the shuffled histories. The perp ladder's rows read the keys perps.py registered."""
    from pipeline.models import perps        # perps imports this module
    res = result(reg)
    shuffled_pts = [[label, _v(reg, f"{p}_null_mean"), _v(reg, f"{p}_null_lo"), _v(reg, f"{p}_null_hi")]
                    for label, p in ROWS]
    real_pts = [[label, _v(reg, f"{p}_mean")] for label, p in ROWS]

    def d(key: str) -> str | None:
        return reg.display(key) if key in reg and reg.value(key) is not None else None

    table_rows = [
        ["Selling into rallies", "holding", d("vol_rally_mean"), d("vol_rally_median"), d("vol_rally_null_mean"),
         d("vol_rally_null_lo"), d("vol_rally_null_hi"), d("vol_rally_rank"), d("vol_rally_beat"),
         d("vol_rally_sold"), d("vol_windows")],
        ["Selling into rallies", "selling at the start", d("vol_rally_cash_mean"), d("vol_rally_cash_median"),
         d("vol_rally_cash_null_mean"), d("vol_rally_cash_null_lo"), d("vol_rally_cash_null_hi"),
         d("vol_rally_cash_rank"), None, d("vol_rally_sold"), d("vol_windows")],
        ["Rebalancing, per NEAR stake", "buy-and-hold", d("vol_rebal_mean"), d("vol_rebal_median"),
         d("vol_rebal_null_mean"), d("vol_rebal_null_lo"), d("vol_rebal_null_hi"), d("vol_rebal_rank"),
         d("vol_rebal_beat"), None, d("vol_windows")],
        ["Rebalancing growth, pp of the book", "buy-and-hold", d("vol_rebal_log"), d("vol_rebal_log_median"),
         d("vol_rebal_log_null_mean"), None, None, None, None, None, None],
        *[[f"Perp ladder, {name}", "holding", d(f"vol_perp_{v}_mean"), d(f"vol_perp_{v}_median"),
           d(f"vol_perp_{v}_null_mean"), d(f"vol_perp_{v}_null_lo"), d(f"vol_perp_{v}_null_hi"),
           d(f"vol_perp_{v}_rank"), d(f"vol_perp_{v}_beat"), None, d(f"vol_perp_{v}_windows")]
          for v, name, *_ in perps.VENUES],
    ]
    csv_rows = []
    for v, p in zip(res.wins, res.per):
        tau = p["taus"][res.levels.index(RALLY_PCT)] if res.levels else None
        rb, bh, w = p["rb"], p["bh"], res.w
        csv_rows.append([
            iso(v.s), iso(v.e), rnd(p["p_h"], 6), rnd(F.pct(v.y_w)), rnd(F.pct(v.sig_w)),
            (min(tau + 1, HORIZON_DAYS) if tau is not None else None),
            rnd(100 * (p["v_a"] - p["hold"])), rnd(100 * (p["v_a"] - p["cash_h"])),
            rnd(100 * (rb - bh) / w) if rb is not None and w else None,
            rnd(100 * math.log(rb / bh)) if rb is not None else None])
    lst_source = supply.RPC_SOURCE + ": meta-pool.near get_st_near_price, linear-protocol.near ft_price"
    perp_sources, perp_queries = perps.sources_and_queries(("hl", "bn"))
    chart = Chart(
        id="c25", section="A5", priority="lead", claims=["H10"], verdicts=["H10"], title="", sentence="",
        form="dot-range", eyebrow="A5 · tests H10",
        sources=[s for s in (res.prices.source, supply.fred_source("DFF"), lst_source) if s] + perp_sources,
        queries=[market.near_query(res.prices), fred_query("DFF")] + supply.lst_queries(res.smp) + perp_queries,
        table={"columns": ["strategy", "compared_with", "real_mean_pp", "real_median_pp", "shuffled_mean_pp",
                           "shuffled_low_pp", "shuffled_high_pp", "rank_pct", "beat_pct", "sold_pct", "windows"],
               "rows": table_rows},
        code_ref="pipeline/models/volatility.py::strategies_chart",
        formula_ids=["F-vol-windows", "F-vol-null", "F-vol-rally", "F-vol-rebal", "F-perp-windows", "F-perp-ladder",
                     "F-lst", "F-ffcomp"],
        x={"type": "linear", "unit": "pp", "legend": "series", "dp": 2}, y={"type": "category", "unit": "pp"},
        series=[Series("shuffled", "Shuffled histories: average and middle band", "context", shuffled_pts,
                       role="context", unit="pp"),
                Series("real", "NEAR's real history", "near", real_pts, role="emphasis", unit="pp")],
        reference_lines=[{"x": 0, "label": "no gain"}],
        coverage={"first": _v(reg, "vol_first_start"), "last": _v(reg, "vol_last_end"), "n": _v(reg, "vol_windows") or 0,
                  "gaps": []},
        # the latest fetch among every file the rows read, the perp ladder's included
        as_of=files_as_of(res.files + [f for vid in ("hl", "bn") for k in (f"vol_perp_{vid}_rank",) if k in reg
                                       for f in reg.get(k).source_files]),
    )
    chart.csv_table = {"columns": ["start", "end", "price_factor", "staking_window_pct", "sigma_window_pct",
                                   "rally_sold_day", "rally_adv_pp", "rally_vs_cash_pp", "rebal_adv_pp",
                                   "rebal_log_pp"], "rows": csv_rows}
    bands = (_v(reg, "vol_rally_band"), _v(reg, "vol_rebal_band"), _v(reg, "vol_perp_band"))
    stem = ("Selling into rallies beat {vol_rally_rank} of NEAR's shuffled histories, rebalancing {vol_rebal_rank}, "
            "and the perp ladder {vol_perp_hl_rank} on Hyperliquid and {vol_perp_bn_rank} on Binance")
    ending = (", inside what chance gives, so no capture shows." if bands == ("inside",) * 3 else
              ": NEAR's real order of moves paid a patient holder more than chance."
              if "above" in bands and "below" not in bands else
              ": NEAR's real order of moves cost a patient holder against chance."
              if "below" in bands and "above" not in bands else
              ": one strategy beat chance and another lost to it." if "above" in bands and "below" in bands else None)
    chart.title_template = supply.pick(reg, [
        *([stem + ending] if ending else []),
        "Three ways to sell NEAR's volatility, against holding and against NEAR's own moves shuffled.",
    ])
    sentence = ("Each row is a strategy's average gain over a year across its overlapping one-year windows, in "
                "percentage points (pp) of the starting NEAR stake, against holding NEAR: the blue dot is NEAR's real history, the "
                "gray ring and bar the average and middle band of {vol_null_draws} shuffled histories of NEAR's own "
                "daily moves.")
    chart.sentence_template = supply.pick(reg, [
        sentence,
        "Each row is a strategy's average gain over a year, in points of the starting NEAR stake, on NEAR's real "
        "history and on histories made of NEAR's own daily moves shuffled in blocks.",
    ])
    chart.caveat_template = supply.pick(reg, [
        "Even with no pattern in prices, selling on a rise only moves gains between years, so a strategy shows "
        "capture only by beating the shuffles, and the ladder counts only when both venues agree, because "
        "Hyperliquid's record holds {vol_perp_hl_years} separate years.",
        "Even with no pattern in prices, selling on a rise only moves gains between years, so a strategy shows "
        "capture only by beating the shuffles, and the ladder counts only when both venues agree.",
    ])
    chart.notes_templates = notes_for(reg, [
        "The shuffles cut NEAR's daily moves into blocks of {vol_null_block_days} and keep every move, the whole "
        "trend and each window's staking and fed funds; only the order of the blocks changes, and the band leaves "
        "out the highest and lowest {vol_rank_lo} of them.",
        "Selling into rallies sells at the next close after NEAR first closes {vol_rally_pct} above cost, else at the "
        "year's end; it sold in {vol_rally_sold} of the windows.",
        "Against selling at the start and holding dollars at fed funds, selling into rallies averaged "
        "{vol_rally_cash_mean}, against {vol_rally_cash_null_mean} in the shuffles.",
        "Rebalancing holds the desk's NEAR share, {vol_rebal_share}, and resets it every {vol_rebal_days}; its row is "
        "per unit of NEAR held at the start.",
        "Rebalancing's growth over buy-and-hold averaged {vol_rebal_log} of the book, against "
        "{vol_rebal_log_null_mean} in the shuffles; theory puts a ceiling of {vol_rebal_theory} on it, which "
        "buy-and-hold partly earns too.",
        "The perp ladder keeps the NEAR staked and sells {vol_perp_slice_pct} of the stake short at the close after "
        "each {vol_perp_rung_pct} rise, at most {vol_perp_cap_pct}, buying each slice back after a "
        "{vol_perp_rung_pct} fall; its rows include funding, fees, slippage and the interest forgone on collateral.",
        "On Hyperliquid the ladder covers {vol_perp_hl_windows} overlapping windows from {vol_perp_hl_first_start} and "
        "beat {vol_perp_hl_rank} of the shuffles; over those windows NEAR's price changed {vol_perp_hl_trend_mean} on "
        "average, and any strategy that gives up a rise ranks high on a span whose rallies sit at its edges.",
        "On Binance it covers {vol_perp_bn_windows} overlapping windows from {vol_perp_bn_first_start}, nearly the "
        "windows of the other two strategies.",
        "Staking accrues while NEAR is held, and dollars from a sale earn compounded fed funds to the year's end.",
        "The windows overlap: {vol_windows} start days from {vol_first_start} to {vol_last_start} span {vol_years} "
        "separate years, and every shuffle has the same overlap.",
        "NEAR's price changed {vol_trend_mean} over the average window and {vol_trend_median} over the typical one; "
        "the shuffles keep that trend.",
    ])
    if not res.wins:
        chart.status = "unavailable"
        chart.empty_state = empty_state(f"Not computed: {res.reason}.",
                                        "NEAR daily closes, FRED DFF and liquid-staking samples over a year.")
    elif not ready(reg, "vol_rally_rank", "vol_rebal_rank", "vol_perp_hl_rank", "vol_perp_bn_rank"):
        chart.status = "partial"
        chart.status_banner = ("The shuffled histories, the desk share or the perp ladder's ranks are missing, so the "
                               "strategies are shown without the benchmark the claim reads.")
    return chart.resolve(reg)
