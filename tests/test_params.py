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

from pipeline.models import macro, market, params, staking, supply, sweep

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


def test_h2_band_and_window_match_the_rule():
    rule = RULES["H2"]["supported"]
    # abs(gap_cpi - target) <= tol
    tol = literal(rule, f"abs(gap_cpi - {P['h2_target_pp']})", "LtE")
    assert tol == P["h2_tol_pp"]
    assert macro.H2_TARGET_PP == P["h2_target_pp"] and macro.H2_TOL_PP == P["h2_tol_pp"]
    # the window length is in the registry key the rule reads
    assert f"h2_months_in_band_{P['h2_window_months']}" in rule
    assert macro.H2_WINDOW_MONTHS == P["h2_window_months"]


def test_h4_sd_cutoff_matches_the_rule_and_the_c12_title():
    # H4 reads the quarters under the issuance rule now in force; the c12 title gate reads the same key and cut-off
    assert literal(RULES["H4"]["supported"], "spread_q_sd_rule", "Lt") == P["h4_sd_pp"]
    assert staking.H4_SD_PP == P["h4_sd_pp"]
    src = Path(staking.__file__).read_text()
    assert 'sd = val(reg, "spread_q_sd_rule")' in src and "sd < H4_SD_PP" in src


def test_h10_majority_matches_the_rule_and_the_c05_title():
    assert literal(RULES["H10"]["supported"], "burn_share_365", "GtE") == P["burn_majority_pct"]
    assert supply.BURN_MAJORITY_PCT == P["burn_majority_pct"]


def test_h9_window_matches_the_metric_name_and_the_test_text():
    within = P["within_pp"]
    assert market.WITHIN_PP == within
    # H9 reads the windows that end in the trailing year (_1y)
    assert f"pct_within_{int(within)}pp_1y" in RULES["H9"]["metrics"]
    assert f"within {int(within)} pp" in RULES["H9"]["test"]


def test_s_hypo_matches_tripwire_t6():
    assert literal(RULES["tripwires"]["T6"]["expr"], "s_now", "Gt") == pytest.approx(P["s_hypo"] * 100)
    assert staking.S_HYPO == P["s_hypo"]


def test_band_and_sweep_majority_are_read_from_params():
    assert staking.BAND_PP == P["band_pp"]
    assert sweep.MAJORITY == P["sweep_majority"]


def test_t3_burn_pct_matches_tripwire_t3():
    # burn_needed_t3 (the thesis paragraph's "the tripwire that would confirm the case needs …") uses T3's threshold
    assert literal(RULES["tripwires"]["T3"]["expr"], "burn_share_365", "GtE") == P["t3_burn_pct"]
    assert supply.T3_BURN_PCT == P["t3_burn_pct"]


def test_h10_needs_an_upper_bound_above_the_majority_too():
    # an upper bound alone cannot support H10, and the sampled share needs its bound to clear the same bar
    rule = RULES["H10"]["supported"]
    assert literal(rule, "burn_share_365_ub", "GtE") == P["burn_majority_pct"]
    assert "burn_method != 'upper bound'" in rule
