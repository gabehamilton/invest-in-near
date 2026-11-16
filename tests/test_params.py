"""Model parameters (thesis/rules.toml [params]) against the rule literals they mirror.

A verdict rule and the model that feeds it must use one threshold. The rule expressions keep their literals so the
page prints a readable rule; this test pins each [params] entry to the literal in the rule, and checks that the
models actually use the parameter, so neither side can change alone.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

from pipeline.models import advantage, market, params, perps, staking, supply, sweep, volatility

RULES = tomllib.loads((Path(__file__).resolve().parents[1] / "thesis" / "rules.toml").read_text())
P = RULES["params"]


def comparisons(expr: str) -> list[tuple[str, str, float]]:
    """(left-hand source, operator, right-hand literal) for every `<expr> <op> <number>` in a rule expression."""
    out = []
    for node in ast.walk(ast.parse(expr, mode="eval")):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.comparators[0], ast.Constant):
            out.append((ast.unparse(node.left), type(node.ops[0]).__name__, node.comparators[0].value))
    return out


def literal(expr: str, left: str, op: str) -> float:
    hits = [v for lhs, o, v in comparisons(expr) if lhs == left and o == op]
    assert len(hits) == 1, (expr, left, op, comparisons(expr))
    return hits[0]


def test_params_loader_reads_the_rules_file():
    assert params.params() == P
    with pytest.raises(KeyError):
        params.param("no_such_parameter")


def test_h4_share_matches_the_rule():
    assert literal(RULES["H4"]["supported"], "adv_share_le0", "LtE") == P["adv_le0_max_pct"]
    assert literal(RULES["H4"]["refuted"], "adv_share_le0", "Gt") == P["adv_le0_max_pct"]
    assert f"{int(P['adv_le0_max_pct'])}% of the months" in RULES["H4"]["test"]
    assert advantage.ADV_LE0_MAX_PCT == P["adv_le0_max_pct"]


def test_h10_strategies_match_the_rule():
    rule = RULES["H10"]
    for key in ("vol_years", "vol_perp_hl_years", "vol_perp_bn_years"):
        assert literal(rule["not_computed_if"], key, "Lt") == P["vol_min_years"]
    for key in ("vol_rally_rank", "vol_rebal_rank", "vol_perp_hl_rank", "vol_perp_bn_rank"):
        assert literal(rule["supported"], key, "GtE") == P["vol_rank_hi"]
        assert literal(rule["supported"], key, "Gt") == P["vol_rank_lo"]
        assert literal(rule["refuted"], key, "LtE") == P["vol_rank_lo"]
        assert literal(rule["refuted"], key, "Lt") == P["vol_rank_hi"]
    assert P["vol_rank_lo"] == 100 - P["vol_rank_hi"]   # the rule is symmetric
    test = rule["test"]
    for text in (f"{int(P['vol_rally_pct'])}% above cost", f"every {int(P['vol_rebal_days'])} days",
                 f"{int(P['vol_null_draws']):,} histories", f"blocks of {int(P['vol_null_block_days'])} days",
                 f"at least {P['vol_rank_hi']:g}%", f"{P['vol_rank_lo']:g}% or fewer"):
        assert text in test, text
    assert "one-year" in test and P["vol_horizon_days"] == 365
    assert volatility.HORIZON_DAYS == P["vol_horizon_days"]
    assert volatility.RALLY_PCT == P["vol_rally_pct"]
    assert volatility.REBAL_DAYS == P["vol_rebal_days"]
    assert volatility.MIN_YEARS == P["vol_min_years"]
    assert volatility.NULL_BLOCK_DAYS == P["vol_null_block_days"]
    assert volatility.NULL_DRAWS == P["vol_null_draws"]
    assert volatility.RANK_HI == P["vol_rank_hi"] and volatility.RANK_LO == P["vol_rank_lo"]
    assert volatility.SEED == P["sim_seed"]
    assert P["vol_rally_pct"] in P["vol_rally_ladder"]
    # the rally ladder and the model call's price are context: no rule or tripwire reads them
    exprs = _rule_exprs()
    assert not [e for e in exprs if "vol_ladder_" in e or "vol_call_now" in e]


def test_desk_resets_at_h10s_marks_and_prints_its_edge_to_the_param():
    """The desk's strategy (b) is H10's: the same reset marks (vol_rebal_days, read by the desk) over the same
    horizon; and the resets' change to the expected value prints to sim_usd_step_edge (design-v6 spec 2.2)."""
    import inspect
    import json

    from pipeline.models import portfolio
    assert P["sim_usd_step_edge"] == 10
    assert portfolio.specs()["sim_reb_edge_usd"] == ["usd", P["sim_usd_step_edge"]]
    assert 'p["vol_rebal_days"]' in inspect.getsource(portfolio.run)
    assert P["sim_horizon_days"] == P["vol_horizon_days"] == volatility.HORIZON_DAYS
    ell = portfolio.periods(volatility.REBAL_DAYS, P["sim_horizon_days"])
    marks = [sum(ell[:i + 1]) for i in range(len(ell))]
    H = volatility.HORIZON_DAYS
    assert marks == list(range(volatility.REBAL_DAYS, H, volatility.REBAL_DAYS)) + [H]
    sim = Path(__file__).resolve().parents[1] / "site" / "data" / "sim.json"
    if sim.exists():
        assert json.loads(sim.read_text())["rebal_days"] == volatility.REBAL_DAYS


def _rule_exprs() -> list[str]:
    exprs = [r.get(f, "") for cid, r in RULES.items() if cid.startswith("H")
             for f in ("supported", "refuted", "partly", "not_computed_if", "untestable_if")]
    exprs += [" ".join(r.get("metrics", []) + r.get("requires", [])) for cid, r in RULES.items() if cid.startswith("H")]
    exprs += [t["expr"] for t in RULES["tripwires"].values()]
    return exprs


def test_h8_matches_the_rule():
    rule = RULES["H8"]
    for key in ("vol_perp_hl_swing_share", "vol_perp_bn_swing_share"):
        assert literal(rule["supported"], key, "GtE") == P["vol_rank_hi"]
        assert literal(rule["refuted"], key, "LtE") == P["vol_rank_lo"]
    for key in ("vol_perp_hl_years", "vol_perp_bn_years"):
        assert literal(rule["not_computed_if"], key, "Lt") == P["vol_min_years"]
    assert not any(k in rule for k in ("untestable_if", "untestable_reason", "trigger"))
    test = rule["test"]
    for text in (f"{int(P['vol_perp_slice_pct'])}% of the stake", f"each {int(P['vol_perp_rung_pct'])}% rise",
                 f"at most {int(P['vol_perp_rungs'])} times", f"{int(P['vol_perp_static_pct'])}% of the stake held all year",
                 f"{int(P['vol_perp_rung_pct'])}% fall", f"{int(P['vol_null_draws']):,} histories",
                 f"blocks of {int(P['vol_null_block_days'])}", f"at least {P['vol_rank_hi']:g}%",
                 f"{P['vol_rank_lo']:g}% or fewer"):
        assert text in test, text
    # the yardstick is half the ladder's most, fixed before a window starts
    assert P["vol_perp_static_pct"] == P["vol_perp_rungs"] * P["vol_perp_slice_pct"] / 2
    for const, key, scale in (("RUNG", "vol_perp_rung_pct", 100), ("SLICE", "vol_perp_slice_pct", 100),
                              ("RUNGS", "vol_perp_rungs", 1), ("STATIC", "vol_perp_static_pct", 100),
                              ("FEE", "vol_perp_fee_pct", 100), ("SLIP", "vol_perp_slip_pct", 100),
                              ("MAINT", "vol_perp_maint_pct", 100), ("COLLATERAL", "vol_perp_collateral", 1),
                              ("TOPUP_AT", "vol_perp_topup_at", 1), ("SWEEP_AT", "vol_perp_sweep_at", 1),
                              ("TRANSFER_DAYS", "vol_perp_transfer_days", 1)):
        assert getattr(perps, const) == P[key] / scale, const
    assert perps.SENS_RATE_ADD == tuple(P["vol_perp_sens_rate_add"])
    assert perps.SENS_COLLATERAL == tuple(P["vol_perp_sens_collateral"])
    assert perps.SENS_TRANSFER_DAYS == tuple(P["vol_perp_sens_transfer_days"])
    assert perps.SENS_SLIP_PCT == tuple(P["vol_perp_sens_slip_pct"])
    assert perps.SENS_LAG_DAYS == P["vol_perp_sens_lag_days"]
    assert (perps.HORIZON_DAYS, perps.MIN_YEARS, perps.BLOCK, perps.DRAWS, perps.RANK_HI, perps.RANK_LO, perps.SEED) == (
        volatility.HORIZON_DAYS, volatility.MIN_YEARS, volatility.NULL_BLOCK_DAYS, volatility.NULL_DRAWS,
        volatility.RANK_HI, volatility.RANK_LO, volatility.SEED)
    # the sensitivity cases inform the data page; no rule or tripwire reads them
    assert not [e for e in _rule_exprs() if "vol_perp_sens_" in e]


def test_h9_window_matches_the_metric_name_and_the_test_text():
    within = P["within_pp"]
    assert market.WITHIN_PP == within
    # H9 reads the windows that end in the trailing year (_1y)
    assert f"pct_within_{int(within)}pp_1y" in RULES["H9"]["metrics"]
    assert f"within {int(within)} pp" in RULES["H9"]["test"]


def test_s_hypo_matches_tripwire_t6():
    assert literal(RULES["tripwires"]["T6"]["expr"], "s_now", "Gt") == pytest.approx(P["s_hypo"] * 100)
    assert staking.S_HYPO == P["s_hypo"]


def test_sweep_majority_is_read_from_params():
    assert sweep.MAJORITY == P["sweep_majority"]


def test_t3_burn_pct_matches_tripwire_t3():
    # burn_needed_t3 (the thesis paragraph's "the tripwire that would confirm the case needs …") uses T3's threshold
    assert literal(RULES["tripwires"]["T3"]["expr"], "burn_share_365", "GtE") == P["t3_burn_pct"]
    assert supply.T3_BURN_PCT == P["t3_burn_pct"]
