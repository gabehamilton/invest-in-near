"""Verdicts: apply thesis/rules.toml to the number registry. Nothing here is written by hand.

Rules are expressions over registry keys, parsed with a small `ast` whitelist (never `eval`):
comparisons (< <= > >= == !=, chained), and / or / not, + - * /, unary minus, abs(), numeric
literals, quoted strings (for text keys such as burn_method) and registry keys. Anything else
(attribute access, subscripts, other calls, lambdas, comprehensions, dunder names) is a RuleError.

Evaluation uses three-valued logic: a key that is null or unregistered is unknown, `False and
unknown` is False, `True or unknown` is True, and any other operation on an unknown is unknown.

Per claim, in this order:
  1. a key in `requires` (default: `metrics`) is null or unregistered  -> Not computed (its null_reason)
  2. `not_computed_if` holds                                            -> Not computed (not_computed_reason)
  3. `untestable_if` holds                                              -> Not testable (untestable_reason)
  4. `supported` holds                                                  -> Supported
  5. `refuted` holds                                                    -> Refuted
  6. `partly` given: holds -> Partly; no `partly` expression           -> Partly
A decisive expression that is unknown gives Not computed with the unknown keys' reasons, never a guess.
If `supported` is unknown but `refuted` holds, the verdict is Refuted (the two rules are mutually exclusive
by construction). If a `partly` expression exists and is false while neither other rule holds, the rules
have a gap: the claim is Not computed and says so.

Tally (registered as n_supported, n_partly, n_refuted, n_untestable, n_not_computed, n_claims, plus
n_claims_total, n_claims_stated, n_claims_hidden): counts stated claims and hidden assumptions only
(kind "stated" or "hidden", the H ids). The H6_near reframe is reported beside them.
n_claims = n_supported + n_partly + n_refuted (the claims that were testable and computed);
n_claims_total = every H claim, so n_claims_total = n_claims + n_untestable + n_not_computed.

Each claim's binding margin (left minus right side of the deciding comparison, in the metric's units;
a difference of two "%" values is in "pp") is registered as margin_<claim_id> so pages print it via num().
In verdicts.json, `margin` is that number's display text (or null) and `margin_detail` holds the comparison.
"""

from __future__ import annotations

import ast
import operator
import re
from typing import Any, Callable

from pipeline.numbers import Registry

SUPPORTED = "Supported"
PARTLY = "Partly"
REFUTED = "Refuted"
NOT_TESTABLE = "Not testable"
NOT_COMPUTED = "Not computed"
STATES = (SUPPORTED, PARTLY, REFUTED, NOT_TESTABLE, NOT_COMPUTED)

TALLY_KINDS = ("stated", "hidden")
CLAIM_KINDS = ("stated", "hidden", "reframe")
EXPR_FIELDS = ("not_computed_if", "untestable_if", "supported", "partly", "refuted")

NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class RuleError(ValueError):
    """A rule expression or rules-file entry that the evaluator refuses to run."""


# --------------------------------------------------------------------------- parsing

_CMP_OPS: dict[type, tuple[Callable[[Any, Any], bool], str]] = {
    ast.Lt: (operator.lt, "<"),
    ast.LtE: (operator.le, "≤"),
    ast.Gt: (operator.gt, ">"),
    ast.GtE: (operator.ge, "≥"),
    ast.Eq: (operator.eq, "="),
    ast.NotEq: (operator.ne, "≠"),
}
_BIN_OPS: dict[type, tuple[Callable[[Any, Any], Any], str, int]] = {
    ast.Add: (operator.add, "+", 5),
    ast.Sub: (operator.sub, "-", 5),
    ast.Mult: (operator.mul, "×", 6),
    ast.Div: (operator.truediv, "/", 6),
}


NEITHER = "neither the Supported nor the Refuted rule holds"


def parse(expr: str) -> ast.Expression:
    """Parse a rule expression and check it against the whitelist. Raises RuleError."""
    if not isinstance(expr, str) or not expr.strip():
        raise RuleError(f"rule expression must be a non-empty string, got {expr!r}")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as exc:
        raise RuleError(f"cannot parse rule {expr!r}: {exc.msg}") from None
    _check_bool(tree.body, expr)
    return tree


def _check_bool(node: ast.AST, expr: str) -> None:
    """A rule must be boolean: a comparison, and/or of rules, or not a rule."""
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        for value in node.values:
            _check_bool(value, expr)
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        _check_bool(node.operand, expr)
    elif isinstance(node, ast.Compare):
        for op in node.ops:
            if type(op) not in _CMP_OPS:
                raise RuleError(f"operator {type(op).__name__} is not allowed in rule {expr!r}")
        for operand in (node.left, *node.comparators):
            _check_value(operand, expr)
    else:
        raise RuleError(f"rule {expr!r} must be a comparison (or and/or/not of comparisons); "
                        f"found {type(node).__name__}")


def _check_value(node: ast.AST, expr: str) -> None:
    if isinstance(node, ast.Constant):
        if type(node.value) not in (int, float, str):
            raise RuleError(f"literal {node.value!r} is not allowed in rule {expr!r} (numbers and strings only)")
    elif isinstance(node, ast.Name):
        if not isinstance(node.ctx, ast.Load) or not NAME_RE.match(node.id) or node.id == "abs":
            raise RuleError(f"name {node.id!r} is not a registry key in rule {expr!r}")
    elif isinstance(node, ast.BinOp):
        if type(node.op) not in _BIN_OPS:
            raise RuleError(f"operator {type(node.op).__name__} is not allowed in rule {expr!r}")
        _check_value(node.left, expr)
        _check_value(node.right, expr)
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        _check_value(node.operand, expr)
    elif isinstance(node, ast.Call):
        if not (isinstance(node.func, ast.Name) and node.func.id == "abs"):
            raise RuleError(f"only abs() may be called in rule {expr!r}")
        if len(node.args) != 1 or node.keywords or isinstance(node.args[0], ast.Starred):
            raise RuleError(f"abs() takes exactly one positional argument in rule {expr!r}")
        _check_value(node.args[0], expr)
    else:
        raise RuleError(f"{type(node).__name__} is not allowed in rule {expr!r}")


def _key_nodes(node: ast.AST) -> list[ast.Name]:
    """Name nodes that are registry keys (the function name in abs(...) is not a key)."""
    funcs = {id(c.func) for c in ast.walk(node) if isinstance(c, ast.Call)}
    return [n for n in ast.walk(node) if isinstance(n, ast.Name) and id(n) not in funcs]


def keys_in(expr: str) -> list[str]:
    """Registry keys referenced by an expression, in order of appearance, without duplicates."""
    names = _key_nodes(parse(expr))
    names.sort(key=lambda n: (n.lineno, n.col_offset))  # ast.walk is breadth-first; use source order
    return list(dict.fromkeys(n.id for n in names))


# --------------------------------------------------------------------------- pretty printing

def _fmt_const(value: Any) -> str:
    if isinstance(value, str):
        return f"“{value}”"
    return repr(value)


def _pp(node: ast.AST, parent: int = 0) -> str:
    if isinstance(node, ast.BoolOp):
        prec = 1 if isinstance(node.op, ast.Or) else 2
        word = " or " if isinstance(node.op, ast.Or) else " and "
        text = word.join(_pp(v, prec + 1) for v in node.values)
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        prec, text = 3, "not " + _pp(node.operand, 4)
    elif isinstance(node, ast.Compare):
        prec = 4
        parts = [_pp(node.left, 5)]
        for op, right in zip(node.ops, node.comparators):
            parts += [_CMP_OPS[type(op)][1], _pp(right, 5)]
        text = " ".join(parts)
    elif isinstance(node, ast.BinOp):
        _, sym, prec = _BIN_OPS[type(node.op)]
        text = f"{_pp(node.left, prec)} {sym} {_pp(node.right, prec + 1)}"
    elif isinstance(node, ast.UnaryOp):
        prec, text = 7, ("-" if isinstance(node.op, ast.USub) else "+") + _pp(node.operand, 7)
    elif isinstance(node, ast.Call):
        prec, text = 8, "|" + _pp(node.args[0], 0) + "|"
    elif isinstance(node, ast.Name):
        prec, text = 9, node.id
    elif isinstance(node, ast.Constant):
        prec, text = 9, _fmt_const(node.value)
    else:  # pragma: no cover - parse() rejects everything else
        raise RuleError(f"cannot print {type(node).__name__}")
    return f"({text})" if prec < parent else text


def pretty(expr: str) -> str:
    """Human form of a rule: ≥ ≤ ≠ =, |x| for abs(x), × for *. Same meaning, same keys."""
    return _pp(parse(expr).body)


# --------------------------------------------------------------------------- evaluation

class Evaluation:
    """Result of evaluating one expression: True / False / None (unknown)."""

    def __init__(self, expr: str, result: bool | None, comparisons: list[dict], unknown: dict[str, str],
                 unregistered: list[str]):
        self.expr = expr
        self.result = result
        self.comparisons = comparisons      # in source order: {text, left, right, result, diff, unit}
        self.unknown = unknown              # key -> reason (null or unregistered keys that were read)
        self.unregistered = unregistered

    def reason(self) -> str:
        return "; ".join(self.unknown[k] for k in sorted(self.unknown))


class Lookup:
    """Registry view used by the evaluator: value, unit and why a key is unknown."""

    def __init__(self, reg: Registry):
        self.reg = reg

    def registered(self, key: str) -> bool:
        return key in self.reg

    def value(self, key: str) -> tuple[Any, str | None]:
        if key not in self.reg:
            return None, f"{key} is not registered by any model"
        number = self.reg.get(key)
        if number.value is None:
            return None, f"{key}: {number.null_reason}"
        return number.value, None

    def unit(self, key: str) -> str | None:
        return self.reg.get(key).unit if key in self.reg else None


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def evaluate(expr: str, lookup: Lookup) -> Evaluation:
    tree = parse(expr)
    comparisons: list[dict] = []
    unknown: dict[str, str] = {}
    unregistered: list[str] = []

    def first_name(node: ast.AST) -> str | None:
        """The first registry key in source order (the `abs` in abs(...) is a function, not a key, so a
        comparison on abs(a - b) carries a's unit)."""
        names = sorted(_key_nodes(node), key=lambda n: (n.lineno, n.col_offset))
        return names[0].id if names else None

    def value(node: ast.AST) -> Any:
        """Numeric/string value, or None when unknown."""
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            v, why = lookup.value(node.id)
            if v is None:
                unknown[node.id] = why or f"{node.id} is null"
                if not lookup.registered(node.id) and node.id not in unregistered:
                    unregistered.append(node.id)
            return v
        if isinstance(node, ast.UnaryOp):
            x = value(node.operand)
            if x is None:
                return None
            if not _is_num(x):
                unknown[f"{first_name(node)}"] = f"{first_name(node)}: unary minus on a non-number"
                return None
            return -x if isinstance(node.op, ast.USub) else x
        if isinstance(node, ast.BinOp):
            a, b = value(node.left), value(node.right)
            if a is None or b is None:
                return None
            if not (_is_num(a) and _is_num(b)):
                key = first_name(node) or "expression"
                unknown[key] = f"{key}: arithmetic on a non-number"
                return None
            if isinstance(node.op, ast.Div) and b == 0:
                key = first_name(node.right) or first_name(node) or "expression"
                unknown[key] = f"{key}: division by zero"
                return None
            return _BIN_OPS[type(node.op)][0](a, b)
        if isinstance(node, ast.Call):
            x = value(node.args[0])
            if x is None:
                return None
            if not _is_num(x):
                key = first_name(node) or "expression"
                unknown[key] = f"{key}: abs() of a non-number"
                return None
            return abs(x)
        raise RuleError(f"unexpected node {type(node).__name__}")  # pragma: no cover

    def truth(node: ast.AST) -> bool | None:
        if isinstance(node, ast.BoolOp):
            results = [truth(v) for v in node.values]  # evaluate all: collects every comparison
            if isinstance(node.op, ast.And):
                if any(r is False for r in results):
                    return False
                return None if any(r is None for r in results) else True
            if any(r is True for r in results):
                return True
            return None if any(r is None for r in results) else False
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            r = truth(node.operand)
            return None if r is None else not r
        if isinstance(node, ast.Compare):
            left_node = node.left
            overall: bool | None = True
            for op, right_node in zip(node.ops, node.comparators):
                a, b = value(left_node), value(right_node)
                text = f"{_pp(left_node, 5)} {_CMP_OPS[type(op)][1]} {_pp(right_node, 5)}"
                key = first_name(left_node) or first_name(right_node)
                unit = lookup.unit(key) if key else None
                record = {"text": text, "left": a, "right": b, "result": None, "diff": None,
                          "unit": "pp" if unit == "%" else unit, "keys": _names(left_node) + _names(right_node),
                          # a product or ratio of two inputs has no unit a distance can carry (R2-1)
                          "scaled": _scales_keys(left_node) or _scales_keys(right_node)}
                if a is None or b is None:
                    result = None
                elif _is_num(a) and _is_num(b):
                    result = _CMP_OPS[type(op)][0](a, b)
                    record["diff"] = a - b
                elif isinstance(a, str) and isinstance(b, str):
                    result = _CMP_OPS[type(op)][0](a, b)
                elif type(op) in (ast.Eq, ast.NotEq):
                    result = _CMP_OPS[type(op)][0](a, b)
                else:
                    unknown[key or "expression"] = f"{key}: cannot compare {type(a).__name__} with {type(b).__name__}"
                    result = None
                record["result"] = result
                comparisons.append(record)
                if result is False:
                    overall = False
                elif result is None and overall is True:
                    overall = None
                left_node = right_node
            return overall
        raise RuleError(f"unexpected node {type(node).__name__}")  # pragma: no cover

    result = truth(tree.body)
    return Evaluation(expr, result, comparisons, unknown, unregistered)


def _scales_keys(node: ast.AST) -> bool:
    """True when the expression multiplies or divides two sides that both read registry keys."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.BinOp) and isinstance(sub.op, (ast.Mult, ast.Div)) \
                and _names(sub.left) and _names(sub.right):
            return True
    return False


def _names(node: ast.AST) -> list[str]:
    return [n.id for n in _key_nodes(node)]


# --------------------------------------------------------------------------- rules file helpers

def claims(rules: dict) -> dict[str, dict]:
    """Claim tables in file order (tables with a `kind`)."""
    return {cid: t for cid, t in rules.items() if isinstance(t, dict) and "kind" in t}


def tripwires(rules: dict) -> dict[str, dict]:
    return dict(rules.get("tripwires", {}))


def validate_rules(rules: dict) -> None:
    """Static checks: kinds, required fields, every expression parses, claimed text is verbatim."""
    meta = rules.get("meta", {})
    hypothesis, thesis = meta.get("hypothesis", ""), meta.get("thesis", "")
    problems = []
    every = claims(rules)
    for cid, rule in every.items():
        if rule["kind"] not in CLAIM_KINDS:
            problems.append(f"{cid}: unknown kind {rule['kind']!r}")
        for field in ("title", "test", "metrics", "chart", "section"):
            if field not in rule:
                problems.append(f"{cid}: missing {field}")
        if rule["kind"] in ("stated", "hidden", "reframe"):
            claimed = rule.get("claimed", "")
            if not claimed or not (claimed in hypothesis or (thesis and claimed in thesis)):
                problems.append(f"{cid}: claimed text is not a verbatim fragment of meta.hypothesis or meta.thesis")
        if rule["kind"] == "hidden" and not rule.get("assumption"):
            problems.append(f"{cid}: hidden assumption needs `assumption`")
        for field in ("supported", "refuted"):
            if field not in rule:
                problems.append(f"{cid}: missing {field}")
        if "untestable_if" in rule and not (rule.get("untestable_reason") and rule.get("trigger")):
            problems.append(f"{cid}: untestable_if needs untestable_reason and trigger")
        if "not_computed_if" in rule and not rule.get("not_computed_reason"):
            problems.append(f"{cid}: not_computed_if needs not_computed_reason")
        for field in EXPR_FIELDS:
            if field in rule:
                try:
                    parse(rule[field])
                except RuleError as exc:
                    problems.append(f"{cid}.{field}: {exc}")
    for tid, trip in tripwires(rules).items():
        for field in ("metric", "metric_label", "threshold_text", "expr", "meaning", "chart"):
            if field not in trip:
                problems.append(f"tripwires.{tid}: missing {field}")
        if trip.get("group", "break") not in ("break", "confirm"):
            problems.append(f"tripwires.{tid}: group must be 'break' or 'confirm'")
        if "expr" in trip:
            try:
                parse(trip["expr"])
            except RuleError as exc:
                problems.append(f"tripwires.{tid}.expr: {exc}")
    if problems:
        raise RuleError("thesis/rules.toml: " + "; ".join(problems))


def contract_keys(rules: dict) -> list[str]:
    """Keys the rules require models to register (metrics, requires, tripwire metric/keys)."""
    keys: list[str] = []
    for rule in claims(rules).values():
        keys += list(rule.get("metrics", [])) + list(rule.get("requires", []))
    for trip in tripwires(rules).values():
        keys += [trip["metric"], *trip.get("keys", [])]
    return sorted(set(keys))


def rule_text(rule: dict) -> str:
    """One line: every state's condition, in evaluation order."""
    parts = []
    if "not_computed_if" in rule:
        parts.append(f"Not computed if {pretty(rule['not_computed_if'])}")
    if "untestable_if" in rule:
        parts.append(f"Not testable if {pretty(rule['untestable_if'])}")
    parts.append(f"Supported if {pretty(rule['supported'])}")
    parts.append(f"Refuted if {pretty(rule['refuted'])}")
    parts.append(f"Partly if {pretty(rule['partly'])}" if "partly" in rule else "otherwise Partly")
    return " · ".join(parts)


# --------------------------------------------------------------------------- per-claim evaluation

def _values(keys: list[str], reg: Registry) -> dict[str, dict]:
    out = {}
    for key in keys:
        if key in reg:
            n = reg.get(key)
            out[key] = {"value": n.value, "display": n.display, "unit": n.unit, "null_reason": n.null_reason}
        else:
            out[key] = {"value": None, "display": "n/a", "unit": None,
                        "null_reason": f"{key} is not registered by any model"}
    return out


def _margin(comparisons: list[dict], want: bool) -> dict | None:
    """First numeric comparison whose result equals `want` (source order: primary metric first). A comparison
    on a product or ratio of two inputs is skipped: its difference is not in the metric's unit."""
    for c in comparisons:
        if c["result"] is want and c["diff"] is not None and not c.get("scaled"):
            return {"text": c["text"], "diff": c["diff"], "unit": c["unit"] or "x", "keys": c["keys"]}
    return None


def evaluate_claim(cid: str, rule: dict, reg: Registry) -> dict:
    lookup = Lookup(reg)
    metrics = list(rule.get("metrics", []))
    requires = list(rule.get("requires", metrics))
    out: dict[str, Any] = {
        "id": cid,
        "kind": rule["kind"],
        "title": rule.get("title", ""),
        "section": rule.get("section"),
        "verdict": None,
        "rule_text": rule_text(rule),
        "metric_keys": metrics,
        "values": _values(metrics, reg),
        "binding_condition": None,
        "margin": None,
        "margin_detail": None,
        "margin_key": f"margin_{cid}",
        "chart_ids": list(rule.get("charts", [rule["chart"]] if "chart" in rule else [])),
        "null_reason": None,
        "reason": None,
        "trigger": None,
        "unregistered": [],
    }

    def not_computed(reason: str, unregistered: list[str] | None = None) -> dict:
        out.update(verdict=NOT_COMPUTED, null_reason=reason, reason=reason,
                   unregistered=sorted(set(unregistered or [])))
        return out

    # 1. required inputs
    missing = []
    unregistered = []
    for key in requires:
        v, why = lookup.value(key)
        if v is None:
            missing.append(why)
            if key not in reg:
                unregistered.append(key)
    if missing:
        return not_computed("; ".join(missing), unregistered)

    # 2. explicit not-computed condition, 3. not testable
    for field, verdict, reason in (("not_computed_if", NOT_COMPUTED, rule.get("not_computed_reason")),
                                   ("untestable_if", NOT_TESTABLE, rule.get("untestable_reason"))):
        if field not in rule:
            continue
        ev = evaluate(rule[field], lookup)
        if ev.result is None:
            return not_computed(ev.reason(), ev.unregistered)
        if ev.result:
            out.update(verdict=verdict, binding_condition=pretty(rule[field]), reason=reason,
                       margin=_margin(ev.comparisons, True))
            if verdict == NOT_COMPUTED:
                out["null_reason"] = reason
            else:
                out["trigger"] = rule.get("trigger")
            return out

    # 4-6. supported, refuted, partly
    sup = evaluate(rule["supported"], lookup)
    if sup.result is True:
        out.update(verdict=SUPPORTED, binding_condition=pretty(rule["supported"]),
                   margin=_margin(sup.comparisons, True))
        return out
    ref = evaluate(rule["refuted"], lookup)
    if ref.result is True:
        out.update(verdict=REFUTED, binding_condition=pretty(rule["refuted"]),
                   margin=_margin(ref.comparisons, True))
        return out
    if sup.result is None or ref.result is None:
        unknown = {**sup.unknown, **ref.unknown}
        return not_computed("; ".join(unknown[k] for k in sorted(unknown)),
                            sup.unregistered + ref.unregistered)
    if "partly" in rule:
        par = evaluate(rule["partly"], lookup)
        if par.result is None:
            return not_computed(par.reason(), par.unregistered)
        if par.result is False:
            return not_computed(f"rules gap: no state of {cid} matched the values (check thesis/rules.toml)")
        out.update(verdict=PARTLY, binding_condition=pretty(rule["partly"]),
                   margin=_margin(par.comparisons, True))
        return out
    # between the two rules there is no single threshold to measure a distance from, so no margin
    out.update(verdict=PARTLY, binding_condition=NEITHER, margin=None)
    return out


def evaluate_tripwire(tid: str, trip: dict, reg: Registry) -> dict:
    lookup = Lookup(reg)
    ev = evaluate(trip["expr"], lookup)
    keys = list(dict.fromkeys([trip["metric"], *trip.get("keys", [])]))
    state = "not computed" if ev.result is None else ("tripped" if ev.result else "clear")
    return {
        "id": tid,
        "metric": trip["metric"],
        "metric_label": trip.get("metric_label") or trip["metric"],  # the human name the page prints
        "keys": keys,
        "threshold_text": trip["threshold_text"],
        "expr": trip["expr"],
        "rule_text": pretty(trip["expr"]),
        "meaning": trip["meaning"],
        "group": trip.get("group", "break"),   # break: part of the thesis fails; confirm: the thesis gains ground, or a closed test opens
        "chart": trip["chart"],
        "state": state,
        "tripped": state == "tripped",
        "reason": ev.reason() if ev.result is None else None,
        "values": _values(keys, reg),
        "unregistered": sorted(set(ev.unregistered)),
    }


# --------------------------------------------------------------------------- registry outputs

def _files_of(keys: list[str], reg: Registry) -> list[str]:
    """Union of the keys' source files (already ROOT-relative 'data/...', which Registry.put accepts)."""
    files: set[str] = set()
    for key in keys:
        if key in reg:
            files.update(reg.get(key).source_files)
    return sorted(files)


def run(reg: Registry, rules: dict) -> tuple[dict, dict]:
    """Evaluate every claim and tripwire; register tally and margin numbers. Returns (verdicts, tripwires)."""
    validate_rules(rules)
    verdicts: dict[str, Any] = {}
    for cid, rule in claims(rules).items():
        verdicts[cid] = evaluate_claim(cid, rule, reg)

    # margins (one key per claim; null when no numeric comparison decided the verdict)
    for cid, v in verdicts.items():
        m = v["margin"]
        if m is not None:
            # the margin is a distance from the threshold (unsigned); margin_detail keeps the signed diff
            n = reg.put(f"margin_{cid}", abs(m["diff"]), m["unit"], "F-margin",
                        source_files=_files_of(m["keys"], reg), inputs=m["keys"], signed=False)
            m["value"], m["display"] = n.value, n.display
            # pages print `margin` as text; the comparison it came from stays in margin_detail
            v["margin"], v["margin_detail"] = n.display, m
        else:
            reason = {NOT_COMPUTED: f"{cid} was not computed",
                      PARTLY: f"{cid}: no numeric comparison decided the verdict"}.get(
                v["verdict"], f"{cid}: the deciding rule has no numeric comparison")
            if v["binding_condition"] == NEITHER:
                reason = f"{cid} sits between the Supported and Refuted rules, so no single threshold decides it"
            reg.put(f"margin_{cid}", None, "x", "F-margin", null_reason=reason)

    # tally over H claims (stated + hidden)
    tallied = [cid for cid, v in verdicts.items() if v["kind"] in TALLY_KINDS]
    by_state = {s: [cid for cid in tallied if verdicts[cid]["verdict"] == s] for s in STATES}
    tally = {
        "supported": len(by_state[SUPPORTED]),
        "partly": len(by_state[PARTLY]),
        "refuted": len(by_state[REFUTED]),
        "not_testable": len(by_state[NOT_TESTABLE]),
        "not_computed": len(by_state[NOT_COMPUTED]),
        "claims": len(by_state[SUPPORTED]) + len(by_state[PARTLY]) + len(by_state[REFUTED]),
        "total": len(tallied),
        "stated": sum(1 for cid in tallied if verdicts[cid]["kind"] == "stated"),
        "hidden": sum(1 for cid in tallied if verdicts[cid]["kind"] == "hidden"),
        # per kind, so prose never mixes the author's stated claims with assumptions they did not state
        **{f"{what}_{kind}": sum(1 for cid in tallied if verdicts[cid]["kind"] == kind
                                 and verdicts[cid]["verdict"] in states)
           for kind in ("stated", "hidden")
           for what, states in (("refuted", (REFUTED,)), ("supported", (SUPPORTED,)), ("partly", (PARTLY,)),
                                ("decided", (SUPPORTED, PARTLY, REFUTED)))},
        "ids": {s: by_state[s] for s in STATES},
        "counted": tallied,
        "separate": [cid for cid in verdicts if cid not in tallied],
        "note": rules.get("meta", {}).get("tally_note", ""),
    }
    metric_keys = sorted({k for cid in tallied for k in verdicts[cid]["metric_keys"]})
    files = _files_of(metric_keys, reg)
    for key, count in (("n_supported", tally["supported"]), ("n_partly", tally["partly"]),
                       ("n_refuted", tally["refuted"]), ("n_untestable", tally["not_testable"]),
                       ("n_not_computed", tally["not_computed"]), ("n_claims", tally["claims"]),
                       ("n_claims_total", tally["total"]), ("n_claims_stated", tally["stated"]),
                       ("n_claims_hidden", tally["hidden"]),
                       ("n_refuted_stated", tally["refuted_stated"]), ("n_refuted_hidden", tally["refuted_hidden"]),
                       ("n_decided_stated", tally["decided_stated"]), ("n_decided_hidden", tally["decided_hidden"]),
                       ("n_supported_stated", tally["supported_stated"]),
                       ("n_partly_stated", tally["partly_stated"])):
        reg.put(key, count, "count", "F-tally", source_files=files, inputs=tallied)
    verdicts["tally"] = tally

    # the Supported claim with the smallest margin in percentage points, so the page can name the thinnest point
    # without hand-picking it (count margins are not comparable and are left out)
    thin = [(reg.value(f"margin_{cid}"), cid) for cid, v in verdicts.items()
            if cid != "tally" and v["verdict"] == SUPPORTED
            and f"margin_{cid}" in reg and reg.value(f"margin_{cid}") is not None
            and reg.get(f"margin_{cid}").unit == "pp"]
    pick = min(thin) if thin else None
    reg.put("thinnest_supported_id", pick[1] if pick else None, "text", "F-margin",
            source_files=files, inputs=[f"margin_{c} {m:.4f} pp" for m, c in sorted(thin)],
            null_reason=None if pick else "no Supported claim has a margin in percentage points")

    trips = {tid: evaluate_tripwire(tid, trip, reg) for tid, trip in tripwires(rules).items()}
    return verdicts, trips

