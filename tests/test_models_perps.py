"""Offline tests for pipeline.models.perps (chart c26; claim H8; the perp ladder's rows of claim H10).

Most tests run one window of the simulation on a hand-made price line (`sim`); a few write synthetic perp bars,
funding files, fed funds and liquid-staking samples through the `raw` fixture and run the model. One test reads the
archived data in data/raw/perps and compares the real-history figures with the prototype's. The resampled
histories run at a small draw count, except one test that runs the full count on a span of a few windows.
"""

from __future__ import annotations

import csv
import json
import math
import os
import random
import re
import shutil
import statistics
import sys
import tomllib
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import config, numbers
from pipeline.chartspec import dump
from pipeline.models import load, perps as P, verdicts, volatility as V
from pipeline.models.params import RULES_PATH, param
from pipeline.models.portfolio import Prng, q7
from pipeline.numbers import Registry

sys.path.insert(0, str(Path(__file__).parent))
import test_no_traces as NT  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
ARCHIVE = REPO / "data" / "raw" / "perps"
STAMP = "2026-11-10T00:00:00+00:00"
D0 = date(2022, 1, 3)
H = P.HORIZON_DAYS
FEW = 20     # resampled histories for the tests that only need the keys
CAPN = P.COLLATERAL * P.RUNGS * P.SLICE
RPC_COLS = ["date", "height", "block_time_utc", "epoch_id", "total_supply_near", "validator_stake_near",
            "n_validators", "stake_epoch_start_height", "staked_ratio", "stnear_price", "linear_price",
            "treasury_near", "protocol_version", "max_inflation_rate"]

# sim_window's tuple
ADV, KEPT, FUND, COST, INT, LIQ, LIQD, TRADES, SHORT, PEAK, PRICE, PH, MARGIN = range(13)


# =========================================================================== one window on a hand-made line

def knobs(**over) -> P.Knobs:
    return replace(P.knobs(), **over)


def sim(prices: list[float], *, highs: list[float] | None = None, fu: list[float] | None = None, dff: float = 0.0,
        static: float | None = None, **over) -> tuple:
    """One window from the first close to the last (H = len - 1); highs default to the closes, funding to zero."""
    p = knobs(H=len(prices) - 1, **over)
    cl = [float(x) for x in prices]
    hi = [float(x) for x in (highs or prices)]
    fu = fu or [0.0] * len(prices)
    return P.sim_window(0, cl, hi, fu, P.fed_line([dff] * (len(prices) - 1)), p, static=static)


def lean(prices: list[float], *, highs: list[float] | None = None, fu: list[float] | None = None, dff: float = 0.0,
         static: float | None = None, **over) -> float:
    """The same window through the lean kernels the resampled histories use (advantage only)."""
    p = knobs(H=len(prices) - 1, **over)
    cl = [float(x) for x in prices]
    hi = [float(x) for x in (highs or prices)]
    fu = fu or [0.0] * len(prices)
    C = P.kernel_consts(p)
    ex = P.ex_table([0], P.fed_line([dff] * (len(prices) - 1)), p.H)[0]
    if static is None:
        return P.ladder_adv(0, cl, hi, P.funding_tables(fu, C), ex, C)
    return P.static_adv(0, cl, hi, [f * static * 0.5 for f in fu], ex, C, static)


def held(prices: list[float], **kw) -> list[int]:
    """Slices short through each day k = 1..H (the short from close k - 1), read off a funding of one on day k."""
    out = []
    for k in range(1, len(prices)):
        fu = [0.0] * len(prices)
        fu[k] = 1.0
        x = sim(prices, fu=fu, **kw)[FUND] / (0.5 * (prices[k - 1] + prices[k]) / prices[0]) / P.SLICE
        assert x == pytest.approx(round(x), abs=1e-9)
        out.append(round(x))
    return out


def ex_of(prices: list[float], dff: float):
    LC = P.fed_line([dff] * (len(prices) - 1))
    return lambda m: math.exp(LC[-1] - LC[m])


def sold(c: float) -> float:
    return P.SLICE * c * (1 - P.SLIP) * (1 - P.FEE)


def bought(c: float) -> float:
    return P.SLICE * c * (1 + P.SLIP) * (1 + P.FEE)


def records(n: int, seed: int, sd: float = 0.05, spike: float = 0.0):
    """Random day records (close move, high move, funding) for t = 0..n; a spike lifts the day's high by 80%."""
    rng = random.Random(seed)
    cr, hr, fu = [1.0], [1.0], [0.0]
    for _ in range(n):
        m = math.exp(rng.gauss(0, sd))
        h = max(m, 1.0) * (1 + abs(rng.gauss(0, sd / 2)))
        if spike and rng.random() < spike:
            h *= 1.8
        cr.append(m)
        hr.append(h)
        fu.append(rng.gauss(1e-4, 3e-4))
    return cr, hr, fu


# --------------------------------------------------------------------------- 1. a flat price

def test_a_flat_price_never_trades_and_keeps_minus_the_interest_on_the_deposit():
    prices = [2.0] * 41
    r = sim(prices, fu=[3e-4] * 41, dff=5.0)
    ex = ex_of(prices, 5.0)
    assert r[TRADES] == 0 and r[FUND] == 0 and r[COST] == 0 and not r[LIQD]
    assert r[PEAK] == CAPN                                   # never topped up
    assert r[INT] == CAPN * ex(0) - CAPN
    assert r[KEPT] == -r[INT] and r[ADV] == -r[INT] and r[PRICE] == 0
    assert r[MARGIN] == math.inf and r[SHORT] == 0


# --------------------------------------------------------------------------- 2. a staircase

def test_a_staircase_sells_one_slice_a_day_to_the_cap_at_the_close():
    prices = [1.105 ** k for k in range(7)] + [1.105 ** 6] * 6
    assert held(prices) == [0, 1, 2, 3, 4, 5] + [5] * 6
    r = sim(prices)
    assert r[TRADES] == 2 * P.RUNGS and not r[LIQD]
    # each sale at that day's close less slippage and the fee, bought back at the end close
    venue = sum(sold(prices[k]) for k in range(1, 6)) - P.RUNGS * bought(prices[-1])
    assert r[ADV] == pytest.approx(venue, rel=1e-12)          # no fed funds: the advantage is the venue's P&L
    assert r[COST] == pytest.approx(sum(P.SLICE * prices[k] * (P.SLIP + (1 - P.SLIP) * P.FEE) for k in range(1, 6))
                                    + P.RUNGS * P.SLICE * prices[-1] * (P.SLIP + (1 + P.SLIP) * P.FEE), rel=1e-12)
    assert r[SHORT] == pytest.approx(P.SLICE * sum(held(prices)) / (len(prices) - 1), rel=1e-12)
    assert r[MARGIN] >= 1


# --------------------------------------------------------------------------- 3. a daily swing

def test_a_daily_swing_sells_and_buys_back_every_day_and_kept_leaves_out_the_price_gain():
    prices = [1.0, 1.12] * 10 + [1.0]
    assert held(prices) == [0, 1] * 10
    r = sim(prices, fu=[1e-4] * len(prices), dff=4.0)
    assert r[TRADES] == 20
    assert r[PRICE] > 0 and r[PRICE] == pytest.approx(10 * P.SLICE * 0.12, rel=1e-9)   # the gross trading gain
    assert r[KEPT] == pytest.approx(r[FUND] - r[COST] - r[INT], abs=1e-15)


# --------------------------------------------------------------------------- 4. the base

def test_the_base_moves_down_a_rung_at_a_time_and_never_up():
    # 0.8 takes the empty ladder's base two rungs down (1/g, then 1/g^2); 0.91 then sells (above 1/g), and the rise
    # to 1.5 sells all five rungs above that base; back at 0.99 three are bought back, because the base did not move up
    prices = [1.0, 0.8, 0.9, 0.91, 0.82, 1.5, 0.99, 0.99]
    assert held(prices) == [0, 0, 0, 1, 0, 5, 2]
    assert sim(prices)[TRADES] == 1 + 1 + 5 + 3 + 2
    # without the fall the same rise sells only four rungs (1.1 to 1.4641) and 0.99 buys all of them back
    assert held([1.0, 1.0, 0.95, 1.0, 1.0, 1.5, 0.99, 0.99]) == [0, 0, 0, 0, 0, 4, 0]


# --------------------------------------------------------------------------- 5. funding

def test_funding_is_paid_on_yesterdays_short_at_the_mean_of_the_two_closes():
    prices = [1.0, 1.105, 1.2, 1.2, 1.0]
    fu = [0.0, 1e-4, 2e-4, 3e-4, 4e-4]
    s = P.SLICE
    r = sim(prices, fu=fu)
    # day 1: the slice sold at close 1 earns nothing that day; day 4: the slice bought back at close 4 is credited
    want = fu[2] * s * 0.5 * (1.105 + 1.2) + fu[3] * s * 0.5 * (1.2 + 1.2) + fu[4] * s * 0.5 * (1.2 + 1.0)
    assert r[FUND] == pytest.approx(want, rel=1e-12)
    assert held(prices) == [0, 1, 1, 1]


# --------------------------------------------------------------------------- 6. collateral

JUMP = [1.0, 1.65] + [1.0] * 9          # five slices sold at 1.65, all bought back at 1.0 the next day


def _jump_accounts(td: int, sweep: bool = True):
    """The hand accounts of JUMP: a top-up sent at close 1 (below TOPUP_AT), held at the venue until it arrives,
    then the excess swept home (above SWEEP_AT, nothing in flight)."""
    Hj = len(JUMP) - 1
    Q = CAPN
    for _ in range(P.RUNGS):
        Q += sold(1.65)
    equity, need = Q - P.RUNGS * P.SLICE * 1.65, CAPN * 1.65
    assert equity < P.TOPUP_AT * need
    top = need - equity
    for _ in range(P.RUNGS):
        Q -= bought(1.0)
    assert Q > P.SWEEP_AT * CAPN                  # already above the sweep level, but the top-up is in flight
    Q += top
    ex = ex_of(JUMP, 5.0)
    dep = CAPN * ex(0) + top * ex(1) - (CAPN + top)
    wd = Q - CAPN
    home = min(1 + 2 * td, Hj)
    return top, Q / CAPN, dep - ((wd * ex(home) - wd) if sweep else 0.0)


@pytest.mark.parametrize("td", [1, 3, 7])
def test_a_top_up_arrives_after_the_transfer_days_and_costs_fed_funds_from_its_send_day(td):
    top, _, interest = _jump_accounts(td)
    r = sim(JUMP, dff=5.0, td=td)
    assert r[PEAK] == pytest.approx(CAPN + top, rel=1e-12)
    assert r[INT] == pytest.approx(interest, rel=1e-10)
    assert lean(JUMP, dff=5.0, td=td) == r[ADV]


def test_top_up_and_sweep_only_past_their_levels():
    Q = CAPN
    for _ in range(P.RUNGS):
        Q += sold(1.65)
    ratio = (Q - P.RUNGS * P.SLICE * 1.65) / (CAPN * 1.65)
    assert sim(JUMP, dff=5.0, topup_at=ratio - 0.005)[PEAK] == CAPN
    assert sim(JUMP, dff=5.0, topup_at=ratio + 0.005)[PEAK] > CAPN
    top, level, _ = _jump_accounts(P.TRANSFER_DAYS)
    _, _, no_sweep = _jump_accounts(P.TRANSFER_DAYS, sweep=False)
    assert sim(JUMP, dff=5.0, sweep_at=level + 0.005)[INT] == pytest.approx(no_sweep, rel=1e-10)
    assert sim(JUMP, dff=5.0, sweep_at=level - 0.005)[INT] < no_sweep


# --------------------------------------------------------------------------- 7. liquidation

def test_a_high_through_the_maintenance_level_liquidates_and_stops_the_overlay():
    prices = [1.0, 1.65, 1.65, 1.65, 1.65, 1.0, 1.0, 1.0, 1.0]
    highs = list(prices)
    highs[2] = 2.6
    fu = [0.0] + [1e-4] * 8
    r = sim(prices, highs=highs, fu=fu, dff=5.0)
    Q = CAPN
    for _ in range(P.RUNGS):
        Q += sold(1.65)
    z = P.RUNGS * P.SLICE
    top = CAPN * 1.65 - (Q - z * 1.65)
    assert 0 < Q - z * 2.6 < P.MAINT * z * 2.6
    assert r[LIQD] and r[LIQ] == pytest.approx(Q - z * 2.6, rel=1e-12)
    assert r[MARGIN] < 1 and r[MARGIN] == pytest.approx((Q - z * 2.6) / (P.MAINT * z * 2.6), rel=1e-12)
    assert r[TRADES] == P.RUNGS and r[FUND] == 0             # no funding, no buyback, no trade after the call
    ex = ex_of(prices, 5.0)
    home = min(1 + P.TRANSFER_DAYS + P.TRANSFER_DAYS, len(prices) - 1)      # the top-up arrives, then goes home
    interest = (CAPN * ex(0) + top * ex(1) - (CAPN + top)) - (top * ex(home) - top)
    assert r[INT] == pytest.approx(interest, rel=1e-10)
    assert r[ADV] == pytest.approx(-CAPN - interest, rel=1e-12)   # the deposit and the sales' dollars are lost
    assert r[KEPT] == pytest.approx(r[FUND] - r[COST] - r[INT] - r[LIQ], abs=1e-15)
    assert lean(prices, highs=highs, fu=fu, dff=5.0) == r[ADV]


# --------------------------------------------------------------------------- 8. the accounts add up

@pytest.fixture(scope="module")
def rough():
    """A random line with spiking highs: some windows are liquidated, some are not."""
    n = H + 40
    cr, hr, fu = records(n, seed=11, sd=0.06, spike=0.01)
    cl, hi, fu = P.path(cr, hr, fu)
    LC = P.fed_line([4.0] * n)
    p = P.knobs()
    lad = [P.sim_window(o, cl, hi, fu, LC, p) for o in range(41)]
    sta = [P.sim_window(o, cl, hi, fu, LC, p, static=P.STATIC) for o in range(41)]
    return cl, hi, fu, LC, p, lad, sta


def test_the_accounts_add_up_on_every_window(rough):
    *_, lad, sta = rough
    assert any(r[LIQD] for r in lad) and not all(r[LIQD] for r in lad)
    for r in lad + sta:
        assert r[ADV] == pytest.approx(r[PRICE] + r[KEPT], abs=1e-12)
        assert r[KEPT] == pytest.approx(r[FUND] - r[COST] - r[INT] - r[LIQ], abs=1e-12)
        assert (r[MARGIN] < 1) == bool(r[LIQD])


@pytest.mark.parametrize("change", [{}, {"td": 1}, {"td": 7}, {"lam": 0.5}, {"lam": 1.5}, {"slip": 0.005}],
                         ids=["default", "td1", "td7", "lam0.5", "lam1.5", "slip0.5%"])
def test_the_lean_kernels_equal_the_full_simulation_exactly(rough, change):
    cl, hi, fu, LC, p, lad, sta = rough
    if change:     # the kernels' fast paths depend on the transfer time, the collateral and the fills
        p = replace(p, **change)
        lad = [P.sim_window(o, cl, hi, fu, LC, p) for o in range(41)]
        sta = [P.sim_window(o, cl, hi, fu, LC, p, static=P.STATIC) for o in range(41)]
    C = P.kernel_consts(p)
    FH = P.funding_tables(fu, C)
    fs = [f * P.STATIC * 0.5 for f in fu]
    EX = P.ex_table(list(range(41)), LC, H)
    assert [P.ladder_adv(o, cl, hi, FH, EX[o], C) for o in range(41)] == [r[ADV] for r in lad]
    assert [P.static_adv(o, cl, hi, fs, EX[o], C, P.STATIC) for o in range(41)] == [r[ADV] for r in sta]


# --------------------------------------------------------------------------- 9. no foresight

def test_prices_after_day_k_leave_every_decision_up_to_day_k_alone():
    cr, hr, _ = records(60, seed=4, sd=0.05)
    cl, hi, _ = P.path(cr, hr, [0.0] * 61)
    k = 30
    later = cl[:k + 1] + [x * (1.7 if i % 2 else 0.6) for i, x in enumerate(cl[k + 1:])]
    later_hi = hi[:k + 1] + [x * 1.05 for x in later[k + 1:]]
    assert held(cl, highs=hi)[:k + 1] == held(later, highs=later_hi)[:k + 1]   # the short through day k + 1
    for static in (None, P.STATIC):
        cut = sim(cl[:k + 1], highs=hi[:k + 1], dff=4.0, static=static)
        assert sim(later[:k + 1], highs=later_hi[:k + 1], dff=4.0, static=static) == cut
    assert sim(cl, highs=hi) != sim(later, highs=later_hi)


# --------------------------------------------------------------------------- 10. funding per day

def test_funding_days_floor_each_stamp_and_close_the_day_at_midnight():
    rows = [{"time": "2024-01-02T00:00:00Z", "funding_rate": "1"},      # closes 1 January
            {"time": "2024-01-02T00:00:41Z", "funding_rate": "2"},      # late, floored to 00:00
            {"time": "2024-01-02T01:00:00Z", "funding_rate": "4"},
            {"time": "2024-01-02T23:59:59Z", "funding_rate": "8"},
            {"time": "2024-01-03T00:00:00.031Z", "funding_rate": "16"},  # closes 2 January
            {"time": "2024-01-03T05:00:00Z", "funding_rate": ""}]
    tot, cnt = P.funding_days(rows)
    assert tot == {date(2024, 1, 1): 3.0, date(2024, 1, 2): 28.0}
    assert cnt == {date(2024, 1, 1): 2, date(2024, 1, 2): 3}
    assert P.usable(24, 24) and P.usable(23, 24) and not P.usable(22, 24)
    assert P.usable(3, 3) and not P.usable(2, 3)
    # more settlements than a day holds (rows merged twice, or a shorter interval) never pass
    assert not P.usable(25, 24) and not P.usable(48, 24) and not P.usable(4, 3) and not P.usable(6, 3)


def test_a_day_with_a_settlement_at_another_interval_is_left_out():
    def row(day, hour, hours):
        return {"time": f"2024-01-0{day}T{hour:02d}:00:00Z", "interval_hours": hours, "funding_rate": "1"}
    # a 00:00 stamp closes the day before, so 2 January is 08:00, 16:00 and the next 00:00
    rows = [row(2, 8, "8"), row(2, 16, "8"), row(3, 0, "8"), row(3, 8, "8"), row(3, 12, "4"), row(3, 16, "8"),
            row(4, 0, "8")]
    tot, cnt = P.funding_days(rows, 3)
    assert cnt == {date(2024, 1, 2): 3} and tot == {date(2024, 1, 2): 3.0}   # 3 January holds a four-hour row
    assert P.funding_days(rows)[1][date(2024, 1, 3)] == 4                    # without the venue's interval: kept
    assert P.odd_interval(row(3, 12, "4"), 3) and not P.odd_interval(row(3, 8, "8"), 3)
    assert not P.odd_interval({"time": "x", "funding_rate": "1"}, 24)       # Hyperliquid writes no interval


# =========================================================================== synthetic raw files

@pytest.fixture
def raw(tmp_path, monkeypatch):
    raw_dir = tmp_path / "data" / "raw"
    raw_dir.mkdir(parents=True)
    for mod in (config, load, numbers):
        monkeypatch.setattr(mod, "ROOT", tmp_path)
        monkeypatch.setattr(mod, "RAW_DIR", raw_dir)
    (tmp_path / "thesis").mkdir()
    for name in ("constants.toml", "sources.toml"):
        shutil.copy(REPO / "thesis" / name, tmp_path / "thesis" / name)
    monkeypatch.setattr(P, "DRAWS", FEW)
    monkeypatch.setattr(V, "NULL_DRAWS", FEW)
    return tmp_path


def put_csv(root: Path, rel: str, header: list[str], rows: list[list]) -> None:
    path = root / "data" / "raw" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows([["" if v is None else v for v in r] for r in rows])
    path.with_suffix(".meta.json").write_text(json.dumps({"fetched_at": STAMP, "source_url": "test"}))


def day(k: int) -> date:
    return D0 + timedelta(days=k)


def stamp(k: int, hour: int) -> str:
    t = datetime(D0.year, D0.month, D0.day, tzinfo=timezone.utc) + timedelta(days=k, hours=hour)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def wiggle(n: int, seed: int = 7, sd: float = 0.04) -> list[float]:
    rng = random.Random(seed)
    out, x = [], 0.0
    for _ in range(n):
        out.append(round(5 * math.exp(x), 10))
        x += rng.gauss(0, sd)
    return out


def bars(closes: list[float], first: int = 0) -> list[list]:
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append([day(first + i).isoformat(), repr(o), repr(round(max(o, c) * 1.01, 10)),
                    repr(round(min(o, c) * 0.99, 10)), repr(c), 1, "", 1])
    return out


def world(root: Path, n: int, *, seed: int = 7, venues: tuple[str, ...] = ("hl", "bn"), kraken: int | None = 200,
          terms: bool = True, spot: bool = False, drop: dict | None = None, rates: dict | None = None):
    """n days of perp bars and funding on each venue (Binance's line a little different), DFF at 3% and daily
    liquid-staking samples growing 5% a year; Kraken's funding over the last `kraken` days. drop = {venue: [(day,
    hour), ...]} removes settlements; rates = {venue: f(day, hour) -> rate} sets them."""
    drop, rates = drop or {}, rates or {}
    rng = random.Random(seed + 1)
    hl = wiggle(n, seed)
    bn = [round(c * (1 + 0.01 * math.sin(k / 9)), 10) for k, c in enumerate(hl)]
    head = ["date", "open", "high", "low", "close", "volume_base", "volume_quote", "trades"]

    def fund(vid: str, hours: list[int], first: int = 0):
        f = rates.get(vid) or (lambda k, h: round(rng.gauss(1e-5, 2e-5), 12))
        return [(stamp(k, h), f(k, h)) for k in range(first, n) for h in hours if (k, h) not in drop.get(vid, ())]

    if "hl" in venues:
        put_csv(root, "perps/near_perp_hyperliquid_daily.csv", head, bars(hl))
        put_csv(root, "perps/near_funding_hyperliquid_hourly.csv", ["time", "time_ms", "funding_rate", "premium"],
                [[t, 0, repr(x), 0] for t, x in fund("hl", list(range(1, 25)))])
    if "bn" in venues:
        put_csv(root, "perps/near_perp_binance_daily.csv", head, bars(bn))
        put_csv(root, "perps/near_funding_binance_8h.csv", ["time", "time_ms", "interval_hours", "funding_rate"],
                [[t, 0, 8, repr(x)] for t, x in fund("bn", [8, 16, 24])])
    if kraken:
        put_csv(root, "perps/near_funding_kraken_hourly.csv", ["time", "funding_rate", "funding_usd"],
                [[t, repr(x), 0] for t, x in fund("kr", list(range(1, 25)), first=n - kraken)])
    if terms:
        path = root / "data" / "raw" / "perps" / "near_perp_terms.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checked_at": STAMP, "venues": [
            {"venue": "Hyperliquid", "contract": {"name": "NEAR", "maxLeverage": 10},
             "context": {"openInterest": "2000000", "markPx": "2.5", "funding": "0.0000125"}}]}))
        path.with_suffix(".meta.json").write_text(json.dumps({"fetched_at": STAMP, "source_url": "test"}))
    if spot:
        put_csv(root, "market/NEARUSDT_binance_daily.csv",
                ["date", "open", "high", "low", "close", "volume_base", "volume_quote"],
                [[day(k).isoformat(), 1, 1, 1, repr(p), 1, 1] for k, p in enumerate(hl)])
    put_csv(root, "fred/DFF.csv", ["date", "value"], [[day(k).isoformat(), 3.0] for k in range(-10, n + 10)])
    put_csv(root, "rpc/history_samples.csv", RPC_COLS,
            [[day(k).isoformat(), 1000 + k, None, "E", 1e9, None, None, None, None, repr(1.05 ** (k / 365.25)),
              repr(1.05 ** (k / 365.25)), None, 80, 0.025] for k in range(0, n + 2)])
    return hl, bn


def registry(share: float | None = 10) -> Registry:
    reg = Registry()
    if share is not None:
        reg.put("sim_share", share, "%", "FAKE")
    return reg


def test_one_missing_hour_is_counted_and_a_missing_eight_hour_settlement_drops_the_day(raw):
    n = H + 30
    world(raw, n, drop={"hl": [(12, 5)], "bn": [(20, 16)]})
    res = P.run()
    hl, bn = res.venues["hl"], res.venues["bn"]
    assert hl.partial == [day(12)] and len(hl.starts) == 30 and hl.fail["funding"] == 0
    # Binance's day 20 is unusable: starts 0..19 hold it in s+1..e
    assert bn.starts[0] == day(20) and len(bn.starts) == 10 and bn.fail["funding"] == 20
    reg = registry()
    P.compute(reg)
    assert reg.value("vol_perp_hl_partial_days") == 1 and reg.value("vol_perp_bn_partial_days") == 0
    assert reg.value("vol_perp_bn_first_start") == day(20).isoformat()


# --------------------------------------------------------------------------- 11. the redraws

def test_redraws_follow_the_prng_rule_and_can_repeat_a_block():
    N, block, draws, seed = 95, 30, 6, 7
    bl = P.blocks_idx(N, block)
    assert [len(b) for b in bl] == [30, 30, 30, 5]
    rng, want = Prng(seed), []
    for _ in range(draws):
        seq, filled = [], 0
        while filled < N:
            b = (rng.next() * len(bl)) >> 32
            seq.append(b)
            filled += len(bl[b])
        want.append(seq)
    assert P.redraw_orders(N, block, draws, seed) == want
    perms = P.redraw_perms(N, block, draws, seed)
    assert all(len(p) == N + 1 and p[0] == 0 for p in perms)
    assert any(len(set(s)) < len(s) for s in want)            # a block drawn twice
    assert all(p[1:] == [d for b in s for d in bl[b]][:N] for p, s in zip(perms, want))
    # the shuffles are the block orders of F-vol-null on the same seed
    assert P.shuffle_orders(N, block, draws, seed) == V.block_orders(len(bl), draws, seed)
    assert all(sorted(p) == list(range(N + 1)) and p[0] == 0 for p in P.shuffle_perms(N, block, draws, seed))


def test_a_history_moves_whole_day_records():
    cr, hr, fu = records(95, seed=2)
    perm = P.redraw_perms(95, 30, 1, 3)[0]
    cl, hi, f = P.path(cr, hr, fu, perm)
    for t in range(1, 96):
        assert cl[t] / cl[t - 1] == pytest.approx(cr[perm[t]], rel=1e-12)
        assert hi[t] / cl[t - 1] == pytest.approx(hr[perm[t]], rel=1e-12)
        assert f[t] == fu[perm[t]]


# --------------------------------------------------------------------------- 12. the yardstick

def test_the_yardstick_trades_twice_on_the_ladders_collateral():
    flat = [1.0] * 31
    lad, sta = sim(flat, dff=4.0), sim(flat, dff=4.0, static=P.STATIC)
    assert sta[TRADES] == 2 and sta[INT] == lad[INT]          # the same deposit, no transfer
    assert sta[COST] == pytest.approx(P.STATIC * (P.SLIP + (1 - P.SLIP) * P.FEE)
                                      + P.STATIC * (P.SLIP + (1 + P.SLIP) * P.FEE), rel=1e-12)
    # on a flat price the swing is the ladder's costs and interest less the yardstick's
    assert lad[ADV] - sta[ADV] == pytest.approx((-lad[COST] - lad[INT]) - (-sta[COST] - sta[INT]), abs=1e-15)
    assert sim([1.0, 1.12] * 10 + [1.0], static=P.STATIC)[TRADES] == 2
    # a jump tops the yardstick up to the ladder's target, COLLATERAL x RUNGS x SLICE x the price
    jump = [1.0, 1.65, 1.65, 1.65, 1.65]
    Q = CAPN + P.STATIC * (1 - P.SLIP) * (1 - P.FEE)
    top = CAPN * 1.65 - (Q - P.STATIC * 1.65)
    assert sim(jump, static=P.STATIC)[PEAK] == pytest.approx(CAPN + top, rel=1e-12)
    assert lean(jump, static=P.STATIC, dff=4.0) == sim(jump, static=P.STATIC, dff=4.0)[ADV]


def test_swing_is_the_ladder_less_the_yardstick_per_window(rough):
    *_, lad, sta = rough
    s = P.summary(lad, sta)
    assert s["swing"] == [100 * (a[ADV] - b[ADV]) for a, b in zip(lad, sta)]
    assert s["swing_mean"] == statistics.fmean(s["swing"])
    assert s["static_adv"] == statistics.fmean(100 * b[ADV] for b in sta)


# --------------------------------------------------------------------------- 13, 14. centring and the bands

def test_centring_on_hand_values():
    xs = [-3.0, -1.0, 0.5, 2.0, 6.5]                 # mean 1
    c = P.centred(0.5, xs)
    cs = sorted(x - 1 + 0.5 for x in xs)              # -3.5, -1.5, 0.0, 1.5, 6.0
    assert c["raw_mean"] == 1.0
    assert c["share"] == 40.0                          # the centred zero is not above zero
    assert c["lo"] == q7(cs, P.RANK_LO / 100) and c["hi"] == q7(cs, P.RANK_HI / 100)
    assert P.share_band(P.RANK_HI) == "above" and P.share_band(P.RANK_LO) == "below"
    assert P.share_band(50) == P.share_band(P.RANK_HI - 0.1) == P.share_band(P.RANK_LO + 0.1) == "inside"
    assert P.share_band(None) is None


@pytest.mark.parametrize("a, b, want", [
    ("above", "above", "above"), ("below", "below", "below"), ("above", "inside", "inside"),
    ("inside", "below", "inside"), ("above", "below", "inside"), ("inside", "inside", "inside"),
    (None, "above", None), ("below", None, None)])
def test_both_venues_must_agree(a, b, want):
    assert P.both(a, b) == want


# --------------------------------------------------------------------------- 15. the rules

def rule(cid: str) -> dict:
    with RULES_PATH.open("rb") as fh:
        return tomllib.load(fh)[cid]


def h8_reg(hl: float, bn: float, hl_years: int = 3, bn_years: int = 4) -> Registry:
    reg = Registry()
    reg.put("vol_perp_hl_swing_share", hl, "%", "FAKE")
    reg.put("vol_perp_bn_swing_share", bn, "%", "FAKE")
    reg.put("vol_perp_hl_years", hl_years, "count", "FAKE")
    reg.put("vol_perp_bn_years", bn_years, "count", "FAKE")
    return reg


@pytest.mark.parametrize("hl, bn, years, want", [
    (97.5, 97.5, (3, 4), verdicts.SUPPORTED), (97.5, 50, (3, 4), verdicts.PARTLY), (50, 97.5, (3, 4), verdicts.PARTLY),
    (2.5, 2.5, (3, 4), verdicts.REFUTED), (2.5, 50, (3, 4), verdicts.PARTLY), (99, 1, (3, 4), verdicts.PARTLY),
    (77.6, 53.8, (3, 4), verdicts.PARTLY),
    (97.5, 97.5, (2, 4), verdicts.NOT_COMPUTED), (97.5, 97.5, (3, 2), verdicts.NOT_COMPUTED)])
def test_h8_on_hand_values(hl, bn, years, want):
    assert verdicts.evaluate_claim("H8", rule("H8"), h8_reg(hl, bn, *years))["verdict"] == want


@pytest.mark.parametrize("hl, bn, want", [(97.5, 97.5, verdicts.SUPPORTED), (97.5, 50, verdicts.PARTLY),
                                          (2.5, 2.5, verdicts.REFUTED), (97.2, 46.9, verdicts.PARTLY)])
def test_h10_reads_the_ladder_on_both_venues(hl, bn, want):
    reg = Registry()
    for key, value in (("vol_rally_rank", 50), ("vol_rebal_rank", 50), ("vol_perp_hl_rank", hl),
                       ("vol_perp_bn_rank", bn)):
        reg.put(key, value, "%", "FAKE")
    for key, value in (("vol_years", 5), ("vol_perp_hl_years", 3), ("vol_perp_bn_years", 4)):
        reg.put(key, value, "count", "FAKE")
    assert verdicts.evaluate_claim("H10", rule("H10"), reg)["verdict"] == want


# --------------------------------------------------------------------------- 16. no windows

def test_no_data_nulls_every_key_and_leaves_both_claims_not_computed(raw):
    reg = registry()
    P.compute(reg)
    venue_keys = [k for k, _ in reg.items() if k.startswith(("vol_perp_hl_", "vol_perp_bn_", "vol_perp_kr_"))]
    assert len(venue_keys) >= 2 * 45
    for key in venue_keys + ["vol_perp_band", "vol_perp_swing_band", "vol_perp_year_first", "vol_perp_year_last"]:
        assert reg.value(key) is None and reg.get(key).null_reason, key
    assert "Hyperliquid" in reg.get("vol_perp_hl_mean").null_reason
    assert reg.get("vol_perp_hl_max_leverage").null_reason.endswith("near_perp_terms.json is missing")
    cb_keys = [k for k, _ in reg.items() if k.startswith("vol_perp_cb_") and k != CB_ACCESS_KEY]
    assert sorted(cb_keys) == sorted(f"vol_perp_cb_{k}" for k in CB_KEYS)
    for key in cb_keys:
        assert reg.value(key) is None and reg.get(key).null_reason.endswith("near_perp_coinbase_terms.json is missing")
    assert reg.value(CB_ACCESS_KEY) == cb_access_read()      # a declared constant: no market file needed
    assert reg.value("vol_perp_rung_pct") == param("vol_perp_rung_pct")          # the echoes stay
    chart = P.swing_chart(reg)
    dump(chart.to_json())
    assert chart.status == "unavailable" and chart.empty_state
    assert verdicts.evaluate_claim("H8", rule("H8"), reg)["verdict"] == verdicts.NOT_COMPUTED
    assert verdicts.evaluate_claim("H10", rule("H10"), reg)["verdict"] == verdicts.NOT_COMPUTED


def test_a_missing_terms_or_funding_file_nulls_only_its_keys(raw):
    world(raw, H + 20, terms=False, kraken=None)
    reg = registry()
    P.compute(reg)
    assert reg.value("vol_perp_hl_mean") is not None and reg.value("vol_perp_bn_swing_share") is not None
    for key in ("vol_perp_hl_max_leverage", "vol_perp_hl_oi_usd", "vol_perp_year_first", "vol_perp_year_last",
                "vol_perp_hl_short_paid", "vol_perp_bn_short_paid", "vol_perp_kr_short_paid",
                "vol_perp_hl_funding_year", "vol_perp_bn_funding_year", "vol_perp_hl_const_share"):
        assert reg.value(key) is None and reg.get(key).null_reason, key
    assert "near_funding_kraken_hourly.csv is missing" in reg.get("vol_perp_year_first").null_reason


def test_a_missing_bar_inside_the_span_nulls_the_resampled_keys(raw):
    world(raw, 2 * H + 40)
    path = raw / "data" / "raw" / "perps" / "near_perp_binance_daily.csv"
    lines = path.read_text().splitlines()
    gap = H + 20                          # windows end before it or start after it, so it sits inside the span
    path.write_text("\n".join(lines[:1 + gap] + lines[2 + gap:]) + "\n")
    reg = registry()
    P.compute(reg)
    starts = P.result(reg).venues["bn"].starts
    assert starts[0] == day(0) and day(19) in starts and day(20) not in starts and day(gap + 1) in starts
    assert reg.value("vol_perp_bn_mean") is not None
    for key in ("vol_perp_bn_rank", "vol_perp_bn_swing_share", "vol_perp_bn_null_mean", "vol_perp_bn_swing_lo"):
        assert reg.value(key) is None and reg.get(key).null_reason == P.SPAN_GAP, key
    assert reg.value("vol_perp_band") is None and "Binance" in reg.get("vol_perp_band").null_reason
    assert reg.value("vol_perp_hl_rank") is not None


# --------------------------------------------------------------------------- 17. the charts

def copy_with(reg: Registry, **over) -> Registry:
    out = Registry()
    for key, n in reg.items():
        value = over.pop(key) if key in over else n.value
        out.put(key, value, n.unit, n.formula_id, source_files=n.source_files, inputs=n.inputs,
                null_reason=(n.null_reason or "test") if value is None else None, decimals=n.decimals,
                signed=n.signed)
    return out


@pytest.fixture
def modelled(raw):
    world(raw, H + 40, spot=True)
    reg = registry()
    V.compute(reg)
    P.compute(reg)
    return reg


def test_c26_and_c25_validate(modelled):
    reg = modelled
    c26 = P.swing_chart(reg)
    dump(c26.to_json())
    assert c26.claims == ["H8"] and c26.status == "ok"
    assert [p[0] for p in c26.series[0].points] == ["Hyperliquid", "Binance"]
    assert len(c26.csv_table["rows"]) == reg.value("vol_perp_hl_windows") + reg.value("vol_perp_bn_windows")
    assert c26.table["columns"] == P.TABLE_COLUMNS
    c25 = V.strategies_chart(reg)
    dump(c25.to_json())
    assert c25.status == "ok"
    assert [p[1] for p in c25.series[1].points][2:] == [reg.value("vol_perp_hl_mean"), reg.value("vol_perp_bn_mean")]


def test_c26_table_holds_every_case_with_labels_from_the_params(modelled):
    chart = P.swing_chart(modelled)
    labels = [label for label, _ in P.sens_cases()]
    rows = chart.table["rows"]
    assert len(rows) == 2 * len(labels)
    assert [r[1] for r in rows] == labels * 2 and [r[0] for r in rows] == ["Hyperliquid"] * len(labels) + [
        "Binance"] * len(labels)
    band = P.TABLE_COLUMNS.index("swing_share_pct")
    assert rows[0][band] is not None and all(r[band] is None for r in rows[1:len(labels)])
    allowed = ({f"{a:g}" for a in P.SENS_RATE_ADD} | {f"{x:g}" for x in P.SENS_COLLATERAL}
               | {str(d) for d in P.SENS_TRANSFER_DAYS} | {f"{q:.2f}" for q in P.SENS_SLIP_PCT}
               | {str(H), str(P.SENS_LAG_DAYS)})
    for label in labels:
        assert set(re.findall(r"\d+(?:\.\d+)?", label)) <= allowed, label
    by = dict(zip(labels, rows))
    assert by[f"latest {H} start days"][2] == str(min(H, int(modelled.value('vol_perp_hl_windows'))))


def test_c26_title_follows_the_bands(modelled):
    res = P.result(modelled)
    titles = {}
    for name, (both_, hl, bn) in {"above": ("above", "above", "above"), "below": ("below", "below", "below"),
                                  "both inside": ("inside", "inside", "inside"),
                                  "disagree": ("inside", "above", "inside"), "none": (None, None, "inside")}.items():
        r2 = copy_with(modelled, vol_perp_swing_band=both_, vol_perp_hl_swing_band=hl, vol_perp_bn_swing_band=bn)
        P._RESULTS[r2] = res
        chart = P.swing_chart(r2)
        dump(chart.to_json())
        titles[name] = chart.title
    assert titles["above"].startswith("Selling NEAR's swings on a perpetual beat a short that never trades on both")
    assert "fell behind a short that never trades on both venues" in titles["below"]
    assert "redrawn months put zero inside both ranges" in titles["both inside"]
    assert titles["disagree"].endswith("and the venues disagree once months are redrawn.")
    assert titles["none"] == "What selling NEAR's swings on a perpetual made over a short that never trades, on two venues."
    assert not any(NT.says(t, NT.BANNED_TITLES) for t in titles.values())


# --------------------------------------------------------------------------- 18. the late fill

def test_a_late_fill_decides_at_one_close_and_fills_at_the_next():
    prices = [1.0, 1.105, 1.2, 1.2, 1.2]
    assert held(prices) == [0, 1, 1, 1]
    assert held(prices, lag=1) == [0, 0, 1, 1]
    r = sim(prices, lag=1)
    assert r[COST] == pytest.approx(P.SLICE * 1.2 * (P.SLIP + (1 - P.SLIP) * P.FEE)
                                    + P.SLICE * 1.2 * (P.SLIP + (1 + P.SLIP) * P.FEE), rel=1e-12)
    # the same decisions as step 3, one close later
    cr, hr, _ = records(60, seed=9, sd=0.04)
    cl, _, _ = P.path(cr, hr, [0.0] * 61)
    now, late = held(cl), held(cl, lag=1)
    assert not sim(cl)[LIQD] and not sim(cl, lag=1)[LIQD]
    assert late == [0] + now[:-1] and any(now)


# --------------------------------------------------------------------------- 19. funding across venues

def test_funding_facts_on_three_synthetic_files(raw):
    n = 60
    term = P.HL_INTEREST_HOURLY

    def hl_rate(k, h):
        return term if h % 3 else (term + 5e-13 if h == 3 else term + 2e-12 if h == 6 else -1e-5)

    world(raw, n, kraken=30, rates={"hl": hl_rate, "bn": lambda k, h: -1e-4 if h == 8 else 2e-4,
                                    "kr": lambda k, h: -1e-6 if h < 6 else 1e-6},
          drop={"kr": [(n - 20, 4), (n - 20, 5)], "bn": [(n - 1, 24)]})
    fx = P.funding_facts()
    # Kraken covers the last 30 days; its day n-20 lacks two hours; Binance's last day lacks its 00:00 settlement
    assert (fx["first"], fx["last"]) == (day(n - 30), day(n - 2))
    v = fx["venues"]
    days = 29
    assert v["hl"]["n"] == 24 * days and v["bn"]["n"] == 3 * days and v["kr"]["n"] == 24 * days - 2
    # Hyperliquid: of the eight hours h % 3 == 0 each day, h = 3 sits within 1e-12 of the term, h = 6 does not
    assert v["hl"]["const"] == pytest.approx(100 * (16 + 1) / 24)
    assert v["hl"]["short_paid"] == pytest.approx(100 * 6 / 24)
    assert v["bn"]["short_paid"] == pytest.approx(100 / 3)
    assert v["kr"]["short_paid"] == pytest.approx(100 * (5 * days - 2) / (24 * days - 2))
    xs = [-1e-4, 2e-4, 2e-4] * days
    assert v["bn"]["year"] == pytest.approx(100 * statistics.fmean(xs) * 1095, rel=1e-12)
    assert "year" not in v["kr"] and "const" not in v["bn"]
    reg = registry()
    P.compute(reg)
    assert reg.value("vol_perp_year_first") == day(n - 30).isoformat()
    assert reg.value("vol_perp_kr_short_paid") is not None
    assert "vol_perp_kr_funding_year" not in reg


# --------------------------------------------------------------------------- 19b. Coinbase's NEAR Perp (F-perp-coinbase)

CB_KEYS = ("listed", "days", "contract", "oi_usd", "funding_interval", "short_margin_overnight",
           "short_margin_intraday", "margin_rise")
CB_ACCESS_KEY = "vol_perp_cb_access_read"


def cb_access_read() -> str:
    """The retrieval date of Coinbase's US eligibility quotes in thesis/constants.toml."""
    return tomllib.loads((REPO / "thesis" / "constants.toml").read_text())[P.CB_ACCESS]["retrieved"]


def coinbase(root: Path, *, listed: str = "2022-01-05T14:00:00Z", first: int = 2, n: int = 30, skip: int | None = None,
             product: dict | None = None) -> None:
    """Coinbase's NEAR Perp: a product snapshot shaped like the venue's record and n daily bars from day `first`
    (one day, `skip`, left out)."""
    put_csv(root, "perps/near_perp_coinbase_daily.csv", ["date", "open", "high", "low", "close", "volume_contracts"],
            [[day(k).isoformat(), 2, 2.2, 1.9, 2.1, 10] for k in range(first, first + n) if k != skip])
    record = product if product is not None else {
        "product_id": P.CB_PRODUCT, "new_at": listed,
        "future_product_details": {"contract_size": "500", "funding_interval": "3600s", "open_interest": "1000",
                                   "index_price": "2.5", "intraday_margin_rate": {"short_margin_rate": "0.2"},
                                   "overnight_margin_rate": {"short_margin_rate": "0.6"}}}
    path = root / "data" / "raw" / "perps" / "near_perp_coinbase_terms.json"
    path.write_text(json.dumps({"checked_at": STAMP, "venue": "Coinbase Derivatives", "product_id": P.CB_PRODUCT,
                                "product": record}))
    path.with_suffix(".meta.json").write_text(json.dumps({"fetched_at": STAMP, "source_url": "test"}))


def test_coinbase_keys_read_the_snapshot_and_the_bars_from_the_listing_day(raw):
    world(raw, H + 20)
    coinbase(raw, first=1, n=31, skip=10)            # a bar the day before the listing, one day missing after it
    reg = registry()
    P.compute(reg)
    got = {k: reg.value(f"vol_perp_cb_{k}") for k in CB_KEYS}
    assert got == {"listed": day(2).isoformat(), "days": 29, "contract": 500, "oi_usd": 1000 * 500 * 2.5,
                   "funding_interval": 1, "short_margin_overnight": 60, "short_margin_intraday": 20,
                   "margin_rise": pytest.approx(100 * 0.4 / 1.6, abs=1e-4)}
    assert all(reg.get(f"vol_perp_cb_{k}").formula_id == "F-perp-coinbase" for k in CB_KEYS)
    assert "1 days missing between them" in reg.get("vol_perp_cb_days").inputs[0]
    assert reg.get("vol_perp_cb_days").source_files == ["data/raw/perps/near_perp_coinbase_daily.csv",
                                                        "data/raw/perps/near_perp_coinbase_terms.json"]
    # the margin rise is where equity 1 - r meets the overnight margin m (1 + r)
    r, m = got["margin_rise"] / 100, got["short_margin_overnight"] / 100
    assert 1 - r == pytest.approx(m * (1 + r), abs=1e-6)
    # no claim and no tripwire reads them (the record is shorter than one window)
    with RULES_PATH.open("rb") as fh:
        rules = tomllib.load(fh)
    assert "vol_perp_cb_" not in json.dumps({k: v for k, v in rules.items() if k != "labels"})


def test_c26_cites_coinbase_and_says_the_collateral_covers_its_margin(raw):
    world(raw, H + 20, spot=True)
    coinbase(raw)
    reg = registry()
    V.compute(reg)
    P.compute(reg)
    chart = P.swing_chart(reg)
    dump(chart.to_json())
    assert any("NER-20DEC30-CDE" in s for s in chart.sources)
    assert {q.file for q in chart.queries} >= {"data/raw/perps/near_perp_coinbase_daily.csv",
                                               "data/raw/perps/near_perp_coinbase_terms.json"}
    assert "{vol_perp_cb_listed}" in chart.caveat_template
    note = next(t for t in chart.notes_templates if "{vol_perp_cb_short_margin_overnight}" in t)
    assert "{vol_perp_collateral}" in note and "{vol_perp_cb_margin_rise}" in note and "covers" in note
    assert any("{vol_perp_cb_days}" in t and "approved US holder can run the ladder" in t for t in chart.notes_templates)
    # Coinbase's own access conditions, with the day its pages were read
    access = next(t for t in chart.notes_templates if "{vol_perp_cb_access_read}" in t)
    assert "approved application" in access and "eligible address" in access and "no US state" in access


def test_coinbase_access_read_is_the_declared_constants_retrieval_date(raw):
    """The day Coinbase's US eligibility pages were read is the retrieval date of their declared quotes (formula
    K-cb_us_derivatives_access), whatever the market files hold; without the constant the c26 note keeps the
    conditions and drops only the date."""
    coinbase(raw)
    reg = registry()
    P.compute(reg)
    got = reg.get(CB_ACCESS_KEY)
    assert got.value == cb_access_read() and got.unit == "date"
    assert got.formula_id == f"K-{P.CB_ACCESS}" and got.source_files == ["thesis/constants.toml"]
    (raw / "thesis" / "constants.toml").write_text("")
    world(raw, H + 20, spot=True)
    reg = registry()
    V.compute(reg)
    P.compute(reg)
    assert reg.value(CB_ACCESS_KEY) is None and "not declared" in reg.get(CB_ACCESS_KEY).null_reason
    chart = P.swing_chart(reg)
    note = next(t for t in chart.notes_templates if "{vol_perp_cb_days}" in t)
    assert "{vol_perp_cb_access_read}" not in note and "eligible address" in note


def test_a_missing_or_foreign_coinbase_record_nulls_only_its_keys(raw):
    world(raw, H + 20)
    coinbase(raw, product={"product_id": "NEAR-PERP-INTX"})
    reg = registry()
    P.compute(reg)
    for k in CB_KEYS:
        assert reg.value(f"vol_perp_cb_{k}") is None, k
        assert reg.get(f"vol_perp_cb_{k}").null_reason.endswith("holds no NER-20DEC30-CDE record"), k
    assert reg.value("vol_perp_hl_mean") is not None and reg.value("vol_perp_hl_oi_usd") is not None
    (raw / "data" / "raw" / "perps" / "near_perp_coinbase_daily.csv").unlink()
    coinbase(raw, product={"product_id": P.CB_PRODUCT, "new_at": "2022-01-05T14:00:00Z",
                           "future_product_details": {"contract_size": "500"}})
    (raw / "data" / "raw" / "perps" / "near_perp_coinbase_daily.csv").unlink()
    reg = registry()
    P.compute(reg)
    assert reg.value("vol_perp_cb_contract") == 500 and reg.value("vol_perp_cb_listed") == day(2).isoformat()
    assert reg.get("vol_perp_cb_days").null_reason == "data/raw/perps/near_perp_coinbase_daily.csv is missing"
    assert reg.get("vol_perp_cb_oi_usd").null_reason == "the snapshot lacks it"
    assert "overnight short margin" in reg.get("vol_perp_cb_margin_rise").null_reason


# --------------------------------------------------------------------------- 20. the archived data

# proto/results.md, revision 2: the r2-base runs (real history), funding_facts.json, and per sensitivity case
# (windows, swing, ladder adv, kept) in the order of sens_cases()
PROTO = {
    "hl": {"windows": 741, "first_start": '2023-11-01', "last_end": '2026-11-10', "years": 3, "adv_mean": 16.877234994163405, "adv_median": 24.035580084035367, "adv_worst": -87.2960174700543, "adv_worst_start": '2023-11-29', "funding_mean": 7.841414794615787, "cost_mean": 0.5191066171823883, "interest_mean": 2.017722725455279, "liq_loss_mean": 0.0, "kept_mean": 5.30458545197812, "static_adv_mean": 5.861843840304862, "static_funding_mean": 4.990189004440227, "static_kept_mean": 2.66075642807278, "swing_mean": 11.015391153858545, "swing_median": 11.68767022557924, "swing_worst": -42.32386529199563, "swing_worst_start": '2025-10-11', "liq_windows": 0, "min_margin": 2.7212821579352156, "short_mean": 24.5506072874494, "peak_net_max": 475.6924343259458, "trend_mean": -12.804349648928316},  # noqa: E501
    "bn": {"windows": 1317, "first_start": '2022-03-24', "last_end": '2026-10-30', "years": 4, "adv_mean": -16.22968937256849, "adv_median": 11.244270849831928, "adv_worst": -200.8474788820834, "adv_worst_start": '2023-05-26', "funding_mean": 6.335257861750763, "cost_mean": 0.4452623986826011, "interest_mean": 3.5495028289188406, "liq_loss_mean": 0.0, "kept_mean": 2.3404926341493213, "static_adv_mean": -9.316776450042308, "static_funding_mean": 3.405602178682453, "static_kept_mean": 0.02714844683169198, "swing_mean": -6.912912922526183, "swing_median": 1.6712640223865538, "swing_worst": -99.19713263040055, "swing_worst_start": '2023-05-21', "liq_windows": 0, "min_margin": 2.07456052293601, "short_mean": 25.32089327134105, "peak_net_max": 746.1146573583158, "trend_mean": 37.375699587496044},  # noqa: E501
}
PROTO_SENS = {
    "hl": [(741, 11.015391153858545, 16.877234994163405, 5.30458545197812), (365, 4.842651501131458, 11.796706599929433, 0.5327562905495186), (741, 11.195921471026741, 15.551291306943302, 3.9786417647580157), (741, 11.379597670121145, 14.17706838516247, 2.604418842977184), (741, 11.055574143136932, 17.514433025747902, 5.082954739082031), (741, 1.5871969014561962, 8.636651693609059, 1.5092470536027258), (741, 11.026016256319528, 16.947065659010928, 5.37441611682564), (741, 5.882812782596494, 11.60861856354966, 3.2744431595855317), (741, 10.378825326305725, 16.14489877530267, 4.572249233117387), (741, 9.741811438376525, 15.411993297848577, 3.8393437556632923), (741, 11.564256550417761, 17.426100390722624, 5.184036137856777)],  # noqa: E501
    "bn": [(1317, -6.912912922526183, -16.22968937256849, 2.3404926341493213), (365, 6.3361921167080375, 12.733104905807963, -0.915013344854193), (1317, -7.046039594400111, -18.43103870749553, 0.1391432992222816), (1317, -7.1821570828351975, -20.694538319478365, -2.1243563127605536), (1317, -4.786293038517229, -13.328935206113384, 2.1355460452396735), (1317, 0.0362215135798171, -7.757843412483305, -0.6717508879574648), (1317, -6.896600741040526, -16.142808810894373, 2.4273731958234346), (1317, -8.994151076476893, -18.47899117696439, 1.5883610513985822), (1317, -7.419098969604669, -16.85689333457598, 1.7132886721418332), (1317, -7.925711127668608, -17.48454904811461, 1.0856329586032052), (1317, -6.708750267476199, -16.025526717518506, 2.298597923553364)],  # noqa: E501
}
PROTO_FUNDING = {"first": "2025-11-12", "last": "2026-10-30", "hl": (7.483474976392824, 10.923134686827193),
                 "bn": (26.345609065155806, 3.910879872521246), "kr": 55.63954175032479, "hl_const": 77.57318224740321}
# Cases whose definition here differs from the prototype's arithmetic by design (the report says why): the rate
# cases add the points inside each day's log step, ln(1 + (DFF + a) / 36000), where the prototype added a / 36000 to
# the log; Binance's latest year reuses the default windows, where the prototype rebuilt the line from a later
# origin, which moves one exact-tie rung crossing.
NEAR = {("hl", 2): 5e-3, ("hl", 3): 5e-3, ("bn", 2): 5e-3, ("bn", 3): 5e-3, ("bn", 1): 1e-2}
ARCHIVE_STAMP = "2026-11-13T17:28:14+00:00"
NEW_ARCHIVE_OK = "PERPS_NEW_ARCHIVE"     # set while the figures below are re-pinned after a new pull


def same_archive() -> None:
    """The figures here belong to one pull: a new pull fails these tests, rather than skipping them, until the
    figures are re-pinned (or PERPS_NEW_ARCHIVE is set for that run)."""
    got = json.loads((ARCHIVE / "near_perp_hyperliquid_daily.meta.json").read_text()).get("fetched_at")
    if got != ARCHIVE_STAMP:
        if os.environ.get(NEW_ARCHIVE_OK):
            pytest.skip(f"data/raw/perps was pulled at {got}; {NEW_ARCHIVE_OK} is set")
        pytest.fail(f"data/raw/perps was pulled at {got}, not {ARCHIVE_STAMP}: re-pin the prototype figures in "
                    f"this file, or set {NEW_ARCHIVE_OK}=1 for the run that does")


@pytest.fixture(scope="module")
def archived():
    """The real-history run on data/raw/perps (no resampled histories): once per session."""
    if not (ARCHIVE / "near_perp_hyperliquid_daily.csv").exists():
        pytest.skip("data/raw/perps is absent")
    same_archive()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(P, "DRAWS", 0)
        return P.run()


@pytest.mark.parametrize("vid", ["hl", "bn"])
def test_the_archived_data_reproduce_the_prototype(archived, vid):
    res, want = archived, PROTO[vid]
    v, real = res.venues[vid], res.real[vid]
    assert (len(v.starts), v.starts[0].isoformat(), v.last_end.isoformat(), v.years) == (
        want["windows"], want["first_start"], want["last_end"], want["years"])
    assert v.starts[real["adv_worst_i"]].isoformat() == want["adv_worst_start"]
    assert v.starts[real["swing_worst_i"]].isoformat() == want["swing_worst_start"]
    assert real["liq_windows"] == want["liq_windows"]
    for mine, theirs in (("adv_mean", "adv_mean"), ("adv_median", "adv_median"), ("adv_worst", "adv_worst"),
                         ("funding", "funding_mean"), ("costs", "cost_mean"), ("interest", "interest_mean"),
                         ("liq_loss", "liq_loss_mean"), ("kept_mean", "kept_mean"), ("static_adv", "static_adv_mean"),
                         ("static_funding", "static_funding_mean"), ("static_kept", "static_kept_mean"),
                         ("swing_mean", "swing_mean"), ("swing_median", "swing_median"),
                         ("swing_worst", "swing_worst"), ("min_margin", "min_margin"), ("short", "short_mean"),
                         ("peak", "peak_net_max"), ("trend", "trend_mean")):
        assert real[mine] == pytest.approx(want[theirs], abs=1e-6), mine
    assert v.partial == ([date(2024, 8, 15)] if vid == "hl" else [])
    assert res.null_reason[vid] == "vol_null_draws is zero"


@pytest.mark.parametrize("vid", ["hl", "bn"])
def test_the_archived_sensitivity_rows_match_the_prototype(archived, vid):
    rows = archived.sens[vid]
    assert [r["label"] for r in rows] == [label for label, _ in P.sens_cases()]
    for i, (row, (n, swing, adv, kept)) in enumerate(zip(rows, PROTO_SENS[vid])):
        tol = NEAR.get((vid, i), 1e-6)
        assert row["windows"] == n, row["label"]
        for got, want in ((row["swing_mean"], swing), (row["adv_mean"], adv), (row["kept_mean"], kept)):
            assert got == pytest.approx(want, abs=tol), row["label"]


def test_the_archived_funding_facts_and_terms(archived):
    fx = archived.facts
    assert (fx["first"].isoformat(), fx["last"].isoformat()) == (PROTO_FUNDING["first"], PROTO_FUNDING["last"])
    v = fx["venues"]
    for vid in ("hl", "bn"):
        assert (v[vid]["short_paid"], v[vid]["year"]) == pytest.approx(PROTO_FUNDING[vid], abs=1e-6)
    assert v["kr"]["short_paid"] == pytest.approx(PROTO_FUNDING["kr"], abs=1e-6)
    assert v["hl"]["const"] == pytest.approx(PROTO_FUNDING["hl_const"], abs=1e-6)
    # Hyperliquid's maintenance margin is half the initial margin at its maximum leverage
    assert P.MAINT == 1 / (2 * archived.terms["max_leverage"])


CB_ARCHIVE_STAMP = "2026-11-14T03:26:48+00:00"


@pytest.fixture(scope="module")
def cb_archived():
    """Coinbase's NEAR Perp as archived (its own pull, so its own stamp)."""
    if not (ARCHIVE / "near_perp_coinbase_terms.json").exists():
        pytest.skip("data/raw/perps holds no Coinbase snapshot")
    return P.read_coinbase()


def test_the_ladders_collateral_covers_coinbases_short_margin(cb_archived):
    """The ladder keeps vol_perp_collateral dollars per dollar of its largest short on the venue (the cap); Coinbase's
    overnight and intraday short margins, fractions of the short's value, must fit inside that on the archived
    snapshot, whatever the pull."""
    cb = cb_archived
    assert cb.get("reason") is None
    assert 0 < cb["short_intraday"] <= cb["short_overnight"] <= param("vol_perp_collateral")
    # at the cap the short is worth K s of the stake and the deposit COLLATERAL K s: equity per dollar short
    assert CAPN / (P.RUNGS * P.SLICE) == param("vol_perp_collateral") >= cb["short_overnight"]
    # with no top-up, equity 1 - r per dollar short meets the margin m (1 + r) at r = margin_rise
    r, m = cb["margin_rise"], cb["short_overnight"]
    assert 0 < r < 1 and 1 - r == pytest.approx(m * (1 + r), abs=1e-12)


def test_the_archived_coinbase_figures(cb_archived):
    got = json.loads((ARCHIVE / "near_perp_coinbase_terms.meta.json").read_text()).get("fetched_at")
    if got != CB_ARCHIVE_STAMP:
        if os.environ.get(NEW_ARCHIVE_OK):
            pytest.skip(f"the Coinbase snapshot was pulled at {got}; {NEW_ARCHIVE_OK} is set")
        pytest.fail(f"the Coinbase snapshot was pulled at {got}, not {CB_ARCHIVE_STAMP}: re-pin the figures here")
    cb = cb_archived
    assert (cb["listed"], cb["first_bar"], cb["last_bar"]) == (date(2026, 2, 28), date(2026, 2, 28),
                                                             date(2026, 11, 13))
    assert (cb["days"], cb["missing_days"], cb["contract"], cb["interval_h"]) == (259, 0, 500, 1)
    assert cb["oi_usd"] == pytest.approx(4457 * 500 * 3.384953, abs=1e-6)
    assert (cb["short_overnight"], cb["short_intraday"]) == (0.561625, 0.1999385)
    # under a year of bars: no one-year window, so no claim could read this venue yet
    assert cb["days"] < H + 1


def test_the_built_numbers_carry_the_resampled_figures():
    """The full draw count runs once in the build; when site/data/numbers.json holds its figures, they are 7.3's."""
    path = REPO / "site" / "data" / "numbers.json"
    numbers_json = json.loads(path.read_text()) if path.exists() else {}
    if "vol_perp_hl_swing_share" not in numbers_json:
        pytest.skip("site/data/numbers.json predates the perp ladder")
    if not (ARCHIVE / "near_perp_hyperliquid_daily.csv").exists():
        pytest.skip("data/raw/perps is absent")
    same_archive()
    want = {"vol_perp_hl_rank": 97.2, "vol_perp_bn_rank": 46.9, "vol_perp_hl_swing_share": 77.6,
            "vol_perp_bn_swing_share": 53.8, "vol_perp_hl_null_mean": -26.7192, "vol_perp_bn_null_mean": -22.3684,
            "vol_perp_hl_swing_raw_mean": -17.8168, "vol_perp_bn_swing_raw_mean": -14.5041,
            "vol_perp_band": "inside", "vol_perp_swing_band": "inside"}
    assert {k: numbers_json[k]["value"] for k in want} == want


# --------------------------------------------------------------------------- 21. the full draw count

def test_full_draw_count_runs_the_same_in_parallel_and_serially(raw, monkeypatch):
    world(raw, H + 6)                                     # six windows on each venue
    monkeypatch.setenv("SIM_WORKERS", "1")
    few = P.run()
    monkeypatch.setattr(P, "DRAWS", int(param("vol_null_draws")))
    serial = P.run()
    assert serial.workers == 1 and serial.draws == int(param("vol_null_draws"))
    assert all(len(serial.nulls[v]) == len(serial.redraws[v]) == serial.draws for v in ("hl", "bn"))
    # one stream: the first histories of the full run are the histories of the short run
    assert few.nulls["hl"] == serial.nulls["hl"][:FEW] and few.redraws["bn"] == serial.redraws["bn"][:FEW]
    monkeypatch.setenv("SIM_WORKERS", "3")
    monkeypatch.setattr(P, "CHUNK", 90)
    fan = P.run()
    assert fan.nulls == serial.nulls and fan.redraws == serial.redraws
    if fan.fallback:
        pytest.skip(f"the same result, but serially: {fan.fallback}")
    assert fan.workers == 3
