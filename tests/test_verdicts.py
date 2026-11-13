"""The rule evaluator (pipeline/models/verdicts.py), thesis/rules.toml and verdict-keyed copy. Offline."""

from __future__ import annotations

import tomllib

import pytest

from pipeline import config
from pipeline.build import BuildError, Helpers
from pipeline.models import verdicts as V
from pipeline.numbers import Registry

RULES_PATH = config.ROOT / "thesis" / "rules.toml"
CONTRACT_CLAIMS = ["H1", "H1b", "H2", "H3", "H4", "H5", "H6", "H7", "H8", "H9", "H10", "H12", "H6_near"]


def reg_with(**values) -> Registry:
    reg = Registry()
    for key, value in values.items():
        if isinstance(value, tuple):          # (None, reason)
            reg.put(key, None, "%", "F-test", null_reason=value[1])
        elif isinstance(value, str):
            reg.put(key, value, "text", "F-test")
        else:
            reg.put(key, value, "%", "F-test")
    return reg


def ev(expr: str, **values):
    return V.evaluate(expr, V.Lookup(reg_with(**values))).result


def claim(**fields) -> dict:
    base = {"kind": "stated", "title": "t", "test": "t", "metrics": ["a"], "chart": "c01", "section": "O1",
            "supported": "a > 1", "refuted": "a < 0"}
    base.update(fields)
    return base


# ---------------------------------------------------------------- whitelist parser

@pytest.mark.parametrize("expr", [
    "a > 1", "a >= 1 and b < 2", "not a == 1", "abs(a - 1.5) <= 0.5", "a * b < 0", "-a < 1",
    "a <= b + 0.5 or c > 2", "burn_method != 'upper bound'", "0 < a < 2", "a / b >= 0.75",
])
def test_parser_accepts_rule_grammar(expr):
    V.parse(expr)


@pytest.mark.parametrize("expr", [
    "a.real > 1",                      # attribute access
    "a[0] > 1",                        # subscript
    "max(a, b) > 1",                   # a call other than abs
    "__import__('os').system('x')",    # dunder call
    "abs(a, b) > 1",                   # abs with two arguments
    "abs(x=a) > 1",                    # keyword argument
    "(lambda: 1)() > 0",               # lambda
    "[x for x in a] == 1",             # comprehension
    "a == True",                       # bool constant
    "a == None",                       # None constant
    "__class__ > 1",                   # dunder name
    "A > 1",                           # not a registry key shape
    "a + 1",                           # not a boolean expression
    "(a := 1) > 0",                    # walrus
    "f'{a}' == 'x'",                   # f-string
    "a > 1; b > 1",                    # statements
    "a ** 2 > 1",                      # operator outside the whitelist
    "abs > 1",                         # abs as a name
])
def test_parser_rejects_everything_else(expr):
    with pytest.raises(V.RuleError):
        V.parse(expr)


def test_keys_and_pretty():
    assert V.keys_in("abs(gap_cpi - 1.5) <= 0.5 and h2_months >= 9") == ["gap_cpi", "h2_months"]
    assert V.keys_in("abs(a) > 1 and abs(b - a) < 2") == ["a", "b"]
    assert V.pretty("abs(a - 1.5) <= 0.5 and b != 'x'") == "|a - 1.5| ≤ 0.5 and b ≠ “x”"
    assert V.pretty("a * b < 0 or not c >= 2") == "a × b < 0 or not c ≥ 2"


# ---------------------------------------------------------------- three-valued logic

def test_kleene_logic_with_unknowns():
    missing = (None, "no data")
    assert ev("a > 1 and b > 1", a=0, b=missing) is False      # False and unknown
    assert ev("a > 1 or b > 1", a=2, b=missing) is True        # True or unknown
    assert ev("a > 1 and b > 1", a=2, b=missing) is None
    assert ev("a > 1 or b > 1", a=0, b=missing) is None
    assert ev("not b > 1", b=missing) is None
    assert ev("not_registered > 1") is None
    assert ev("a / b > 1", a=1, b=0) is None                   # division by zero is unknown, not an error
    assert ev("m != 'upper bound'", m="exact") is True
    assert ev("m > 1", m="exact") is None                      # type mismatch is unknown


def test_evaluation_reports_reasons_and_unregistered():
    e = V.evaluate("a > 1 and zz > 1", V.Lookup(reg_with(a=(None, "a has no samples"))))
    assert e.result is None
    assert "a has no samples" in e.reason()
    assert "zz" in e.unregistered


# ---------------------------------------------------------------- every verdict state

def test_supported_and_refuted():
    assert V.evaluate_claim("H1", claim(), reg_with(a=2))["verdict"] == V.SUPPORTED
    out = V.evaluate_claim("H1", claim(), reg_with(a=-1))
    assert out["verdict"] == V.REFUTED and out["binding_condition"] == "a < 0"
    assert out["margin"]["diff"] == pytest.approx(-1) and out["margin"]["unit"] == "pp"


def test_partly_by_default_and_by_expression():
    out = V.evaluate_claim("H1", claim(), reg_with(a=0.5))
    # between the two rules there is no single threshold to measure a margin from
    assert out["verdict"] == V.PARTLY and out["binding_condition"] == V.NEITHER and out["margin"] is None
    r = claim(partly="a >= 0.25")
    assert V.evaluate_claim("H1", r, reg_with(a=0.5))["verdict"] == V.PARTLY
    gap = V.evaluate_claim("H1", r, reg_with(a=0.1))
    assert gap["verdict"] == V.NOT_COMPUTED and "rules gap" in gap["null_reason"]


def test_not_computed_from_null_or_unregistered_input():
    out = V.evaluate_claim("H1", claim(), reg_with(a=(None, "needs RPC samples")))
    assert out["verdict"] == V.NOT_COMPUTED and out["null_reason"] == "a: needs RPC samples"
    out = V.evaluate_claim("H1", claim(), Registry())
    assert out["verdict"] == V.NOT_COMPUTED and out["unregistered"] == ["a"]


def test_not_computed_when_decisive_expression_is_unknown():
    r = claim(metrics=["a"], supported="a > 1 and b > 1", refuted="a < 0 or b < 0")
    out = V.evaluate_claim("H1", r, reg_with(a=2, b=(None, "b is missing")))
    assert out["verdict"] == V.NOT_COMPUTED and "b is missing" in out["null_reason"]
    # refuted holds even though b is unknown: a < 0 decides
    assert V.evaluate_claim("H1", r, reg_with(a=-1, b=(None, "x")))["verdict"] == V.REFUTED


def test_not_testable_wins_over_supported():
    r = claim(metrics=["n"], requires=["n"], untestable_if="n == 0", untestable_reason="no venue",
              trigger="a venue lists options", supported="vrp > 0", refuted="vrp <= 0")
    out = V.evaluate_claim("H8", r, reg_with(n=0))
    assert out["verdict"] == V.NOT_TESTABLE and out["reason"] == "no venue"
    assert out["trigger"] == "a venue lists options"
    assert V.evaluate_claim("H8", r, reg_with(n=2))["verdict"] == V.NOT_COMPUTED   # vrp unregistered


def test_not_computed_if_gives_its_reason():
    r = claim(not_computed_if="a == 0", not_computed_reason="no windows")
    out = V.evaluate_claim("H2", r, reg_with(a=0))
    assert out["verdict"] == V.NOT_COMPUTED and out["null_reason"] == "no windows"
    assert V.evaluate_claim("H2", r, reg_with(a=5))["verdict"] == V.SUPPORTED


def test_tripwire_states():
    trip = {"metric": "a", "threshold_text": "t", "expr": "a > 1", "meaning": "m", "chart": "c01"}
    assert V.evaluate_tripwire("T1", trip, reg_with(a=2))["tripped"] is True
    assert V.evaluate_tripwire("T1", trip, reg_with(a=0))["state"] == "clear"
    assert V.evaluate_tripwire("T1", trip, reg_with(a=(None, "x")))["state"] == "not computed"


# ---------------------------------------------------------------- run(): registry outputs and tally

def test_run_registers_tally_over_h_claims_only():
    rules = {
        "meta": {"hypothesis": "alpha beta gamma delta"},
        "H1": claim(claimed="alpha"),
        "H2": claim(claimed="beta"),
        "H9": claim(kind="hidden", claimed="gamma", assumption="x", metrics=["b"]),
        "H6_near": claim(kind="reframe", claimed="delta"),
        "tripwires": {"T1": {"metric": "a", "metric_label": "Metric a", "threshold_text": "t", "expr": "a > 5",
                             "meaning": "m", "chart": "c01"}},
    }
    reg = reg_with(a=2, b=(None, "b missing"))
    verdicts, trips = V.run(reg, rules)
    tally = verdicts["tally"]
    assert tally["counted"] == ["H1", "H2", "H9"] and tally["separate"] == ["H6_near"]
    assert (tally["supported"], tally["not_computed"], tally["claims"], tally["total"]) == (2, 1, 2, 3)
    for key, want in (("n_supported", 2), ("n_partly", 0), ("n_refuted", 0), ("n_untestable", 0),
                      ("n_not_computed", 1), ("n_claims", 2)):
        n = reg.get(key)
        assert (n.value, n.unit, n.formula_id) == (want, "count", "F-tally"), key
    # the margin is an unsigned distance; the signed comparison stays in margin_detail
    assert verdicts["H1"]["margin"] == "1.00 pp" and reg.get("margin_H1").formula_id == "F-margin"
    assert reg.get("margin_H1").signed is False and verdicts["H1"]["margin_detail"]["diff"] is not None
    assert reg.get("margin_H9").value is None and reg.get("margin_H9").null_reason
    assert trips["T1"]["state"] == "clear" and trips["T1"]["metric_label"] == "Metric a"


def test_partly_between_the_rules_has_no_margin_and_tallies_by_kind():
    # a Partly that neither rule decides has no threshold to measure from, and the tally splits by kind
    rules = {"meta": {"hypothesis": "alpha beta"},
             "H1": claim(claimed="alpha", supported="a > 5", refuted="a < 0"),
             "H2": claim(claimed="beta", metrics=["b"], supported="b > 1", refuted="b < 0")}
    verdicts, _ = V.run(reg_with(a=2, b=3), rules)
    assert verdicts["H1"]["verdict"] == "Partly" and verdicts["H1"]["binding_condition"] == V.NEITHER
    assert verdicts["H1"]["margin"] is None
    reg = reg_with(a=2, b=3)
    V.run(reg, rules)
    assert reg.value("margin_H1") is None and "between the Supported and Refuted" in reg.get("margin_H1").null_reason
    assert reg.value("margin_H2") == pytest.approx(2.0)
    assert (reg.value("n_supported_stated"), reg.value("n_partly_stated"), reg.value("n_decided_stated")) == (1, 1, 2)


def test_margin_skips_a_comparison_on_a_ratio_of_two_keys():
    # "a / b >= 2" has no unit a distance can carry, so the margin falls to the next comparison
    rules = {"meta": {"hypothesis": "alpha"},
             "H1": claim(claimed="alpha", metrics=["a", "b"], supported="a / b >= 2 and a > 1", refuted="a < 0")}
    reg = reg_with(a=8, b=2)
    verdicts, _ = V.run(reg, rules)
    assert verdicts["H1"]["verdict"] == "Supported"
    assert verdicts["H1"]["margin_detail"]["text"].startswith("a >")
    assert reg.value("margin_H1") == pytest.approx(7.0)


def test_margin_on_abs_of_a_difference_carries_the_key_unit():
    # H12's margin is |staker - nonstaker| in pp, not a multiple ("abs" is not a registry key)
    rules = {"meta": {"hypothesis": "alpha"},
             "H1": claim(claimed="alpha", metrics=["a", "b"], supported="abs(a - b) < 0.1",
                         refuted="abs(a - b) >= 0.1")}
    reg = reg_with(a=1.61, b=-2.32)
    verdicts, _ = V.run(reg, rules)
    assert verdicts["H1"]["verdict"] == "Refuted"
    assert reg.get("margin_H1").unit == "pp" and reg.value("margin_H1") == pytest.approx(3.83)


def test_thinnest_supported_id_names_the_smallest_pp_margin():
    rules = {"meta": {"hypothesis": "alpha beta gamma"},
             "H1": claim(claimed="alpha", supported="a > 1"),
             "H2": claim(claimed="beta", metrics=["b"], supported="b > 1", refuted="b < 0"),
             "H3": claim(claimed="gamma", metrics=["c"], supported="c > 1", refuted="c < 0")}
    reg = reg_with(a=1.5, b=4, c=-1)
    V.run(reg, rules)
    n = reg.get("thinnest_supported_id")
    assert (n.value, n.unit, n.formula_id) == ("H1", "text", "F-margin")
    reg = reg_with(a=-1, b=-1, c=-1)
    V.run(reg, rules)
    assert reg.value("thinnest_supported_id") is None and reg.get("thinnest_supported_id").null_reason


def test_tripwire_group_is_break_or_confirm():
    trip = {"metric": "a", "metric_label": "Metric a", "threshold_text": "t", "expr": "a > 5", "meaning": "m",
            "chart": "c01"}
    rules = {"meta": {"hypothesis": "alpha"}, "H1": claim(claimed="alpha"),
             "tripwires": {"T1": trip, "T2": {**trip, "group": "confirm"}}}
    _, trips = V.run(reg_with(a=2), rules)
    assert (trips["T1"]["group"], trips["T2"]["group"]) == ("break", "confirm")
    rules["tripwires"]["T3"] = {**trip, "group": "maybe"}
    with pytest.raises(V.RuleError, match="group"):
        V.validate_rules(rules)


def test_rules_file_groups_every_tripwire_and_labels_every_metric(rules):
    import re
    labels = rules.get("labels") or {}
    for tid, trip in V.tripwires(rules).items():
        assert trip.get("group") in ("break", "confirm"), tid
    shown = {k for r in V.claims(rules).values() for k in r.get("metrics", [])}
    assert not sorted(shown - set(labels)), "metrics without a [labels] entry"
    for key, label in labels.items():
        assert isinstance(label, str) and label.strip(), key
        assert not re.search(r"\d", label), f"[labels].{key} types a digit"


def test_copy_conditions_on_registry_keys_name_text_keys(rules, model_registry):
    """A copy condition that is not a claim, tripwire or cross-check reads a registry text key (the build looks
    it up after verdicts run); a typo or a numeric key would otherwise fail only when that copy is chosen."""
    import copy as _copy
    import re
    reg = _copy.deepcopy(model_registry)
    V.run(reg, rules)
    copy = tomllib.loads((config.ROOT / "thesis" / "copy.toml").read_text())
    named = set(V.claims(rules)) | set(V.tripwires(rules)) | set(rules.get("crosschecks", {}))
    bad = {}
    for name, entry in copy.items():
        for v in entry.get("variants", []):
            for cond in v.get("when", []):
                subject = re.match(r"^\s*([A-Za-z0-9_§.\-]+)", cond).group(1)
                if subject in named:
                    continue
                if subject not in reg or reg.get(subject).unit != "text":
                    bad.setdefault(name, []).append(subject)
    assert not bad, bad


def test_validate_rules_rejects_paraphrased_claim_and_bad_expression():
    rules = {"meta": {"hypothesis": "NEAR is deflationary"}, "H1": claim(claimed="NEAR is not inflationary")}
    with pytest.raises(V.RuleError, match="verbatim"):
        V.validate_rules(rules)
    rules = {"meta": {"hypothesis": "x"}, "H2": claim(claimed="x", supported="a.b > 1")}
    with pytest.raises(V.RuleError, match="H2.supported"):
        V.validate_rules(rules)


# ---------------------------------------------------------------- the real thesis/rules.toml

@pytest.fixture(scope="module")
def rules():
    with RULES_PATH.open("rb") as fh:
        return tomllib.load(fh)


def test_rules_file_is_valid_and_complete(rules):
    V.validate_rules(rules)
    assert list(V.claims(rules)) == CONTRACT_CLAIMS
    assert {cid for cid, r in V.claims(rules).items() if r["kind"] == "hidden"} == {"H9", "H10", "H12"}
    assert {cid for cid, r in V.claims(rules).items() if r["kind"] == "reframe"} == {"H6_near"}
    assert set(rules) == {"meta", "params", "labels", "tripwires", "crosschecks", *CONTRACT_CLAIMS}
    assert list(V.tripwires(rules)) == [f"T{i}" for i in range(1, 9)]
    assert sorted(rules["crosschecks"]) == [f"X{i}" for i in (1, 2, 3, 4, 5, 6, 7, 9)]
    assert {x for x, c in rules["crosschecks"].items() if c.get("blocks_verify")} == {"X1", "X3"}


CLAIM_FIELDS = {"kind", "title", "claimed", "assumption", "test", "metrics", "requires", "not_computed_if",
                "not_computed_reason", "untestable_if", "untestable_reason", "trigger", "supported", "partly",
                "refuted", "chart", "charts", "section"}
TRIPWIRE_FIELDS = {"metric", "metric_label", "keys", "threshold_text", "expr", "meaning", "chart", "group",
                   "now_text"}


def test_rules_meta_is_the_thesis_and_its_boundaries(rules):
    """[meta] holds the author's hypothesis, thesis and boundaries verbatim, and the tally note; every claim and
    tripwire uses only the fields the header documents and sits in a section of the page."""
    meta = rules["meta"]
    assert set(meta) == {"hypothesis", "tally_note", "thesis", "boundaries"}
    assert "Past and future governance conditions are not relevant" in meta["boundaries"]
    assert meta["thesis"] == "NEAR is deflationary relative to the dollar and other currencies."
    for cid, r in V.claims(rules).items():
        assert set(r) <= CLAIM_FIELDS, (cid, sorted(set(r) - CLAIM_FIELDS))
        assert r["section"] in {f"{g}{i}" for g in "AO" for i in range(1, 6)}, cid
    for tid, t in V.tripwires(rules).items():
        assert set(t) <= TRIPWIRE_FIELDS, (tid, sorted(set(t) - TRIPWIRE_FIELDS))


def test_rules_file_thresholds_are_parsed_not_evaluated_as_python(rules):
    for cid, r in V.claims(rules).items():
        for field in V.EXPR_FIELDS:
            if field in r:
                assert set(V.keys_in(r[field])) <= set(V.contract_keys(rules)) | {
                    "vrp_near_mean", "vrp_near_t", "vrp_near_days"}, (cid, field)


@pytest.mark.parametrize("narrower,wider,expected", [
    (2, 0, V.SUPPORTED),     # below the panel medians on both readings
    (1, 1, V.PARTLY),        # split
    (1, 0, V.PARTLY),        # narrower on one, mixed on the other
    (0, 0, V.PARTLY),        # mixed on both readings is not "above on both"
    (0, 1, V.PARTLY),
    (0, 2, V.REFUTED),       # above the panel medians on both readings
])
def test_h5_refuted_only_when_wider_on_both_readings(rules, narrower, wider, expected):
    """H5: Supported below on both, Partly split, Refuted above on both. H5 reads today's readings
    (h5_now_*) against the panel medians."""
    r = V.claims(rules)["H5"]
    out = V.evaluate_claim("H5", r, reg_with(h5_now_narrower=narrower, h5_now_wider=wider,
                                             near_reading_a=1.0, near_reading_b=1.0, panel_median_abs_a=2.0,
                                             panel_median_abs_b=2.0, n_panel=10))
    assert out["verdict"] == expected


# ---------------------------------------------------------------- verdict-keyed copy (fail closed)

def copy_helper(copy: dict, verdicts: dict) -> Helpers:
    data = {"rules": {"meta": {"hypothesis": "h"}, **{cid: claim() for cid in verdicts}},
            "verdicts": {cid: {"verdict": v} for cid, v in verdicts.items()},
            "numbers": {}, "tripwires": {}, "crosschecks": {}, "sources": [], "charts": {}, "copy": copy}
    return Helpers(data, "md")


def test_copy_first_match_wins_and_fallback():
    copy = {"headline": {"variants": [
        {"when": ["H1=Refuted", "H9=Supported"], "text": "first"},
        {"when": ["H1=Refuted"], "text": "second"},
        {"when": [], "text": "fallback"}]}}
    assert copy_helper(copy, {"H1": "Refuted", "H9": "Supported"}).copy("headline") == "first"
    assert copy_helper(copy, {"H1": "Refuted", "H9": "Partly"}).copy("headline") == "second"
    assert copy_helper(copy, {"H1": "Supported", "H9": "Partly"}).copy("headline") == "fallback"


def test_copy_conditions_alternatives_negation_and_state_names():
    copy = {"x": {"variants": [{"when": ["H9=Supported|Partly", "H1!=Not computed"], "text": "yes"},
                               {"when": [], "text": "no"}]}}
    assert copy_helper(copy, {"H1": "Not testable", "H9": "Partly"}).copy("x") == "yes"
    assert copy_helper(copy, {"H1": "Not computed", "H9": "Partly"}).copy("x") == "no"


def test_copy_without_match_fails_closed():
    copy = {"headline": {"variants": [{"when": ["H1=Supported"], "text": "never"}]}}
    with pytest.raises(BuildError, match="fails closed"):
        copy_helper(copy, {"H1": "Refuted"}).copy("headline")
    with pytest.raises(BuildError, match="no \\[missing\\] table"):
        copy_helper(copy, {"H1": "Refuted"}).copy("missing")
    with pytest.raises(BuildError, match="H99"):
        copy_helper({"x": {"variants": [{"when": ["H99=Refuted"], "text": "t"}]}}, {"H1": "Refuted"}).copy("x")


# ---------------------------------------------------------------- rule hygiene (every key registered; exclusive rules)

EXPR_FIELDS = ("supported", "partly", "refuted", "untestable_if", "not_computed_if")


def _names(expr: str) -> set[str]:
    import ast
    return {n.id for n in ast.walk(ast.parse(expr, mode="eval")) if isinstance(n, ast.Name) and n.id != "abs"}


@pytest.fixture(scope="module")
def model_registry():
    """Every model's compute() on this repository's data/raw, in build order (offline)."""
    import importlib

    from pipeline.build import MODEL_ORDER, MODEL_PACKAGE
    reg = Registry()
    for name in MODEL_ORDER:
        importlib.import_module(f"{MODEL_PACKAGE}.{name}").compute(reg)
    return reg


def test_every_rule_key_is_registered_by_a_model(rules, model_registry):
    """A rule that reads a key no model writes would silently read as unknown; every key must be registered
    (null with a reason is fine). Verdict tally keys are registered by the verdict module itself."""
    missing = {}
    for cid, rule in V.claims(rules).items():
        keys = set(rule.get("metrics", [])) | set(rule.get("requires", []))
        for field in EXPR_FIELDS:
            if field in rule:
                keys |= _names(rule[field])
        absent = sorted(k for k in keys if k not in model_registry)
        if absent:
            missing[cid] = absent
    for tid, trip in V.tripwires(rules).items():
        absent = sorted(k for k in _names(trip["expr"]) | set(trip.get("keys", [])) if k not in model_registry)
        if absent:
            missing[tid] = absent
    assert not missing, missing


def _candidates(rule: dict, key: str) -> list:
    """Values around every literal the key is compared with, plus small integers and zero."""
    import ast
    out = {0.0, 1.0, 2.0, 4.0, -1.0}
    texts = set()
    for field in ("supported", "refuted"):
        for node in ast.walk(ast.parse(rule[field], mode="eval")):
            if isinstance(node, ast.Compare) and key in _names(ast.unparse(node)):
                for c in ast.walk(node):
                    if isinstance(c, ast.Constant) and isinstance(c.value, (int, float)):
                        for v in (c.value, c.value - 0.01, c.value + 0.01, -c.value, c.value / 2):
                            out.add(float(v))
                    elif isinstance(c, ast.Constant) and isinstance(c.value, str):
                        texts.add(c.value)
    return sorted(texts | {"other"}) if texts else sorted(out)


def test_supported_and_refuted_are_never_both_true(rules, model_registry):
    """Rule order must never be what decides between Supported and Refuted: on a grid of values around every
    threshold, no claim's supported and refuted expressions hold at once (counts are never negative)."""
    import itertools
    both = []
    for cid, rule in V.claims(rules).items():
        if "supported" not in rule or "refuted" not in rule:
            continue
        keys = sorted(_names(rule["supported"]) | _names(rule["refuted"]))
        counts = {k for k in keys if k in model_registry and model_registry.get(k).unit in ("count", "days")}
        grids = [[v for v in _candidates(rule, k) if k not in counts or v >= 0] for k in keys]
        for combo in itertools.product(*grids):
            reg = Registry()
            for k, v in zip(keys, combo):
                reg.put(k, v, "text" if isinstance(v, str) else "%", "F-test")
            lookup = V.Lookup(reg)
            if V.evaluate(rule["supported"], lookup).result is True and \
                    V.evaluate(rule["refuted"], lookup).result is True:
                both.append((cid, dict(zip(keys, combo))))
                break
    assert not both, both
