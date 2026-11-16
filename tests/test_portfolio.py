"""The desk simulation's model (pipeline/models/portfolio.py): spec section 9, tests 3 to 8, the display rounding,
and strategy (b) in the desk (design-v6 spec 8.2, tests 1 to 10).

The model runs from site/data/sim.json through the Python twin (tests/_sim_twin.py), so these tests check the same
doubles the browser simulates. Registry values come from site/data/numbers.json. Every test skips when the payload
is absent (the simulation's inputs are not in data/raw).
"""

from __future__ import annotations

import json
import math
import re
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from pipeline import numbers
from pipeline.models import portfolio as pf

sys.path.insert(0, str(Path(__file__).parent))
import _sim_twin as tw  # noqa: E402

NUMBERS = tw.ROOT / "site" / "data" / "numbers.json"
needs_payload = pytest.mark.skipif(tw.payload() is None or not NUMBERS.exists(), reason="no sim.json or numbers.json")


def registry() -> dict:
    return json.loads(NUMBERS.read_text(encoding="utf-8"))


_EV: dict = {}


def ev(st: pf.State, inp: pf.Inputs | None = None) -> dict:
    """evaluate() on the full twin; a state's figures are computed once per session (inputs other than the payload's
    are never cached)."""
    P, sims, base = tw.twin()
    if inp is not None:
        return pf.evaluate(sims, inp, st, P["spread"], pf.cfg_payload(P))
    key = repr(st)
    if key not in _EV:
        _EV[key] = pf.evaluate(sims, base, st, P["spread"], pf.cfg_payload(P))
    return _EV[key]


def default_state() -> pf.State:
    P = tw.payload()
    return pf.state_of(P["default"], P)


# ------------------------------------------------------------------------------------------------ 3. the mean

@needs_payload
def test_matched_factors_average_one_and_the_simulated_mean_is_the_closed_form():
    P, sims, inp = tw.twin()
    for sim in sims:
        assert abs(pf.mean(sim["G"]) - 1) < 1e-12 and abs(pf.mean(sim["X"]) - 1) < 1e-12
    for c in tw.states(P):
        st = pf.state_of(c, P)
        if st.w == 0:
            continue
        cf = pf.closed_form(st, inp.rates, inp.V)
        S, _ = pf.units(sims[st.win], st, inp.rates)
        assert abs(cf["part"] * pf.mean(S) - cf["mean"]) <= 1e-9 * cf["part"], c
        assert abs(cf["carry"] + cf["price"] + cf["cost"] - cf["mean"]) <= 1e-9 * cf["part"], c


# ------------------------------------------------------------------------------------------- 4. drift guard

@needs_payload
def test_window_factors_average_one_in_level_not_in_log():
    for w in tw.payload()["windows"]:
        assert abs(pf.mean(w["a"]) - 1) < 1e-12 and abs(pf.mean(w["b"]) - 1) < 1e-12
        assert abs(pf.mean([math.log(a) for a in w["a"]])) > 1e-6, "demeaned in log would hand NEAR a drift"


@needs_payload
def test_drift_presets_are_their_formulas():
    mu, reg = tw.payload()["mu"], registry()
    g, m2, cpi = (reg[k]["value"] / 100 for k in ("g_365", "m2_yoy", "cpi_yoy"))
    assert mu["flat"] == 0.0
    assert mu["value"] == pf.sig12(1 / (1 + g) - 1)
    assert mu["thesis"] == pf.sig12((1 + m2) / (1 + g) - 1)
    assert mu["cpi"] == pf.sig12((1 + cpi) / (1 + g) - 1)


@needs_payload
def test_no_sim_key_reads_price_history():
    """The drift is a stated assumption: no sim_* figure takes a past price change, return or peak as an input."""
    history = re.compile(r"return|\bret_|peak|\bath\b|all.time|trailing|drawdown|_chg\b|pct_change", re.I)
    reg = registry()
    keys = [k for k in reg if k.startswith("sim_")]
    assert keys
    for k in keys:
        for i in reg[k].get("inputs") or []:
            assert not history.search(i), f"{k} lists {i!r}"


# ---------------------------------------------------------------------------------------------- 5. properties

@needs_payload
def test_no_near_share_gives_zeros():
    out = ev(replace(default_state(), w=0.0))
    raw, specs = out["raw"], tw.payload()["specs"]
    dollars = [k for k in raw if specs[k][0].startswith("usd") and k != "sim_cash_interest_usd"]
    assert "sim_mean_usd" in dollars and "sim_es1d975_usd" in dollars
    for k in dollars:
        assert raw[k] == 0, k
    assert raw["sim_exit_days"] == raw["sim_lst_share"] == raw["sim_es_desk_pct"] == 0
    assert all(d == 0 for d in out["dots"])


@needs_payload
def test_unit_distribution_does_not_depend_on_desk_size_nor_held_on_share():
    # held, the NEAR part's own year scales with it; reset, the share sets how much each reset trades, so only the
    # desk size drops out
    _, _, inp = tw.twin()
    keys = ("p05", "median", "p95", "es95", "es975", "p_beat", "be_mu_median", "ahead_n", "dots")
    held = replace(default_state(), rb=0)
    base = {k: ev(held)["dist"][k] for k in keys}
    others = (ev(replace(held, w=held.w * 2)), ev(replace(held, w=held.w / 3)), ev(held, replace(inp, V=inp.V * 7)))
    for other in others:
        assert {k: other["dist"][k] for k in keys} == base
    st = default_state()
    reset = ev(st)
    assert {k: ev(st, replace(inp, V=inp.V * 7))["dist"][k] for k in keys} == {k: reset["dist"][k] for k in keys}
    assert {k: ev(replace(st, w=st.w * 2))["dist"][k] for k in keys} != {k: reset["dist"][k] for k in keys}


@needs_payload
def test_outcomes_rise_with_the_drift():
    P = tw.payload()
    st = default_state()
    mus = [P["mu"]["lo"], P["mu"]["value"], P["mu"]["flat"], P["mu"]["thesis"], P["mu"]["hi"]]
    runs = [ev(replace(st, mu=m))["raw"] for m in sorted(mus)]
    for k in ("sim_mean_usd", "sim_median_usd", "sim_p05_usd", "sim_p95_usd", "sim_p_beat_r"):
        seq = [r[k] for r in runs]
        assert seq == sorted(seq), k
    assert all(a["sim_mean_usd"] < b["sim_mean_usd"] for a, b in zip(runs, runs[1:]))


@needs_payload
def test_bad_year_average_is_at_least_the_one_in_twenty_loss():
    P = tw.payload()
    for c in tw.states(P):
        d = ev(pf.state_of(c, P))["dist"]
        assert d["es95"] >= 0 - d["p05"], c
        assert d["es975"] >= d["es95"], c


@needs_payload
def test_staking_off_is_a_zero_yield():
    _, _, inp = tw.twin()
    st = default_state()
    off = ev(replace(st, k=0))
    zero = ev(st, replace(inp, rates=replace(inp.rates, y=0.0)))
    assert off["raw"] == zero["raw"] and off["dist"] == zero["dist"]


@needs_payload
def test_the_median_break_even_drift_wins_half_the_years():
    P, sims, inp = tw.twin()
    st = replace(default_state(), rb=0)      # held: the quantile of the per-path break-evens (test 6 has the reset)
    _, M = pf.units(sims[st.win], st, inp.rates)
    be = ev(st)["dist"]["be_mu_median"]
    assert be == pf.q7(M, P["tail"]["mid"])
    n = len(M)
    assert abs(ev(replace(st, mu=be))["dist"]["p_beat"] - P["fmt"]["pct"] / 2) <= 2 * P["fmt"]["pct"] / n


# ---------------------------------------------------------------------------------- 6. common random numbers

@needs_payload
def test_controls_leave_the_simulated_paths_alone():
    P, sims, _ = tw.twin()

    def state() -> list:
        return [[tw.digest(s[k]) for k in ("G", "X", "Gs")] + [sum(s["H"])] +
                [tw.digest(q) for q in s["gq"]] + [tw.digest(q) for q in s["xq"]] + [s["kG"], s["kX"]] for s in sims]
    before = state()
    for c in tw.states(P):
        ev(pf.state_of(c, P))
        pf.outcomes(sims, pf.inputs_from_payload(P), c, P)
    assert state() == before


# --------------------------------------------------------------------------------------------- 7. consistency

@needs_payload
def test_simulated_year_dispersion_matches_realized_volatility():
    _, sims, _ = tw.twin()
    logs = [math.log(g) for g in sims[0]["G"]]
    m = pf.mean(logs)
    sd = math.sqrt(pf.total((v - m) ** 2 for v in logs) / (len(logs) - 1))
    rv = registry()["rv_365_near"]["value"] / 100
    assert abs(sd / rv - 1) < 0.03, (sd, rv)


# ------------------------------------------------------------------------------------------ 8. seed stability

@needs_payload
def test_every_seed_spread_is_below_its_display_step():
    reg = registry()
    spreads = {k[len("sim_mc_spread_"):]: v["value"] for k, v in reg.items() if k.startswith("sim_mc_spread_")}
    assert spreads
    for name, spread in spreads.items():
        assert spread < reg["sim_mc_step_" + name]["value"], name


# --------------------------------------------------------------------------- the default state and the payload

@needs_payload
def test_python_twin_prints_the_registry_at_the_defaults():
    P, sims, inp = tw.twin()
    reg = registry()
    out = pf.outcomes(sims, inp, P["default"], P)
    assert {k: out[k] for k in P["expect"]} == P["expect"]
    assert all(reg[k]["display"] == v for k, v in P["expect"].items() if k in reg)


@needs_payload
def test_the_plot_counts_are_the_tails_they_name():
    # "N of sim_dots simulated years end worse than p05 / better than p95": each count is its quantile's tail, and the
    # worse count is also the bad-year average's tail
    reg, tail = registry(), tw.payload()["tail"]
    dots = reg["sim_dots"]["value"]
    assert dots == tail["dots"]
    assert reg["sim_tail_n"]["value"] == round(tail["lo"] * dots) == dots // tail["es_every"]
    assert reg["sim_tail_hi_n"]["value"] == dots - round(tail["hi"] * dots)


@needs_payload
def test_every_name_the_page_lists_resolves():
    P, sims, inp = tw.twin()
    d = pf.outcomes(sims, inp, P["default"], P)
    page = P["page"]
    for key, chip, text, sub in page["tiles"]:
        assert key in d and chip in P["chips"] and text in P["copy"] and (not sub or sub in P["copy"])
    for key, label, mode in page["strip"]:
        assert key in d and label in P["copy"] and (not mode or mode in P["mu"])
    for label, fig in page["liq"]:
        assert label in P["copy"] and (fig in P["copy"] or fig in d)
    assert all(name in P["copy"] for name in page["notes"] + page["left"] + [page["compare"]])
    assert page["notes"][page["notes"].index("sim_method") + 1] == "sim_rebal_note"
    assert page["left"][-1] == "sim_left_resets" and P["chips"]["rb"] == "H10"


# ------------------------------------------------------------------------------------------- display rounding

def test_rounding_goes_half_away_from_zero_and_never_prints_minus_zero():
    assert (pf.round_dp(2.5, 0), pf.round_dp(-2.5, 0)) == (3.0, -3.0)
    assert (pf.round_dp(0.125, 2), pf.round_dp(-0.125, 2)) == (0.13, -0.13)
    assert (pf.round_step(15000.0, 10000), pf.round_step(-15000.0, 10000)) == (20000.0, -20000.0)
    for v in (pf.round_dp(-0.004, 2), pf.round_step(-4000.0, 10000)):
        assert v == 0 and math.copysign(1, v) == 1
    assert pf.display(-0.004, ["pct", 2]) == pf.display(0.004, ["pct", 2]) == numbers.fmt(0.0, "%", 2)
    # a compact dollar figure is the registry's own format of the rounded value, ties included
    for v, step in ((2.5e12, 1e10), (9.995e6, 10000), (1.25e6, 10000)):
        r, dp = pf.shown(v, ["usd", step])
        assert pf.display(v, ["usd", step]) == numbers.fmt(r, "USD", dp)


# ------------------------------------------------------------------ strategy (b) in the desk (design-v6 spec 8.2)

def day_loop(P: dict, n: int) -> list[dict]:
    """F-sim-path without period products, as the held-only engine ran it: one u32 a day for both windows."""
    rng = pf.Prng(P["seed"])
    wins = [(w["a"], w["b"]) for w in P["windows"]]
    out = [([0.0] * n, [0.0] * n, [0] * n) for _ in wins]
    for i in range(n):
        g, x, h = [1.0] * len(wins), [1.0] * len(wins), [0] * len(wins)
        for _ in range(P["horizon"]):
            r = rng.next()
            for k, (a, b) in enumerate(wins):
                j = (r * len(a)) >> 32
                g[k] *= a[j]
                x[k] *= b[j]
                if g[k] <= P["half"]:
                    h[k] = 1
        for k in range(len(wins)):
            out[k][0][i], out[k][1][i], out[k][2][i] = g[k], x[k], h[k]
    return [pf.matched(G, X, H) for G, X, H in out]


def held_units(sim: dict, st: pf.State, R: pf.Rates) -> list[float]:
    """F-sim-unit per path, in path order (portfolio.units sorts them)."""
    ru, eur_f, y = pf.legs(st, R)
    near_f = (1 + st.mu) * (1 + st.k * y)
    return [near_f * g - 1 - (st.s * ru + (1 - st.s) * (eur_f * x - 1)) - st.c for g, x in zip(sim["G"], sim["X"])]


def reset_units(sim: dict, st: pf.State, R: pf.Rates, reset: bool = True) -> list[float]:
    A, U, E = pf.reb_factors(st, R, sim["ell"], sim["kG"], sim["kX"])
    M, K, T = pf.reb_paths(sim, A, U, E, st.w, st.s, reset=reset)
    return pf.reb_units(M, K, T, st.w, st.h, st.c)


@needs_payload
def test_1_period_products_leave_the_day_engine_bit_identical():
    P, sims, _ = tw.small()
    ref = day_loop(P, P["paths"])
    for sim, r in zip(sims, ref):
        for k in ("G", "X", "Gs"):
            assert tw.digest(sim[k]) == tw.digest(r[k]), k
        assert sim["H"] == r["H"]
        assert (sim["mg"], sim["mx"]) == (r["mg"], r["mx"])
        # the period products multiply out to the raw year and the day factors undo the matching
        assert sim["ell"] == pf.periods(P["rebal_days"], P["horizon"]) and sum(sim["ell"]) == P["horizon"]
        for i in range(0, P["paths"], 97):
            g = x = 1.0
            for p in range(len(sim["ell"])):
                g, x = g * sim["gq"][p][i], x * sim["xq"][p][i]
            assert abs(g / sim["mg"] - sim["G"][i]) <= 1e-12 * sim["G"][i]
            assert abs(x / sim["mx"] - sim["X"][i]) <= 1e-12 * sim["X"][i]
        assert abs(pf.pw(sim["kG"], P["horizon"]) * sim["mg"] - 1) < 1e-12
        assert abs(pf.pw(sim["kX"], P["horizon"]) * sim["mx"] - 1) < 1e-12


@needs_payload
def test_2_held_through_the_periods_is_f_sim_unit():
    # 1.3 without the resets (the gc normalisation): within 1e-9 of each path's u, relative (absolute below one)
    P, sims, inp = tw.twin()
    for c in (tw.ctrl(P), tw.ctrl(P, win=1), tw.ctrl(P, w=0.08, mu="thesis", s=0.5), tw.ctrl(P, k=0, mu="value")):
        st = pf.state_of(c, P)
        sim = sims[st.win]
        for a, b in zip(reset_units(sim, st, inp.rates, reset=False), held_units(sim, st, inp.rates)):
            assert abs(a - b) <= 1e-9 * max(abs(b), 1.0), c


@needs_payload
def test_3_resetting_an_all_near_desk_is_holding_it():
    P, sims, inp = tw.twin()
    for win in (0, 1):
        st = replace(pf.state_of(tw.ctrl(P, win=win), P), w=1.0)
        sim = sims[win]
        for a, b in zip(reset_units(sim, st, inp.rates), held_units(sim, st, inp.rates)):
            assert abs(a - b) <= 1e-9 * max(abs(b), 1.0)


@needs_payload
def test_4_the_untraded_book_is_the_closed_form_and_the_resets_add_their_edge():
    P, sims, inp = tw.twin()
    for c in (tw.ctrl(P), tw.ctrl(P, w=0.08, mu="thesis"), tw.ctrl(P, k=0, s=0.5)):
        st = pf.state_of(c, P)
        cf = pf.closed_form(st, inp.rates, inp.V)
        A0, U0, E0 = pf.reb_factors(st, inp.rates, sims[0]["ell"])
        Mh, Kh, Th = pf.book(A0, U0, E0, st.w, st.s, reset=False)
        # relative to the book: the mean is the small difference of two books near one, so its own relative error
        # is that of the books divided by the mean's share of them (about 5e-12 at the defaults)
        assert Th == 0 and abs((Mh - Kh) - cf["mean"] / inp.V) <= 1e-12 * Mh, c
        reset, held = ev(st), ev(replace(st, rb=0))
        assert reset["raw"]["sim_mean_usd"] == held["raw"]["sim_mean_usd"] + reset["raw"]["sim_reb_edge_usd"]
        assert held["raw"]["sim_mean_usd"] == cf["mean"]


@needs_payload
def test_5_the_break_even_trading_cost_erases_the_expected_benefit():
    P, _, _ = tw.twin()
    r = ev(default_state())["raw"]["sim_be_cost"] / 100
    at = pf.state_of(tw.ctrl(P, rt=P["bp"] * r), P)
    assert at.cu == 0 and at.h == at.rt / (2 * pf.BP)
    assert abs(ev(at)["raw"]["sim_mean_usd"]) < 1


@needs_payload
def test_6_the_median_break_even_is_the_middle_path_break_even():
    P, sims, inp = tw.twin()
    st = default_state()
    sim, half = sims[st.win], len(sims[st.win]["G"]) / 2
    be = pf.be_median_reb(sim, st, inp.rates, *inp.mu_bounds)
    assert ev(st)["raw"]["sim_be_mu_median"] == 100 * be

    def ahead(mu: float, s: dict = sim) -> int:
        return sum(1 for u in reset_units(s, replace(st, mu=mu), inp.rates) if u > 0)
    assert ahead(be) >= half > ahead(math.nextafter(be, -math.inf))
    # the pruned bisection equals one that counts every path at every step (small twin)
    Ps, small, si = tw.small()
    s0 = small[st.win]
    lo, hi = si.mu_bounds
    while True:
        mid = (lo + hi) / 2
        if mid <= lo or mid >= hi:
            break
        if ahead(mid, s0) >= len(s0["G"]) / 2:
            hi = mid
        else:
            lo = mid
    assert pf.be_median_reb(s0, st, si.rates, *si.mu_bounds) == hi


@needs_payload
def test_7_the_reset_budget_share_spends_the_cash_rate_within_a_float():
    P, sims, inp = tw.twin()
    st, tail = default_state(), P["tail"]
    rbar = pf.closed_form(st, inp.rates, inp.V)["rbar"]

    def f(w: float, sim: dict) -> float:
        return w * pf.es_unit_reb(sim, replace(st, w=w), inp.rates, tail) - rbar
    # the build's share (sig12 in the payload) on the full twin, and the registry's rounding of it
    w = P["share"]["w_b_reb"]
    assert abs(f(w, sims[st.win])) <= 1e-12
    assert registry()["sim_w_budget_reb"]["value"] == pf.round_dp(100 * w, 2)
    # the bisection's bracket on the small twin: the share spends at most the rate, the next float more
    _, small, _ = tw.small()
    ws = pf.w_budget_reb(small[st.win], st, inp.rates, inp.V, tail, P["share"]["w_b"])
    assert 0 < ws < P["share"]["w_b"]
    assert f(ws, small[st.win]) <= 0 < f(math.nextafter(ws, math.inf), small[st.win])


# today's displays at the defaults, held without trading (design-v6 spec 8.3, the held column)
HELD_DEFAULTS = {
    "sim_mean_usd": "$90,000", "sim_cost_usd": "$0", "sim_median_usd": "-$8.0 million", "sim_p05_usd": "-$17.1 million",
    "sim_p95_usd": "$45 million", "sim_es95_usd": "$17.80 million", "sim_p_beat_r": "30", "sim_p_beat_n": "30",
    "sim_p_behind_n": "70", "sim_es_interest_years": "1.0", "sim_es_desk_pct": "3.6%", "sim_be_mu_median": "70%",
    "sim_be_mu_mean": "-0.46%", "sim_be_cost": "0.48%", "sim_median_price": "-42%", "sim_p_half": "65",
    "sim_median_dir": "behind", "sim_mean_dir": "ahead", "sim_be_mu_median_dir": "rise",
}
# the defaults, reset every vol_rebal_days (the 8.3 reset column)
RESET_DEFAULTS = {
    "sim_mean_usd": "$90,000", "sim_cost_usd": "$0", "sim_median_usd": "-$1 million", "sim_p05_usd": "-$31 million",
    "sim_p95_usd": "$36 million", "sim_es95_usd": "$38 million", "sim_p_beat_r": "45", "sim_p_beat_n": "47",
    "sim_p_behind_n": "53", "sim_es_interest_years": "2.1", "sim_es_desk_pct": "7.6%", "sim_be_mu_median": "5%",
    "sim_be_mu_mean": "-0.46%", "sim_be_cost": "0.20%", "sim_reb_edge_usd": "-$180",
    "sim_reb_turnover_usd": "$51.2 million", "sim_median_price": "-42%", "sim_p_half": "65",
    "sim_strategy_mode": "rebalance", "sim_held_median_usd": "-$8.0 million", "sim_held_p95_usd": "$45 million",
    "sim_held_es95_usd": "$17.80 million", "sim_held_p_beat_r": "30", "sim_held_be_mu_median": "70%",
    "sim_reb_median_usd": "-$1 million", "sim_reb_p95_usd": "$36 million", "sim_reb_es95_usd": "$38 million",
    "sim_reb_p_beat_r": "45", "sim_reb_be_mu_median": "5%",
}


@needs_payload
def test_8_held_mode_at_the_defaults_prints_todays_desk():
    P, sims, inp = tw.twin()
    d = pf.outcomes(sims, inp, tw.ctrl(P, rb=0), P)
    assert {k: d[k] for k in HELD_DEFAULTS} == HELD_DEFAULTS
    assert d["sim_strategy_mode"] == "hold" and d["sim_changed"] == "yes"
    # every key the two modes share prints the same; the held comparison keys are the held figures
    reset = pf.outcomes(sims, inp, P["default"], P)
    mode = set(HELD_DEFAULTS) | {"sim_p05_dir", "sim_p95_dir", "sim_be_cost_dir", "sim_be_mu_mean_dir",
                                 "sim_strategy_mode", "sim_changed"}
    assert {k: v for k, v in d.items() if k not in mode} == {k: v for k, v in reset.items() if k not in mode}
    for k in ("median_usd", "p95_usd", "es95_usd", "p_beat_r", "be_mu_median"):
        assert d["sim_" + k] == d["sim_held_" + k] == reset["sim_held_" + k], k


@needs_payload
def test_9_reset_mode_at_the_defaults_prints_the_spec_figures():
    P, sims, inp = tw.twin()
    d = pf.outcomes(sims, inp, P["default"], P)
    assert P["default"]["rb"] == pf.DEFAULT_RB == 1
    assert {k: d[k] for k in RESET_DEFAULTS} == RESET_DEFAULTS
    reg = registry()
    assert (reg["sim_w_budget"]["display"], reg["sim_w_budget_reb"]["display"]) == ("3.79%", "1.76%")
    assert reg["sim_es975_usd"]["display"] == "$42.2 million"
    assert reg["sim_share"]["value"] == reg["vol_rebal_share"]["value"]   # the desk runs the strategy H10 tested
    assert P["rebal_days"] == reg["vol_rebal_days"]["value"]


@needs_payload
def test_10_every_reset_spread_is_below_its_printed_step():
    P, _, inp = tw.twin()
    steps = {"usd*": P["steps"]["usd"], "pct*": P["steps"]["pp"], "count*": P["steps"]["count"]}
    for win, sp in enumerate(P["spread"]):
        st = pf.state_of(tw.ctrl(P, win=win), P)
        cf = pf.closed_form(st, inp.rates, inp.V)
        scale = abs(cf["part"] * cf["near_f"])
        for key in ("r_median", "r_p05", "r_p95", "r_es95", "r_turnover", "r_p_beat", "r_be_median"):
            assert sp[key] == pf.sig12(sp[key]) and sp[key] > 0, key
        spec = ev(st)["spec"]
        for name, (kind, arg) in {**P["specs_rb"], **{k: v for k, v in P["specs"].items() if "_reb_" in k}}.items():
            if kind not in steps or arg == "r_cost":
                continue
            need = scale * sp[arg] if kind == "usd*" else sp[arg]
            assert need < spec[name][1] == pf.first_above(steps[kind], need), (win, name)


@needs_payload
def test_the_payload_carries_the_reset_fields():
    P = tw.payload()
    assert P["specs_rb"] == pf.specs_rb() and set(P["specs_rb"]) <= set(P["specs"])
    assert P["share"]["w_b_reb"] < P["share"]["w_b"]
    assert P["page"]["compare"] == "sim_compare" and P["bp"] == pf.BP
    for key in ("sim_strategy_mode", "sim_reb_edge_usd", "sim_reb_turnover_usd", "sim_held_median_dir",
                "sim_reb_median_dir", "sim_held_be_mu_median_dir", "sim_reb_be_mu_median_dir"):
        assert key in P["expect"], key
    assert pf.CONTROLS[:2] == ("w", "rb") and "rb" in P["default"]


@needs_payload
def test_c30_dots_carry_their_own_years_price_factor():
    # reset mode: dot k is the path at rank (2k - 1) N / 200 of the reset outcomes, and its factor is that year's G
    P, sims, inp = tw.twin()
    st = default_state()
    e = ev(st)
    sim, n = sims[st.win], len(sims[st.win]["G"])
    order = sorted(range(n), key=lambda i: (e["u_reb"][i], i))
    chart = json.loads((tw.ROOT / "site" / "data" / "charts" / "c30.json").read_text(encoding="utf-8"))
    rows = chart["table"]["rows"]
    assert len(rows) == P["tail"]["dots"]
    for k, row in enumerate(rows, start=1):
        i = order[(2 * k - 1) * n // (2 * P["tail"]["dots"])]
        assert row[2] == f"{sim['G'][i]:.2f}"
        assert e["S"][(2 * k - 1) * n // (2 * P["tail"]["dots"])] == e["u_reb"][i]
