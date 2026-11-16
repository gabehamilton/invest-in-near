"""Selling NEAR's swings on a perpetual: the perp ladder against a short that never trades (chart c26; claim H8; the
third strategy of claim H10).

Formulas:
  F-perp-windows  A start day s ends at e = s + H. Eligible when the venue has a perp daily bar on every day s..e,
                  usable funding on every day s+1..e, DFF on every day in [s, e) and liquid-staking samples at s+1 and
                  e+1 (F-vol-windows' rule). Funding per day: each settlement stamp is floored to its hour; a stamp at
                  00:00 belongs to the previous UTC day, any other to its own; fu_d is the sum of the day's rates. A
                  day is usable with all of its settlements (24 or 3), or on the hourly venue with one missing
                  (counted as zero). The 00:00 settlement that closes day d is credited on the short held through d,
                  so a slice sold at d's close misses it and a slice bought back at that close receives it. Day
                  records t = 1..N over the span S0..S1: (c_t / c_t-1, h_t / c_t-1, fu_t) with h = max(open, high).
                  Consecutive windows overlap.
  F-perp-ladder   One window, prices p_k = c_s+k / c_s, the stake one NEAR worth one dollar at the start close.
                  State: base B = 1, slices short j = 0, venue cash Q = COLLATERAL K s (the opening deposit, on the
                  venue at the start close). Day k = 1..H: (1) if the short z = j s > 0 and Q - z h < MAINT z h at the
                  day's high, the venue closes it, Q - z h (if positive) is forfeited and the overlay stops; (2)
                  funding Q += fu_t z (p_k-1 + p_k) / 2; (3) at the close, while j < K and p_k >= B g^(j+1) sell one
                  slice at p_k (1 - SLIP) less FEE, else while j > 0 and p_k <= B g^(j-1) buy one back at
                  p_k (1 + SLIP) plus FEE; with j = 0, while p_k <= B / g: B = B / g; (4) transfers due today arrive
                  (sent home if the overlay stopped); (5) for k < H with E = Q - z p_k and E* = COLLATERAL K s p_k:
                  if E + in flight < TOPUP_AT E*, send E* - E - in flight (arrives after TRANSFER_DAYS); else if
                  nothing is in flight and E > SWEEP_AT E*, bring E - E* home. At the end close the short is bought
                  back. adv = venue P&L (Q + in flight + withdrawn - deposited) less the fed funds forgone on every
                  dollar sent (exp(LC_e - LC_send) per dollar, ACT/360 as F-ffcomp).
  F-perp-static   The yardstick: the same simulation with step (3) replaced by one short of STATIC of the stake sold
                  at the start close and bought back at the end close; swing = adv - adv_static per window.
  F-perp-kept     funding = sum of step (2); costs = fees and slippage; kept = funding - costs - interest - liq_loss;
                  price = adv - kept.
  F-perp-redraw   The day records cut into blocks of NULL_BLOCK_DAYS, drawn with replacement
                  (b = (next * nblocks) >> 32 on one Prng(sim_seed) stream) until N days are filled, NULL_DRAWS times;
                  each history reruns every window of the ladder and of the yardstick (mean swing m_d). Centred values
                  m_d - mean(m) + swing_real; band = type-7 quantiles at RANK_LO and RANK_HI; share = 100 x centred
                  values above zero / NULL_DRAWS.
  F-perp-funding  The latest year all three funding files cover in full days; per venue the share of settlements
                  with a negative rate (the short paid), Hyperliquid's share at its formula's fixed interest term, and
                  the mean settlement rate times the settlements in a year.
  F-perp-sens     Real history only, ladder and yardstick, one row per case (the latest year of starts, dollars
                  costing more, thinner collateral, transfer time, wider fills, a fill one close late).
  F-perp-coinbase Coinbase Derivatives' US-regulated NEAR Perp, read from its product snapshot and daily bars:
                  the listing day (new_at), the daily bars from that day, NEAR per contract, open interest in
                  dollars (contracts x contract size x index price), the funding interval in hours, the intraday and
                  overnight short margin rates, and the rise after which a short backed by dollars equal to its value
                  at the sale, with no top-up, meets the overnight margin m: 1 - r = m (1 + r), r = (1 - m) / (1 + m).
                  Context only: the record is shorter than one window, so no claim reads these keys.
                  vol_perp_cb_access_read is the day Coinbase's US eligibility pages were read (the declared constant
                  cb_us_derivatives_access, K-cb_us_derivatives_access).
The shuffles of claim H10 reuse F-vol-null on the same day records (each record keeps its move, high and funding).
"""

from __future__ import annotations

import json
import math
import multiprocessing
import statistics
import sys
import weakref
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from pipeline.chartspec import Chart, Query, Series
from pipeline.models import load, supply, volatility
from pipeline.models.macro import empty_state, files_as_of, fred_query, iso, notes_for, put_constant, ready, rnd
from pipeline.models.params import param
from pipeline.models.portfolio import Prng, _workers, q7
from pipeline.numbers import Registry, fmt

HORIZON_DAYS = volatility.HORIZON_DAYS
RUNG = param("vol_perp_rung_pct") / 100
SLICE = param("vol_perp_slice_pct") / 100
RUNGS = int(param("vol_perp_rungs"))
STATIC = param("vol_perp_static_pct") / 100
FEE = param("vol_perp_fee_pct") / 100
SLIP = param("vol_perp_slip_pct") / 100
MAINT = param("vol_perp_maint_pct") / 100
COLLATERAL = param("vol_perp_collateral")
TOPUP_AT = param("vol_perp_topup_at")
SWEEP_AT = param("vol_perp_sweep_at")
TRANSFER_DAYS = int(param("vol_perp_transfer_days"))
MIN_YEARS, BLOCK, DRAWS = volatility.MIN_YEARS, volatility.NULL_BLOCK_DAYS, volatility.NULL_DRAWS
RANK_HI, RANK_LO, SEED = volatility.RANK_HI, volatility.RANK_LO, volatility.SEED
SENS_RATE_ADD = tuple(param("vol_perp_sens_rate_add"))
SENS_COLLATERAL = tuple(param("vol_perp_sens_collateral"))
SENS_TRANSFER_DAYS = tuple(int(d) for d in param("vol_perp_sens_transfer_days"))
SENS_SLIP_PCT = tuple(param("vol_perp_sens_slip_pct"))
SENS_LAG_DAYS = int(param("vol_perp_sens_lag_days"))
VENUES = (("hl", "Hyperliquid", "near_perp_hyperliquid_daily", "near_funding_hyperliquid_hourly", 24),
          ("bn", "Binance", "near_perp_binance_daily", "near_funding_binance_8h", 3))
SIGN_ONLY = (("kr", "Kraken", "near_funding_kraken_hourly", 24),)   # read for the sign of funding only (F-perp-funding)
TERMS = "near_perp_terms"
SOURCE = "perps"
# Hyperliquid's documented funding formula adds a fixed interest term of 0.01% per eight hours, paid hourly; the terms
# snapshot carries only the live rate, so the term is named here
HL_INTEREST_HOURLY = 0.0001 / 8
SAME_RATE = 1e-12            # a settlement "at" the fixed term: |rate - term| below this
PER_YEAR = {24: 8760, 3: 1095}
CHUNK = 25                   # resampled histories per parallel job: small jobs keep the workers evenly busy
EPS = 1e-9                   # relative slack of the kernels' quiet-day bounds (far above float rounding)
SPAN_GAP = "a daily bar is missing inside the window span"
FUND_GAP = "a day inside the window span has no usable funding"
SOURCE_NAMES = {"hl": "Hyperliquid NEAR perpetual (hourly funding, daily candles), https://api.hyperliquid.xyz/info",
                "bn": "Binance USD-M NEARUSDT perpetual (8-hour funding, daily candles), https://data.binance.vision",
                "kr": "Kraken Derivatives PF_NEARUSD perpetual (hourly funding), https://futures.kraken.com",
                "cb": "Coinbase Derivatives NEAR Perp NER-20DEC30-CDE (daily candles, contract snapshot), "
                      "https://api.coinbase.com/api/v3/brokerage/market"}
# Coinbase Derivatives' US-regulated NEAR Perp (F-perp-coinbase): read for its terms and the length of its record only
CB_BARS = "near_perp_coinbase_daily"
CB_TERMS = "near_perp_coinbase_terms"
CB_PRODUCT = "NER-20DEC30-CDE"
CB_ACCESS = "cb_us_derivatives_access"      # thesis/constants.toml: Coinbase's US eligibility, quoted with its read date


# =========================================================================== the knobs of one simulation

@dataclass(frozen=True)
class Knobs:
    """The constants of one simulation; the defaults are the params, a sensitivity case changes one."""
    H: int
    g: float
    s: float
    K: int
    static: float
    fee: float
    slip: float
    mm: float
    lam: float
    topup_at: float
    sweep_at: float
    td: int
    lag: int = 0
    rate_add: float = 0.0


def knobs() -> Knobs:
    return Knobs(HORIZON_DAYS, RUNG, SLICE, RUNGS, STATIC, FEE, SLIP, MAINT, COLLATERAL, TOPUP_AT, SWEEP_AT,
                 TRANSFER_DAYS)


# =========================================================================== data

@dataclass
class Venue:
    vid: str
    name: str
    per_day: int
    files: list[str] = field(default_factory=list)
    reason: str | None = None            # no eligible window, and why
    fail: dict = field(default_factory=dict)
    starts: list[date] = field(default_factory=list)
    offs: list[int] = field(default_factory=list)
    s0: date | None = None
    N: int = 0
    cr: list[float] = field(default_factory=list)     # t = 0..N; index 0 is a placeholder
    hr: list[float] = field(default_factory=list)
    fu: list[float] = field(default_factory=list)
    dff: list[float | None] = field(default_factory=list)   # DFF of span day j (j = 0..N-1)
    LC: list[float] = field(default_factory=list)
    partial: list[date] = field(default_factory=list)       # span days with one settlement missing
    gap: str | None = None               # why the shuffles and redraws are not computed

    @property
    def years(self) -> int | None:
        return (self.last_end - self.starts[0]).days // 365 if self.starts else None

    @property
    def last_end(self) -> date | None:
        return self.starts[-1] + timedelta(days=HORIZON_DAYS) if self.starts else None


def _day_of(stamp: str) -> date:
    """The UTC day a settlement belongs to: floored to its hour, a 00:00 stamp closes the previous day."""
    d = date.fromisoformat(stamp[:10])
    return d - timedelta(days=1) if int(stamp[11:13]) == 0 else d


def odd_interval(r: dict, per_day: int) -> bool:
    """A settlement the file marks with another interval than the venue's (Binance writes interval_hours)."""
    h = r.get("interval_hours")
    return per_day > 0 and h not in (None, "") and float(h) * per_day != 24


def funding_days(rows: list[dict], per_day: int = 0) -> tuple[dict[date, float], dict[date, int]]:
    """Funding summed per UTC day (file order) and the count of settlements per day. A day holding a settlement at
    another interval than the venue's is left out, so it is never usable."""
    tot: dict[date, float] = {}
    cnt: dict[date, int] = {}
    odd: set[date] = set()
    for r in rows:
        if not r.get("time") or r.get("funding_rate") in (None, ""):
            continue
        d = _day_of(r["time"])
        if odd_interval(r, per_day):
            odd.add(d)
        tot[d] = tot.get(d, 0.0) + float(r["funding_rate"])
        cnt[d] = cnt.get(d, 0) + 1
    for d in odd:
        del tot[d], cnt[d]
    return tot, cnt


def usable(n: int, per_day: int) -> bool:
    """Exactly a day's settlements, or on an hourly venue all but one; more than a day holds is never usable."""
    return n == per_day or (per_day == 24 and n == per_day - 1)


def read_bars(name: str) -> tuple[dict[date, tuple[float, float, float, float]], list[str]]:
    rows, files = load.csv_rows(SOURCE, name)
    out = {}
    for r in rows:
        try:
            out[date.fromisoformat(r["date"])] = (float(r["open"]), float(r["high"]), float(r["low"]),
                                                  float(r["close"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out, files


def build_venue(vid: str, name: str, bars_name: str, fund_name: str, per_day: int, dff: dict[date, float],
                lst: volatility.LstIndex, base_files: list[str]) -> Venue:
    """Every eligible window of one venue and the day records over their span (F-perp-windows)."""
    H = HORIZON_DAYS
    v = Venue(vid, name, per_day)
    bars, bfiles = read_bars(bars_name)
    frows, ffiles = load.csv_rows(SOURCE, fund_name)
    v.files = sorted(set(bfiles + ffiles + base_files))
    tot, cnt = funding_days(frows, per_day)
    if not bars:
        v.reason = f"no {name} NEAR perpetual daily bars in data/raw/perps"
        return v
    d0, d1 = min(bars), max(bars)
    days = (d1 - d0).days + 1
    miss_b, bad_f, miss_d = [0], [0], [0]
    for i in range(days):
        d = d0 + timedelta(days=i)
        miss_b.append(miss_b[-1] + (d not in bars))
        bad_f.append(bad_f[-1] + (not usable(cnt.get(d, 0), per_day)))
        miss_d.append(miss_d[-1] + (dff.get(d) is None))
    fail = {"bars": 0, "funding": 0, "dff": 0, "lst": 0}
    n = days - H
    for i in range(max(n, 0)):
        s, e = d0 + timedelta(days=i), d0 + timedelta(days=i + H)
        if miss_b[i + H + 1] - miss_b[i]:
            fail["bars"] += 1
            continue
        if bad_f[i + H + 1] - bad_f[i + 1]:
            fail["funding"] += 1
            continue
        if miss_d[i + H] - miss_d[i]:
            fail["dff"] += 1
            continue
        r0, r1 = lst.edge(s + timedelta(days=1)), lst.edge(e + timedelta(days=1))
        if (supply.lst_yield(r0, r1) if r0 and r1 and r0["time"] < r1["time"] else None) is None:
            fail["lst"] += 1
            continue
        v.starts.append(s)
    v.fail = fail
    if not v.starts:
        v.reason = (f"no {name} NEAR perpetual daily bars span a year" if n <= 0 else
                    f"no one-year span of {name} NEAR perpetual daily bars without a missing day" if fail["bars"] == n
                    else f"no one-year span with usable {name} funding on every day"
                    if fail["bars"] + fail["funding"] == n else
                    "no one-year span with DFF on every day (data/raw/fred/DFF.csv)"
                    if fail["bars"] + fail["funding"] + fail["dff"] == n else
                    "no one-year window has liquid-staking samples near both ends (data/raw/rpc)")
        return v
    s0 = v.s0 = v.starts[0]
    v.offs = [(d - s0).days for d in v.starts]
    N = v.N = (v.starts[-1] - s0).days + H
    hr, cr, fu = [0.0], [1.0], [0.0]
    for t in range(1, N + 1):
        day = s0 + timedelta(days=t)
        a, b = bars.get(day - timedelta(days=1)), bars.get(day)
        if a and b:
            o, h, _, c = b
            hr.append(max(h, o) / a[3])
            cr.append(c / a[3])
        else:               # never inside an eligible window; the resampled histories need every day
            hr.append(1.0)
            cr.append(1.0)
            v.gap = v.gap or SPAN_GAP
        fu.append(tot.get(day, 0.0))
        k = cnt.get(day, 0)
        if not usable(k, per_day):
            v.gap = v.gap or FUND_GAP
        elif k < per_day:
            v.partial.append(day)
    v.cr, v.hr, v.fu = cr, hr, fu
    v.dff = [dff.get(s0 + timedelta(days=j)) for j in range(N)]
    v.LC = fed_line(v.dff)
    return v


def fed_line(rates: list[float | None], add: float = 0.0) -> list[float]:
    """LC[i] = sum over the span's first i days of ln(1 + (DFF + add) / 36000); a day without DFF adds nothing."""
    LC = [0.0]
    for r in rates:
        LC.append(LC[-1] + math.log(1 + (r + add) / 36000) if r is not None else LC[-1])
    return LC


def path(cr: list[float], hr: list[float], fu: list[float], perm: list[int] | None = None):
    """The price line of one history: cl[t] = prod of the close moves to t, hi[t] = cl[t-1] x the high move."""
    if perm is not None:
        hr, cr, fu = [hr[t] for t in perm], [cr[t] for t in perm], [fu[t] for t in perm]
    cl, hi = [1.0], [1.0]
    c = 1.0
    for t in range(1, len(cr)):
        hi.append(c * hr[t])
        c *= cr[t]
        cl.append(c)
    return cl, hi, fu


# =========================================================================== one window, every account

def sim_window(o: int, cl: list[float], hi: list[float], fu: list[float], LC: list[float], p: Knobs,
               static: float | None = None) -> tuple:
    """One window (F-perp-ladder; with `static` set, F-perp-static: step 3 is one short of `static` sold at the
    start close and bought back at the end close). Returns (adv, kept, funding, costs, interest, liq_loss,
    liquidated, trades, mean short, peak net dollars sent, price, p_H, min_margin), dollars per starting dollar of
    NEAR. With p.lag > 0 the decision at close k fills at close k + lag."""
    H, g1, K, s, slip, fee, mm = p.H, 1 + p.g, p.K, p.s, p.slip, p.fee, p.mm
    exp = math.exp
    inv = 1.0 / cl[o]
    lcH = LC[o + H]
    capn = p.lam * K * s
    G = [g1 ** k for k in range(-1, K + 2)]      # G[k + 1] = g1^k
    B, j, Q = 1.0, 0, capn
    want = 0
    wants = deque([0] * p.lag)
    dep_face, dep_cost = Q, Q * exp(lcH - LC[o])
    wd_face = wd_val = 0.0
    pend: list[tuple[int, float]] = []
    pend_tot = 0.0
    fund = cost = liq_loss = 0.0
    live = True
    nfill = 0
    net = peak_net = Q
    short_days = 0.0
    min_margin = math.inf
    prev = 1.0
    fixed = static is not None
    hs_fixed = static or 0.0
    if fixed and hs_fixed > 0:                              # the static short, sold at the start close
        Q += hs_fixed * (1 - slip) * (1 - fee)
        cost += hs_fixed * (slip + (1 - slip) * fee)
        nfill += 1
    for k in range(1, H + 1):
        t = o + k
        c, h = cl[t] * inv, hi[t] * inv
        if live:
            hs = hs_fixed if fixed else j * s
            if hs > 0:
                ratio = (Q - hs * h) / (mm * hs * h)
                if ratio < min_margin:
                    min_margin = ratio
            if hs > 0 and Q - hs * h < mm * hs * h:          # 1 liquidation at the day's high
                liq_loss = max(Q - hs * h, 0.0)
                Q, j, live = 0.0, 0, False
                hs_fixed = 0.0
            else:
                if hs > 0:                                    # 2 funding on the short held through the day
                    x = fu[t] * hs * 0.5 * (prev + c)
                    Q += x
                    fund += x
                short_days += hs
                if not fixed and not p.lag:                   # 3 trade at the close
                    if j < K and c >= B * G[j + 2]:
                        while j < K and c >= B * G[j + 2]:
                            Q += s * c * (1 - slip) * (1 - fee)
                            cost += s * c * (slip + (1 - slip) * fee)
                            j += 1
                            nfill += 1
                    elif j > 0 and c <= B * G[j]:
                        while j > 0 and c <= B * G[j]:
                            Q -= s * c * (1 + slip) * (1 + fee)
                            cost += s * c * (slip + (1 + slip) * fee)
                            j -= 1
                            nfill += 1
                    if j == 0:
                        while c <= B / g1:
                            B /= g1
                elif not fixed:                               # 3' fill the decision of p.lag closes ago, then decide
                    target = wants.popleft()
                    while j < target:
                        Q += s * c * (1 - slip) * (1 - fee)
                        cost += s * c * (slip + (1 - slip) * fee)
                        j += 1
                        nfill += 1
                    while j > target:
                        Q -= s * c * (1 + slip) * (1 + fee)
                        cost += s * c * (slip + (1 + slip) * fee)
                        j -= 1
                        nfill += 1
                    if want < K and c >= B * G[want + 2]:
                        while want < K and c >= B * G[want + 2]:
                            want += 1
                    elif want > 0 and c <= B * G[want]:
                        while want > 0 and c <= B * G[want]:
                            want -= 1
                    if want == 0:
                        while c <= B / g1:
                            B /= g1
                    wants.append(want)
        if pend:                                              # 4 arrivals
            keep = []
            for arr, amt in pend:
                if arr <= k:
                    pend_tot -= amt
                    if live:
                        Q += amt
                    else:
                        back = min(k + p.td, H)
                        wd_face += amt
                        wd_val += amt * exp(lcH - LC[o + back])
                        net -= amt
                else:
                    keep.append((arr, amt))
            pend = keep
        if live and k < H:                                    # 5 top-up or sweep
            hs = hs_fixed if fixed else j * s
            E = Q - hs * c
            Es = capn * c
            if E + pend_tot < p.topup_at * Es:
                amt = Es - E - pend_tot
                dep_face += amt
                dep_cost += amt * exp(lcH - LC[o + k])
                pend.append((k + p.td, amt))
                pend_tot += amt
                net += amt
                peak_net = max(peak_net, net)
            elif pend_tot == 0 and E > p.sweep_at * Es:
                amt = E - Es
                Q -= amt
                arr = min(k + p.td, H)
                wd_face += amt
                wd_val += amt * exp(lcH - LC[o + arr])
                net -= amt
        prev = c
    c = cl[o + H] * inv
    end_short = hs_fixed if fixed else j * s
    if live and end_short > 0:
        Q -= end_short * c * (1 + slip) * (1 + fee)
        cost += end_short * c * (slip + (1 + slip) * fee)
        nfill += 1 if fixed else j
    venue = Q + pend_tot + wd_face - dep_face
    interest = (dep_cost - dep_face) - (wd_val - wd_face)
    adv = venue - interest
    kept = fund - cost - interest - liq_loss
    return (adv, kept, fund, cost, interest, liq_loss, not live, nfill, short_days / H, peak_net, adv - kept, c,
            min_margin)


# =========================================================================== the lean kernels (resampled histories)
#
# The resampled histories need only each window's advantage. These kernels perform the same floating-point
# operations on Q, the transfers and the dollar accounts as sim_window, in the same order, so their advantage is
# bit-identical; they skip the bookkeeping no resampled statistic reads and fast-forward over quiet days: days on
# which, by bounds with a relative slack of EPS, no margin call, trade, arrival, top-up or sweep can happen. On a
# quiet day only the funding is added (as step 2 adds it).

def kernel_consts(p: Knobs) -> tuple:
    g1 = 1 + p.g
    G = [g1 ** k for k in range(-1, p.K + 2)]
    UP = [G[j + 2] if j < p.K else math.inf for j in range(p.K + 1)]
    HS = [j * p.s for j in range(p.K + 1)]
    MHS = [p.mm * hs for hs in HS]
    return (p.H, p.K, p.s, p.td, p.lam * p.K * p.s, p.topup_at, p.sweep_at, g1, G, UP, HS, MHS,
            1 - p.slip, 1 - p.fee, 1 + p.slip, 1 + p.fee, p.mm)


def funding_tables(fu: list[float], C: tuple) -> list[list[float]]:
    """FH[j][t] = fu[t] x (j slices) x 0.5, the left part of step 2's product, evaluated as sim_window does."""
    HS = C[10]
    return [[f * hs * 0.5 for f in fu] for hs in HS]


def ladder_adv(o: int, cl: list[float], hi: list[float], FH: list[list[float]], ex: list[float], C: tuple) -> float:
    """The ladder's advantage in one window (lag 0); FH from funding_tables, ex[m] = exp(LC[o + H] - LC[o + m])."""
    H, K, s, td, capn, tu, sw, g1, G, UP, HS, MHS, sl1m, fe1m, sl1p, fe1p, mm = C
    base = cl[o]
    inv = 1.0 / base
    B = 1.0
    j = 0
    Q = capn
    dep_face = Q
    dep_cost = Q * ex[0]
    wd_face = wd_val = 0.0
    pend: list[tuple[int, float]] = []
    pt = 0.0
    prev = 1.0
    up = B * UP[0]
    dn = -1.0
    rd = B / g1
    k = 0
    t = o
    H1 = H - 1
    while k < H:
        k += 1
        t += 1
        c = cl[t] * inv
        if j:
            hs = HS[j]
            h = hi[t] * inv
            if Q - hs * h < MHS[j] * h:                   # 1 liquidation: what is in flight is sent home on arrival
                Q = 0.0
                for arr, amt in pend:
                    if arr <= H:
                        pt -= amt
                        back = (arr if arr > k else k) + td
                        wd_face += amt
                        wd_val += amt * ex[back if back < H else H]
                venue = Q + pt + wd_face - dep_face
                return venue - ((dep_cost - dep_face) - (wd_val - wd_face))
            Q += FH[j][t] * (prev + c)                  # 2 funding
        if c >= up:                                       # 3 trade at the close
            while j < K and c >= B * G[j + 2]:
                Q += s * c * sl1m * fe1m
                j += 1
            up = B * UP[j]
            dn = B * G[j]
        elif c <= dn:
            while j > 0 and c <= B * G[j]:
                Q -= s * c * sl1p * fe1p
                j -= 1
            up = B * UP[j]
            dn = B * G[j] if j else -1.0
        if j == 0 and c <= rd:
            while c <= B / g1:
                B /= g1
            rd = B / g1
            up = B * UP[0]
        if pend and pend[0][0] <= k:                      # 4 arrivals
            while pend and pend[0][0] <= k:
                amt = pend.pop(0)[1]
                pt -= amt
                Q += amt
        if k < H:                                         # 5 top-up or sweep
            hs = HS[j]
            E = Q - hs * c
            Es = capn * c
            if E + pt < tu * Es:
                amt = Es - E - pt
                dep_face += amt
                dep_cost += amt * ex[k]
                pend.append((k + td, amt))
                pt += amt
            elif pt == 0 and E > sw * Es:
                amt = E - Es
                Q -= amt
                arr = k + td
                wd_face += amt
                wd_val += amt * ex[arr if arr < H else H]
        prev = c
        # fast-forward over quiet days
        if k < H1:
            stop = H1 if not pend or pend[0][0] > H else min(H1, pend[0][0] - 1)
            if k >= stop:
                continue
            if j == 0:                                    # flat: no margin, no funding; Q stays
                hi_rel = (Q + pt) / (tu * capn)
                if up < hi_rel:
                    hi_rel = up
                lo_rel = Q / (sw * capn) if pt == 0 else 0.0
                if lo_rel < rd:
                    lo_rel = rd
                lo_abs = lo_rel * (1 + EPS) * base
                hi_abs = hi_rel * (1 - EPS) * base
                t0 = t
                while k < stop and lo_abs < cl[t + 1] < hi_abs:
                    k += 1
                    t += 1
                if t != t0:
                    prev = cl[t] * inv
            elif Q > 0:
                hs = HS[j]
                fh = FH[j]
                D = Q * 0.005
                Qa = Q - D
                Qb = Q + D
                hi_rel = (Qa + pt) / (hs + tu * capn)
                if up < hi_rel:
                    hi_rel = up
                lo_rel = Qb / (hs + sw * capn) if pt == 0 else 0.0
                if lo_rel < dn:
                    lo_rel = dn
                lo_abs = lo_rel * (1 + EPS) * base
                hi_abs = hi_rel * (1 - EPS) * base
                hl_abs = Qa / (hs + MHS[j]) * (1 - EPS) * base
                while k < stop:
                    t1 = t + 1
                    ct = cl[t1]
                    if not (lo_abs < ct < hi_abs and hi[t1] < hl_abs):
                        break
                    c = ct * inv
                    Q += fh[t1] * (prev + c)
                    prev = c
                    k += 1
                    t = t1
                    if not Qa <= Q <= Qb:                 # step 5 exactly (no trade or arrival is possible today)
                        E = Q - hs * c
                        Es = capn * c
                        if E + pt < tu * Es:
                            amt = Es - E - pt
                            dep_face += amt
                            dep_cost += amt * ex[k]
                            pend.append((k + td, amt))
                            pt += amt
                        elif pt == 0 and E > sw * Es:
                            amt = E - Es
                            Q -= amt
                            arr = k + td
                            wd_face += amt
                            wd_val += amt * ex[arr if arr < H else H]
                        break
    c = cl[o + H] * inv
    if j:
        Q -= HS[j] * c * sl1p * fe1p
    venue = Q + pt + wd_face - dep_face
    return venue - ((dep_cost - dep_face) - (wd_val - wd_face))


def static_adv(o: int, cl: list[float], hi: list[float], fh: list[float], ex: list[float], C: tuple,
               hs: float) -> float:
    """The yardstick's advantage in one window (hs > 0); fh[t] = fu[t] x hs x 0.5."""
    H, K, s, td, capn, tu, sw, g1, G, UP, HS, MHS, sl1m, fe1m, sl1p, fe1p, mm = C
    base = cl[o]
    inv = 1.0 / base
    mhs = mm * hs
    Q = capn
    dep_face = Q
    dep_cost = Q * ex[0]
    wd_face = wd_val = 0.0
    pend: list[tuple[int, float]] = []
    pt = 0.0
    Q += hs * sl1m * fe1m
    prev = 1.0
    k = 0
    t = o
    H1 = H - 1
    while k < H:
        k += 1
        t += 1
        c = cl[t] * inv
        h = hi[t] * inv
        if Q - hs * h < mhs * h:                          # 1 liquidation
            Q = 0.0
            for arr, amt in pend:
                if arr <= H:
                    pt -= amt
                    back = (arr if arr > k else k) + td
                    wd_face += amt
                    wd_val += amt * ex[back if back < H else H]
            venue = Q + pt + wd_face - dep_face
            return venue - ((dep_cost - dep_face) - (wd_val - wd_face))
        Q += fh[t] * (prev + c)                         # 2 funding
        if pend and pend[0][0] <= k:                      # 4 arrivals
            while pend and pend[0][0] <= k:
                amt = pend.pop(0)[1]
                pt -= amt
                Q += amt
        if k < H:                                         # 5 top-up or sweep
            E = Q - hs * c
            Es = capn * c
            if E + pt < tu * Es:
                amt = Es - E - pt
                dep_face += amt
                dep_cost += amt * ex[k]
                pend.append((k + td, amt))
                pt += amt
            elif pt == 0 and E > sw * Es:
                amt = E - Es
                Q -= amt
                arr = k + td
                wd_face += amt
                wd_val += amt * ex[arr if arr < H else H]
        prev = c
        if k < H1 and Q > 0:                              # fast-forward over quiet days
            stop = H1 if not pend or pend[0][0] > H else min(H1, pend[0][0] - 1)
            if k >= stop:
                continue
            D = Q * 0.005
            Qa = Q - D
            Qb = Q + D
            hi_abs = (Qa + pt) / (hs + tu * capn) * (1 - EPS) * base
            lo_abs = Qb / (hs + sw * capn) * (1 + EPS) * base if pt == 0 else 0.0
            hl_abs = Qa / (hs + mhs) * (1 - EPS) * base
            while k < stop:
                t1 = t + 1
                ct = cl[t1]
                if not (lo_abs < ct < hi_abs and hi[t1] < hl_abs):
                    break
                c = ct * inv
                Q += fh[t1] * (prev + c)
                prev = c
                k += 1
                t = t1
                if not Qa <= Q <= Qb:
                    E = Q - hs * c
                    Es = capn * c
                    if E + pt < tu * Es:
                        amt = Es - E - pt
                        dep_face += amt
                        dep_cost += amt * ex[k]
                        pend.append((k + td, amt))
                        pt += amt
                    elif pt == 0 and E > sw * Es:
                        amt = E - Es
                        Q -= amt
                        arr = k + td
                        wd_face += amt
                        wd_val += amt * ex[arr if arr < H else H]
                    break
    c = cl[o + H] * inv
    Q -= hs * c * sl1p * fe1p
    venue = Q + pt + wd_face - dep_face
    return venue - ((dep_cost - dep_face) - (wd_val - wd_face))


def ex_table(offs: list[int], LC: list[float], H: int) -> list[list[float]]:
    """Per window, ex[m] = exp(LC[o + H] - LC[o + m]): what a dollar sent on day m is worth at the window's end."""
    exp = math.exp
    return [[exp(LC[o + H] - LC[o + m]) for m in range(H + 1)] for o in offs]


def load_venues() -> dict[str, Venue]:
    dff_series = load.fred("DFF")
    dff = {d: v for d, v in dff_series.as_dict().items() if v is not None}
    smp = supply.load_samples()
    lst = volatility.LstIndex(smp)
    base = sorted(set(dff_series.files + smp.files))
    return {vid: build_venue(vid, name, bars, fund, per_day, dff, lst, base)
            for vid, name, bars, fund, per_day in VENUES}


def _query(name: str) -> Query | None:
    """The request that fetched one archived perp file, read from its sidecar."""
    meta = load.meta(SOURCE, name)
    url = meta.get("source_url")
    if not url:
        return None
    params = meta.get("params") or {}
    verb = "POST" if "hyperliquid.xyz" in url else "GET"
    body = f"{verb} {url}" + (f" {json.dumps(params, sort_keys=True)}" if params else "")
    ext = "json" if name in (TERMS, CB_TERMS) else "csv"
    return Query("http", url, body, f"data/raw/{SOURCE}/{name}.{ext}")


def sources_and_queries(vids: tuple[str, ...], *, funding: bool = False, terms: bool = False,
                        coinbase: bool = False) -> tuple[list[str], list[Query]]:
    """Source lines and queries of the venues' bars and funding (with funding=True also Kraken's funding file, with
    terms=True the terms snapshot, with coinbase=True Coinbase's NEAR Perp bars and snapshot)."""
    sources, names = [], []
    for vid, _, bars, fund, _ in VENUES:
        if vid in vids:
            sources.append(SOURCE_NAMES[vid])
            names += [bars, fund]
    if funding:
        for vid, _, fund, _ in SIGN_ONLY:
            sources.append(SOURCE_NAMES[vid])
            names.append(fund)
    if terms:
        names.append(TERMS)
    if coinbase:
        sources.append(SOURCE_NAMES["cb"])
        names += [CB_BARS, CB_TERMS]
    return sources, [q for q in (_query(n) for n in names) if q is not None]


# =========================================================================== resampled histories (F-vol-null, F-perp-redraw)

def blocks_idx(N: int, block: int) -> list[list[int]]:
    """Day records 1..N in consecutive blocks of `block` days (the last may be shorter)."""
    idx = list(range(1, N + 1))
    return [idx[i:i + block] for i in range(0, N, block)]


def shuffle_orders(N: int, block: int, draws: int, seed: int) -> list[list[int]]:
    """Each shuffle's block order: volatility.block_orders on the same Prng(seed) stream as the other strategies."""
    return volatility.block_orders(len(blocks_idx(N, block)), draws, seed)


def redraw_orders(N: int, block: int, draws: int, seed: int) -> list[list[int]]:
    """Each redraw's blocks, drawn with replacement (b = (next * nblocks) >> 32 on one Prng(seed) stream) until at
    least N days are filled."""
    sizes = [len(b) for b in blocks_idx(N, block)]
    nb = len(sizes)
    rng = Prng(seed)
    out = []
    for _ in range(draws):
        seq, n = [], 0
        while n < N:
            b = (rng.next() * nb) >> 32
            seq.append(b)
            n += sizes[b]
        out.append(seq)
    return out


def perm_of(seq: list[int], bl: list[list[int]], N: int) -> list[int]:
    """The day-record order of one history: record 0, then the blocks' days, cut to N + 1 entries."""
    perm = [0]
    for b in seq:
        perm += bl[b]
    del perm[N + 1:]
    return perm


def shuffle_perms(N: int, block: int, draws: int, seed: int) -> list[list[int]]:
    bl = blocks_idx(N, block)
    return [perm_of(o, bl, N) for o in shuffle_orders(N, block, draws, seed)]


def redraw_perms(N: int, block: int, draws: int, seed: int) -> list[list[int]]:
    bl = blocks_idx(N, block)
    return [perm_of(o, bl, N) for o in redraw_orders(N, block, draws, seed)]


_W: dict[str, dict] = {}     # per process: each venue's records and the tables every history reuses


def _prep(arrays: dict[str, dict]) -> None:
    """Pool initializer (and the serial path): the venues' day records arrive once per process."""
    for vid, a in arrays.items():
        kn = a["knobs"]
        _W[vid] = {**a, "C": kernel_consts(kn), "EX": ex_table(a["offs"], a["LC"], kn.H),
                   "bl": blocks_idx(len(a["cr"]) - 1, a["block"])}


def _histories(vid: str, kind: str, seqs: list[list[int]]) -> list[float]:
    """Per history: the mean advantage of the ladder over the windows (kind "shuffle"), or the mean swing, the
    ladder less the yardstick (kind "redraw"), in pp. Sums are Python's sum over windows in window order."""
    W = _W[vid]
    cr, hr, fu, offs, EX, C, bl, kn, LC = W["cr"], W["hr"], W["fu"], W["offs"], W["EX"], W["C"], W["bl"], W["knobs"], \
        W["LC"]
    hs = kn.static
    N = len(cr) - 1
    n = len(offs)
    lad_adv, sta_adv = ladder_adv, static_adv
    out = []
    for seq in seqs:
        cl, hi, f = path(cr, hr, fu, perm_of(seq, bl, N))
        FH = funding_tables(f, C)
        lad = [lad_adv(o, cl, hi, FH, EX[i], C) for i, o in enumerate(offs)]
        if kind == "shuffle":
            out.append(100 * (sum(lad) / n))
            continue
        if hs > 0:
            fs = [x * hs * 0.5 for x in f]
            sta = [sta_adv(o, cl, hi, fs, EX[i], C, hs) for i, o in enumerate(offs)]
        else:
            sta = [sim_window(o, cl, hi, f, LC, kn, static=hs)[0] for o in offs]
        out.append(100 * (sum([a - b for a, b in zip(lad, sta)]) / n))
    return out


class _Fan:
    """Resampling jobs (vid, kind, histories) in parallel processes when portfolio._workers allows, else serially.
    Each process receives the arrays once (initializer); the largest jobs go first; results come back in job order,
    so they never depend on the worker count. A failed pool is reported and the same jobs run serially."""

    def __init__(self, arrays: dict, jobs: list[tuple]) -> None:
        self.arrays, self.jobs = arrays, jobs
        self.workers = _workers(len(jobs)) if jobs else 1
        self.fallback: str | None = None
        self.ex = None
        self.futs: list | None = None
        if self.workers > 1:
            try:
                self.ex = ProcessPoolExecutor(max_workers=self.workers, mp_context=multiprocessing.get_context("spawn"),
                                              initializer=_prep, initargs=(arrays,))
                size = {i: len(job[2]) * len(arrays[job[0]]["offs"]) * (2 if job[1] == "redraw" else 1)
                        for i, job in enumerate(jobs)}
                futs: list = [None] * len(jobs)
                for i in sorted(size, key=lambda i: (-size[i], i)):
                    futs[i] = self.ex.submit(_histories, *jobs[i])
                self.futs = futs
            except Exception as exc:   # noqa: BLE001 - a sandbox without process support
                self._fail(exc)

    def _fail(self, exc: Exception) -> None:
        self.fallback = f"the process pool failed ({type(exc).__name__}: {exc}); the histories ran serially"
        self.workers = 1
        self.futs = None
        if self.ex is not None:
            self.ex.shutdown(cancel_futures=True)
            self.ex = None

    def close(self) -> None:
        """Stop the pool and drop the queued jobs (the caller failed before it read the results)."""
        if self.ex is not None:
            self.ex.shutdown(wait=False, cancel_futures=True)
            self.ex = None
        self.futs = None

    def results(self) -> list[list[float]]:
        if self.futs is not None:
            try:
                out = [f.result() for f in self.futs]
                self.ex.shutdown()
                return out
            except Exception as exc:   # noqa: BLE001
                self._fail(exc)
        _prep(self.arrays)
        return [_histories(*job) for job in self.jobs]


# =========================================================================== real history and the sensitivity cases

def window_rows(v: Venue, p: Knobs, *, static: bool = False, LC: list[float] | None = None) -> list[tuple]:
    """sim_window on every eligible window of the real history (the full accounts)."""
    cl, hi, fu = path(v.cr, v.hr, v.fu)
    LC = v.LC if LC is None else LC
    return [sim_window(o, cl, hi, fu, LC, p, static=p.static if static else None) for o in v.offs]


def sens_cases() -> list[tuple[str, dict]]:
    """(label, change) per F-perp-sens case, labels generated from the params."""
    out: list[tuple[str, dict]] = [("default", {}), (f"latest {HORIZON_DAYS} start days", {"latest": True})]
    out += [(f"dollars cost fed funds + {a:g} points", {"rate_add": float(a)}) for a in SENS_RATE_ADD]
    out += [(f"collateral {x:g} of the target", {"lam": float(x)}) for x in SENS_COLLATERAL]
    out += [(f"transfers take {d} day" + ("" if d == 1 else "s"), {"td": d}) for d in SENS_TRANSFER_DAYS]
    out += [(f"slippage {q:.2f}%", {"slip": q / 100}) for q in SENS_SLIP_PCT]
    out.append(("decide at a close, fill at the next close" if SENS_LAG_DAYS == 1 else
                f"decide at a close, fill {SENS_LAG_DAYS} closes later", {"lag": SENS_LAG_DAYS}))
    return out


def summary(lad: list[tuple], sta: list[tuple]) -> dict:
    """The per-venue means of one case (pp of the starting NEAR stake), as F-perp-ladder, -static and -kept."""
    n = len(lad)

    def pp(i: int, rows: list[tuple]) -> list[float]:
        return [100 * r[i] for r in rows]

    adv, kept = pp(0, lad), pp(1, lad)
    swing = [100 * (a[0] - b[0]) for a, b in zip(lad, sta)]
    margins = [r[12] for r in lad]
    return {"windows": n, "adv": adv, "kept": kept, "swing": swing,
            "adv_mean": statistics.fmean(adv), "adv_median": statistics.median(adv), "adv_worst": min(adv),
            "adv_worst_i": adv.index(min(adv)), "adv_beat": 100 * sum(1 for x in adv if x > 0) / n,
            "kept_mean": statistics.fmean(kept), "kept_median": statistics.median(kept), "kept_worst": min(kept),
            "kept_windows": 100 * sum(1 for x in kept if x > 0) / n,
            "funding": statistics.fmean(pp(2, lad)), "costs": statistics.fmean(pp(3, lad)),
            "interest": statistics.fmean(pp(4, lad)), "liq_loss": statistics.fmean(pp(5, lad)),
            "price": statistics.fmean(pp(10, lad)), "liq_windows": sum(1 for r in lad if r[6]),
            "trades": statistics.fmean(r[7] for r in lad), "short": 100 * statistics.fmean(r[8] for r in lad),
            "peak": 100 * max(r[9] for r in lad), "trend": 100 * (statistics.fmean(r[11] for r in lad) - 1),
            "min_margin": min(margins) if min(margins) < math.inf else None,
            "static_adv": statistics.fmean(pp(0, sta)), "static_funding": statistics.fmean(pp(2, sta)),
            "static_kept": statistics.fmean(pp(1, sta)),
            "swing_mean": statistics.fmean(swing), "swing_median": statistics.median(swing), "swing_worst": min(swing),
            "swing_worst_i": swing.index(min(swing)), "swing_windows": 100 * sum(1 for x in swing if x > 0) / n}


def centred(real: float, xs: list[float]) -> dict:
    """F-perp-redraw: the redrawn means shifted to centre on the real figure; band (type-7) and share above zero."""
    mu = statistics.fmean(xs)
    cs = sorted(x - mu + real for x in xs)
    return {"raw_mean": mu, "lo": q7(cs, RANK_LO / 100), "hi": q7(cs, RANK_HI / 100),
            "share": 100 * sum(1 for x in cs if x > 0) / len(cs)}


def share_band(share: float | None) -> str | None:
    if share is None:
        return None
    return "above" if share >= RANK_HI else "below" if share <= RANK_LO else "inside"


def both(a: str | None, b: str | None) -> str | None:
    """The both-venues rule: above only when both are above, below only when both are below."""
    if a is None or b is None:
        return None
    return a if a == b and a in ("above", "below") else "inside"


def run_cases(v: Venue, base: Knobs, lad0: list[tuple], sta0: list[tuple]) -> list[dict]:
    """Every F-perp-sens case of one venue on its real history (the default case reuses the default rows)."""
    rows = []
    for label, change in sens_cases():
        idx = list(range(len(v.offs)))
        if not change:
            lad, sta = lad0, sta0
        elif change.get("latest"):
            cut = v.starts[-1] - timedelta(days=HORIZON_DAYS)
            idx = [i for i, s in enumerate(v.starts) if s > cut]
            lad, sta = [lad0[i] for i in idx], [sta0[i] for i in idx]
        elif "rate_add" in change:
            LC = fed_line(v.dff, change["rate_add"])
            p = replace(base, rate_add=change["rate_add"])
            lad, sta = window_rows(v, p, LC=LC), window_rows(v, p, static=True, LC=LC)
        else:
            p = replace(base, **change)
            lad = window_rows(v, p)
            sta = sta0 if set(change) == {"lag"} else window_rows(v, p, static=True)   # the yardstick never trades
        row = summary(lad, sta)
        row.update(label=label, first=v.starts[idx[0]], last_end=v.starts[idx[-1]] + timedelta(days=HORIZON_DAYS))
        rows.append(row)
    return rows


# =========================================================================== funding across venues (F-perp-funding)

def funding_facts() -> dict:
    """The latest year all three funding files cover in full days, and per venue the share of settlements the short
    paid, the annual rate (Hyperliquid, Binance) and Hyperliquid's share at its formula's fixed term."""
    out: dict = {"files": [], "venues": {}}
    sets, rows = {}, {}
    for vid, _, fund, per_day in [(vid, n, f, d) for vid, n, _, f, d in VENUES] + list(SIGN_ONLY):
        rs, files = load.csv_rows(SOURCE, fund)
        out["files"] += files
        if not files or not rs:
            out.setdefault("missing", []).append(f"data/raw/{SOURCE}/{fund}.csv")
            continue
        parsed = [(_day_of(r["time"]), float(r["funding_rate"])) for r in rs
                  if r.get("time") and r.get("funding_rate") not in (None, "") and not odd_interval(r, per_day)]
        _, cnt = funding_days(rs, per_day)
        sets[vid] = {d for d, c in cnt.items() if usable(c, per_day)}
        rows[vid] = (parsed, per_day)
    if out.get("missing"):
        out["reason"] = f"{', '.join(out['missing'])} is missing"
        return out
    shared = set.intersection(*sets.values())
    if not shared:
        out["reason"] = "the three funding files share no full day"
        return out
    first, last = min(shared), max(shared)
    out["first"], out["last"] = first, last
    for vid, (parsed, per_day) in rows.items():
        xs = [x for d, x in parsed if first <= d <= last]
        rec = {"n": len(xs), "short_paid": 100 * sum(1 for x in xs if x < 0) / len(xs)}
        if vid != "kr":
            rec["year"] = 100 * statistics.fmean(xs) * PER_YEAR[per_day]
        if vid == "hl":
            rec["const"] = 100 * sum(1 for x in xs if abs(x - HL_INTEREST_HOURLY) < SAME_RATE) / len(xs)
        out["venues"][vid] = rec
    return out


def read_terms() -> dict:
    """Hyperliquid's NEAR contract in the terms snapshot: maximum leverage and open interest in dollars."""
    data, files = load.json_file(SOURCE, TERMS)
    out: dict = {"files": files}
    hl = next((v for v in (data or {}).get("venues") or [] if v.get("venue") == "Hyperliquid"), None) \
        if isinstance(data, dict) else None
    if hl is None:
        out["reason"] = (f"data/raw/{SOURCE}/{TERMS}.json is missing" if not files else
                         f"data/raw/{SOURCE}/{TERMS}.json has no Hyperliquid contract")
        return out
    try:
        out["max_leverage"] = float(hl["contract"]["maxLeverage"])
        out["oi_usd"] = float(hl["context"]["openInterest"]) * float(hl["context"]["markPx"])
        out["checked_at"] = data.get("checked_at")
    except (KeyError, TypeError, ValueError):
        out["reason"] = f"data/raw/{SOURCE}/{TERMS}.json lacks maxLeverage, openInterest or markPx"
    return out


def _finite(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def read_coinbase() -> dict:
    """Coinbase Derivatives' NEAR Perp (F-perp-coinbase): the contract's terms in the product snapshot and the daily
    bars from its listing day. A field the snapshot lacks stays None; the record as a whole needs the product id."""
    data, tfiles = load.json_file(SOURCE, CB_TERMS)
    bars, bfiles = read_bars(CB_BARS)
    out: dict = {"terms_files": tfiles, "bars_files": bfiles}
    rec = data.get("product") if isinstance(data, dict) else None
    if not tfiles:
        out["reason"] = f"data/raw/{SOURCE}/{CB_TERMS}.json is missing"
        return out
    if not isinstance(rec, dict) or rec.get("product_id") != CB_PRODUCT:
        out["reason"] = f"data/raw/{SOURCE}/{CB_TERMS}.json holds no {CB_PRODUCT} record"
        return out
    fut = rec.get("future_product_details") or {}
    out["checked_at"] = data.get("checked_at")
    try:
        out["listed"] = date.fromisoformat(str(rec.get("new_at"))[:10])
    except ValueError:
        out["listed"] = None
    size = out["contract"] = _finite(fut.get("contract_size"))
    oi, px = _finite(fut.get("open_interest")), _finite(fut.get("index_price"))
    out["oi_usd"] = oi * size * px if None not in (oi, size, px) else None
    m = out["short_overnight"] = _finite((fut.get("overnight_margin_rate") or {}).get("short_margin_rate"))
    out["short_intraday"] = _finite((fut.get("intraday_margin_rate") or {}).get("short_margin_rate"))
    out["margin_rise"] = (1 - m) / (1 + m) if m is not None and 0 <= m < 1 else None
    interval = str(fut.get("funding_interval") or "")
    out["interval_h"] = int(interval[:-1]) / 3600 if interval.endswith("s") and interval[:-1].isdigit() else None
    days = sorted(d for d in bars if out["listed"] and d >= out["listed"])
    if days:
        out["days"], out["first_bar"], out["last_bar"] = len(days), days[0], days[-1]
        out["missing_days"] = (days[-1] - days[0]).days + 1 - len(days)
    return out


# =========================================================================== the model run

@dataclass
class Result:
    venues: dict[str, Venue]
    lad: dict[str, list[tuple]] = field(default_factory=dict)        # real history, per window (sim_window)
    sta: dict[str, list[tuple]] = field(default_factory=dict)
    real: dict[str, dict] = field(default_factory=dict)              # summary() of the default case
    sens: dict[str, list[dict]] = field(default_factory=dict)
    nulls: dict[str, list[float]] = field(default_factory=dict)      # shuffled means of adv, pp
    redraws: dict[str, list[float]] = field(default_factory=dict)    # redrawn means of swing, pp
    null_reason: dict[str, str | None] = field(default_factory=dict)
    facts: dict = field(default_factory=dict)
    terms: dict = field(default_factory=dict)
    cb: dict = field(default_factory=dict)                           # Coinbase's NEAR Perp (F-perp-coinbase)
    draws: int = 0
    workers: int = 1
    fallback: str | None = None


def run() -> Result:
    import time
    t0 = time.perf_counter()
    res = Result(load_venues())
    base = knobs()
    arrays, jobs = {}, []
    for vid, v in res.venues.items():
        res.null_reason[vid] = v.reason or v.gap or (None if DRAWS > 0 else "vol_null_draws is zero")
        if res.null_reason[vid] is None:
            arrays[vid] = {"cr": v.cr, "hr": v.hr, "fu": v.fu, "offs": v.offs, "LC": v.LC, "knobs": base,
                           "block": BLOCK}
            N = len(v.cr) - 1
            for kind, orders in (("shuffle", shuffle_orders(N, BLOCK, DRAWS, SEED)),
                                 ("redraw", redraw_orders(N, BLOCK, DRAWS, SEED))):
                jobs += [(vid, kind, orders[i:i + CHUNK]) for i in range(0, len(orders), CHUNK)]
    fan = _Fan(arrays, jobs)
    try:
        for vid, v in res.venues.items():          # the real history while the pool works
            if v.reason:
                continue
            res.lad[vid], res.sta[vid] = window_rows(v, base), window_rows(v, base, static=True)
            res.real[vid] = summary(res.lad[vid], res.sta[vid])
            res.sens[vid] = run_cases(v, base, res.lad[vid], res.sta[vid])
        res.facts = funding_facts()
        res.terms = read_terms()
        res.cb = read_coinbase()
    except BaseException:
        fan.close()                                # a failure here stops the queued histories too
        raise
    done = fan.results() if jobs else []
    for (vid, kind, _), part in zip(jobs, done):
        (res.nulls if kind == "shuffle" else res.redraws).setdefault(vid, []).extend(part)
    res.draws, res.workers, res.fallback = (DRAWS if jobs else 0), fan.workers, fan.fallback
    if jobs:
        print(f"perps: {len(jobs)} jobs of up to {CHUNK} histories on {fan.workers} process"
              f"{'es' if fan.workers > 1 else ''} in {time.perf_counter() - t0:.1f} s", file=sys.stderr)
    if fan.fallback:
        print(f"warning: perps: {fan.fallback}", file=sys.stderr)
    return res


_RESULTS: "weakref.WeakKeyDictionary[Registry, Result]" = weakref.WeakKeyDictionary()


def result(reg: Registry) -> Result:
    if reg not in _RESULTS:
        _RESULTS[reg] = run()
    return _RESULTS[reg]


# =========================================================================== registry

PARAM_ECHOES = (("vol_perp_rung_pct", "%", 0), ("vol_perp_slice_pct", "%", 0), ("vol_perp_static_pct", "%", 0),
                ("vol_perp_fee_pct", "%", 3), ("vol_perp_slip_pct", "%", 2), ("vol_perp_maint_pct", "%", 0),
                ("vol_perp_rungs", "count", None), ("vol_perp_transfer_days", "days", None),
                ("vol_perp_collateral", "x", 2), ("vol_perp_topup_at", "x", 2), ("vol_perp_sweep_at", "x", 2))
SHARE = 1        # decimals of a share of windows, histories or settlements
PP = 2           # decimals of a pp figure


def compute(reg: Registry) -> None:
    res = result(reg)

    for key, unit, dec in PARAM_ECHOES:
        reg.put(key, param(key), unit, f"P-{key}", source_files=["thesis/rules.toml"],
                inputs=[f"thesis/rules.toml [params] {key}"], decimals=dec)
    reg.put("vol_perp_cap_pct", param("vol_perp_rungs") * param("vol_perp_slice_pct"), "%", "F-perp-ladder",
            source_files=["thesis/rules.toml"], inputs=["vol_perp_rungs x vol_perp_slice_pct"], decimals=0)

    for vid, v in res.venues.items():
        _venue_keys(reg, res, vid, v)

    for key, part in (("vol_perp_band", "band"), ("vol_perp_swing_band", "swing_band")):
        a, b = (reg.value(f"vol_perp_{vid}_{part}") for vid, *_ in VENUES)
        files = sorted({f for v in res.venues.values() for f in v.files})
        why = "; ".join(f"{res.venues[vid].name}: {reg.get(f'vol_perp_{vid}_{part}').null_reason}"
                        for vid, *_ in VENUES if reg.value(f"vol_perp_{vid}_{part}") is None)
        reg.put(key, both(a, b), "text", "F-vol-null" if part == "band" else "F-perp-redraw", source_files=files,
                inputs=["above if both venues are above, below if both are below, else inside"],
                null_reason=why or None)

    t = res.terms
    why = t.get("reason")
    reg.put("vol_perp_hl_max_leverage", t.get("max_leverage"), "x", "F-perp-ladder", source_files=t["files"],
            inputs=[f"Hyperliquid NEAR contract maxLeverage, snapshot {t.get('checked_at')}"] if not why else [],
            null_reason=why, decimals=0)
    reg.put("vol_perp_hl_oi_usd", t.get("oi_usd"), "USD", "F-perp-ladder", source_files=t["files"],
            inputs=[f"openInterest x markPx, snapshot {t.get('checked_at')}"] if not why else [], null_reason=why)

    fx = res.facts
    why = fx.get("reason")
    year_in = ([f"{iso(fx['first'])}..{iso(fx['last'])}, {(fx['last'] - fx['first']).days + 1} days every funding file "
                "covers in full"] if not why else [])
    for key, d in (("vol_perp_year_first", fx.get("first")), ("vol_perp_year_last", fx.get("last"))):
        reg.put(key, iso(d), "date", "F-perp-funding", source_files=fx["files"], inputs=year_in, null_reason=why)
    ven = fx.get("venues", {})
    names = {vid: name for vid, name, *_ in VENUES + SIGN_ONLY}
    for vid in ("hl", "bn", "kr"):
        r = ven.get(vid, {})
        reg.put(f"vol_perp_{vid}_short_paid", r.get("short_paid"), "%", "F-perp-funding", source_files=fx["files"],
                inputs=year_in + ([f"{r['n']:,} {names[vid]} settlements; share with a negative rate"] if r else []),
                null_reason=why, decimals=SHARE)
    for vid, per_day in (("hl", 24), ("bn", 3)):
        r = ven.get(vid, {})
        reg.put(f"vol_perp_{vid}_funding_year", r.get("year"), "%", "F-perp-funding", source_files=fx["files"],
                inputs=year_in + ([f"mean settlement rate x {PER_YEAR[per_day]:,} settlements a year"] if r else []),
                null_reason=why, decimals=2)
    r = ven.get("hl", {})
    reg.put("vol_perp_hl_const_share", r.get("const"), "%", "F-perp-funding", source_files=fx["files"],
            inputs=year_in + ([f"|rate - {HL_INTEREST_HOURLY:g}| < {SAME_RATE:g}: Hyperliquid's fixed interest term, "
                               "0.01% per eight hours paid hourly"] if r else []),
            null_reason=why, decimals=SHARE)

    _coinbase_keys(reg, res.cb)
    _compare_keys(reg, res)


def _coinbase_keys(reg: Registry, cb: dict) -> None:
    """vol_perp_cb_*: Coinbase Derivatives' US-regulated NEAR Perp (F-perp-coinbase). No rule reads them."""
    why = cb.get("reason")
    tf, bf = cb.get("terms_files", []), cb.get("bars_files", [])
    snap = f"{CB_PRODUCT} product record, snapshot {cb.get('checked_at')}"

    def put(key, value, unit, inputs, files=tf, decimals=None, missing="the snapshot lacks it"):
        reg.put(f"vol_perp_cb_{key}", value, unit, "F-perp-coinbase", source_files=files,
                inputs=inputs if value is not None else [],
                null_reason=None if value is not None else (why or missing), decimals=decimals)

    put("listed", iso(cb.get("listed")), "date", [f"new_at as a UTC day, {snap}"])
    put("contract", cb.get("contract"), "NEAR", [f"contract_size (NEAR per contract), {snap}"])
    put("oi_usd", cb.get("oi_usd"), "USD", [f"open_interest (contracts) x contract_size x index_price, {snap}"])
    put("funding_interval", cb.get("interval_h"), "hours", [f"funding_interval in hours, {snap}"])
    m = cb.get("short_overnight")
    put("short_margin_overnight", 100 * m if m is not None else None, "%",
        [f"100 x overnight_margin_rate.short_margin_rate, {snap}"], decimals=1)
    i = cb.get("short_intraday")
    put("short_margin_intraday", 100 * i if i is not None else None, "%",
        [f"100 x intraday_margin_rate.short_margin_rate, {snap}"], decimals=1)
    r = cb.get("margin_rise")
    put("margin_rise", 100 * r if r is not None else None, "%",
        [f"100 (1 - m) / (1 + m), m = {m:g} the overnight short margin rate: the rise at which a short backed by "
         "dollars equal to its value at the sale, with no top-up, meets that margin"] if r is not None else [],
        decimals=0, missing="the snapshot lacks the overnight short margin rate, or it is not below one")
    put_constant(reg, "vol_perp_cb_access_read", CB_ACCESS, "date", fld="retrieved")
    n = cb.get("days")
    put("days", n, "days",
        [f"daily bars {iso(cb['first_bar'])}..{iso(cb['last_bar'])} from the listing day, "
         f"{cb['missing_days']} days missing between them"] if n else [],
        files=sorted(tf + bf),
        missing=f"data/raw/{SOURCE}/{CB_BARS}.csv is missing" if not bf else "no daily bar from the listing day on")


def _venue_keys(reg: Registry, res: Result, vid: str, v: Venue) -> None:
    """Every vol_perp_<vid>_* key of one venue (None with the venue's reason when it has no window)."""
    files = v.files
    real = res.real.get(vid)
    why = v.reason

    def put(key, value, unit, fid, inputs=(), reason=None, decimals=None, signed=True):
        reg.put(f"vol_perp_{vid}_{key}", value, unit, fid, source_files=files, inputs=inputs,
                null_reason=None if value is not None else (why or reason or "not computed"), decimals=decimals,
                signed=signed)

    span = ([f"{len(v.starts)} start days {iso(v.starts[0])}..{iso(v.starts[-1])}, horizon {HORIZON_DAYS} days",
             "windows failing on bars / funding / DFF / LST edges: "
             f"{v.fail['bars']} / {v.fail['funding']} / {v.fail['dff']} / {v.fail['lst']}"] if real else [])
    put("windows", len(v.starts) if real else None, "count", "F-perp-windows", span)
    put("first_start", iso(v.starts[0]) if real else None, "date", "F-perp-windows", span)
    put("last_start", iso(v.starts[-1]) if real else None, "date", "F-perp-windows", span)
    put("last_end", iso(v.last_end) if real else None, "date", "F-perp-windows", span)
    put("years", v.years if real else None, "count", "F-perp-windows",
        span + ["floor((last_end - first_start) / 365)"])
    put("partial_days", len(v.partial) if real else None, "count", "F-perp-windows",
        [f"{iso(d)}: one settlement missing" for d in v.partial] or ["every day inside the span is complete"])
    put("trend_mean", real["trend"] if real else None, "%", "F-perp-windows", span + ["100 (mean p_H - 1)"])

    lad_in = span + [f"rungs of {param('vol_perp_rung_pct'):g}%, slices of {param('vol_perp_slice_pct'):g}% of the "
                     f"stake, at most {RUNGS}; fills at the daily close"]
    g = (lambda k: real[k]) if real else (lambda k: None)
    put("mean", g("adv_mean"), "pp", "F-perp-ladder", lad_in, decimals=PP)
    put("median", g("adv_median"), "pp", "F-perp-ladder", lad_in, decimals=PP)
    put("worst", g("adv_worst"), "pp", "F-perp-ladder", lad_in, decimals=PP)
    put("worst_start", iso(v.starts[real["adv_worst_i"]]) if real else None, "date", "F-perp-ladder", lad_in)
    put("beat", g("adv_beat"), "%", "F-perp-ladder", lad_in, decimals=SHARE)
    put("liq_windows", g("liq_windows"), "count", "F-perp-ladder", lad_in)
    put("min_margin", g("min_margin"), "x", "F-perp-ladder", lad_in, reason="the ladder was never short",
        decimals=2)
    put("trades", g("trades"), "count", "F-perp-ladder", lad_in + ["mean fills a window"], decimals=1)
    put("short", g("short"), "%", "F-perp-ladder", lad_in + ["mean short held, share of the stake"], decimals=SHARE)
    put("peak_dollars", g("peak"), "%", "F-perp-ladder",
        lad_in + ["largest net dollars sent to the venue in a window, share of the starting stake"], decimals=0)

    for key, k, signed in (("funding", "funding", True), ("costs", "costs", False), ("interest", "interest", False),
                           ("liq_loss", "liq_loss", False), ("kept", "kept_mean", True), ("price", "price", True),
                           ("kept_median", "kept_median", True), ("kept_worst", "kept_worst", True)):
        put(key, g(k), "pp", "F-perp-kept", lad_in, decimals=PP, signed=signed)
    put("kept_windows", g("kept_windows"), "%", "F-perp-kept", lad_in, decimals=SHARE)

    st_in = span + [f"a short of {param('vol_perp_static_pct'):g}% of the stake from the start close to the end "
                    "close, the ladder's collateral rule"]
    for key, k in (("static_adv", "static_adv"), ("static_funding", "static_funding"), ("static_kept", "static_kept"),
                   ("swing", "swing_mean"), ("swing_median", "swing_median"), ("swing_worst", "swing_worst")):
        put(key, g(k), "pp", "F-perp-static", st_in, decimals=PP)
    put("swing_worst_start", iso(v.starts[real["swing_worst_i"]]) if real else None, "date", "F-perp-static", st_in)
    put("swing_windows", g("swing_windows"), "%", "F-perp-static", st_in, decimals=SHARE)

    # the shuffles (H10) and the redraws (H8)
    nulls, reds = res.nulls.get(vid), res.redraws.get(vid)
    nwhy = res.null_reason.get(vid)
    s = volatility.summarize(real["adv_mean"], nulls) if real and nulls else None
    sh_in = ([f"{len(nulls):,} shuffles of {BLOCK}-day blocks of the venue's day records, Prng(sim_seed = {SEED})"]
             if nulls else [])
    for key, k in (("null_mean", "null_mean"), ("null_lo", "lo"), ("null_hi", "hi"), ("excess", "excess")):
        put(key, s[k] if s else None, "pp", "F-vol-null", sh_in, nwhy, decimals=PP)
    put("rank", s["rank"] if s else None, "%", "F-vol-null", sh_in, nwhy, decimals=SHARE)
    put("band", volatility.band(s["rank"]) if s else None, "text", "F-vol-null",
        sh_in + [f"above if rank >= {RANK_HI:g}, below if rank <= {RANK_LO:g}, else inside"], nwhy)
    c = centred(real["swing_mean"], reds) if real and reds else None
    rd_in = ([f"{len(reds):,} redraws of {BLOCK}-day blocks with replacement, Prng(sim_seed = {SEED}), centred on "
              "the real swing"] if reds else [])
    for key, k in (("swing_lo", "lo"), ("swing_hi", "hi"), ("swing_raw_mean", "raw_mean")):
        put(key, c[k] if c else None, "pp", "F-perp-redraw", rd_in, nwhy, decimals=PP)
    put("swing_share", c["share"] if c else None, "%", "F-perp-redraw", rd_in, nwhy, decimals=SHARE)
    put("swing_band", share_band(c["share"]) if c else None, "text", "F-perp-redraw",
        rd_in + [f"above if share >= {RANK_HI:g}, below if share <= {RANK_LO:g}, else inside"], nwhy)


# =========================================================================== like-for-like comparison (F-vol-compare)
# The three strategies for selling NEAR's volatility on the one-year windows all three share: (a) and (b) of
# volatility.py on the windows whose start is also an eligible ladder window of the venue, and the venue's ladder on
# the same windows. Each mean is the advantage over holding the same staked NEAR, pp of the starting NEAR stake, after
# the strategy's own costs (statistics.fmean over the windows). No shuffles. No rule reads these keys.

CMP = (("rally", "rallies"), ("rebal", "rebalancing"), ("ladder", "ladder"))
CMP_SPLIT = "split"


def trim(v: Venue, keep: set[date]) -> Venue:
    """The venue with only the windows starting in `keep`, its day records cut to those windows' span: the arrays
    build_venue makes for that span."""
    t = Venue(v.vid, v.name, v.per_day)
    t.files, t.reason, t.fail, t.partial, t.gap = v.files, v.reason, v.fail, v.partial, v.gap
    t.starts = [d for d in v.starts if d in keep]
    s0 = t.s0 = t.starts[0]
    k0 = (s0 - v.s0).days
    t.offs = [(d - s0).days for d in t.starts]
    N = t.N = (t.starts[-1] - s0).days + HORIZON_DAYS
    t.cr = [1.0] + v.cr[k0 + 1:k0 + N + 1]
    t.hr = [1.0] + v.hr[k0 + 1:k0 + N + 1]
    t.fu = [0.0] + v.fu[k0 + 1:k0 + N + 1]
    t.dff = v.dff[k0:k0 + N]
    t.LC = fed_line(t.dff)
    return t


def _strategy_means(vres: "volatility.Result", keep: set[date]) -> dict:
    """(a) and (b) re-scanned on the windows whose start is in `keep`, on the span of exactly those windows."""
    close = {d: v for d, v in vres.prices.points if v}
    dff = load.fred("DFF").as_dict()
    sub = volatility.Result(vres.files, vres.prices, vres.smp)
    sub.w = vres.w
    # copies: the scan sets each window's offset into its own span
    sub.wins = [replace(w) for w in vres.wins if w.s in keep]
    cx, ret, _gap = volatility._context(sub, close, dff)
    _, per = volatility._strategies(cx, volatility.line(ret), keep=True)
    return {"starts": [w.s for w in sub.wins],
            "rally": statistics.fmean([100 * (q["v_a"] - q["hold"]) for q in per]),
            "rebal": statistics.fmean([100 * (q["rb"] - q["bh"]) / sub.w for q in per])}


def compare(vres: "volatility.Result", res: Result) -> dict[str, dict]:
    """Per venue with a ladder: the common windows and each strategy's mean advantage on them, or the reason why not."""
    out = {}
    for vid, v in res.venues.items():
        if v.reason or vid not in res.lad:
            out[vid] = {"reason": v.reason or "the venue's ladder was not computed"}
            continue
        if vres.reason or not vres.wins or not vres.w:
            out[vid] = {"reason": vres.reason or vres.w_reason or "no strategy (a) or (b) window"}
            continue
        keep = {w.s for w in vres.wins} & set(v.starts)
        if not keep:
            out[vid] = {"reason": f"no one-year window is eligible for both the strategies of H10 and the {v.name} "
                                  "ladder"}
            continue
        ab = _strategy_means(vres, keep)
        if keep == set(v.starts):    # every ladder window is common: trim() would rebuild the same arrays
            starts, rows = list(v.starts), res.lad[vid]
        else:
            tv = trim(v, keep)
            starts, rows = tv.starts, window_rows(tv, knobs())
        if starts != ab["starts"]:
            raise ValueError(f"perps: the common windows of {v.name} differ between the strategies")
        means = {"rally": ab["rally"], "rebal": ab["rebal"], "ladder": statistics.fmean([100 * r[0] for r in rows])}
        best = max(means.values())
        out[vid] = {"windows": len(starts), "first": starts[0], "last": starts[-1], **means,
                    "winner": next(word for key, word in CMP if means[key] == best)}
    return out


def _compare_keys(reg: Registry, res: Result) -> None:
    vres = volatility.result(reg)
    cmp = compare(vres, res)
    winners, whys = [], []
    for vid, name, *_ in VENUES:
        c = cmp.get(vid, {"reason": "no such venue"})
        why = c.get("reason")
        v = res.venues.get(vid)
        files = sorted(set(vres.files + (v.files if v else [])))
        span = ([f"{c['windows']} start days {iso(c['first'])}..{iso(c['last'])}: the one-year windows of strategies "
                 f"(a) and (b) (F-vol-windows) whose start is also an eligible {name} ladder window (F-perp-windows)"]
                if not why else [])

        def put(key, value, unit, inputs=(), decimals=None):
            reg.put(f"vol_cmp_{vid}_{key}", value if not why else None, unit, "F-vol-compare", source_files=files,
                    inputs=list(inputs) if not why else [], null_reason=why, decimals=decimals)

        put("windows", c.get("windows"), "count", span)
        put("first_start", iso(c["first"]) if not why else None, "date", span)
        put("last_start", iso(c["last"]) if not why else None, "date", span)
        for key, words in (("rally", "(a) selling into rallies"),
                           ("rebal", "(b) resetting the NEAR share every vol_rebal_days, at w = sim_share"),
                           ("ladder", f"the {name} perp ladder (F-perp-ladder)")):
            put(key, c.get(key), "pp", span + [f"{words}: mean advantage over holding the same staked NEAR, pp of "
                                               "the starting NEAR stake, after its own costs"], decimals=PP)
        put("winner", c.get("winner"), "text",
            span + ["the strategy with the highest mean advantage on these windows: " +
                    ", ".join(f"{word} (vol_cmp_{vid}_{key})" for key, word in CMP)])
        if why:
            whys.append(f"{name}: {why}")
        else:
            winners.append(c["winner"])
    files = sorted(set(vres.files) | {f for v in res.venues.values() for f in v.files})
    value = None if whys else winners[0] if len(set(winners)) == 1 else CMP_SPLIT
    reg.put("vol_cmp_winner", value, "text", "F-vol-compare", source_files=files,
            inputs=[f"vol_cmp_{vid}_winner" for vid, *_ in VENUES] + [
                f"the common winner when every venue's span agrees, else {CMP_SPLIT}"] if not whys else [],
            null_reason="; ".join(whys) or None)


# =========================================================================== chart c26

TABLE_COLUMNS = ["venue", "case", "windows", "first_start", "last_end", "swing_pp", "swing_low_pp", "swing_high_pp",
                 "swing_share_pct", "swing_raw_mean_pp", "swing_worst_pp", "swing_windows_pct", "advantage_pp",
                 "static_advantage_pp", "funding_pp", "static_funding_pp", "costs_pp", "interest_pp", "liquidation_pp",
                 "kept_pp", "static_kept_pp", "price_pp", "liquidated_windows", "mean_short_pct", "peak_dollars_pct",
                 "min_margin_x"]
CSV_COLUMNS = ["venue", "start", "end", "price_factor", "advantage_pp", "static_advantage_pp", "swing_pp", "kept_pp",
               "funding_pp", "costs_pp", "interest_pp", "liquidation_pp", "price_pp", "trades", "liquidated",
               "mean_short_pct", "peak_dollars_pct", "min_margin_x"]


def charts(reg: Registry) -> list[Chart]:
    return [swing_chart(reg)]


def _f(value, unit: str, decimals: int | None = None, signed: bool = True) -> str | None:
    """A table cell as the registry would print it (rounded as Registry.put rounds, then numbers.fmt)."""
    if value is None:
        return None
    if unit in ("%", "pp", "x"):
        value = round(float(value), 4)
    return fmt(value, unit, decimals, signed=signed)


def _table_row(name: str, row: dict, band: dict | None) -> list:
    return [name, row["label"], _f(row["windows"], "count"), iso(row["first"]), iso(row["last_end"]),
            _f(row["swing_mean"], "pp", PP), _f(band["lo"], "pp", PP) if band else None,
            _f(band["hi"], "pp", PP) if band else None, _f(band["share"], "%", SHARE) if band else None,
            _f(band["raw_mean"], "pp", PP) if band else None, _f(row["swing_worst"], "pp", PP),
            _f(row["swing_windows"], "%", SHARE), _f(row["adv_mean"], "pp", PP), _f(row["static_adv"], "pp", PP),
            _f(row["funding"], "pp", PP), _f(row["static_funding"], "pp", PP), _f(row["costs"], "pp", PP, False),
            _f(row["interest"], "pp", PP, False), _f(row["liq_loss"], "pp", PP, False), _f(row["kept_mean"], "pp", PP),
            _f(row["static_kept"], "pp", PP), _f(row["price"], "pp", PP), _f(row["liq_windows"], "count"),
            _f(row["short"], "%", SHARE), _f(row["peak"], "%", 0), _f(row["min_margin"], "x", 2)]


def swing_chart(reg: Registry) -> Chart:
    """c26: how far the perp ladder ended ahead of a short that never trades on each venue, with the band of the
    centred redrawn histories; the table holds the sensitivity cases."""
    res = result(reg)

    def v_(key: str):
        return reg.value(key) if key in reg else None

    names = [(vid, name) for vid, name, *_ in VENUES]
    redrawn = [[name, v_(f"vol_perp_{vid}_swing"), v_(f"vol_perp_{vid}_swing_lo"), v_(f"vol_perp_{vid}_swing_hi")]
               for vid, name in names]
    real = [[name, v_(f"vol_perp_{vid}_swing")] for vid, name in names]
    rows, csv_rows = [], []
    for vid, name in names:
        reds = res.redraws.get(vid)
        band = centred(res.real[vid]["swing_mean"], reds) if vid in res.real and reds else None
        for i, case in enumerate(res.sens.get(vid, [])):
            rows.append(_table_row(name, case, band if i == 0 else None))
        v = res.venues[vid]
        for s, a, b in zip(v.starts, res.lad.get(vid, []), res.sta.get(vid, [])):
            csv_rows.append([name, iso(s), iso(s + timedelta(days=HORIZON_DAYS)), rnd(a[11], 6), rnd(100 * a[0]),
                             rnd(100 * b[0]), rnd(100 * (a[0] - b[0])), rnd(100 * a[1]), rnd(100 * a[2]),
                             rnd(100 * a[3]), rnd(100 * a[4]), rnd(100 * a[5]), rnd(100 * a[10]), a[7],
                             1 if a[6] else 0, rnd(100 * a[8]), rnd(100 * a[9]),
                             rnd(a[12]) if a[12] < math.inf else None])
    files = sorted({f for v in res.venues.values() for f in v.files} | set(res.facts.get("files", []))
                   | set(res.terms.get("files", [])) | set(res.cb.get("terms_files", []))
                   | set(res.cb.get("bars_files", [])))
    perp_sources, perp_queries = sources_and_queries(("hl", "bn"), funding=True, terms=True, coinbase=True)
    lst_source = supply.RPC_SOURCE + ": meta-pool.near get_st_near_price, linear-protocol.near ft_price"
    smp = supply.load_samples()
    lasts = [d for d in (v_("vol_perp_hl_last_end"), v_("vol_perp_bn_last_end")) if d]
    chart = Chart(
        id="c26", section="A5", priority="lead", claims=["H8"], verdicts=["H8"], title="", sentence="",
        form="dot-range", eyebrow="A5 · tests H8",
        sources=perp_sources + [s for s in (supply.fred_source("DFF"), lst_source) if s],
        queries=perp_queries + [fred_query("DFF")] + supply.lst_queries(smp),
        table={"columns": TABLE_COLUMNS, "rows": rows},
        code_ref="pipeline/models/perps.py::swing_chart",
        formula_ids=["F-perp-windows", "F-perp-ladder", "F-perp-static", "F-perp-kept", "F-perp-redraw",
                     "F-perp-funding", "F-perp-sens", "F-ffcomp"],
        x={"type": "linear", "unit": "pp", "legend": "series", "dp": PP}, y={"type": "category", "unit": "pp"},
        series=[Series("redrawn", "Redrawn histories, centred: middle band", "context", redrawn, role="context",
                       unit="pp"),
                Series("real", "NEAR's real history", "near", real, role="emphasis", unit="pp")],
        reference_lines=[{"x": 0, "label": "no lead over a short that never trades"}],
        coverage={"first": v_("vol_perp_bn_first_start"), "last": max(lasts) if lasts else None,
                  "n": (v_("vol_perp_hl_windows") or 0) + (v_("vol_perp_bn_windows") or 0), "gaps": []},
        as_of=files_as_of(files),
    )
    chart.csv_table = {"columns": CSV_COLUMNS, "rows": csv_rows}
    band, hl_band, bn_band = v_("vol_perp_swing_band"), v_("vol_perp_hl_swing_band"), v_("vol_perp_bn_swing_band")
    fallback = "What selling NEAR's swings on a perpetual made over a short that never trades, on two venues."
    titles = {
        "above": "Selling NEAR's swings on a perpetual beat a short that never trades on both venues, by "
                 "{vol_perp_hl_swing} a year on Hyperliquid and {vol_perp_bn_swing} on Binance, in nearly every "
                 "redrawn history.",
        "below": "Selling NEAR's swings on a perpetual fell behind a short that never trades on both venues: "
                 "{vol_perp_hl_swing} a year on Hyperliquid and {vol_perp_bn_swing} on Binance.",
        "both inside": "Against a short that never trades, selling NEAR's swings made {vol_perp_hl_swing} a year on "
                       "Hyperliquid and {vol_perp_bn_swing} on Binance; redrawn months put zero inside both ranges, "
                       "and Binance's worst year was {vol_perp_bn_swing_worst}.",
        "inside": "Against a short that never trades, selling NEAR's swings made {vol_perp_hl_swing} a year on "
                  "Hyperliquid and {vol_perp_bn_swing} on Binance, and the venues disagree once months are redrawn.",
    }
    pick = ("both inside" if band == "inside" and hl_band == bn_band == "inside" else band)
    chart.title_template = supply.pick(reg, [*([titles[pick]] if pick in titles else []), fallback])
    chart.sentence_template = supply.pick(reg, [
        "Each row is a venue: the blue dot is the ladder's result against a short of {vol_perp_static_pct} of "
        "the stake held all year, in percentage points (pp) of the starting stake a year on NEAR's real history, "
        "after funding, fees, slippage, interest and liquidation; the bar is the middle band of {vol_null_draws} "
        "redrawn histories, centred on the dot.",
        "Each row is a venue: how far selling NEAR's swings on a perpetual ended ahead of a short that never trades, "
        "in points of the starting NEAR stake.",
    ])
    chart.caveat_template = supply.pick(reg, [
        "Hyperliquid and Binance are reported to exclude US persons, and Coinbase's US-regulated NEAR perpetual, "
        "listed {vol_perp_cb_listed}, is too new to test; the windows overlap, so Hyperliquid's record is "
        "{vol_perp_hl_years} separate years and Binance's {vol_perp_bn_years}.",
        "Hyperliquid and Binance are reported to exclude US persons; the one-year windows overlap.",
    ])
    chart.notes_templates = notes_for(reg, [
        "The perp ladder keeps the NEAR staked and sells {vol_perp_slice_pct} of the stake short at the close after "
        "each {vol_perp_rung_pct} rise, at most {vol_perp_cap_pct}, buying each slice back after a "
        "{vol_perp_rung_pct} fall.",
        "The short that never trades sells {vol_perp_static_pct} of the stake at the start and buys it back at the "
        "end, with "
        "the same dollars on the venue; it collected {vol_perp_hl_static_funding} a year in funding on Hyperliquid "
        "against the ladder's {vol_perp_hl_funding}, so most of the funding is paid to any short.",
        "Funding is set by each venue's formula. Between {vol_perp_year_first} and {vol_perp_year_last}, Hyperliquid "
        "paid shorts {vol_perp_hl_funding_year} a year and Binance {vol_perp_bn_funding_year}; Hyperliquid's "
        "formula adds a fixed interest term, and its rate sat exactly at that term in {vol_perp_hl_const_share} of "
        "hours, the hours the perpetual traded close to the spot price. Shorts paid in {vol_perp_hl_short_paid} of "
        "Hyperliquid's hours, {vol_perp_bn_short_paid} of Binance's settlements and {vol_perp_kr_short_paid} of "
        "Kraken's hours.",
        "Against holding, the ladder averaged {vol_perp_hl_mean} on Hyperliquid and {vol_perp_bn_mean} on Binance, "
        "with a worst window of {vol_perp_bn_worst}; the claim that patient holders capture NEAR's volatility tests "
        "that against shuffled histories.",
        "Staked NEAR cannot be posted on the venue, so dollars worth the most the ladder can be short sit there; "
        "after rises they are topped up, because that value grows and to cover losses on the short, with transfers "
        "taking {vol_perp_transfer_days}. At the worst, the dollars sent less those brought home reached "
        "{vol_perp_hl_peak_dollars} of the stake's starting value on Hyperliquid and {vol_perp_bn_peak_dollars} on "
        "Binance. {vol_perp_hl_liq_windows} windows on Hyperliquid and {vol_perp_bn_liq_windows} on Binance were "
        "liquidated at a {vol_perp_maint_pct} maintenance margin on both venues, half the initial margin at "
        "Hyperliquid's {vol_perp_hl_max_leverage} maximum leverage; the closest call left equity "
        "at {vol_perp_hl_min_margin} the maintenance level on Hyperliquid and {vol_perp_bn_min_margin} on Binance. "
        "The table shows thinner collateral and slower transfers.",
        "A redraw picks blocks of {vol_null_block_days} with replacement, so it can leave out the richest months; "
        "each day keeps its price move, its high and its funding together. Redrawing breaks NEAR's longer swings, "
        "which by itself moves the average to {vol_perp_hl_swing_raw_mean} on Hyperliquid, so the redraws are "
        "centred on the real result and show only how far it moves with the months drawn.",
        "Hyperliquid's NEAR perpetual held about {vol_perp_hl_oi_usd} of open positions at the snapshot. near.com "
        "says it routes perpetual trades to Hyperliquid; its own market list and fees are not in this data.",
        "Fees use Hyperliquid's published base taker rate, {vol_perp_fee_pct}, on both venues, and every trade fills "
        "{vol_perp_slip_pct} against the holder; the table shows wider fills and a fill one close later.",
        "Hyperliquid's terms are reported to count US persons among its Restricted Persons, and Binance's futures to "
        "exclude them; neither was read first-hand here. Kraken's instrument record lists only non-US platforms for "
        "its NEAR perpetual.",
        ["Coinbase Derivatives has listed a US-regulated NEAR perpetual-style future since {vol_perp_cb_listed}, "
         "{vol_perp_cb_contract} a contract and margined in dollars, which US customers trade through Coinbase "
         "Financial Markets; it held about {vol_perp_cb_oi_usd} of open positions at the snapshot. "
         "Coinbase's help pages, read {vol_perp_cb_access_read}, say access needs an approved application and an "
         "eligible address and may not be available in every jurisdiction; they name no US state as excluded. An "
         "approved US holder can run the ladder there, but its {vol_perp_cb_days} of daily bars fall short of a "
         "one-year window, so neither claim reads it.",
         "Coinbase Derivatives has listed a US-regulated NEAR perpetual-style future since {vol_perp_cb_listed}, "
         "{vol_perp_cb_contract} a contract and margined in dollars, which US customers trade through Coinbase "
         "Financial Markets; it held about {vol_perp_cb_oi_usd} of open positions at the snapshot. "
         "Coinbase says access needs an approved application and an eligible address and may not be available in "
         "every jurisdiction. An approved US holder can run the ladder there, but its {vol_perp_cb_days} of daily "
         "bars fall short of a one-year window, so neither claim reads it."],
        "Coinbase's margin for a NEAR short is {vol_perp_cb_short_margin_overnight} of its value overnight and "
        "{vol_perp_cb_short_margin_intraday} intraday. The ladder keeps dollars worth {vol_perp_collateral} the "
        "most it can be short on the venue, which covers both; with no top-up, though, a short backed by dollars "
        "equal to its value meets the overnight margin after a rise of about {vol_perp_cb_margin_rise}.",
    ])
    missing = [res.venues[vid] for vid, _ in names if res.venues[vid].reason]
    if missing:
        chart.status = "unavailable"
        chart.empty_state = empty_state(
            f"Not computed: {missing[0].reason}.",
            "Hyperliquid and Binance NEAR perpetual bars and funding, FRED DFF and liquid-staking samples over a year.")
    elif not ready(reg, "vol_perp_hl_swing_share", "vol_perp_bn_swing_share"):
        chart.status = "partial"
        chart.status_banner = ("The redrawn histories are missing, so each venue's swing is shown without the band "
                               "the claim reads.")
    return chart.resolve(reg)
