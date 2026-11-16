"""Number lint: no figure reaches a page unless it came from the registry.

    uv run python -m pipeline.lint              # `make lint` (needs site/data from `make charts`)
    uv run python -m pipeline.lint --root DIR   # lint another checkout (make verify lints its temp rebuild)

1. Lint render. Every template is rendered again in lint mode: num()/text() placeholders, rule(), claimed(),
   cite(), tm(), ident(), chip() and datum() print digit-free markers such as «num», and digits are removed
   from every string the templates read from data files (sources, manifest, rules, constants, cross-checks,
   tripwires, model-written reasons and table cells; {key} placeholders are kept so text() still resolves
   them). Left as they are: registry values, chart titles and title/sentence templates, and
   thesis/open_items.toml (prose, like copy.toml), so a figure printed without num()/text() is caught.
   Then tags, attributes, comments, <script>, <style>, <code> and <pre> (Markdown: fenced and inline code)
   are stripped, and any remaining digit, bad token or brace fails with the file, line and context.
2. Sources of prose. Every copy.toml variant (selected or not) and every chart title/sentence template is
   checked for typed digits, bad tokens, malformed braces and placeholders that are not registry keys.
3. Real output. site/index.html, site/data.html, THESIS.md and site/THESIS.md are checked for bad tokens
   and unresolved braces after the same stripping, and the two THESIS.md copies must be identical.

Bad tokens (case-sensitive, whole words): NaN, undefined and Infinity anywhere; None and null where they
stand as a value (the whole text of an element, table cell or list item, or right after ":" or "="), because
both are also ordinary words in the prose that explains the null policy. A None, NaN or Infinity printed by
a template expression is stopped earlier, by the build's Jinja finalize hook. Braces: any brace in the lint
render; in the real output only placeholder-shaped ones ({key}, {{, }}, {%, %}, {#, #}), since quoted source
code in data files may contain braces.
Exit status 1 on any finding.
"""

from __future__ import annotations

import argparse
import html
import re
import string
import sys
from dataclasses import dataclass
from pathlib import Path

from pipeline import config
from pipeline.build import (TEMPLATES, BuildError, Helpers, Paths, load_site_data, load_toml, render_all)
from pipeline.numbers import parse_spec

BAD_ANYWHERE = re.compile(r"\b(NaN|undefined|Infinity)\b")
BAD_VALUE_WORDS = ("None", "null")
BAD_AFTER_DELIM = re.compile(r"[:=]\s*(None|null)\b")
DIGIT_RE = re.compile(r"\d")
ANY_BRACE = re.compile(r"[{}]")
PLACEHOLDER_BRACE = re.compile(r"\{\{|\}\}|\{%|%\}|\{#|#\}|\{[A-Za-z_][A-Za-z0-9_]*(?::[^{}]*)?\}")
SEP = "\x00"   # stands in for a stripped tag or code span, so element and cell boundaries survive
PUNCT = " \t.,;:()[]*_-–·\"'“”"

_BLOCKS = [
    re.compile(r"<!--.*?-->", re.S),
    re.compile(r"<script\b.*?</script\s*>", re.S | re.I),
    re.compile(r"<style\b.*?</style\s*>", re.S | re.I),
    re.compile(r"<pre\b.*?</pre\s*>", re.S | re.I),
    re.compile(r"<code\b.*?</code\s*>", re.S | re.I),
]
_TAG = re.compile(r"<[^>]*>", re.S)
_FENCE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.S | re.M)
_INLINE_CODE = re.compile(r"`[^`\n]*`")


@dataclass(frozen=True)
class Finding:
    where: str
    line: int
    problem: str
    context: str

    def __str__(self) -> str:
        loc = f"{self.where}:{self.line}" if self.line else self.where
        return f"{loc}: {self.problem}\n    {self.context}"


def _blank(match: re.Match) -> str:
    """Replace a stripped region with a separator and its newlines, so line numbers survive."""
    return SEP + "\n" * match.group(0).count("\n")


def visible_text(text: str, mode: str) -> str:
    """The prose a reader sees: tags, attributes, comments, scripts, styles and code replaced by SEP."""
    if mode == "html":
        for pattern in _BLOCKS:
            text = pattern.sub(_blank, text)
        text = _TAG.sub(_blank, text)
        return html.unescape(text)
    text = _BLOCKS[0].sub(_blank, text)          # HTML comments in Markdown
    text = _FENCE.sub(_blank, text)
    return _INLINE_CODE.sub(SEP, text)


def scan(text: str, mode: str, where: str, *, digits: bool, braces: str = "any") -> list[Finding]:
    """Findings in the visible text. braces: "any" (lint render, prose sources) or "placeholder" (real output)."""
    brace_re = ANY_BRACE if braces == "any" else PLACEHOLDER_BRACE
    findings = []
    for i, raw_line in enumerate(visible_text(text, mode).splitlines(), start=1):
        line = raw_line.replace(SEP, " ")
        problems = []
        m = DIGIT_RE.search(line)
        if digits and m:
            problems.append(("digit typed outside the registry", m.start()))
        for m in BAD_ANYWHERE.finditer(line):
            problems.append((f"bad token {m.group(0)!r}", m.start()))
        for m in BAD_AFTER_DELIM.finditer(line):
            problems.append((f"bad token {m.group(1)!r}", m.start(1)))
        if not BAD_AFTER_DELIM.search(line):
            for segment in re.split(r"[\x00|]", raw_line):
                word = segment.strip(PUNCT)
                if word in BAD_VALUE_WORDS:
                    problems.append((f"bad token {word!r}", max(0, line.find(word))))
        m = brace_re.search(line)
        if m:
            problems.append(("unresolved brace", m.start()))
        for problem, pos in problems:
            findings.append(Finding(where, i, problem, _context(line, pos)))
    return findings


def _context(line: str, pos: int, width: int = 60) -> str:
    start = max(0, pos - width)
    snippet = line[start:pos + width].strip()
    return ("…" if start > 0 else "") + re.sub(r"\s+", " ", snippet)


def _literal_and_names(template: str) -> tuple[str, list[str], str | None]:
    """(literal text, placeholder names, parse error) of a {key} template."""
    try:
        parts = list(string.Formatter().parse(template))
    except ValueError as exc:
        return template, [], str(exc)
    literal = "".join(p[0] for p in parts)
    names = [p[1] for p in parts if p[1] is not None]
    for p in parts:
        if p[1] is not None:
            try:
                parse_spec(p[2])
            except ValueError as exc:
                return template, names, str(exc)
    return literal, names, None


def check_prose(template: str, where: str, numbers: dict) -> list[Finding]:
    """A copy.toml / open_items / chart template string: digits, bad tokens, braces, unknown keys."""
    findings = []
    literal, names, error = _literal_and_names(template)
    if error:
        return [Finding(where, 0, f"malformed placeholder ({error})", template[:120])]
    for name in names:
        if not re.fullmatch(r"[a-z][a-z0-9_]*(:\d+)?", name or ""):
            findings.append(Finding(where, 0, f"placeholder {{{name}}} is not a registry key", template[:120]))
        elif name.split(":")[0] not in numbers:
            findings.append(Finding(where, 0, f"placeholder {{{name}}} is not a registered number", template[:120]))
    for f in scan(literal, "md", where, digits=True):
        findings.append(Finding(where, 0, f.problem, f.context))
    return findings


def lint_copy(paths: Paths, data: dict) -> list[Finding]:
    findings: list[Finding] = []
    copy = load_toml(paths.thesis / "copy.toml", required=False)
    if copy is None:
        return [Finding("thesis/copy.toml", 0, "missing", "copy() cannot choose page prose without it")]
    helper = Helpers(data, "md")
    for name, entry in copy.items():
        variants = entry.get("variants") if isinstance(entry, dict) else None
        if not isinstance(variants, list):
            findings.append(Finding(f"thesis/copy.toml [{name}]", 0, "no variants array", str(entry)[:120]))
            continue
        for i, v in enumerate(variants):
            where = f"thesis/copy.toml [{name}] variant {i + 1}"
            findings += check_prose(str(v.get("text", "")), where, data["numbers"])
            for cond in v.get("when", []):
                try:
                    helper.condition(cond)
                except BuildError as exc:
                    findings.append(Finding(where, 0, "bad condition", str(exc)))
    items = (data.get("open_items") or {}).get("items", [])
    for item in items:
        for field in ("title", "text", "resolve"):
            if isinstance(item.get(field), str):
                findings += check_prose(item[field], f"thesis/open_items.toml [{item.get('id')}].{field}",
                                        data["numbers"])
        for link in item.get("links", []) or []:
            findings += check_prose(str(link.get("label", "")), f"thesis/open_items.toml [{item.get('id')}] link",
                                    data["numbers"])
    for term in data.get("glossary") or []:
        for field in ("term", "text"):
            findings += check_prose(str(term.get(field, "")), f"thesis/glossary.toml [{term.get('id')}].{field}",
                                    data["numbers"])
    labels = (data.get("rules") or {}).get("labels") or {}
    for key, label in labels.items():
        findings += check_prose(str(label), f"thesis/rules.toml [labels].{key}", data["numbers"])
    for tid, trip in ((data.get("rules") or {}).get("tripwires") or {}).items():
        if isinstance(trip, dict) and trip.get("now_text"):
            findings += check_prose(str(trip["now_text"]), f"thesis/rules.toml [tripwires.{tid}].now_text",
                                    data["numbers"])
    return findings


def lint_chart_templates(data: dict) -> list[Finding]:
    findings = []
    for cid, chart in sorted(data["charts"].items()):
        for field in ("title_template", "sentence_template", "caveat_template"):
            t = chart.get(field) or ""
            if t:
                findings += check_prose(t, f"site/data/charts/{cid}.json {field}", data["numbers"])
        for i, t in enumerate(chart.get("notes_templates") or []):
            findings += check_prose(t, f"site/data/charts/{cid}.json notes_templates[{i}]", data["numbers"])
    return findings


def lint_render(paths: Paths, data: dict) -> list[Finding]:
    findings = []
    try:
        rendered = render_all(data, paths, lint=True)
    except BuildError as exc:
        return [Finding("templates", 0, "lint render failed", str(exc))]
    for template, mode, _outs, text in rendered:
        findings += scan(text, mode, f"templates/{template} (lint render)", digits=True)
    return findings


def lint_outputs(paths: Paths) -> list[Finding]:
    findings = []
    for _template, mode, outs in TEMPLATES:
        for rel in outs:
            p = paths.out / rel
            if not p.exists():
                findings.append(Finding(rel, 0, "missing output", "run make charts"))
                continue
            findings += scan(p.read_text(encoding="utf-8"), mode, rel, digits=False, braces="placeholder")
    a, b = paths.out / "THESIS.md", paths.out / "site" / "THESIS.md"
    if a.exists() and b.exists() and a.read_bytes() != b.read_bytes():
        findings.append(Finding("site/THESIS.md", 0, "differs from THESIS.md", "run make charts"))
    return findings


def run(root: Path) -> list[Finding]:
    paths = Paths(root=root, out=root)
    try:
        data = load_site_data(paths)
    except BuildError as exc:
        return [Finding("site/data", 0, "cannot lint", str(exc))]
    return lint_render(paths, data) + lint_copy(paths, data) + lint_chart_templates(data) + lint_outputs(paths)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=config.ROOT, help="repository root to lint")
    args = parser.parse_args(argv)
    findings = run(args.root.resolve())
    if findings:
        for f in findings:
            print(f)
        print(f"lint: FAILED ({len(findings)} finding(s))")
        return 1
    print("lint: OK (no stray digits, bad tokens or unresolved braces)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
