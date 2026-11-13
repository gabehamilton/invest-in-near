"""Front end: templates, page prose and charts.js, tested offline against synthetic site data.

The templates are rendered with a stub of the build's helper contract (num, text, datum, chip, verdict,
rule, claimed, cite, tm, ident, copy) defined here, under StrictUndefined and the same finalize hook as
the build, so a template that prints None, reads a missing field or calls a helper outside the contract
fails here first. Every figure below is fake test data; none of it reaches site/.

Checks:
- the three templates render for several verdict scenarios (mixed, all supported, all refuted, degraded);
- a lint-mode render (helpers print digit-free markers, data strings lose their digits) leaves no digit,
  bad token or brace in the visible text, which is what pipeline/lint.py enforces on the real build;
- the real-mode render has no bad tokens or placeholder braces;
- thesis/copy.toml and thesis/open_items.toml follow their schema and are digit-free;
- the thesis section shows its two tiles, each with its figure, verdicts and charts;
- site/assets/charts.js parses (`node --check`) and loads nothing from the network;
- if pipeline.build imports, the same synthetic data also renders through the real helpers.
"""

from __future__ import annotations

import html
import math
import re
import shutil
import string
import subprocess
import tomllib
from pathlib import Path

import jinja2
import pytest
from markupsafe import Markup, escape

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"
THESIS = ROOT / "thesis"
ASSETS = ROOT / "site" / "assets"
PAGES = (("index.html.j2", "html"), ("data.html.j2", "html"), ("THESIS.md.j2", "md"))
MARK = "«{}»"


def load(name: str) -> dict:
    return tomllib.loads((THESIS / name).read_text(encoding="utf-8"))


RULES = load("rules.toml")
COPY = load("copy.toml")
OPEN_ITEMS = load("open_items.toml")
GLOSSARY = load("glossary.toml").get("terms", [])
CONSTANTS = load("constants.toml")
CLAIMS = {cid: t for cid, t in RULES.items() if isinstance(t, dict) and "kind" in t}
TRIPS = dict(RULES.get("tripwires", {}))
XCHKS = dict(RULES.get("crosschecks", {}))

EXPR_FIELDS = ("not_computed_if", "untestable_if", "supported", "partly", "refuted")
IDENT_RE = re.compile(r"(?=.*[A-Za-z§])[A-Za-z0-9_§.\-]{1,64}")
COND_RE = re.compile(r"^\s*([A-Za-z0-9_§.\-]+)\s*(!=|=)\s*(.+?)\s*$")
PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)(?::[a-z0-9]+)?\}")
ICONS = {"Supported": "✓", "Partly": "◐", "Refuted": "✕", "Not testable": "?", "Not computed": "–", "Open": "○"}


# =========================================================================== synthetic site data

def placeholders(text: str) -> set[str]:
    return set(PLACEHOLDER.findall(text or ""))


def chart_specs() -> dict[str, dict]:
    """One fake chart per id the pages show, covering every form, status and panel shape."""
    t = lambda d: f"2025-{d:02d}-01"  # noqa: E731
    line = lambda sid, ent, **kw: {"id": sid, "label": f"Series {sid}", "entity": ent, "unit": "%",  # noqa: E731
                                   "points": [[t(m), (None if m == 4 else m * 0.5)] for m in range(1, 9)],
                                   "role": kw.get("role", "emphasis"), "panel": kw.get("panel"),
                                   "dashed": kw.get("dashed", False)}
    cats = lambda sid, ent, vals, **kw: {"id": sid, "label": f"Series {sid}", "entity": ent, "unit": "%",  # noqa: E731
                                         "points": [[f"Cat {i}", v] for i, v in enumerate(vals)],
                                         "role": kw.get("role", "emphasis"), "panel": kw.get("panel"),
                                         "dashed": kw.get("dashed", False)}
    table = {"columns": ["window", "value_pct", "note"], "rows": [["Cat 0", 1.25, "first"], ["Cat 1", None, True]]}
    venues = {"columns": ["venue", "near_options", "checked_at_http_status", "endpoint"],
              "rows": [["Deribit", "no", "2026-11-10T22:37:15+00:00 (HTTP 200)",
                        "https://www.deribit.com/api/v2/public/get_instruments?currency=any&kind=option"],
                       ["Aevo", "no", "2026-11-10T22:37:15+00:00", "not an url"]]}
    specs = {
        "c01": {"form": "small-multiples", "panels": [{"id": "earn", "label": "What you earn", "series_ids": ["a", "m"]},
                                                       {"id": "infl", "label": "What inflation takes", "series_ids": ["b"]}],
                "series": [line("a", "near", panel="earn"), line("m", "context", role="model", dashed=True, panel="earn"),
                           line("b", "usd", panel="infl")],
                "annotations": [{"x": t(3), "label": "Fed starts raising", "kind": "fed_turn", "panel": "earn"},
                                {"x": t(6), "label": "issuance ceiling cut", "kind": "ceiling_change"}]},
        "c02": {"form": "line", "series": [line("g", "near"), line("ceil", "context", role="context")],
                "bands": [{"axis": "x", "from": t(2), "to": t(3), "label": "Cut"}],
                "reference_lines": [{"y": 0, "label": "zero"}],
                "annotations": [{"x": t(5), "label": "Ceiling cut", "kind": "event"}]},
        "c03": {"form": "diverging-bar", "series": [cats("st", "near", [1.5, 2.0]), cats("ns", "negative", [-1.0, -2.0])]},
        "c05": {"form": "column", "series": [cats("exact", "near", [0.5, None]), cats("ub", "near", [None, 1.2], dashed=True)],
                "y": {"unit": "%", "domain": [0, 100]}, "reference_lines": [{"y": 100, "label": "burn equals issuance"}]},
        "c06": {"form": "column", "status_banner": "This chart rests on one source.",
                "series": [line("vol", "third"), line("ctx", "context", role="context")]},
        "c07": {"form": "line", "status": "fallback", "series": [line("r", "near"), line("ff", "usd")]},
        "c08": {"form": "bar", "series": [cats("gap", "near", [0.3]), cats("day", "context", [5.4])]},
        "c09": {"form": "line", "status": "partial", "series": [line("nv", "near"), line("btc", "context", role="context")]},
        "c10": {"form": "table-heatmap", "series": [cats("now", "near", [4.2, 3.5, 3.1]), cats("cut", "near", [2.7, 2.2, 2.0])],
                "y": {"unit": "%", "diverging_center": 4.1}, "reference_lines": [{"y": 4.1, "label": "FOMC median"}]},
        "c11": {"form": "small-multiples", "status": "error",
                "panels": [{"id": p, "label": f"Panel {p}", "series_ids": [f"n{p}", f"u{p}"]} for p in ("m", "c")],
                "series": [line(f"{e}{p}", ent, panel=p) for p in ("m", "c") for e, ent in (("n", "near"), ("u", "usd"))]},
        "c12": {"form": "diverging-bar", "series": [cats("sq", "near", [9.7, -0.2], role="diverging"),
                                                    cats("qtd", "context", [0.3], role="context")],
                "bands": [{"axis": "y", "from": -0.5, "to": 0.5, "label": "in balance"}],
                "annotations": [{"x": "Cat 0", "label": "LST data begins", "kind": "data_boundary"}]},
        "c13": {"form": "dot-range", "x": {"type": "linear", "domain": [-12, 12], "clamp": True},
                "panels": [{"id": "own", "label": "Own", "series_ids": ["d1"]}, {"id": "usd", "label": "Dollar", "series_ids": ["d2"]}],
                "series": [{"id": "d1", "label": "Own", "entity": "near", "unit": "pp", "panel": "own", "role": "emphasis",
                            "dashed": False, "points": [["NEAR", 1.6, -1.0, 3.0], ["Turkey", 32.1, 20.0, 40.0]]},
                           {"id": "d2", "label": "Dollar", "entity": "usd", "unit": "pp", "panel": "usd", "role": "emphasis",
                            "dashed": False, "points": [["NEAR", 0.4, None, None]]}],
                "reference_lines": [{"x": 0, "label": "zero"}]},
        "c14": {"form": "table",
                "panels": [{"id": "checklist", "label": "Checklist", "row_templates": [
                               {"requirement": "Principal stable", "usd": "Within the band on {fake_a} of days",
                                "usd_outcome_key": "fake_outcome", "near": "Never fell", "near_outcome_key": "missing_key"},
                               {"requirement": "Legal vehicle", "usd": "Not a sweep vehicle", "usd_outcome_key": None,
                                "near": None, "near_outcome_key": None}]},
                           {"id": "liquidity", "label": "Epoch duration", "series_ids": ["eh"]}],
                "series": [{**line("eh", "context", role="context", panel="liquidity"), "unit": "hours"}]},
        "c15": {"form": "diverging-bar",
                "y": {"unit": "%", "domain": [-100, 100], "clamp": True},
                "panels": [{"id": "venues", "label": "Which venues list options", "table": venues},
                           {"id": "quarters", "label": "By quarter", "series_ids": ["f"], "table": table}],
                "series": [cats("f", "near", [5.0, -1.0], role="diverging", panel="quarters")]},
        "c16": {"form": "empty", "status": "unavailable",
                "empty_state": {"reason": "BigQuery not configured", "needs": "GCP_PROJECT",
                                "fix_command": "make check-bq"}},
        "c21": {"form": "dot-range", "x": {"type": "linear", "domain": [-5, 15], "clamp": True},
                "series": [{"id": "g", "label": "Money growth", "entity": "usd", "unit": "%", "panel": None,
                            "role": "emphasis", "dashed": False,
                            "points": [["US", 4.5, None, None], ["Euro area", 3.1, None, None]]}],
                "reference_lines": [{"x": 2.4, "label": "NEAR supply growth"}]},
    }
    out = {}
    for cid, sp in specs.items():
        claims = [c for c, r in CLAIMS.items() if cid in (r.get("charts") or [r.get("chart")])]
        out[cid] = {
            "id": cid, "status": "ok", "x": {"type": "time"}, "y": {"unit": "%"}, "panels": [], "series": [],
            "reference_lines": [], "bands": [], "annotations": [], "table": table, "values": {},
            "title_template": "This chart shows {fake_a} against {fake_b}",
            "sentence_template": "Each point is one window; the latest is {fake_a}.",
            "claims": claims, "verdicts": claims, "sources": ["Fake source 2 (https://example.test/v1)"],
            "coverage": {"first": "2021-01-01", "last": "2026-11-09"}, "as_of": "2026-11-10T01:02:03Z",
            "queries": [{"kind": "http", "endpoint": "https://example.test/api?id=1", "file": "data/raw/x/y.csv",
                         "body_or_sql": "SELECT 1 WHERE a > {b}"}],
            "formula_ids": ["F-growth", "F-new-thing"], "code_ref": "pipeline/models/fake.py:chart_2",
            "empty_state": None, "status_banner": None, **sp,
        }
    return out


STATE_SETS = {
    "mixed": {"H1": "Refuted", "H1b": "Supported", "H2": "Partly", "H3": "Supported", "H4": "Not testable",
              "H5": "Refuted", "H6": "Refuted", "H6_near": "Supported", "H7": "Supported", "H8": "Not testable",
              "H9": "Refuted", "H10": "Refuted", "H12": "Refuted"},
    "all_supported": {},
    "all_refuted": {},
    "degraded": {"H8": "Not testable"},
}
DEFAULT_STATE = {"mixed": "Partly", "all_supported": "Supported", "all_refuted": "Refuted", "degraded": "Not computed"}


def template_keys() -> set[str]:
    """Registry keys the templates name in literal num('key') and num_text('key') calls."""
    keys = set()
    for path in TEMPLATES.glob("*.j2"):
        src = path.read_text(encoding="utf-8")
        keys |= set(re.findall(r"num(?:_text)?\('([a-z][a-z0-9_]*)'", src))
    return keys


def make_data(scenario: str) -> dict:
    degraded = scenario == "degraded"
    charts = chart_specs()
    if degraded:
        for cid in ("c02", "c03", "c05"):
            charts[cid].update(status="unavailable", series=[], empty_state={"reason": "Source failed", "needs": None})

    keys = {"g_365", "staker_share_365", "y_lst_365", "burn_share_365", "rv_365_near", "intents_vol_365d",
            "burn_method", "fake_a", "fake_b", "fake_outcome", "bq_max_date",
            "n_sources", "n_sources_ok", "n_sources_stale", "n_sources_missing"}
    if not degraded:  # the degraded build stands in for an older registry: the pill falls back to stale/missing
        keys |= {"n_sources_attention", "n_sources_not_configured"}
    for entry in COPY.values():
        for v in entry.get("variants", []):
            keys |= placeholders(v.get("text", ""))
    for item in OPEN_ITEMS.get("items", []):
        for f in ("title", "text", "resolve"):
            keys |= placeholders(item.get(f, ""))
    for term in GLOSSARY:
        keys |= placeholders(term.get("term", "")) | placeholders(term.get("text", ""))
    for cid, r in CLAIMS.items():
        keys |= set(r.get("metrics") or []) | {f"margin_{cid}"}
    for t in TRIPS.values():
        keys |= {t["metric"], *t.get("keys", [])} | placeholders(t.get("now_text", ""))
    keys |= template_keys()
    xkeys = {xid: {"value": f"xchk_{xid.lower()}_value", "n": f"xchk_{xid.lower()}_n"} for xid in XCHKS}
    for m in xkeys.values():
        keys |= set(m.values())

    # registry text keys that copy conditions name (burn_method_short, thinnest_supported_id, ...): the build reads
    # their value as the condition's state, so the stub registers them as text
    subjects = {COND_RE.match(c).group(1) for e in COPY.values() for v in e.get("variants", []) for c in v.get("when", [])}
    text_keys = subjects - set(CLAIMS) - set(TRIPS) - set(XCHKS) - set(charts)
    keys |= text_keys

    numbers = {}
    for i, k in enumerate(sorted(keys)):
        null = (degraded and i % 3 == 0) or k == "bq_max_date"
        v = None if null else round(0.37 * (i + 1) - 4, 2)
        numbers[k] = {"key": k, "value": v, "display": None if null else f"{v:.2f}%", "unit": "%",
                      "formula_id": "F-growth" if i % 2 else "K-treasury_share", "inputs": ["fake_input_2"],
                      "source_files": ["data/raw/fake/file_1.csv"], "as_of": "2026-11-10T00:00:00Z",
                      "status": "ok", "null_reason": "fake reason: input 7 missing" if null else None}
        if k in text_keys:
            numbers[k].update(unit="text", value=None if null else "fake", display=None if null else "fake")
    verdicts = {}
    for cid, r in CLAIMS.items():
        state = STATE_SETS[scenario].get(cid, DEFAULT_STATE[scenario])
        nc = state == "Not computed"
        verdicts[cid] = {
            "id": cid, "kind": r["kind"], "title": r.get("title"), "section": r.get("section"), "verdict": state,
            "rule_text": "rule text 1", "metric_keys": [k for k in r.get("metrics") or [] if k in numbers],
            "values": {}, "binding_condition": "" if nc else f"{(r.get('metrics') or ['x'])[0]} > 0",
            "margin": None if nc else "+0.5 pp", "margin_detail": None,
            "margin_key": f"margin_{cid}", "chart_ids": r.get("charts") or [r.get("chart")] if r.get("chart") else [],
            "null_reason": "input 3 is null" if nc else None,
            "reason": "no venue lists options" if state == "Not testable" else None,
            "trigger": r.get("trigger"), "unregistered": [],
        }
    counted = [c for c, r in CLAIMS.items() if r["kind"] in ("stated", "hidden")]
    by = lambda s: [c for c in counted if verdicts[c]["verdict"] == s]  # noqa: E731
    verdicts["tally"] = {"supported": len(by("Supported")), "partly": len(by("Partly")), "refuted": len(by("Refuted")),
                         "not_testable": len(by("Not testable")), "not_computed": len(by("Not computed")),
                         "claims": len(by("Supported")) + len(by("Partly")) + len(by("Refuted")),
                         "total": len(counted), "stated": sum(1 for c in counted if CLAIMS[c]["kind"] == "stated"),
                         "hidden": sum(1 for c in counted if CLAIMS[c]["kind"] == "hidden"), "counted": counted,
                         "separate": [c for c in CLAIMS if c not in counted], "ids": {}, "note": "fake note 2"}

    trip_states = ["clear", "tripped", "not computed"]
    tripwires = {}
    for i, (tid, t) in enumerate(TRIPS.items()):
        st = "not computed" if degraded else trip_states[i % 3]
        tripwires[tid] = {"id": tid, "metric": t["metric"], "keys": t.get("keys", []), "threshold_text": t["threshold_text"],
                          "expr": t["expr"], "rule_text": t["expr"], "meaning": t["meaning"], "chart": t.get("chart"),
                          "state": st, "tripped": st == "tripped", "reason": "metric 5 is null" if st != "clear" else None,
                          "values": {}, "unregistered": []}
    x_states = ["pass", "fail", "not run", "withheld"]
    crosschecks = {}
    for i, (xid, x) in enumerate(XCHKS.items()):
        st = "not run" if degraded else x_states[i % 4]
        crosschecks[xid] = {"id": xid, "title": x.get("title"), "tolerance_text": x.get("tolerance_text"),
                            "tolerance": 1.0, "observed": "x 0.99 · n 30" if st in ("pass", "fail") else None,
                            "detail": None, "blocks_verify": bool(x.get("blocks_verify")), "needs": x.get("needs"),
                            "fix": x.get("fix"), "status": st, "reason": "needs a key 2" if st != "pass" else None,
                            "keys": xkeys[xid]}

    states = ["ok", "lagging", "stale", "partial", "failed", "not_configured", "withheld"]
    sources = []
    for i, st in enumerate(states):
        missing = st in ("not_configured", "withheld")
        sources.append({"id": f"src{i}", "name": f"Source {i}", "class": "approved-primary", "endpoint": "https://e.test/1",
                        "auth": "none", "state": "failed" if degraded else st, "first_obs": None if missing else "2020-10-01",
                        "last_obs": None if missing else "2026-11-09", "lag_days": None if missing else i,
                        "fetched_at": None if missing else "2026-11-10T00:00:00Z", "rows": None if missing else 100 + i,
                        "path": None if missing else f"data/raw/s{i}/*.csv", "sha256": None if missing else "ab12" * 16,
                        "committed": not missing, "tos_note": "Terms 1.2 apply" if i % 2 else None,
                        "fix": "uv run python -m pipeline.pull --only x" if st != "ok" else None,
                        "stale_text": None, "note": None, "files": []})

    build = {"as_of": "2026-11-10T00:00:00Z", "git_sha": "", "build_id": "abc123def456", "rules_sha256": "f" * 64,
             "inputs_sha256": "e" * 64,
             "constants_sha256": "d" * 64, "site_url": "https://example.test/site/", "repo_url": "https://example.test/repo"}
    return {
        "numbers": numbers, "verdicts": verdicts, "charts": charts, "sources": sources, "tripwires": tripwires,
        "crosschecks": crosschecks, "rules": RULES, "constants": CONSTANTS, "copy": COPY, "open_items": OPEN_ITEMS,
        "glossary": GLOSSARY,
        "build": build, "manifest": {"build": build, "charts": [], "raw_inputs": [], "outputs": []},
    }


# =========================================================================== stub of the helper contract

STRUCTURAL = {"id", "key", "keys", "metric", "metrics", "requires", "metric_keys", "margin_key", "chart",
              "charts", "chart_ids", "claims", "verdicts", "section", "status", "state", "verdict", "kind",
              "unit", "counted", "separate", "ids", "class", "committed", "form", "entity", "role", "panel",
              "priority", "rule", "blocks_verify", "unregistered", "type", "scale", "refs",
              "affects", "tripped", "formula_id", "formula_ids"}


def _strip(s: str) -> str:
    out, last = [], 0
    for m in PLACEHOLDER.finditer(s):
        out += [re.sub(r"\d", "", s[last:m.start()]), m.group(0)]
        last = m.end()
    return "".join(out + [re.sub(r"\d", "", s[last:])])


def _mask(obj, keep=lambda p: False, path=(), cells=lambda p: False):
    if keep(path):
        return obj
    if isinstance(obj, dict):
        return {k: (v if k in STRUCTURAL else _mask(v, keep, path + (k,), cells)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask(v, keep, path + ("*",), cells) for v in obj]
    if isinstance(obj, str):
        return _strip(obj)
    if isinstance(obj, (int, float)) and not isinstance(obj, bool) and cells(path):
        return MARK.format("datum")
    return obj


def lint_view(data: dict) -> dict:
    """What lint mode shows the templates: digits removed from data strings, registry figures kept."""
    out = dict(data)
    out["numbers"] = _mask(data["numbers"], lambda p: len(p) == 2 and p[1] in ("value", "display"))
    out["verdicts"] = _mask(data["verdicts"], lambda p: len(p) == 2 and p[1] == "margin")
    out["charts"] = _mask(data["charts"], lambda p: len(p) == 2 and p[1] in ("title_template", "sentence_template",
                                                                             "caveat_template", "notes_templates",
                                                                             "status_banner"),
                          cells=lambda p: len(p) >= 3 and (p[1] == "series" or p[1:3] == ("table", "rows")))
    out["glossary"] = data.get("glossary", [])   # prose like copy.toml: digit-free, kept as written
    for name in ("tripwires", "sources", "manifest", "crosschecks", "rules", "constants", "build"):
        out[name] = _mask(data[name])
    return out


class StubError(Exception):
    pass


class Stub:
    """The rendering contract of pipeline/build.py, reduced to what the templates may rely on."""

    def __init__(self, data: dict, mode: str, lint: bool):
        self.data, self.html, self.lint = data, mode == "html", lint

    def _out(self, h: str, md: str):
        return Markup(h) if self.html else md

    def _mark(self, name: str, tag: str = "span", cls: str = ""):
        attr = f' class="{cls}"' if cls else ""
        return self._out(f"<{tag}{attr}>{MARK.format(name)}</{tag}>", MARK.format(name))

    def num(self, key, decimals=None, absolute=False):
        n = self.data["numbers"].get(key)
        if n is None:
            raise StubError(f"num({key!r}): not registered")
        if self.lint:
            return self._out(f'<span class="num" data-key="{escape(key)}">{MARK.format("num")}</span>', MARK.format("num"))
        if n["value"] is None:
            return self._out(f'<span class="num null" data-key="{escape(key)}" tabindex="0" '
                             f'title="{escape(n["null_reason"] or "")}">n/a</span>', "n/a")
        shown = n["display"].lstrip("-") if absolute else n["display"]   # the real helper prints the magnitude
        return self._out(f'<span class="num" data-key="{escape(key)}" tabindex="0" '
                         f'title="{escape(n["formula_id"])}">{escape(shown)}</span>', shown)

    def num_text(self, key):
        """num() as plain text for <option> and <button> labels: the same guards, no span."""
        n = self.data["numbers"].get(key)
        if n is None:
            raise StubError(f"num_text({key!r}): not registered")
        if self.lint:
            return self._out(str(escape(MARK.format("num"))), MARK.format("num"))
        shown = "n/a" if n["value"] is None else n["display"]
        return self._out(str(escape(shown)), shown)

    def text(self, template):
        if template is None:
            raise StubError("text(None)")
        h, md = [], []
        for literal, name, _spec, _conv in string.Formatter().parse(str(template)):
            h.append(str(escape(literal)))
            md.append(literal)
            if name is not None:
                r = self.num(name)
                h.append(str(r))
                md.append(str(r))
        return self._out("".join(h), "".join(md))

    def datum(self, value, unit=None, decimals=None):
        if self.lint or value == MARK.format("datum"):
            return self._out(f"<data>{MARK.format('datum')}</data>", MARK.format("datum"))
        if value is None or value == "":
            return self._out('<data class="null">n/a</data>', "n/a")
        return self._out(f'<data value="{escape(str(value))}">{escape(str(value))}</data>', str(value))

    def verdict(self, cid):
        if cid not in CLAIMS or cid not in self.data["verdicts"]:
            raise StubError(f"verdict({cid!r}): not a claim")
        return self.data["verdicts"][cid]

    def chip(self, cid):
        v = self.verdict(cid)["verdict"]
        if self.lint:
            return self._mark("chip", cls="chip")
        slug = v.lower().replace(" ", "-")
        return self._out(f'<span class="chip chip-{slug}" data-verdict="{slug}"><span class="chip-icon" '
                         f'aria-hidden="true">{ICONS[v]}</span> <span class="chip-word">{v}</span></span>', f"{ICONS[v]} {v}")

    def _rule_text(self, ident, field):
        if ident in CLAIMS:
            r = CLAIMS[ident]
            if field == "rule":
                return " · ".join(f"{f} {r[f]}" for f in EXPR_FIELDS if f in r)
            if field in EXPR_FIELDS:
                if field in r:
                    return r[field]
                if field == "partly":
                    return "otherwise"
                raise StubError(f"rule({ident!r}, {field!r}): not defined")
            if field == "binding":
                return self.data["verdicts"][ident].get("binding_condition") or ""
            if isinstance(r.get(field), str):
                return r[field]
            raise StubError(f"rule({ident!r}, {field!r}): no such text field")
        for table, text_fields in ((TRIPS, {"rule": "expr", "expr": "expr"}), (XCHKS, {"rule": "tolerance_text",
                                                                                     "tolerance": "tolerance_text"})):
            if ident in table:
                t = table[ident]
                if field in text_fields:
                    return t[text_fields[field]]
                if isinstance(t.get(field), str):
                    return t[field]
                raise StubError(f"rule({ident!r}, {field!r}): no such field")
        if ident in CONSTANTS and isinstance(CONSTANTS[ident], dict):
            c = CONSTANTS[ident]
            if field in ("rule", "value"):
                if "value" in c:
                    return str(c["value"])
                if "lo" in c and "hi" in c:
                    return f"{c['lo']}–{c['hi']}"
                return str(c.get("quote", ""))
            if isinstance(c.get(field), str):
                return c[field]
            raise StubError(f"rule({ident!r}, {field!r}): no such constant field")
        if ident == "meta" and isinstance(RULES.get("meta", {}).get(field), str):
            return RULES["meta"][field]
        raise StubError(f"rule({ident!r}): not a claim, tripwire, cross-check or constant")

    def rule(self, ident, field="rule"):
        text = self._rule_text(ident, field)
        if self.lint:
            return self._mark("rule", cls="rule")
        return self._out(f'<span class="rule">{escape(text)}</span>', text)

    def claimed(self, cid):
        text = (RULES.get("meta", {}).get(cid, "") if cid in ("hypothesis", "thesis")
                else CLAIMS.get(cid, {}).get("claimed", ""))
        if not text:
            raise StubError(f"claimed({cid!r}): no verbatim fragment")
        if self.lint:
            return self._mark("claimed", tag="q", cls="claimed")
        return self._out(f'<q class="claimed">{escape(text)}</q>', f"“{text}”")

    def cite(self, text, href=None):
        if self.lint:
            return self._mark("cite", tag="cite")
        return self._out(f"<cite>{escape(str(text))}</cite>", str(text))

    def tm(self, iso):
        if self.lint:
            return self._mark("time", tag="time")
        if not iso:
            return self._out('<span class="time null">n/a</span>', "n/a")
        return self._out(f'<time datetime="{escape(str(iso))}">{escape(str(iso))}</time>', str(iso))

    def ident(self, text, link=None):
        t = str(text)
        if self.lint:
            return self._mark("id", cls="id")
        if not IDENT_RE.fullmatch(t):
            raise StubError(f"ident({t!r}): not an identifier")
        if link:
            return self._out(f'<a class="id" href="{escape(link + t)}">{escape(t)}</a>', f"[{t}]({link}{t})")
        return self._out(f'<span class="id">{escape(t)}</span>', t)

    def _state(self, subject):
        d = self.data
        if subject in CLAIMS:
            return d["verdicts"][subject]["verdict"]
        if subject in d["tripwires"]:
            return d["tripwires"][subject]["state"]
        if subject in d["crosschecks"]:
            return d["crosschecks"][subject]["status"]
        for s in d["sources"]:
            if s["id"] == subject:
                return s["state"]
        if subject in d["charts"]:
            return d["charts"][subject]["status"]
        n = d["numbers"].get(subject)
        if isinstance(n, dict) and n.get("unit") == "text":
            return "n/a" if n.get("value") is None else str(n["value"])
        raise StubError(f"copy condition subject {subject!r} is unknown")

    def condition(self, cond):
        m = COND_RE.match(cond)
        if not m:
            raise StubError(f"bad condition {cond!r}")
        subject, op, wanted = m.groups()
        norm = lambda s: re.sub(r"[\s_\-]", "", str(s)).lower()  # noqa: E731
        hit = norm(self._state(subject)) in {norm(w) for w in wanted.split("|")}
        return hit if op == "=" else not hit

    def copy(self, name):
        entry = COPY.get(name)
        if not isinstance(entry, dict):
            raise StubError(f"copy({name!r}): no such entry")
        for v in entry["variants"]:
            if all(self.condition(c) for c in v.get("when", [])):
                return self.text(v["text"])
        raise StubError(f"copy({name!r}): no variant matches and no fallback")

    def copy_chart(self, name, default):
        entry = COPY.get(name)
        if not isinstance(entry, dict):
            raise StubError(f"copy_chart({name!r}): no such entry")
        for v in entry["variants"]:
            if all(self.condition(c) for c in v.get("when", [])):
                chart = v.get("chart") or default
                if chart not in self.data["charts"]:
                    raise StubError(f"copy_chart({name!r}): chart {chart!r} is not in the chart set")
                return chart
        raise StubError(f"copy_chart({name!r}): no variant matches and no fallback")

    def trip_now(self, tid):
        t = TRIPS.get(tid)
        if t is None:
            raise StubError(f"trip_now({tid!r}): no such tripwire")
        return self.text(t["now_text"]) if t.get("now_text") else self.num(t["metric"])

    def globals(self):
        return {n: getattr(self, n) for n in ("num", "text", "datum", "chip", "verdict", "rule", "claimed", "cite",
                                              "tm", "ident", "copy", "copy_chart", "trip_now", "num_text")}


def finalize(value):
    if value is None:
        raise StubError("a template expression printed None")
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise StubError(f"a template expression printed {value!r}")
    return value


def render(scenario: str, lint: bool = False) -> dict[str, tuple[str, str]]:
    return render_data(make_data(scenario), lint)


def render_data(data: dict, lint: bool = False) -> dict[str, tuple[str, str]]:
    view = lint_view(data) if lint else data
    out = {}
    for name, mode in PAGES:
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES)), autoescape=(mode == "html"),
                                 undefined=jinja2.StrictUndefined, finalize=finalize, keep_trailing_newline=True)
        env.globals.update(Stub(data, mode, lint).globals())
        ctx = {k: view[k] for k in ("numbers", "verdicts", "charts", "sources", "rules", "constants", "open_items",
                                    "tripwires", "crosschecks", "build", "manifest", "glossary")}
        ctx["claim_order"] = list(CLAIMS)
        out[name] = (env.get_template(name).render(**ctx), mode)
    return out


# =========================================================================== a scanner like pipeline/lint.py

_BLOCKS = [re.compile(p, re.S | re.I) for p in (r"<!--.*?-->", r"<script\b.*?</script\s*>", r"<style\b.*?</style\s*>",
                                                r"<pre\b.*?</pre\s*>", r"<code\b.*?</code\s*>")]


def visible(text: str, mode: str) -> str:
    keep_lines = lambda m: "\x00" + "\n" * m.group(0).count("\n")  # noqa: E731
    if mode == "html":
        for p in _BLOCKS:
            text = p.sub(keep_lines, text)
        return html.unescape(re.sub(r"<[^>]*>", keep_lines, text, flags=re.S))
    text = _BLOCKS[0].sub(keep_lines, text)
    text = re.sub(r"^(```|~~~).*?^\1[^\n]*$", keep_lines, text, flags=re.S | re.M)
    return re.sub(r"`[^`\n]*`", "\x00", text)


def findings(text: str, mode: str, *, digits: bool, braces: str) -> list[str]:
    brace_re = re.compile(r"[{}]") if braces == "any" else re.compile(r"\{\{|\}\}|\{%|%\}|\{#|#\}|\{[A-Za-z_]\w*\}")
    out = []
    for i, raw in enumerate(visible(text, mode).splitlines(), 1):
        line = raw.replace("\x00", " ")
        if digits and re.search(r"\d", line):
            out.append(f"{i}: digit: {line.strip()[:120]}")
        if re.search(r"\b(NaN|undefined|Infinity)\b", line) or re.search(r"[:=]\s*(None|null)\b", line):
            out.append(f"{i}: bad token: {line.strip()[:120]}")
        if any(seg.strip(" \t.,;:()[]*_-–·\"'“”") in ("None", "null") for seg in re.split(r"[\x00|]", raw)):
            out.append(f"{i}: bad token as a value: {line.strip()[:120]}")
        if brace_re.search(line):
            out.append(f"{i}: brace: {line.strip()[:120]}")
    return out


# =========================================================================== tests

SCENARIOS = list(STATE_SETS)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_pages_render_in_real_mode(scenario):
    pages = render(scenario)
    index, data, thesis = (pages[n][0] for n, _ in PAGES)
    for cid in chart_specs():
        page = data if cid == "c16" else index
        assert f'id="{cid}"' in page, f"chart card {cid} missing"
    assert index.count("<h1") == 1 and data.count("<h1") == 1
    assert 'class="skip" href="#main"' in index and 'id="main"' in index
    for cid in CLAIMS:
        assert f'data-claim="{cid}"' in index + data, f"no verdict detail for {cid}"
    for page in (index, data):
        foot = page[page.index('<footer class="site-foot"'):]
        assert '<time datetime="2026-11-10T00:00:00Z">' in foot and "Source on GitHub" in foot, "footer: date and source"
    assert "Data as of 2026-11-10T00:00:00Z" in thesis
    assert "## Not computed, and why" in thesis
    for name, (text, mode) in pages.items():
        problems = findings(text, mode, digits=False, braces="placeholder")
        assert not problems, f"{name} ({scenario}):\n" + "\n".join(problems[:20])


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_lint_render_has_no_typed_digits(scenario):
    for name, (text, mode) in render(scenario, lint=True).items():
        problems = findings(text, mode, digits=True, braces="any")
        assert not problems, f"{name} ({scenario}, lint render):\n" + "\n".join(problems[:20])


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_agent_module_closes_the_page_with_the_recommended_agent_first(scenario):
    index = render(scenario)["index.html.j2"][0]
    assert 'href="#agent"' in index, "nav links to the module"
    module = index[index.index('<section id="agent"'):index.index("</main>")]
    assert index.index('<section id="limits"') < index.index('<section id="agent"'), "the module follows the limits"
    order = [module.index(f'id="agent-{a}"') for a in ("nearai", "chatgpt", "claude")]
    assert order == sorted(order), "NEAR AI first, then ChatGPT, then Claude"
    prompt = module[module.index('class="agent-prompt"'):]
    assert prompt.count("<pre") == 1 and "SITE/" not in module, "one prompt, with its site URL filled in"
    assert "make explore THESIS=" in module, "the terminal route is named"
    for name in ("THESIS.md", "verdicts.json", "numbers.json", "tripwires.json", "llms.txt"):
        assert name in module, f"the prompt points the agent at {name}"
    assert 'data-attach="THESIS.md"' in module and module.count("data-attach") == 1, "only the NEAR AI card attaches"
    assert module.count("<button") == 3 and module.count(" hidden") == 3, "copy buttons need JS, so they start hidden"


def test_degraded_build_shows_reasons_not_numbers():
    index = render("degraded")["index.html.j2"][0]
    assert 'class="num null"' in index
    assert "Not computed" in index and 'data-status="unavailable"' in index
    assert 'class="banner"' in index, "fallback, partial and error charts carry a banner"


def test_every_chart_form_has_a_plot_or_table():
    index = render("mixed")["index.html.j2"][0]
    for cid, ch in chart_specs().items():
        if cid == "c16" or ch.get("status") == "unavailable":
            continue
        card = index[index.index(f'id="{cid}"'):]
        card = card[:card.index("</figure>")]
        assert 'class="plot' in card or 'class="twin twin-visible' in card, cid
        if 'class="plot' in card:
            assert f'aria-controls="{cid}-table"' in card and f'id="{cid}-table"' in card, f"{cid} has no table twin"
        assert "Show query" in card, cid


def test_copy_toml_schema():
    subjects = set(CLAIMS) | set(TRIPS) | set(XCHKS)
    for name, entry in COPY.items():
        variants = entry.get("variants")
        assert isinstance(variants, list) and variants, f"[{name}] has no variants"
        assert variants[-1].get("when", []) == [], f"[{name}] last variant must be the fallback (when = [])"
        for v in variants:
            text = v["text"]
            literal = "".join(p[0] for p in string.Formatter().parse(text))
            assert not re.search(r"\d", literal), f"[{name}] types a digit: {literal[:80]}"
            assert not re.search(r"[{}]", literal), f"[{name}] has a stray brace"
            for key in placeholders(text):
                assert re.fullmatch(r"[a-z][a-z0-9_]*", key), f"[{name}] placeholder {key}"
            for cond in v.get("when", []):
                m = COND_RE.match(cond)
                assert m, f"[{name}] bad condition {cond!r}"
                # anything else must be a registry text key (checked against the model registry in
                # tests/test_verdicts.py::test_copy_conditions_on_registry_keys_name_text_keys)
                assert m.group(1) in subjects or re.fullmatch(r"[a-z][a-z0-9_]*", m.group(1)), \
                    f"[{name}] condition names {m.group(1)!r}"
    disclosure = COPY["disclosure"]["variants"][-1]["text"]
    assert "Claude Code" in disclosure and "public data" in disclosure


def test_open_items_schema():
    items = OPEN_ITEMS.get("items", [])
    assert items
    ids = [it["id"] for it in items]
    assert len(ids) == len(set(ids))
    for it in items:
        for f in ("id", "title", "status", "text", "resolve"):
            assert isinstance(it.get(f), str) and it[f].strip(), f"{it.get('id')}: missing {f}"
        for f in ("title", "status", "text", "resolve"):
            literal = "".join(p[0] for p in string.Formatter().parse(it[f]))
            assert not re.search(r"\d", literal), f"{it['id']}.{f} types a digit"
        for link in it.get("links", []):
            assert link["href"].startswith("https://") and not re.search(r"\d", link["label"])


def test_templates_print_no_raw_numbers_from_data():
    """Template source: data numbers reach a page only through num(), text(), datum() or raw()."""
    for path in TEMPLATES.glob("*.j2"):
        src = path.read_text(encoding="utf-8")
        for m in re.finditer(r"\{\{\s*([^}]*?)\s*\}\}", src):
            expr = m.group(1)
            assert not re.search(r"\.(value|display|margin)\b(?!_)", expr), f"{path.name}: prints {expr!r}"


def test_charts_js_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    r = subprocess.run([node, "--check", str(ASSETS / "charts.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_assets_load_nothing_from_the_network():
    js = (ASSETS / "charts.js").read_text(encoding="utf-8")
    css = (ASSETS / "style.css").read_text(encoding="utf-8")
    remote = [u for u in re.findall(r"https?://[^\s'\"`]+", js) if not u.startswith("http://www.w3.org/")]
    assert not remote, f"charts.js references remote URLs (XML namespaces aside): {remote}"
    assert "import(" not in js and "@import" not in css and "url(http" not in css
    assert "fetch(`data/charts/" in js, "charts fetch their own JSON relative to the page"


def test_entity_colors_defined_for_both_themes():
    css = (ASSETS / "style.css").read_text(encoding="utf-8")
    for ent in ("near", "usd", "third", "context", "positive", "negative"):
        assert len(re.findall(rf"--c-{ent}:\s*#[0-9a-f]{{6}}", css)) >= 3, f"--c-{ent} needs light, OS-dark and toggle-dark"
    assert "prefers-color-scheme: dark" in css and '[data-theme="dark"]' in css
    assert "prefers-reduced-motion" in css


def test_real_helpers_render_the_same_data(tmp_path):
    """The same synthetic data through pipeline/build.py's helpers, when that module imports."""
    try:
        from pipeline import build as b
    except Exception as exc:  # another group may be mid-edit; the stub tests above still ran
        pytest.skip(f"pipeline.build does not import: {exc}")
    data = make_data("mixed")
    for verdict in data["verdicts"].values():
        verdict.setdefault("values", {})
    paths = b.Paths(root=ROOT, out=tmp_path)
    for lint in (False, True):
        try:
            rendered = b.render_all(data, paths, lint=lint)
        except b.BuildError as exc:
            pytest.fail(f"render_all(lint={lint}) failed: {exc}")
        for template, mode, _outs, text in rendered:
            problems = findings(text, mode, digits=lint, braces="any" if lint else "placeholder")
            assert not problems, f"{template} (lint={lint}):\n" + "\n".join(problems[:20])


def card_of(page: str, cid: str) -> str:
    card = page[page.index(f'<figure class="card" id="{cid}"'):]
    return card[:card.index("</figure>")]


@pytest.mark.parametrize("scenario", ["mixed", "degraded"])
def test_status_pill_shows_counts_and_links_to_the_board(scenario):
    pages = render(scenario)
    index, data = pages["index.html.j2"][0], pages["data.html.j2"][0]
    assert '<a class="pill"' not in index, "the source-status pill sits on the data page only"
    start = data.index('<a class="pill"')
    pill = data[start:data.index("</a>", start)]
    assert 'href="data.html#status"' in pill
    keys = (("n_sources_ok", "n_sources_attention", "n_sources_not_configured") if scenario == "mixed"
            else ("n_sources_ok", "n_sources_stale", "n_sources_missing"))
    for k in keys:
        assert f'data-key="{k}"' in pill, f"pill lacks {k}"
    words = html.unescape(re.sub(r"<[^>]+>", "", pill))
    assert "current" in words and re.search(r"\bneeds? attention\b", words) and "not configured" in words
    # one source needs attention, several need it: the verb follows the count the pill prints
    for count, verb in ((1, "needs"), (3, "need")):
        data_ = make_data(scenario)
        data_["numbers"][keys[1]].update(value=count, display=str(count), unit="count")
        page = render_data(data_)["data.html.j2"][0]
        cut = page[page.index('<a class="pill"'):]
        said = html.unescape(re.sub(r"<[^>]+>", "", cut[:cut.index("</a>")]))
        assert re.search(rf"\b{count} {verb} attention\b", said), (count, said)
    assert "Source 1" not in words, "source names belong in the tooltip, not the pill text"
    assert re.search(r'title="[^"]*Source 1: ', pill), "the tooltip names every source and its state"
    assert 'class="dot"' not in index and 'class="dots"' not in index


def test_table_cells_shorten_endpoints_and_check_times():
    card = card_of(render("mixed")["index.html.j2"][0], "c15")
    assert '<a class="endpoint" href="https://www.deribit.com/api/v2/public/get_instruments?currency=any&amp;kind=option"' in card
    assert 'title="https://www.deribit.com/' in card and ">endpoint</a>" in card
    assert ">not an url<" in card, "a cell that is not a URL prints as data"
    assert '<time datetime="2026-11-10T22:37:15+00:00">' in card
    assert '<span class="http"><code class="raw">HTTP 200</code></span>' in card
    assert ">Checked at</th>" in card and ">Endpoint</th>" in card


def test_tripwire_table_names_metrics_in_words():
    index = render("mixed")["index.html.j2"][0]
    start = index.index('<table class="tripwires"')
    table = index[start:index.index("</table>", start)]
    head = table[table.index("<thead>"):table.index("</thead>")]
    assert re.findall(r">([^<]+)</th>", head) == ["Metric", "Now", "Threshold", "State", "What it would mean", "Chart"]
    assert list(TRIPS) == [f"T{i}" for i in range(1, 9)]
    for tid, t in TRIPS.items():
        row = index[index.index(f'id="trip-{tid}"'):]
        row = row[:row.index("</tr>")]
        if t.get("metric_label"):
            assert str(escape(t["metric_label"])) in row, f"{tid} row lacks its metric label"
        assert f'<span class="id">{tid}</span>' in row


def test_clipped_bars_get_a_break_mark_and_keep_their_value():
    """A bar beyond a clamped axis (y.clamp on a bar chart such as c08) is cut with a break mark, its label prints
    the full value and its title names the series; the square end at the edge replaces the rounded one."""
    js = (ASSETS / "charts.js").read_text(encoding="utf-8")
    body = js[js.index("function drawRows("):]
    start = body.index("if (form === 'bar') {")
    bar = body[start:body.index("} else {", start)]
    assert "P.clamped" in bar and "break-mark" in bar and "titled(bar" in bar
    assert "const t = fmt(v, se.unit);" in bar, "the label prints the unclipped value"
    css = (ASSETS / "style.css").read_text(encoding="utf-8")
    assert ".break-mark line" in css


def test_light_aqua_passes_contrast():
    css = (ASSETS / "style.css").read_text(encoding="utf-8")
    root = css[css.index(":root {"):css.index("}", css.index(":root {"))]
    assert "--c-third: #199e70;" in root, "light-mode aqua uses the 3:1 step (#1baf7a measured 2.74:1)"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_no_attribute_value_holds_markup(scenario):
    """copy() output (num() spans with their own quotes) never lands inside an attribute value."""
    for name, (out, _ctx) in render(scenario).items():
        if not name.endswith(".html.j2"):
            continue
        bad = re.findall(r'\s[a-z-]+="[^"]*<span', out)
        assert not bad, (name, bad[:3])


def test_evidence_follows_the_film_order_with_claim_blocks_and_a_ledger():
    """#evidence groups the claims and charts in the film's scene order. Each claim is an article (#claim-<id>) the
    evidence dialog can move, with its verdict chip, its plain test and no rule disclosure; the ledger rows carry
    other ids (ledger-<id>). The c10 card stays with the tripwires. THESIS.md keeps the two survivors' titles."""
    pages = render("mixed")
    index, thesis = pages["index.html.j2"][0], pages["THESIS.md.j2"][0]
    evidence = index[index.index('<section id="evidence"'):index.index('<section id="mind"')]
    groups = re.findall(r'<section class="ev-group" id="ev-([a-z]+)"', evidence)
    assert groups == ["title", "word", "money", "yield", "cost", "ledger"], groups
    placed = {"title": (["H1b"], ["c21"]), "word": (["H1", "H10"], ["c02", "c05", "c06"]),
              "money": (["H2"], ["c11"]), "yield": (["H3", "H5", "H4"], ["c01", "c13", "c12"]),
              "cost": (["H12", "H6", "H6_near", "H9", "H7"], ["c03", "c14", "c07", "c09", "c08"]),
              "ledger": (["H8"], ["c15"])}
    for gid, (claims, charts) in placed.items():
        group = evidence[evidence.index(f'id="ev-{gid}"'):]
        group = group[:group.index("</section>\n  <section class=\"ev-group\"") if gid != "ledger" else None]
        assert re.findall(r'<article class="claim-block" id="claim-([A-Za-z0-9_]+)"', group) == claims, gid
        assert re.findall(r'<figure class="card" id="(c[0-9]+)"', group) == charts, gid
    for cid in ("H1", "H1b", "H2", "H3", "H4", "H5", "H6", "H6_near", "H7", "H8", "H9", "H10", "H12"):
        block = index[index.index(f'<article class="claim-block" id="claim-{cid}"'):]
        block = block[:block.index("</article>")]
        assert 'class="chip vchip"' in block and "<dt>The test</dt>" in block, cid
        assert 'class="verdict"' not in block and "rule and result" not in block, cid
        assert f'<tr id="ledger-{cid}"' in evidence, cid
    assert '<tr id="claim-' not in index
    mind = index[index.index('<section id="mind"'):index.index('<section id="limits"')]
    assert 'id="c10"' in mind
    head = thesis.index("## The thesis that survives")
    part = thesis[head:thesis.index("\n## ", head + 1)]
    stub = Stub(make_data("mixed"), "md", False)
    for title in ("tile_money_title", "tile_rates_title"):
        assert f"**{stub.copy(title)}**" in part, title
