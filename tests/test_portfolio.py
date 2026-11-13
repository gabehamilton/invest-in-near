"""The desk simulation's model (pipeline/models/portfolio.py): spec section 9, tests 3 to 8, and the display rounding.

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


def ev(st: pf.State, inp: pf.Inputs | None = None) -> dict:
    P, sims, base = tw.twin()
    return pf.evaluate(sims, inp or base, st, P["spread"], pf.cfg_payload(P))


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
def test_unit_distribution_does_not_depend_on_share_or_desk_size():
    _, _, inp = tw.twin()
    st = default_state()
    keys = ("p05", "median", "p95", "es95", "es975", "p_beat", "be_mu_median", "ahead_n", "dots")
    base = {k: ev(st)["dist"][k] for k in keys}
    for other in (ev(replace(st, w=st.w * 2)), ev(replace(st, w=st.w / 3)), ev(st, replace(inp, V=inp.V * 7))):
        assert {k: other["dist"][k] for k in keys} == base


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
    st = default_state()
    _, M = pf.units(sims[st.win], st, inp.rates)
    be = ev(st)["dist"]["be_mu_median"]
    assert be == pf.q7(M, P["tail"]["mid"])
    n = len(M)
    assert abs(ev(replace(st, mu=be))["dist"]["p_beat"] - P["fmt"]["pct"] / 2) <= 2 * P["fmt"]["pct"] / n


# ---------------------------------------------------------------------------------- 6. common random numbers

@needs_payload
def test_controls_leave_the_simulated_paths_alone():
    P, sims, _ = tw.twin()
    before = [[tw.digest(s[k]) for k in ("G", "X", "Gs")] + [sum(s["H"])] for s in sims]
    for c in tw.states(P):
        ev(pf.state_of(c, P))
        pf.outcomes(sims, pf.inputs_from_payload(P), c, P)
    assert [[tw.digest(s[k]) for k in ("G", "X", "Gs")] + [sum(s["H"])] for s in sims] == before


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
    assert all(name in P["copy"] for name in page["notes"] + page["left"])


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
