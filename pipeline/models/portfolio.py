"""The desk simulation: a trading desk's idle money held in dollars and euros, with and without a NEAR part.

Frame. A desk of size V (a scenario value, thesis/constants.toml [desk_scenario.size]) holds share s of its cash in
dollars and 1 - s in euros for one year. The mix moves share w into liquid-staked NEAR and keeps the s : 1 - s split
on the rest (pro rata funding), so the only choice is "NEAR or cash". Nominal dollars, buy and hold, rates held flat.

Formulas (anchors on the data page):
  F-sim-obs      the latest observation on or before latest_common_date (ESTR, DGS1; EFFR is effr_latest).
  F-sim-rate     R = (1 + r/36000)^365 - 1 for overnight rates (EFFR, ESTR), ACT/360 daily compounding; y = y_pct/100.
                 Rounded to twelve significant digits so every platform bakes the same doubles.
  F-sim-tbill    one-year Treasury (DGS1, bond-equivalent yield): R_T = (1 + y/200)^2 - 1, twelve significant digits.
  F-sim-pool     a_d = P(d)/P(d-1) over the window's calendar days; b_d = X(d)/X(previous fixing) on DEXUSEU days,
                 else 1. Gross demeaning: a~ = a / mean(a), b~ = b / mean(b) (never log demeaning).
  F-sim-path     path i: horizon iid draws of one joint day index j (xoshiro128** seeded by splitmix32,
                 j = floor(u32 * n / 2^32)); G_i = prod a~_j, X_i = prod b~_j; h_i = 1 when the running product of
                 a~ is ever <= sim_half (read before moment matching). Then G_i /= mean(G), X_i /= mean(X).
  F-sim-unit     u_i = (1 + mu)(1 + k y) G_i - 1 - [ s R_U + (1 - s)((1 + r_EUR) X_i - 1) ] - c
  F-sim-benefit  D_i = w V u_i
  F-sim-mean     E[D] = w V [ (1 + mu)(1 + k y) - 1 - R_B - c ],  R_B = s R_U + (1 - s) r_EUR. Closed form; split into
                 carry w V (k y - R_B), of which the euro leg w V (1 - s)(R_U - r_EUR); price w V mu (1 + k y);
                 costs -w V c.
  F-sim-drift    value flat mu_v = 1/(1 + g) - 1; money mu_m = (1 + m)/(1 + g) - 1; prices mu_p = (1 + pi)/(1 + g) - 1,
                 one growth basis g = g_365 for all three.
  F-sim-dist     sorted u; type-7 quantiles; p05, median, p95; ES95 = -mean(worst N/20); dot k = S[(2k - 1) N/200].
  F-sim-be       mean break-even mu_E* = (1 + R_B + c)/(1 + k y) - 1; per path mu_i = (1 + R_B,i + c)/((1 + k y) G_i) - 1,
                 median break-even = type-7 median of mu_i; break-even cost c* = (1 + mu)(1 + k y) - 1 - R_B.
  F-sim-budget   w_b = R_B / ES95_unit; ES95 in years of the whole desk's interest = ES95 / (V R_B).
  F-sim-liq      days = w V / (p x median daily USD volume); Coinbase volume_base x close, Binance volume_quote (USDT);
                 instant-pool cover = nslp_usd / (w V); LST value = stNEAR and LiNEAR supply x own NEAR price x close.
  F-sim-day      one-day 99% historical VaR and 97.5% ES of the NEAR part from the window's raw a_d - 1.
  F-sim-block    sensitivity only: circular blocks of sim_block_days joint days.
  F-sim-lognormal sensitivity only: G = exp(sigma Z) with the pool's annual log variance, moment matched.
  F-sim-seeds    the spread of each simulated figure across sim_seed_checks; it sets the figure's display step.

Display rule shared with site/assets/sim.js: a figure is rounded half away from zero on a scaled integer before it is
formatted, so both languages print the same string. The browser twin reads site/data/sim.json (written here) and
never calls pow, exp or log; Python simulates from the same doubles the payload carries.
"""

from __future__ import annotations

import math
import os
import sys
import tomllib
import weakref
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from pipeline import config
from pipeline.chartspec import Chart, Query, Series, dump
from pipeline.models import load
from pipeline.models.macro import constants, constants_path, derived_query, fred_query, iso, upstream
from pipeline.models.market import near_prices, near_query
from pipeline.models.params import params
from pipeline.numbers import Registry, fmt

M32 = 0xFFFFFFFF
SIM_JSON = "site/data/sim.json"
SIM_FILE = "pipeline/models/portfolio.py"
COINBASE = "near_usd_coinbase_daily"
BINANCE = "NEARUSDT_binance_daily"
ESTR = "ECBESTRVOLWGTTRMDMNRT"
UNITS = {"usd": "USD", "pct": "%", "pctstep": "%", "count": "count", "countstep": "count", "days": "days"}
# the c30 plot's geometry and the controls' ranges (sim.js reads both from the payload; it types no number);
# flip: a marker further right than this share of the width ends its label at its line instead of starting it there
PLOT = {"w": 640, "r": 5, "pad": 12, "top": 26, "bottom": 30, "ten": 10, "flip": 0.55}
# settle_ms: the live headline waits this long after the last input; part: the participation range [min, max, step]
UI = {"settle_ms": 400, "mu_step": 0.1, "part": [1, 100, 1], "cost_max": 1000}


class Missing(Exception):
    """An input the simulation needs is absent: no sim_* key is registered and the desk is not shown."""


# =========================================================================== PRNG (xoshiro128** seeded by splitmix32)

def seed_state(seed: int) -> list[int]:
    """Four splitmix32 outputs from the seed: the xoshiro128** state."""
    a, out = seed & M32, []
    for _ in range(4):
        a = (a + 0x9E3779B9) & M32
        t = a ^ (a >> 16)
        t = (t * 0x21F0AAAD) & M32
        t ^= t >> 15
        t = (t * 0x735A2D97) & M32
        t ^= t >> 15
        out.append(t)
    if not any(out):
        raise ValueError("xoshiro128** state may not be all zero")
    return out


class Prng:
    def __init__(self, seed: int) -> None:
        self.s = seed_state(seed)

    def next(self) -> int:
        s0, s1, s2, s3 = self.s
        r = (s1 * 5) & M32
        r = ((((r << 7) | (r >> 25)) & M32) * 9) & M32
        t = (s1 << 9) & M32
        s2 ^= s0
        s3 ^= s1
        s1 ^= s2
        s0 ^= s3
        s2 ^= t
        s3 = ((s3 << 11) | (s3 >> 21)) & M32
        self.s = [s0, s1, s2, s3]
        return r


def prng_vector(seed: int, n: int) -> list[int]:
    p = Prng(seed)
    return [p.next() for _ in range(n)]


# =========================================================================== shared rounding and display

def sig12(x: float) -> float:
    return float(f"{x:.12g}")


def round_dp(v: float, dp: int) -> float:
    """Round half away from zero to dp decimals (the rule sim.js uses)."""
    k = 10 ** dp
    n = math.floor(abs(v) * k + 0.5)
    out = (-n if v < 0 else n) / k
    return 0.0 if out == 0 else out


def round_step(v: float, step: float) -> float:
    """Round half away from zero to a whole multiple of step."""
    n = math.floor(abs(v) / step + 0.5)
    out = float((-n if v < 0 else n) * step)
    return 0.0 if out == 0 else out


def usd_dp(step: float) -> int:
    """Decimals of a millions display for a dollar step (at least a million -> 0, a hundred thousand -> 1, else 2)."""
    return 0 if step >= 1_000_000 else 1 if step >= 100_000 else 2


def first_above(ladder: list, need: float) -> float:
    for s in ladder:
        if s > need:
            return s
    return ladder[-1]


def shown(value: float, spec: list) -> tuple[float, int | None]:
    """(rounded value, registry decimals) for a concrete display spec."""
    kind, arg = spec
    if kind == "usd":
        v = round_step(value, arg)
        return v, (usd_dp(arg) if abs(v) >= 1_000_000 else None)
    if kind in ("pctstep", "countstep"):
        return round_step(value, arg), 0
    return round_dp(value, arg), arg


def display(value: float, spec: list, absolute: bool = False) -> str:
    v, dp = shown(value, spec)
    return fmt(v, UNITS[spec[0]], dp, absolute=absolute)


def direction(v: float) -> str:
    return "ahead" if v > 0 else "behind" if v < 0 else "level"


def rise(v: float) -> str:
    return "rise" if v > 0 else "fall" if v < 0 else "level"


# =========================================================================== statistics (explicit left-to-right loops)

def total(xs) -> float:
    s = 0.0
    for x in xs:
        s += x
    return s


def mean(xs) -> float:
    return total(xs) / len(xs)


def q7(sorted_xs, p: float) -> float:
    n = len(sorted_xs)
    h = (n - 1) * p
    f = math.floor(h)
    if f + 1 >= n:
        return sorted_xs[n - 1]
    return sorted_xs[f] + (h - f) * (sorted_xs[f + 1] - sorted_xs[f])


def median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


# =========================================================================== pools

@dataclass
class Pool:
    days: int
    first: date
    a: list[float]     # raw daily price ratios
    b: list[float]     # raw euro ratios (1 off fixing days)
    at: list[float]    # gross-demeaned
    bt: list[float]
    n_fix: int


def build_pool(close: dict[date, float], fx: list[tuple[date, float]], end: date, days: int) -> Pool:
    """F-sim-pool over the calendar days (end - days, end]."""
    dates = [end - timedelta(days=days - 1 - i) for i in range(days)]
    fx_at = dict(fx)
    fx_dates = [d for d, _ in fx]
    a, b, n_fix = [], [], 0
    for d in dates:
        p1, p0 = close.get(d), close.get(d - timedelta(days=1))
        if not p1 or not p0:
            raise Missing(f"NEAR close missing on {iso(d)} or the day before")
        a.append(p1 / p0)
        if d in fx_at:
            prev = [x for x in fx_dates if x < d]
            if not prev:
                raise Missing(f"no DEXUSEU fixing before {iso(d)}")
            b.append(fx_at[d] / fx_at[prev[-1]])
            n_fix += 1
        else:
            b.append(1.0)
    ma, mb = mean(a), mean(b)
    return Pool(days, dates[0], a, b, [x / ma for x in a], [x / mb for x in b], n_fix)


# =========================================================================== engines

def simulate(windows: list[tuple[list[float], list[float]]], n_paths: int, horizon: int, seed: int,
             half: float) -> list[dict]:
    """F-sim-path for two windows from one stream: one u32 a day serves both, path-major, day-minor."""
    s0, s1, s2, s3 = seed_state(seed)
    (a1, b1), (a2, b2) = windows
    n1, n2 = len(a1), len(a2)
    G1, X1, G2, X2 = [0.0] * n_paths, [0.0] * n_paths, [0.0] * n_paths, [0.0] * n_paths
    H1, H2 = [0] * n_paths, [0] * n_paths
    for i in range(n_paths):
        g1 = x1 = g2 = x2 = 1.0
        h1 = h2 = 0
        for _ in range(horizon):
            r = (s1 * 5) & M32
            r = ((((r << 7) | (r >> 25)) & M32) * 9) & M32
            t = (s1 << 9) & M32
            s2 ^= s0
            s3 ^= s1
            s1 ^= s2
            s0 ^= s3
            s2 ^= t
            s3 = ((s3 << 11) | (s3 >> 21)) & M32
            j = (r * n1) >> 32
            g1 *= a1[j]
            x1 *= b1[j]
            if g1 <= half:
                h1 = 1
            j = (r * n2) >> 32
            g2 *= a2[j]
            x2 *= b2[j]
            if g2 <= half:
                h2 = 1
        G1[i], X1[i], G2[i], X2[i], H1[i], H2[i] = g1, x1, g2, x2, h1, h2
    return [matched(G1, X1, H1), matched(G2, X2, H2)]


def matched(G: list[float], X: list[float], H: list[int] | None) -> dict:
    mg, mx = mean(G), mean(X)
    G = [g / mg for g in G]
    return {"G": G, "X": [x / mx for x in X], "H": H, "Gs": sorted(G)}


def simulate_blocks(a: list[float], b: list[float], n_paths: int, horizon: int, seed: int, block: int) -> dict:
    """F-sim-block: circular blocks of `block` joint days (sensitivity rows only)."""
    rng = Prng(seed)
    n = len(a)
    G, X = [0.0] * n_paths, [0.0] * n_paths
    for i in range(n_paths):
        g = x = 1.0
        left = horizon
        while left > 0:
            start = (rng.next() * n) >> 32
            for k in range(min(block, left)):
                j = (start + k) % n
                g *= a[j]
                x *= b[j]
            left -= block
        G[i], X[i] = g, x
    return matched(G, X, None)


def simulate_lognormal(a: list[float], n_paths: int, horizon: int, seed: int) -> dict:
    """F-sim-lognormal: G = exp(sigma Z), sigma^2 = horizon x var(log a~), euro flat (sensitivity rows only)."""
    logs = [math.log(x) for x in a]
    m = mean(logs)
    sigma = math.sqrt(total((v - m) ** 2 for v in logs) / (len(logs) - 1) * horizon)
    rng = Prng(seed)
    G: list[float] = []
    while len(G) < n_paths:
        u1 = (rng.next() + 1) / 4294967297
        u2 = rng.next() / 4294967296
        rad = math.sqrt(-2.0 * math.log(u1))
        for z in (rad * math.cos(2 * math.pi * u2), rad * math.sin(2 * math.pi * u2)):
            if len(G) < n_paths:
                G.append(math.exp(sigma * z))
    return matched(G, [1.0] * n_paths, None)


def _workers(n_jobs: int) -> int:
    """Processes for the independent runs. Results never depend on it; only `python -m` and pytest entry points
    fan out (a spawned child re-imports the main script, which an unguarded script would run again)."""
    env = os.environ.get("SIM_WORKERS")
    if env:
        return max(1, int(env))
    main = sys.modules.get("__main__")
    safe = getattr(main, "__spec__", None) is not None or Path(getattr(main, "__file__", "") or "").name in (
        "pytest", "py.test")
    return min(n_jobs, os.cpu_count() or 1) if safe else 1


def run_jobs(jobs: list[tuple]) -> list:
    """Each (function, *args) job, in order; in parallel processes when allowed, serially otherwise."""
    if _workers(len(jobs)) > 1:
        try:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor
            ctx = multiprocessing.get_context("spawn")
            with ProcessPoolExecutor(max_workers=_workers(len(jobs)), mp_context=ctx) as ex:
                futures = [ex.submit(fn, *args) for fn, *args in jobs]
                return [f.result() for f in futures]
        except Exception:   # noqa: BLE001 - a sandbox without process support runs the same jobs serially
            pass
    return [fn(*args) for fn, *args in jobs]


# =========================================================================== one control state

@dataclass
class Rates:
    RU: float      # dollar overnight, annual effective (EFFR)
    RT: float      # one-year Treasury, annual effective (DGS1, bond-equivalent)
    RF: float      # fed funds compounded over the last year (ffcomp_365)
    rE: float      # euro overnight, annual effective (ESTR)
    y: float       # liquid-staking yield, last year
    y90: float
    ys60: float


@dataclass
class State:
    w: float = 0.0
    s: float = 1.0
    mu: float = 0.0
    k: int = 1
    c: float = 0.0
    p: float = 0.1        # participation
    win: int = 0          # window index
    venue: str = "cb"     # cb | all
    usd: str = "effr"     # effr | dgs1 | ffcomp (sensitivity rows)
    eur: str = "flat"     # flat | parity (sensitivity rows)
    y: str = "y"          # y | y90 | ys60 (sensitivity rows)


def legs(st: State, R: Rates) -> tuple[float, float, float]:
    """(dollar leg rate, euro leg factor 1 + r, yield) for a state."""
    ru = {"effr": R.RU, "dgs1": R.RT, "ffcomp": R.RF}[st.usd]
    eur_f = 1 + ru if st.eur == "parity" else 1 + R.rE
    y = {"y": R.y, "y90": R.y90, "ys60": R.ys60}[st.y]
    return ru, eur_f, y


def closed_form(st: State, R: Rates, V: float) -> dict:
    """F-sim-mean, its decomposition and the exact break-evens (sim.js: closed())."""
    ru, eur_f, y = legs(st, R)
    ky = st.k * y
    near_f = (1 + st.mu) * (1 + ky)
    rbar = st.s * ru + (1 - st.s) * (eur_f - 1)
    part = st.w * V
    return {
        "near_f": near_f, "rbar": rbar, "part": part,
        "mean": part * (near_f - 1 - rbar - st.c),
        "carry": part * (ky - rbar),
        "euro": part * ((1 - st.s) * (ru - (eur_f - 1))),
        "price": part * (st.mu * (1 + ky)),
        "cost": part * (0 - st.c),
        "be_mu_mean": (1 + rbar + st.c) / (1 + ky) - 1,
        "be_cost": near_f - 1 - rbar,
        "interest": V * rbar,
    }


def units(sim: dict, st: State, R: Rates) -> tuple[list[float], list[float]]:
    """Sorted u_i (F-sim-unit) and sorted per-path break-even drifts mu_i (F-sim-be)."""
    ru, eur_f, y = legs(st, R)
    ky = st.k * y
    near_f = (1 + st.mu) * (1 + ky)
    s_ru = st.s * ru
    oms = 1 - st.s
    c = st.c
    G, X = sim["G"], sim["X"]
    u, m = [0.0] * len(G), [0.0] * len(G)
    for i in range(len(G)):
        rb = s_ru + oms * (eur_f * X[i] - 1)
        u[i] = near_f * G[i] - 1 - rb - c
        m[i] = (1 + rb + c) / ((1 + ky) * G[i]) - 1
    u.sort()
    m.sort()
    return u, m


def distribution(S: list[float], M: list[float], tail: dict) -> dict:
    n = len(S)
    if n % (2 * tail["dots"]):
        raise ValueError("sim_paths must be a multiple of twice sim_dots")
    worst, deep = n // tail["es_every"], n // tail["es_deep_every"]
    dots = [S[(2 * k - 1) * n // (2 * tail["dots"])] for k in range(1, tail["dots"] + 1)]
    behind = 0
    while behind < n and S[behind] <= 0:
        behind += 1
    ahead_n = 0
    for d in dots:
        if d > 0:
            ahead_n += 1
    return {
        "p05": q7(S, tail["lo"]), "median": q7(S, tail["mid"]), "p95": q7(S, tail["hi"]),
        "es95": 0 - total(S[:worst]) / worst, "es975": 0 - total(S[:deep]) / deep,
        "p_beat": 100 * (n - behind) / n, "dots": dots, "ahead_n": ahead_n,
        "be_mu_median": q7(M, tail["mid"]),
    }


# =========================================================================== display specs

def specs() -> dict:
    """Every live figure's display spec. ["usd*", key] / ["pct*", key] / ["count*", key] take their step from the
    seed spread (resolved per state by resolve())."""
    exact = params()["sim_usd_step_exact"]
    out = {k: ["usd", exact] for k in (
        "sim_near_usd", "sim_cash_interest_usd", "sim_mean_usd", "sim_carry_usd", "sim_carry_euro_usd",
        "sim_price_usd", "sim_cost_usd", "sim_mean_value_flat_usd", "sim_mean_flat_usd", "sim_mean_thesis_usd",
        "sim_mean_thesis_cpi_usd", "sim_mean_parity_usd", "sim_mean_dollar_only_usd", "sim_mean_dgs1_usd",
        "sim_mean_dgs1_parity_usd", "sim_day_swing_usd", "sim_var1d99_usd", "sim_es1d975_usd")}
    out.update({"sim_median_usd": ["usd*", "median"], "sim_p05_usd": ["usd*", "p05"],
                "sim_p95_usd": ["usd*", "p95"], "sim_es95_usd": ["usd*", "es95"],
                "sim_be_mu_median": ["pct*", "be_median"], "sim_median_price": ["pct*", "price"],
                "sim_p_beat_r": ["count*", "p_beat"], "sim_p_half": ["count*", "p_half"]})
    out.update({
        "sim_share": ["pct", 1], "sim_split_usd": ["pct", 0], "sim_split_eur": ["pct", 0], "sim_mu": ["pct", 2],
        "sim_participation": ["pct", 0], "sim_be_mu_mean": ["pct", 2], "sim_be_cost": ["pct", 2],
        "sim_es_desk_pct": ["pct", 1], "sim_nslp_cover": ["pct", 1], "sim_lst_share": ["pct", 1],
        "sim_p_beat_n": ["count", 0], "sim_p_behind_n": ["count", 0], "sim_es_interest_years": ["count", 1],
        "sim_exit_days": ["days", 1], "sim_exit_days_p": ["days", 0], "sim_exit_days_short": ["days", 1],
    })
    return out


def resolve(spec: list, need: dict, steps: dict) -> list:
    """A concrete spec: the dynamic kinds take the first ladder step above the spread they need."""
    kind, arg = spec
    if kind == "usd*":
        return ["usd", first_above(steps["usd"], need[arg])]
    if kind == "pct*":
        return ["pctstep", first_above(steps["pp"], need[arg])]
    if kind == "count*":
        return ["countstep", first_above(steps["count"], need[arg])]
    return spec


def cfg_params() -> dict:
    """What evaluate() reads besides the state: tail levels, window lengths, display ladders and specs."""
    p = params()
    return {"tail": tail_cfg(), "days": list(p["sim_windows"]), "specs": specs(),
            "steps": {"usd": p["sim_steps_usd"], "pp": p["sim_steps_pp"], "count": p["sim_steps_count"]}}


def cfg_payload(P: dict) -> dict:
    """The same, from a sim.json payload (the parity test runs the twin on payloads it builds)."""
    return {"tail": P["tail"], "days": [w["days"] for w in P["windows"]], "specs": P["specs"], "steps": P["steps"]}


DIRS = {"sim_mean_dir": "sim_mean_usd", "sim_median_dir": "sim_median_usd", "sim_p05_dir": "sim_p05_usd",
        "sim_p95_dir": "sim_p95_usd", "sim_mean_value_flat_dir": "sim_mean_value_flat_usd",
        "sim_mean_flat_dir": "sim_mean_flat_usd",
        "sim_mean_thesis_dir": "sim_mean_thesis_usd", "sim_mean_thesis_cpi_dir": "sim_mean_thesis_cpi_usd",
        "sim_mean_parity_dir": "sim_mean_parity_usd", "sim_mean_dollar_only_dir": "sim_mean_dollar_only_usd",
        "sim_mean_dgs1_dir": "sim_mean_dgs1_usd", "sim_mean_dgs1_parity_dir": "sim_mean_dgs1_parity_usd"}
RISE = {"sim_be_mu_mean_dir": "sim_be_mu_mean", "sim_be_mu_median_dir": "sim_be_mu_median",
        "sim_be_cost_dir": "sim_be_cost"}
BRACKET = (("sim_mean_value_flat_usd", "value", None, None, None), ("sim_mean_flat_usd", "flat", None, None, None),
           ("sim_mean_thesis_usd", "thesis", None, None, None),
           ("sim_mean_thesis_cpi_usd", "cpi", None, None, None), ("sim_mean_parity_usd", None, None, "parity", None),
           ("sim_mean_dollar_only_usd", None, None, None, 1.0), ("sim_mean_dgs1_usd", None, "dgs1", None, None),
           ("sim_mean_dgs1_parity_usd", None, "dgs1", "parity", None))


# =========================================================================== inputs

@dataclass
class Inputs:
    end: date
    pools: list[Pool]
    fx_last: date
    rates: Rates
    raw: dict
    mu: dict
    V: float
    split: float
    split_basis: str
    estr_basis: str
    participation: float
    share_cfg: dict
    split_step: float
    mu_bounds: tuple[float, float]
    vol: dict
    lst_mcap: float
    sigma_d: float
    files: dict
    day: list = None     # one_day() of each window (the payload carries var99 and es975)


def _need(reg: Registry, key: str) -> Any:
    v, why = upstream(reg, key)
    if v is None:
        raise Missing(why or key)
    return v


def _obs_on_or_before(series_id: str, end: date) -> tuple[float, date, list[str]]:
    s = load.fred(series_id)
    obs = [(d, v) for d, v in s.points if v is not None and d <= end]
    if not obs:
        raise Missing(f"FRED {series_id} has no observation on or before {iso(end)}")
    return obs[-1][1], obs[-1][0], s.files


def _volumes(end: date, days: int) -> tuple[float, float, list[str]]:
    lo, hi = (end - timedelta(days=days - 1)).isoformat(), end.isoformat()
    cb, cbf = load.csv_rows("market", COINBASE)
    bn, bnf = load.csv_rows("market", BINANCE)
    cbv = [float(r["volume_base"]) * float(r["close"]) for r in cb
           if lo <= r["date"] <= hi and r.get("volume_base") and r.get("close")]
    bnv = [float(r["volume_quote"]) for r in bn if lo <= r["date"] <= hi and r.get("volume_quote")]
    if len(cbv) < days or len(bnv) < days:
        raise Missing(f"daily volume missing inside the last {days} days")
    return median(cbv), median(bnv), cbf + bnf


def scenario() -> dict:
    ds = constants().get("desk_scenario")
    if not isinstance(ds, dict):
        raise Missing("thesis/constants.toml has no [desk_scenario] tables")
    for part in ("size", "share", "split", "participation", "mu_bounds"):
        if not isinstance(ds.get(part), dict):
            raise Missing(f"thesis/constants.toml has no [desk_scenario.{part}]")
    return ds


def gather(reg: Registry) -> Inputs:
    p = params()
    end = date.fromisoformat(_need(reg, "latest_common_date"))
    prices = near_prices()
    close = {d: v for d, v in prices.points if v}
    eur = load.fred("DEXUSEU")
    fx = [(d, v) for d, v in eur.points if v is not None and d <= end]
    if not fx:
        raise Missing("no DEXUSEU fixing on or before the window end")
    pools = [build_pool(close, fx, end, days) for days in p["sim_windows"]]

    effr = _need(reg, "effr_latest")
    try:
        estr, estr_day, estr_files = _obs_on_or_before(ESTR, end)
        estr_basis, estr_from = "estr", f"{ESTR} on {iso(estr_day)}, the latest on or before latest_common_date"
    except Missing:
        # no ESTR file: the euro-area policy rate pay.py registers as pol_eur_latest (F-policy-latest). The pay model
        # runs after this one, so its reader is called here rather than its key read; nothing is recomputed.
        from pipeline.models import pay
        estr, _, pol_inputs, estr_files, _ = pay.rate("EUR")
        if estr is None:
            raise
        estr_basis, estr_from = "policy", f"pol_eur_latest (euro-area policy rate): {', '.join(pol_inputs)}"
    dgs1, dgs1_day, dgs1_files = _obs_on_or_before("DGS1", end)
    raw = {k: _need(reg, k) for k in ("y_lst_365", "y_lst_90", "y_s60", "ffcomp_365", "g_365", "m2_yoy", "cpi_yoy",
                                      "rv_365_near", "sigma_d", "nslp_usd", "near_close_latest", "unstake_hours_lo",
                                      "unstake_hours_hi", "cut_date")}
    raw.update(effr=effr, estr=estr, estr_from=estr_from, dgs1=dgs1, dgs1_day=dgs1_day)
    if date.fromisoformat(raw["cut_date"]) >= end - timedelta(days=p["sim_horizon_days"] - 1):
        raise Missing("the liquid-staking yield window starts before the issuance cut")

    rates = Rates(
        RU=sig12((1 + effr / 36000) ** 365 - 1), RT=sig12((1 + dgs1 / 200) ** 2 - 1),
        RF=sig12(raw["ffcomp_365"] / 100), rE=sig12((1 + estr / 36000) ** 365 - 1),
        y=sig12(raw["y_lst_365"] / 100), y90=sig12(raw["y_lst_90"] / 100), ys60=sig12(raw["y_s60"] / 100))
    g = raw["g_365"] / 100
    mu = {"flat": 0.0, "value": sig12(1 / (1 + g) - 1), "thesis": sig12((1 + raw["m2_yoy"] / 100) / (1 + g) - 1),
          "cpi": sig12((1 + raw["cpi_yoy"] / 100) / (1 + g) - 1)}

    ds = scenario()
    usd_t = reg.value("fx_usd_turnover") if "fx_usd_turnover" in reg else None
    eur_t = reg.value("fx_eur_turnover") if "fx_eur_turnover" in reg else None
    step = ds["split"]["step_pct"]
    if usd_t and eur_t:
        split, split_basis = round_dp(100 * usd_t / (usd_t + eur_t) / step, 0) * step / 100, "turnover"
    else:
        split, split_basis = float(ds["split"]["value"]), "scenario"

    vol, vol_files = {}, []
    for days in p["sim_windows"]:
        cbm, bnm, vol_files = _volumes(end, days)
        vol[(days, "cb")], vol[(days, "bn")] = cbm, bnm

    snap, snap_files = load.json_file("rpc", "snapshot_now")
    lst = (snap or {}).get("lst") or {}
    supplies = {f.get("symbol"): f.get("total_supply") for f in (snap or {}).get("ft_supplies", [])}
    if not (lst.get("stnear_price") and lst.get("linear_price") and supplies.get("STNEAR") and supplies.get("LINEAR")):
        raise Missing("rpc/snapshot_now.json lacks stNEAR or LiNEAR supply and price")
    lst_near = supplies["STNEAR"] * lst["stnear_price"] + supplies["LINEAR"] * lst["linear_price"]
    files = {"near": prices.files, "fx": eur.files, "effr": reg.get("effr_latest").source_files, "estr": estr_files,
             "dgs1": dgs1_files, "vol": vol_files, "snap": snap_files, "y": reg.get("y_lst_365").source_files,
             "macro": reg.get("m2_yoy").source_files + reg.get("g_365").source_files, "consts": [constants_path()],
             "nslp": reg.get("nslp_usd").source_files}
    return Inputs(end=end, pools=pools, fx_last=fx[-1][0], rates=rates, raw=raw, mu=mu, V=float(ds["size"]["value"]),
                  split=split, split_basis=split_basis, estr_basis=estr_basis,
                  participation=float(ds["participation"]["value"]), share_cfg=ds["share"], split_step=step,
                  mu_bounds=(ds["mu_bounds"]["lo"] / 100, ds["mu_bounds"]["hi"] / 100), vol=vol,
                  lst_mcap=lst_near * raw["near_close_latest"], sigma_d=sig12(raw["sigma_d"] / 100), files=files,
                  day=[one_day(pool) for pool in pools])


def tail_cfg() -> dict:
    p = params()
    return {"lo": p["sim_tail_lo"], "mid": p["sim_tail_mid"], "hi": p["sim_tail_hi"], "es_every": p["sim_es_every"],
            "es_deep_every": p["sim_es_deep_every"], "dots": p["sim_dots"]}


def one_day(pool: Pool) -> dict:
    """F-sim-day on the window's raw daily moves (per dollar of the NEAR part)."""
    p = params()
    moves = sorted(x - 1 for x in pool.a)
    n_es = math.floor(len(moves) * (1 - p["sim_day_es"]) + 1e-9)
    return {"var99": sig12(0 - q7(moves, 1 - p["sim_day_var"])), "es975": sig12(0 - total(moves[:n_es]) / n_es),
            "n": len(moves), "n_es": n_es}


# =========================================================================== outcomes for one state (sim.js mirrors it)

def evaluate(sims: list[dict], inp: Inputs, st: State, spread: list[dict] | None, cfg: dict | None = None) -> dict:
    """Every live figure for one control state, raw, plus the concrete display spec of each."""
    cfg = cfg or cfg_params()
    R, V, tail, windows = inp.rates, inp.V, cfg["tail"], cfg["days"]
    sim = sims[st.win]
    cf = closed_form(st, R, V)
    S, Mu = units(sim, st, R)
    dist = distribution(S, Mu, tail)
    part, near_f = cf["part"], cf["near_f"]
    cb, bn = inp.vol[(windows[0], "cb")], inp.vol[(windows[0], "bn")]
    cbs, bns = inp.vol[(windows[1], "cb")], inp.vol[(windows[1], "bn")]
    vol = cb + bn if st.venue == "all" else cb
    vol_short = cbs + bns if st.venue == "all" else cbs
    day = inp.day[st.win]
    half = 0
    for h in sim["H"] or []:
        half += h
    out = {
        "sim_share": 100 * st.w, "sim_near_usd": part, "sim_cash_interest_usd": cf["interest"],
        "sim_split_usd": 100 * st.s, "sim_split_eur": 100 * (1 - st.s), "sim_mu": 100 * st.mu,
        "sim_participation": 100 * st.p,
        "sim_mean_usd": cf["mean"], "sim_carry_usd": cf["carry"], "sim_carry_euro_usd": cf["euro"],
        "sim_price_usd": cf["price"], "sim_cost_usd": cf["cost"],
        "sim_median_usd": part * dist["median"], "sim_p05_usd": part * dist["p05"], "sim_p95_usd": part * dist["p95"],
        "sim_es95_usd": part * dist["es95"],
        "sim_median_price": 100 * ((1 + st.mu) * q7(sim["Gs"], tail["mid"]) - 1),
        "sim_p_beat_n": dist["ahead_n"], "sim_p_behind_n": tail["dots"] - dist["ahead_n"], "sim_p_beat_r": dist["p_beat"],
        "sim_p_half": 100 * half / len(S),
        "sim_be_mu_mean": 100 * cf["be_mu_mean"], "sim_be_mu_median": 100 * dist["be_mu_median"],
        "sim_be_cost": 100 * cf["be_cost"],
        "sim_es_interest_years": part * dist["es95"] / cf["interest"] if cf["interest"] > 0 else 0.0,
        "sim_es_desk_pct": 100 * st.w * dist["es95"],
        "sim_exit_days": part / vol, "sim_exit_days_p": part / (st.p * vol), "sim_exit_days_short": part / vol_short,
        "sim_nslp_cover": 100 * inp.raw["nslp_usd"] / part if part > 0 else 0.0,
        "sim_lst_share": 100 * part / inp.lst_mcap,
        "sim_day_swing_usd": part * inp.sigma_d, "sim_var1d99_usd": part * day["var99"],
        "sim_es1d975_usd": part * day["es975"],
    }
    for key, mu_key, usd, eur, s in BRACKET:
        alt = replace(st, mu=inp.mu[mu_key] if mu_key else st.mu, usd=usd or st.usd, eur=eur or st.eur,
                      s=st.s if s is None else s)
        out[key] = closed_form(alt, R, V)["mean"]
    need = {}
    if spread is not None:
        sp = spread[st.win]
        scale = abs(part * near_f)
        need = {"median": scale * sp["median"], "p05": scale * sp["p05"], "p95": scale * sp["p95"],
                "es95": scale * sp["es95"], "be_median": sp["be_median"], "price": abs(1 + st.mu) * sp["price"],
                "p_beat": sp["p_beat"], "p_half": sp["p_half"]}
    spec = {k: resolve(v, need, cfg["steps"]) if need else v for k, v in cfg["specs"].items()}
    return {"raw": out, "spec": spec, "dots": [part * d for d in dist["dots"]], "dist": dist, "cf": cf, "day": day}


def displays(ev: dict) -> dict:
    return {k: display(v, ev["spec"][k]) for k, v in ev["raw"].items()}


# =========================================================================== the run

@dataclass
class Result:
    inp: Inputs
    sims: list[dict]
    default: State
    w_b: float
    spread: list[dict]
    live: dict
    seeds: list[dict]
    sens: list[dict]
    grid: list[dict]
    corr: float
    rv_engine: float


_RESULTS: "weakref.WeakKeyDictionary[Registry, Result | str]" = weakref.WeakKeyDictionary()


def _spread_of(evs: list[dict], near_f: float, part: float) -> dict:
    """Max minus min across seeds of each simulated figure; dollar figures per dollar of part x near_f."""
    def rng(f) -> float:
        vals = [f(e) for e in evs]
        return max(vals) - min(vals)
    unit = abs(part * near_f)
    return {"median": sig12(rng(lambda e: e["raw"]["sim_median_usd"]) / unit),
            "p05": sig12(rng(lambda e: e["raw"]["sim_p05_usd"]) / unit),
            "p95": sig12(rng(lambda e: e["raw"]["sim_p95_usd"]) / unit),
            "es95": sig12(rng(lambda e: e["raw"]["sim_es95_usd"]) / unit),
            "be_median": sig12(rng(lambda e: e["raw"]["sim_be_mu_median"])),
            "price": sig12(rng(lambda e: e["raw"]["sim_median_price"])),
            "p_beat": sig12(rng(lambda e: e["raw"]["sim_p_beat_r"])),
            "p_half": sig12(rng(lambda e: e["raw"]["sim_p_half"]))}


def default_share(w_b: float, cfg: dict) -> float:
    step = cfg["default_step_bp"]
    bp = math.floor(w_b * 10000 / step) * step
    return min(max(bp, cfg["step_bp"]), cfg["max_bp"]) / 10000


def run(reg: Registry) -> Result:
    p = params()
    inp = gather(reg)
    N, H, seed, half = p["sim_paths"], p["sim_horizon_days"], p["sim_seed"], p["sim_half"]
    if N % (2 * p["sim_dots"]):
        raise ValueError("sim_paths must be a multiple of twice sim_dots")
    wins = [(pool.at, pool.bt) for pool in inp.pools]
    jobs = [(simulate, wins, N, H, s, half) for s in [seed, *p["sim_seed_checks"]]]
    jobs += [(simulate_blocks, pool.at, pool.bt, N, H, seed, p["sim_block_days"]) for pool in inp.pools]
    done = run_jobs(jobs)
    sims, checks, blocks = done[0], done[1:1 + len(p["sim_seed_checks"])], done[1 + len(p["sim_seed_checks"]):]
    logn = [simulate_lognormal(pool.at, N, H, seed) for pool in inp.pools]

    # the interest-budget share at the default state decides the default NEAR share (AD-2)
    base = State(w=1.0, s=inp.split, mu=p["sim_mu_default"], k=1, p=inp.participation)
    S0, M0 = units(sims[0], base, inp.rates)
    w_b = closed_form(base, inp.rates, inp.V)["rbar"] / distribution(S0, M0, tail_cfg())["es95"]
    default = replace(base, w=default_share(w_b, inp.share_cfg))

    # seed spread per window at the default state (F-sim-seeds): it sets each simulated figure's display step
    spread = []
    for win in range(len(inp.pools)):
        st = replace(default, win=win)
        evs = [evaluate(s, inp, st, None) for s in [sims, *checks]]
        cf = closed_form(st, inp.rates, inp.V)
        spread.append(_spread_of(evs, cf["near_f"], cf["part"]))
    live = evaluate(sims, inp, default, spread)
    seeds = [evaluate(s, inp, default, spread) for s in checks]

    sens = []
    for case, win, sim in (("iid", 0, sims), ("iid", 1, sims), ("block", 0, blocks), ("block", 1, blocks),
                           ("lognormal", 0, logn), ("lognormal", 1, logn)):
        sens.append({"case": case, "win": win, "ev": evaluate(sim, inp, replace(default, win=win), spread)})
    for case, change in (("y90", {"y": "y90"}), ("ys60", {"y": "ys60"}), ("dgs1", {"usd": "dgs1"}),
                         ("ffcomp", {"usd": "ffcomp"}), ("parity", {"eur": "parity"}), ("unstaked", {"k": 0})):
        sens.append({"case": case, "win": 0, "ev": evaluate(sims, inp, replace(default, **change), spread)})

    grid = []
    for bp in inp.share_cfg["grid_bp"]:
        for mode in ("value", "flat", "thesis"):
            st = replace(default, w=bp / 10000, mu=inp.mu[mode])
            grid.append({"bp": bp, "mode": mode, "ev": evaluate(sims, inp, st, spread)})

    pool = inp.pools[0]
    pairs = [(a - 1, b - 1) for a, b in zip(pool.a, pool.b) if b != 1.0]
    ma, mb = mean([x for x, _ in pairs]), mean([y for _, y in pairs])
    cov = total((x - ma) * (y - mb) for x, y in pairs)
    corr = cov / math.sqrt(total((x - ma) ** 2 for x, _ in pairs) * total((y - mb) ** 2 for _, y in pairs))
    logs = [math.log(g) for g in sims[0]["G"]]
    ml = mean(logs)
    rv_engine = 100 * math.sqrt(total((v - ml) ** 2 for v in logs) / (len(logs) - 1))
    return Result(inp=inp, sims=sims, default=default, w_b=w_b, spread=spread, live=live, seeds=seeds, sens=sens,
                  grid=grid, corr=corr, rv_engine=rv_engine)


def result(reg: Registry) -> Result | None:
    if reg not in _RESULTS:
        try:
            _RESULTS[reg] = run(reg)
        except Missing as exc:
            _RESULTS[reg] = str(exc)
    r = _RESULTS[reg]
    return r if isinstance(r, Result) else None


def missing_reason(reg: Registry) -> str | None:
    r = _RESULTS.get(reg)
    return r if isinstance(r, str) else None


# =========================================================================== registry

FORMULA = {
    "sim_share": "F-sim-budget", "sim_near_usd": "F-sim-benefit", "sim_cash_interest_usd": "F-sim-mean",
    "sim_split_usd": "F-sim-unit", "sim_split_eur": "F-sim-unit", "sim_mu": "F-sim-drift",
    "sim_participation": "K-desk_scenario", "sim_mean_usd": "F-sim-mean", "sim_carry_usd": "F-sim-mean",
    "sim_carry_euro_usd": "F-sim-mean", "sim_price_usd": "F-sim-mean", "sim_cost_usd": "F-sim-mean",
    "sim_median_usd": "F-sim-dist", "sim_p05_usd": "F-sim-dist", "sim_p95_usd": "F-sim-dist",
    "sim_es95_usd": "F-sim-dist", "sim_median_price": "F-sim-dist", "sim_p_beat_n": "F-sim-dist",
    "sim_p_behind_n": "F-sim-dist", "sim_p_beat_r": "F-sim-dist", "sim_p_half": "F-sim-path",
    "sim_be_mu_mean": "F-sim-be", "sim_be_mu_median": "F-sim-be", "sim_be_cost": "F-sim-be",
    "sim_es_interest_years": "F-sim-budget", "sim_es_desk_pct": "F-sim-dist", "sim_exit_days": "F-sim-liq",
    "sim_exit_days_p": "F-sim-liq", "sim_exit_days_short": "F-sim-liq", "sim_nslp_cover": "F-sim-liq",
    "sim_lst_share": "F-sim-liq", "sim_day_swing_usd": "F-sim-day", "sim_var1d99_usd": "F-sim-day",
    "sim_es1d975_usd": "F-sim-day", "sim_mean_value_flat_usd": "F-sim-mean", "sim_mean_flat_usd": "F-sim-mean",
    "sim_mean_thesis_usd": "F-sim-mean",
    "sim_mean_thesis_cpi_usd": "F-sim-mean", "sim_mean_parity_usd": "F-sim-mean",
    "sim_mean_dollar_only_usd": "F-sim-mean", "sim_mean_dgs1_usd": "F-sim-mean",
    "sim_mean_dgs1_parity_usd": "F-sim-mean",
}
MEANING = {
    "sim_share": "default NEAR share of the desk: the interest-budget share floored to the slider step",
    "sim_near_usd": "the NEAR part in dollars (share x desk)",
    "sim_cash_interest_usd": "a year of interest on the whole desk at today's blended cash rate",
    "sim_mean_usd": "expected benefit of the mix against dollars and euros only (closed form)",
    "sim_carry_usd": "staking yield over the blended cash rate on the NEAR part",
    "sim_carry_euro_usd": "of the carry: euro cash earning less than dollar cash, euro flat on average",
    "sim_price_usd": "the price assumption's share of the expected benefit",
    "sim_cost_usd": "trading and custody costs entered (none by default)",
    "sim_median_usd": "the typical simulated year (median)", "sim_p05_usd": "one year in twenty is worse than this",
    "sim_p95_usd": "one year in twenty is better than this",
    "sim_es95_usd": "average shortfall of the worst five simulated years in a hundred",
    "sim_median_price": "NEAR's price change in the typical simulated year",
    "sim_p_beat_n": "filled dots: simulated years in a hundred that end ahead", "sim_p_behind_n": "hollow dots",
    "sim_p_beat_r": "simulated years in a hundred that end ahead, to the precision the seeds support",
    "sim_p_half": "simulated years in a hundred in which the NEAR part is at some point worth half its entry value",
    "sim_be_mu_mean": "price change at which the expected benefit is zero",
    "sim_be_mu_median": "average price change the mix needs to come out ahead in most years",
    "sim_be_cost": "trading and custody costs that would erase the expected benefit",
    "sim_es_interest_years": "the bad-year average in years of the whole desk's interest",
    "sim_es_desk_pct": "the bad-year average as a share of the whole desk",
    "sim_exit_days": "days to sell the NEAR part at a whole median day's volume, last year",
    "sim_exit_days_p": "days to sell at the participation assumption, last year's volume",
    "sim_exit_days_short": "days to sell at a whole median day's volume, last ninety days",
    "sim_nslp_cover": "the instant-exit pool as a share of the NEAR part",
    "sim_lst_share": "the NEAR part as a share of the liquid-staking market (at most: rNEAR is unpriced)",
    "sim_day_swing_usd": "one ordinary day's move of the NEAR part (one standard deviation)",
}


def _put(reg: Registry, key: str, value: Any, unit: str, formula: str, files, inputs, decimals=None,
         signed: bool = True) -> None:
    reg.put(key, value, unit, formula, source_files=sorted({str(f) for f in files}), inputs=inputs,
            decimals=decimals, signed=signed)


def describe(st: State) -> str:
    return (f"default state: NEAR share {st.w:g}, dollar share {st.s:g}, price change {st.mu:g}, staked {st.k}, "
            f"costs {st.c:g}, window {params()['sim_windows'][st.win]} days, exit venue {st.venue}")


def compute(reg: Registry) -> None:
    r = result(reg)
    if r is None:
        return   # an input is missing: no sim_* key, so the desk is not shown
    p, inp, R, f = params(), r.inp, r.inp.rates, r.inp.files
    near, fx, consts = f["near"], f["fx"], f["consts"]
    w0, w1 = p["sim_windows"]
    sources = near + fx + f["effr"] + f["estr"] + f["y"] + consts
    _put(reg, "estr_latest", inp.raw["estr"], "%", "F-sim-obs", f["estr"], [inp.raw["estr_from"]])
    _put(reg, "dgs1_latest", inp.raw["dgs1"], "%", "F-sim-obs", f["dgs1"],
         [f"DGS1 on {iso(inp.raw['dgs1_day'])}, the latest on or before latest_common_date"])
    _put(reg, "sim_r_usd", 100 * R.RU, "%", "F-sim-rate", f["effr"], ["effr_latest", "(1 + r/36000)^365 - 1"])
    _put(reg, "sim_r_eur", 100 * R.rE, "%", "F-sim-rate", f["estr"], ["estr_latest", "(1 + r/36000)^365 - 1"])
    _put(reg, "sim_r_dgs1", 100 * R.RT, "%", "F-sim-tbill", f["dgs1"], ["dgs1_latest", "(1 + y/200)^2 - 1"])
    _put(reg, "sim_y", 100 * R.y, "%", "F-sim-rate", f["y"], ["y_lst_365: average of stNEAR and LiNEAR, last year"])
    for key, mode, ins in (("sim_mu_value_flat", "value", ["g_365"]), ("sim_mu_thesis_m2", "thesis", ["m2_yoy", "g_365"]),
                           ("sim_mu_thesis_cpi", "cpi", ["cpi_yoy", "g_365"])):
        _put(reg, key, round_dp(100 * inp.mu[mode], 2), "%", "F-sim-drift", f["macro"], ins, decimals=2)
    _put(reg, "sim_corr_near_eur_365", r.corr, "ratio", "F-sim-pool", near + fx,
         [f"daily NEAR and DEXUSEU moves on fixing days of the last {w0} days (fixings at noon New York, closes "
          "at midnight UTC)"])
    for (days, venue), v in sorted(inp.vol.items()):
        _put(reg, f"sim_vol_med_{days}_{venue}", round_step(v, 100000), "USD", "F-sim-liq", f["vol"],
             [f"median daily {'Coinbase volume_base x close' if venue == 'cb' else 'Binance volume_quote (USDT)'}, "
              f"last {days} days to {iso(inp.end)}"], decimals=1)
    _put(reg, "sim_lst_mcap_usd", round_step(inp.lst_mcap, 100000), "USD", "F-sim-liq", f["snap"] + near,
         ["stNEAR supply x stNEAR price + LiNEAR supply x LiNEAR price, x near_close_latest (rNEAR is unpriced)"],
         decimals=1)
    _put(reg, "sim_desk_usd", inp.V, "USD", "K-desk_scenario", consts, ["desk_scenario.size (scenario)"],
         decimals=0)
    _put(reg, "sim_horizon_days", p["sim_horizon_days"], "days", "P-sim", [], ["rules.toml [params] sim_horizon_days"])
    _put(reg, "sim_paths", p["sim_paths"], "count", "P-sim", [], ["rules.toml [params] sim_paths"])
    _put(reg, "sim_dots", p["sim_dots"], "count", "P-sim", [], ["rules.toml [params] sim_dots"])
    _put(reg, "sim_tail_n", p["sim_dots"] // p["sim_es_every"], "count", "P-sim", [],
         ["sim_dots / sim_es_every: the worst years in a hundred behind the bad-year average"])
    _put(reg, "sim_tail_hi_n", p["sim_dots"] - round(p["sim_tail_hi"] * p["sim_dots"]), "count", "P-sim", [],
         ["sim_dots - sim_tail_hi x sim_dots: the best years in a hundred, better than sim_p95_usd"])
    _put(reg, "sim_window_short", w1, "days", "P-sim", [], ["rules.toml [params] sim_windows"])
    _put(reg, "sim_seed_n", len(p["sim_seed_checks"]), "count", "F-sim-seeds", [], ["rules.toml sim_seed_checks"])
    _put(reg, "sim_fix_days", inp.pools[0].n_fix, "count", "F-sim-pool", fx, [f"DEXUSEU fixings in the last {w0} days"])
    _put(reg, "sim_fix_days_short", inp.pools[1].n_fix, "count", "F-sim-pool", fx,
         [f"DEXUSEU fixings in the last {w1} days"])
    _put(reg, "sim_fx_last", iso(inp.fx_last), "date", "F-sim-pool", fx, ["last DEXUSEU fixing on or before the end"])
    _put(reg, "sim_window_end", iso(inp.end), "date", "F-sim-pool", near, ["latest_common_date"])
    _put(reg, "sim_w_budget", round_dp(100 * r.w_b, 2), "%", "F-sim-budget", sources,
         ["the blended cash rate / the bad-year average per dollar of NEAR, at the default state"], decimals=2)
    _put(reg, "sim_w_one_day", round_dp(100 * inp.vol[(w0, "cb")] / inp.V, 1), "%", "F-sim-liq", f["vol"] + consts,
         [f"sim_vol_med_{w0}_cb / sim_desk_usd: the share one median Coinbase day could absorb"], decimals=1)
    _put(reg, "sim_rv_engine", round_dp(r.rv_engine, 1), "%", "F-sim-path", near,
         ["standard deviation of log G over the simulated years (compare rv_365_near)"], decimals=1)
    tail = r.live["dist"]
    _put(reg, "sim_es975_usd", round_step(tail["es975"] * r.live["cf"]["part"], 100000), "USD", "F-sim-dist", sources,
         ["mean of the worst sim_paths / sim_es_deep_every simulated years"], decimals=1)
    _put(reg, "sim_day_obs", r.live["day"]["n"], "count", "F-sim-day", near, ["daily moves in the window"])
    _put(reg, "sim_day_es_n", r.live["day"]["n_es"], "count", "F-sim-day", near, ["worst days behind the one-day ES"])
    # the exit at the default size on each venue choice (fixed; the live sim_exit_days follows the venue control)
    part, cb, bn = r.live["cf"]["part"], inp.vol[(w0, "cb")], inp.vol[(w0, "bn")]
    for key, vol, words in (("sim_exit_days_cb", cb, "Coinbase"), ("sim_exit_days_all", cb + bn, "Coinbase and Binance")):
        v, dp = shown(part / vol, ["days", 1])
        _put(reg, key, v, "days", "F-sim-liq", f["vol"] + sources,
             [f"sim_near_usd / the median daily volume on {words}, last {w0} days"], decimals=dp, signed=False)

    # every live figure at the default state; sim.js recomputes each and must print the same display
    model = [describe(r.default), "rules.toml [params] sim_seed, sim_paths, sim_horizon_days"]
    for key, value in r.live["raw"].items():
        v, dp = shown(value, r.live["spec"][key])
        files = sources + (f["vol"] + f["snap"] + f["nslp"] if FORMULA[key] == "F-sim-liq" else [])
        _put(reg, key, v, UNITS[r.live["spec"][key][0]], FORMULA[key], files,
             [MEANING.get(key, key.removeprefix("sim_").replace("_", " ")), *model], decimals=dp)
    for dkey, vkey in DIRS.items():
        _put(reg, dkey, direction(reg.value(vkey)), "text", FORMULA[vkey], sources, [vkey])
    for dkey, vkey in RISE.items():
        _put(reg, dkey, rise(reg.value(vkey)), "text", "F-sim-be", sources, [vkey])
    for key, value in modes(r.default, inp, "flat", False).items():
        _put(reg, key, value, "text", "P-sim", consts, ["the default control state"])

    # seed spread (data page): each simulated figure's spread across the seeds, and the step it sets
    for key in ("sim_median_usd", "sim_p05_usd", "sim_p95_usd", "sim_es95_usd", "sim_be_mu_median",
                "sim_p_beat_r", "sim_p_half"):
        vals = [r.live["raw"][key]] + [s["raw"][key] for s in r.seeds]
        spread, spec = max(vals) - min(vals), r.live["spec"][key]
        unit = "USD" if spec[0] == "usd" else "pp" if spec[0] == "pctstep" else "count"
        name = key.removeprefix("sim_")
        seeds = ", ".join(str(s) for s in [p["sim_seed"], *p["sim_seed_checks"]])
        _put(reg, f"sim_mc_spread_{name}", round_step(spread, 1000) if unit == "USD" else round_dp(spread, 1), unit,
             "F-sim-seeds", sources, [f"{key} across seeds {seeds}"],
             decimals=(1 if spread >= 1e6 else None) if unit == "USD" else 1, signed=False)
        _put(reg, f"sim_mc_step_{name}", spec[1], unit, "F-sim-seeds", sources,
             [f"the first step in rules.toml above the spread of {key}"], decimals=(None if unit == "USD" else 0),
             signed=False)


def modes(st: State, inp: Inputs, mu_mode: str, changed: bool) -> dict:
    """The text keys copy variants switch on (sim.js modes() mirrors it)."""
    return {"sim_mu_mode": mu_mode, "sim_stake_mode": "staked" if st.k else "unstaked",
            "sim_cost_mode": "none" if st.c == 0 else "entered", "sim_window_mode": "year" if st.win == 0 else "short",
            "sim_venue_mode": "coinbase" if st.venue == "cb" else "both", "sim_changed": "yes" if changed else "no",
            "sim_split_basis": inp.split_basis, "sim_estr_basis": inp.estr_basis}


CONTROLS = ("w", "s", "mu", "own", "k", "win", "venue", "p", "rt", "cu")


def state_of(ctrl: dict, P: dict) -> State:
    """A control state (the payload's `default` shape) as a model State; sim.js toState() mirrors it."""
    mu = ctrl["own"] if ctrl["mu"] == "own" else P["mu"][ctrl["mu"]]
    return State(w=ctrl["w"], s=ctrl["s"], mu=mu, k=ctrl["k"], c=(ctrl["rt"] + ctrl["cu"]) / P["bp"], p=ctrl["p"],
                 win=ctrl["win"], venue=ctrl["venue"])


def inputs_from_payload(P: dict) -> Inputs:
    """The Inputs evaluate() reads, rebuilt from site/data/sim.json (the parity test simulates from the payload)."""
    w0, w1 = (w["days"] for w in P["windows"])
    L, R = P["liq"], P["rates"]
    return Inputs(end=None, pools=[], fx_last=None, rates=Rates(**R), raw={"nslp_usd": L["nslp"]},
                  mu={k: P["mu"][k] for k in ("flat", "value", "thesis", "cpi")}, V=P["V"], split=P["default"]["s"],
                  split_basis=P["basis"]["split"], estr_basis=P["basis"]["estr"], participation=P["default"]["p"],
                  share_cfg={}, split_step=P["split_step"], mu_bounds=(P["mu"]["lo"], P["mu"]["hi"]),
                  vol={(w0, "cb"): L["cb"], (w0, "bn"): L["bn"], (w1, "cb"): L["cbs"], (w1, "bn"): L["bns"]},
                  lst_mcap=L["lst"], sigma_d=P["sigma_d"], files={}, day=[w["day"] for w in P["windows"]])


def outcomes(sims: list[dict], inp: Inputs, ctrl: dict, P: dict) -> dict:
    """Every live display and text key for one control state (what sim.js prints for it)."""
    st = state_of(ctrl, P)
    ev = evaluate(sims, inp, st, P["spread"], cfg_payload(P))
    out = displays(ev)
    shown_v = {k: shown(v, ev["spec"][k])[0] for k, v in ev["raw"].items()}
    out.update({d: direction(shown_v[v]) for d, v in DIRS.items()})
    out.update({d: rise(shown_v[v]) for d, v in RISE.items()})
    changed = any(ctrl[k] != P["default"][k] for k in CONTROLS if k != "own") or (
        ctrl["mu"] == "own" and ctrl["own"] != P["default"]["own"])
    out.update(modes(st, inp, ctrl["mu"], changed))
    return out


# =========================================================================== charts c30 to c33

SOURCES = ["Binance spot NEARUSDT daily klines", "Coinbase Exchange NEAR-USD daily candles",
           "FRED: DEXUSEU, EFFR, ECBESTRVOLWGTTRMDMNRT, DGS1", "NEAR RPC: stNEAR and LiNEAR rates and supply",
           "thesis/constants.toml [desk_scenario] (scenario values)"]


def _queries() -> list[Query]:
    return [near_query(near_prices()), fred_query("DEXUSEU"), fred_query("EFFR"), fred_query(ESTR), fred_query("DGS1"),
            derived_query("u_i = (1 + mu)(1 + k y) G_i - 1 - [s R_U + (1 - s)((1 + r_EUR) X_i - 1)] - c; D_i = w V u_i; "
                          "iid joint-day bootstrap of the window's daily moves, gross-demeaned, moment-matched")]


def charts(reg: Registry) -> list[Chart]:
    r = result(reg)
    if r is None:
        return []
    return [c30(reg, r), c31(reg, r), c32(reg, r), c33(reg, r)]


def dot_layout(dots: list[float], marks: dict[str, float]) -> dict:
    """c30 geometry (sim.js layout() mirrors it): dots stacked in bins of one diameter on a linear dollar axis."""
    w, r, pad = PLOT["w"], PLOT["r"], PLOT["pad"]
    lo, hi = min(dots[0], 0.0), max(dots[-1], 0.0)
    if hi == lo:
        hi = lo + 1
    span = w - 2 * pad
    x = lambda v: pad + (v - lo) / (hi - lo) * span   # noqa: E731
    bins: dict[int, int] = {}
    out = []
    for v in dots:
        b = math.floor((x(v) - pad) / (2 * r))
        k = bins.get(b, 0)
        bins[b] = k + 1
        out.append([round(pad + (2 * b + 1) * r, 1), k, 1 if v > 0 else 0, k])
    rows = max(bins.values())
    h = PLOT["top"] + rows * 2 * r + PLOT["bottom"]
    base = h - PLOT["bottom"]
    for d in out:
        d[1] = round(base - (2 * d[1] + 1) * r, 1)
    return {"w": w, "h": h, "base": base, "zero": round(x(0.0), 1), "dots": out, "flip": PLOT["flip"],
            "marks": {k: round(x(v), 1) for k, v in marks.items()}}


def c30(reg: Registry, r: Result) -> Chart:
    p = params()
    live = r.live
    spec = live["spec"]["sim_median_usd"]
    G = r.sims[0]["Gs"]
    n = len(G)
    rows, csv_rows = [], []
    for k, d in enumerate(live["dots"], start=1):
        idx = (2 * k - 1) * n // (2 * p["sim_dots"])
        rows.append([k, display(d, spec), f"{G[idx]:.2f}"])
        csv_rows.append([k, round_step(d, 1000), round(G[idx], 4)])
    raw = live["raw"]
    geo = dot_layout(live["dots"], {"median": raw["sim_median_usd"], "mean": raw["sim_mean_usd"],
                                   "p05": raw["sim_p05_usd"], "p95": raw["sim_p95_usd"]})
    chart = Chart(
        id="c30", section="desk", priority="lead", claims=["H7", "H9"], verdicts=[], title="", sentence="",
        form="table", sources=SOURCES, queries=_queries(),
        table={"columns": ["simulated_year", "difference_against_dollars_and_euros", "near_price_factor"],
               "rows": rows,
               "footnote": "Each row is one simulated year in a hundred, from worst to best: the middle year of its "
                           "hundredth of all the simulated years. The price factor is NEAR's price change behind a "
                           "year of that rank, before staking yield."},
        code_ref=f"{SIM_FILE}::c30", formula_ids=["F-sim-path", "F-sim-unit", "F-sim-benefit", "F-sim-dist"],
        x={"type": "linear", "unit": "USD", "layout": geo}, y={"unit": "count"},
        title_template="{sim_dots} simulated years for the {sim_near_usd} NEAR part: ahead in {sim_p_beat_n}, behind "
                       "in {sim_p_behind_n}",
        sentence_template="Against dollars and euros only, with daily moves like the last year's and the price flat "
                          "on average, the typical simulated year ends {sim_median_usd:abs} {sim_median_dir} and the "
                          "average ends {sim_mean_usd:abs} {sim_mean_dir}.",
        notes_templates=["The average is exact (closed form); the typical year and the tails come from {sim_paths} "
                         "simulated years, resampling whole days of NEAR and euro moves together."])
    chart.csv_table = {"columns": ["simulated_year", "difference_usd", "near_price_factor"], "rows": csv_rows}
    return chart.resolve(reg)


MODE_WORDS = {"value": "demand flat in dollars", "flat": "price flat on average",
              "thesis": "demand keeps pace with dollar money"}


def c31(reg: Registry, r: Result) -> Chart:
    p = params()
    typ, avg, rows, csv_rows = [], [], [], []
    for g in r.grid:
        ev = g["ev"]
        raw, sp = ev["raw"], ev["spec"]
        share = display(raw["sim_share"], ["pct", 1])
        part = display(raw["sim_near_usd"], sp["sim_near_usd"])
        if g["mode"] == "flat":
            label = f"{share} · {part}"
            step = sp["sim_median_usd"][1]
            typ.append([label, round_step(raw["sim_median_usd"], step), round_step(raw["sim_p05_usd"], step),
                        round_step(raw["sim_p95_usd"], step)])
            avg.append([label, round_step(raw["sim_mean_usd"], p["sim_usd_step_exact"])])
        rows.append([share, MODE_WORDS[g["mode"]], display(raw["sim_mean_usd"], sp["sim_mean_usd"]),
                     display(raw["sim_median_usd"], sp["sim_median_usd"]),
                     display(raw["sim_p05_usd"], sp["sim_p05_usd"]),
                     f"{display(raw['sim_p_beat_r'], sp['sim_p_beat_r'])} of {p['sim_dots']}",
                     display(raw["sim_exit_days"], sp["sim_exit_days"])])
        csv_rows.append([g["bp"] / 100, MODE_WORDS[g["mode"]], round_step(raw["sim_mean_usd"], 1000),
                         round_step(raw["sim_median_usd"], 1000), round_step(raw["sim_p05_usd"], 1000),
                         round_dp(raw["sim_p_beat_r"], 1), round_dp(raw["sim_exit_days"], 2)])
    chart = Chart(
        id="c31", section="desk", priority="supporting", claims=["H3", "H7"], verdicts=[], title="", sentence="",
        form="dot-range", sources=SOURCES, queries=_queries(),
        table={"columns": ["near_share", "price_assumption", "expected", "typical_year", "one_year_in_twenty_worse",
                           "simulated_years_ahead", "days_to_sell_on_coinbase"], "rows": rows,
               "footnote": "Staked, no costs, daily moves like the last year's. The expected value is exact; the "
                           "other columns come from the simulated years."},
        code_ref=f"{SIM_FILE}::c31", formula_ids=["F-sim-mean", "F-sim-dist", "F-sim-liq"],
        x={"type": "linear", "unit": "USD"}, y={"type": "category", "unit": "USD"},
        series=[Series(id="typical", label="Typical simulated year, with one year in twenty worse and better",
                       entity="near", points=typ, unit="USD"),
                Series(id="expected", label="Expected (exact)", entity="context", points=avg, unit="USD",
                       role="context")],
        reference_lines=[{"x": 0, "label": "Same as dollars and euros only"}],
        title_template="More NEAR scales the outcome, not the odds: ahead in about {sim_p_beat_r} of {sim_dots} "
                       "simulated years at every share",
        sentence_template="Each row is a NEAR share of the {sim_desk_usd} desk with the price flat on average: the dot "
                          "is the typical simulated year, the bar runs from one year in twenty worse to one year in "
                          "twenty better, and the open mark is the exact expected value.")
    chart.csv_table = {"columns": ["near_share_pct", "price_assumption", "expected_usd", "typical_year_usd",
                                   "one_year_in_twenty_worse_usd", "simulated_years_ahead_per_hundred",
                                   "days_to_sell_on_coinbase"], "rows": csv_rows}
    return chart.resolve(reg)


def c32(reg: Registry, r: Result) -> Chart:
    p = params()
    inp, w0, w1 = r.inp, *p["sim_windows"]
    st = r.default
    part = st.w * inp.V
    cb, bn, cbs, bns = inp.vol[(w0, "cb")], inp.vol[(w0, "bn")], inp.vol[(w1, "cb")], inp.vol[(w1, "bn")]
    pp = display(100 * st.p, ["pct", 0])
    d1, d0 = ["days", 1], ["days", 0]
    rows = [
        ["Days to sell on Coinbase at a whole median day's volume, last year", display(part / cb, d1), "measured ratio"],
        ["Days to sell on Coinbase at a whole median day's volume, last ninety days", display(part / cbs, d1),
         "measured ratio"],
        [f"Days to sell on Coinbase at {pp} of each day's volume", display(part / (st.p * cb), d0),
         "assumption (scenario)"],
        ["Days to sell on Coinbase and Binance at a whole median day's volume, last year",
         display(part / (cb + bn), d1), "measured ratio; Binance trades against USDT and is closed to US persons"],
        ["Days to sell on Coinbase and Binance at a whole median day's volume, last ninety days",
         display(part / (cbs + bns), d1), "measured ratio"],
        [f"Days to sell on Coinbase and Binance at {pp} of each day's volume", display(part / (st.p * (cb + bn)), d0),
         "assumption (scenario)"],
        ["Median daily volume, Coinbase, last year", display(cb, ["usd", 100000]), "measured"],
        ["Median daily volume, Coinbase, last ninety days", display(cbs, ["usd", 100000]), "measured"],
        ["Median daily volume, Binance (USDT), last year", display(bn, ["usd", 100000]), "measured"],
        ["Median daily volume, Binance (USDT), last ninety days", display(bns, ["usd", 100000]), "measured"],
        ["Instant-exit pool as a share of the NEAR part", display(100 * inp.raw["nslp_usd"] / part, ["pct", 1]),
         "measured; the pool charges a fee that rises as it drains"],
        ["Unbonding before unstaked NEAR can move", f"{fmt(inp.raw['unstake_hours_lo'], 'hours')} to "
                                                    f"{fmt(inp.raw['unstake_hours_hi'], 'hours')}", "measured"],
        ["NEAR part as a share of the liquid-staking market (at most: rNEAR is unpriced)",
         display(100 * part / inp.lst_mcap, ["pct", 1]), "measured"],
        ["Liquid-staking token depth on exchanges", "not measured", "no approved source"],
    ]
    chart = Chart(
        id="c32", section="desk", priority="supporting", claims=["H6"], verdicts=[], title="", sentence="",
        form="table", sources=SOURCES, queries=_queries(),
        table={"columns": ["measure", "value", "basis"], "rows": rows},
        code_ref=f"{SIM_FILE}::c32", formula_ids=["F-sim-liq"], x={"type": "category"}, y={"unit": "text"},
        title_template="Getting out: about {sim_exit_days} to sell the {sim_near_usd} NEAR part at a whole median "
                       "day of Coinbase volume",
        sentence_template="Selling the NEAR part on Coinbase alone would take about {sim_exit_days} at a whole median "
                          "day's volume, and {sim_exit_days_p} if the desk were {sim_participation} of each day's "
                          "selling, an assumption.")
    chart.csv_table = {"columns": ["measure", "value", "basis"], "rows": rows}
    return chart.resolve(reg)


SENS_LABEL = {
    "iid": "Whole days resampled one at a time (the default)", "block": "Runs of whole days resampled together",
    "lognormal": "A smooth bell curve with the same volatility", "y90": "Staking yield of the last ninety days",
    "ys60": "Staking yield if the staked share reached the tripwire level",
    "dgs1": "Dollar leg at a one-year Treasury", "ffcomp": "Dollar leg at fed funds compounded over the last year",
    "parity": "Euro priced to offset its lower rate (interest parity)", "unstaked": "NEAR held but not staked",
}


def sens_rows(r: Result) -> tuple[list, list]:
    """c33's rows: displays for the page's table, and exact values for the CSV."""
    p = params()
    rows, csv_rows = [], []
    for row in r.sens:
        raw, sp = row["ev"]["raw"], row["ev"]["spec"]
        label = SENS_LABEL[row["case"]]
        if row["case"] in ("iid", "block", "lognormal"):
            label += ", moves like the last year's" if row["win"] == 0 else ", moves like the last ninety days'"
        rows.append([label, display(raw["sim_mean_usd"], sp["sim_mean_usd"]),
                     display(raw["sim_median_usd"], sp["sim_median_usd"]),
                     display(raw["sim_p05_usd"], sp["sim_p05_usd"]), display(raw["sim_p95_usd"], sp["sim_p95_usd"]),
                     f"{display(raw['sim_p_beat_r'], sp['sim_p_beat_r'])} of {p['sim_dots']}",
                     display(0 - raw["sim_es95_usd"], sp["sim_es95_usd"])])
        csv_rows.append([label, round_step(raw["sim_mean_usd"], 1000), round_step(raw["sim_median_usd"], 1000),
                         round_step(raw["sim_p05_usd"], 1000), round_step(raw["sim_p95_usd"], 1000),
                         round_dp(raw["sim_p_beat_r"], 1), round_step(0 - raw["sim_es95_usd"], 1000)])
    return rows, csv_rows


def c33(reg: Registry, r: Result) -> Chart:
    rows, csv_rows = sens_rows(r)
    chart = Chart(
        id="c33", section="desk", priority="supporting", claims=["H3", "H7"], verdicts=[], title="", sentence="",
        form="table", sources=SOURCES, queries=_queries(),
        table={"columns": ["case", "expected", "typical_year", "one_year_in_twenty_worse",
                           "one_year_in_twenty_better", "simulated_years_ahead", "bad_year_average"], "rows": rows,
               "footnote": "Every row keeps the default NEAR share, split, price assumption and costs and changes one "
                           "thing. The expected value is exact; the rest come from simulated years."},
        code_ref=f"{SIM_FILE}::c33",
        formula_ids=["F-sim-mean", "F-sim-dist", "F-sim-block", "F-sim-lognormal", "F-sim-tbill"],
        x={"type": "category"}, y={"unit": "text"},
        title_template="What moves the answer: rates and staking move the expected value, the window moves the spread",
        sentence_template="Each row changes one input from the defaults, where the expected benefit is "
                          "{sim_mean_usd:abs} {sim_mean_dir} and the typical year {sim_median_usd:abs} "
                          "{sim_median_dir}.")
    chart.csv_table = {"columns": ["case", "expected_usd", "typical_year_usd", "one_year_in_twenty_worse_usd",
                                   "one_year_in_twenty_better_usd", "simulated_years_ahead_per_hundred",
                                   "bad_year_average_usd"], "rows": csv_rows}
    return chart.resolve(reg)


# =========================================================================== sim.json (the browser twin's payload)

# the simulation's words, and the desk frame's tile labels, plot labels and assumption line it redraws live
COPY_PREFIXES = ("sim_", "film_tile_", "film_plot_", "film_desk_assumption")
# claim chips on the tiles and the price options (sim.js draws one only when the page holds that claim's block)
CHIPS = {"mean": "H3", "median": "H9", "beat": "H7", "bad": "H7", "exit": "H6", "value": "H12", "thesis": "H1b"}
# What sim.js lays out under the plot, in order. Each name is a copy entry or a sim_* key, so the wording and its
# order live here and tests/test_portfolio.py checks every name resolves.
PAGE = {
    # [key, chip, text, sub-line]: the baked tiles sim.js fills
    "tiles": [["sim_mean_usd", "mean", "sim_tile_mean_text", "sim_tile_mean_euro"],
              ["sim_median_usd", "median", "sim_tile_median_text", ""],
              ["sim_p_beat_r", "beat", "sim_tile_beat_text", ""],
              ["sim_es95_usd", "bad", "sim_tile_bad_text", "sim_tile_bad_sub"],
              ["sim_exit_days", "exit", "sim_tile_exit_text", "sim_tile_exit_sub"]],
    # [key, label, the drift option that lights the cell ("" for a cell no option reaches)]
    "strip": [["sim_mean_value_flat_usd", "sim_strip_value_flat", "value"],
              ["sim_mean_flat_usd", "sim_strip_flat", "flat"],
              ["sim_mean_thesis_usd", "sim_strip_thesis", "thesis"], ["sim_mean_parity_usd", "sim_strip_parity", ""],
              ["sim_mean_dollar_only_usd", "sim_strip_dollar_only", ""], ["sim_mean_dgs1_usd", "sim_strip_dgs1", ""]],
    # [label, figure]: the figure is a copy entry when one exists, a sim_* key otherwise
    "liq": [["sim_liq_days", "sim_exit_days"], ["sim_liq_days_p", "sim_exit_days_p"],
            ["sim_liq_short", "sim_exit_days_short"], ["sim_liq_pool", "sim_nslp_cover"],
            ["sim_liq_unbond", "sim_liq_unbond_v"], ["sim_liq_lst", "sim_lst_share"],
            ["sim_liq_depth", "sim_liq_depth_v"]],
    "notes": ["sim_why_typical", "sim_stake_note", "sim_window_note", "sim_method", "film_desk_assumption"],
    "left": ["sim_left_failure", "sim_left_lst", "sim_left_rates", "sim_left_costs", "sim_left_accounting",
             "sim_left_wrongway"],
}


def sim_copy(reg: Registry, live_keys: set[str]) -> tuple[dict, dict]:
    """Every sim_* copy entry (variants switch on sim_* text keys only) and the tile labels, and the fixed displays
    their placeholders need that the browser does not recompute."""
    import string
    path = config.ROOT / "thesis" / "copy.toml"
    entries = tomllib.loads(path.read_text()) if path.exists() else {}
    out, fixed = {}, {}
    for name, entry in sorted(entries.items()):
        if not name.startswith(COPY_PREFIXES) or not isinstance(entry, dict):
            continue
        out[name] = [{"when": v.get("when", []), "text": v.get("text", "")} for v in entry.get("variants", [])]
        for v in out[name]:
            for _, key, spec, _ in string.Formatter().parse(v["text"]):
                if key and key not in live_keys and key in reg:
                    fixed.setdefault(key, {})[spec or ""] = reg.display(key, spec or None)
    return out, fixed


def payload(reg: Registry, r: Result) -> dict:
    p, inp, R = params(), r.inp, r.inp.rates
    w0, w1 = p["sim_windows"]
    live_keys = set(r.live["raw"]) | set(DIRS) | set(RISE) | set(modes(r.default, inp, "flat", False))
    copy, fixed = sim_copy(reg, live_keys)
    expect = {k: reg.get(k).display for k in sorted(live_keys)}
    files = sorted({sf for k in live_keys for sf in reg.get(k).source_files})
    st = r.default
    return {
        "as_of": reg.get("latest_common_date").value, "seed": p["sim_seed"], "paths": p["sim_paths"],
        "horizon": p["sim_horizon_days"], "half": p["sim_half"], "tail": tail_cfg(),
        "windows": [{"days": pool.days, "a": pool.at, "b": pool.bt, "fix": pool.n_fix,
                     "day": {k: v for k, v in one_day(pool).items() if k in ("var99", "es975")}}
                    for pool in inp.pools],
        "rates": {"RU": R.RU, "RT": R.RT, "RF": R.RF, "rE": R.rE, "y": R.y, "y90": R.y90, "ys60": R.ys60},
        "mu": {**inp.mu, "lo": inp.mu_bounds[0], "hi": inp.mu_bounds[1]},
        "V": inp.V,
        "default": {"w": st.w, "s": st.s, "mu": "flat", "own": 0.0, "k": st.k, "win": st.win, "venue": st.venue,
                    "p": st.p, "rt": 0, "cu": 0},
        "share": {"step_bp": inp.share_cfg["step_bp"], "max_bp": inp.share_cfg["max_bp"], "w_b": sig12(r.w_b),
                  "w_one_day": sig12(inp.vol[(w0, "cb")] / inp.V)},
        "split_step": inp.split_step, "bp": 10000, "basis": {"split": inp.split_basis, "estr": inp.estr_basis},
        "liq": {"cb": inp.vol[(w0, "cb")], "bn": inp.vol[(w0, "bn")], "cbs": inp.vol[(w1, "cb")],
                "bns": inp.vol[(w1, "bn")], "nslp": inp.raw["nslp_usd"], "lst": inp.lst_mcap},
        "sigma_d": inp.sigma_d, "spread": r.spread,
        "steps": {"usd": p["sim_steps_usd"], "pp": p["sim_steps_pp"], "count": p["sim_steps_count"]},
        "specs": specs(), "dirs": DIRS, "rise": RISE, "bracket": [list(b) for b in BRACKET],
        # tie: digits sim.js reads past a display's last decimal to find an exact half-way value, which Python's
        # format rounds to even and toFixed rounds up
        "fmt": {"scales": [[1e12, "trillion"], [1e9, "billion"], [1e6, "million"]], "group": 3, "ten": 10,
                "pct": 100, "usd_dp": [[1000000, 0], [100000, 1], [0, 2]], "units": UNITS, "tie": 30},
        "plot": PLOT, "ui": UI, "chips": CHIPS, "page": PAGE,
        "copy": copy, "fixed": fixed, "expect": expect, "source_files": files,
    }


def site_files(reg: Registry) -> dict[str, str]:
    """Extra site files this model writes (pipeline/build.py emits them with the other data files)."""
    r = result(reg)
    if r is None:
        return {}
    return {SIM_JSON: dump(payload(reg, r)) + "\n"}
