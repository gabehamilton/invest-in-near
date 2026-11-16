"""pipeline/lint.py: the number lint on synthetic pages and on a synthetic build. Offline."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from pipeline import build as b
from pipeline import lint

sys.path.insert(0, str(Path(__file__).parent))
import _build_fixture as fx  # noqa: E402


def problems(text: str, mode: str = "html", **kw) -> list[str]:
    kw.setdefault("digits", True)
    return [f.problem for f in lint.scan(text, mode, "t", **kw)]


# ---------------------------------------------------------------- the scanner

def test_stray_digit_in_prose_is_caught():
    assert problems("<p>We checked 16 charts.</p>") == ["digit typed outside the registry"]
    assert problems("We checked 16 charts.", "md") == ["digit typed outside the registry"]


def test_digits_in_allowed_html_contexts_pass():
    page = """<html><head><style>.a{width:10px}</style><script>var x = 3; if (a) { b() }</script></head>
    <body><!-- built 2026 --><p class="c2" data-n="7">Run <code>make charts 2</code>.</p>
    <pre>uv run python -m pipeline.build   # 1 of 3
    </pre><span class="num" data-key="g_365">«num»</span></body></html>"""
    assert problems(page) == []


def test_digits_in_allowed_markdown_contexts_pass():
    md = "Text with `code 123` inline.\n\n```sh\nmake verify  # step 2\n```\n\n~~~\n{json: 1}\n~~~\n"
    assert problems(md, "md") == []


def test_bad_tokens():
    assert problems("<p>Growth was NaN this year.</p>") == ["bad token 'NaN'"]
    assert problems("<p>Infinity</p><p>undefined value</p>") == ["bad token 'Infinity'", "bad token 'undefined'"]
    assert problems("<td>None</td>") == ["bad token 'None'"]
    assert problems("| a | null |", "md") == ["bad token 'null'"]
    assert problems("<p>Fetched: None</p>") == ["bad token 'None'"]
    # ordinary words in prose that explains the null policy are allowed
    assert problems("<p>A null input gives Not computed; None of the venues list options.</p>") == []
    # lowercase nan/none are not the tokens
    assert problems("<p>nonetheless</p>") == []


def test_unresolved_braces():
    assert problems("<p>Supply grew {growth}.</p>") == ["unresolved brace"]
    assert problems("<p>Supply grew {{ g }}.</p>", braces="placeholder") == ["unresolved brace"]
    # real output: quoted source code may contain braces, only placeholder shapes fail
    assert problems("<p>if x { y(1, 10) }</p>", digits=False, braces="placeholder") == []
    assert problems("<p>left {growth} over</p>", digits=False, braces="placeholder") == ["unresolved brace"]


def test_findings_carry_line_and_context():
    f = lint.scan("<p>ok</p>\n<p>\n  there were 12 of them\n</p>", "html", "x.html", digits=True)
    assert len(f) == 1 and f[0].line == 3 and "there were 12 of them" in f[0].context
    assert str(f[0]).startswith("x.html:3: digit typed outside the registry")


def test_prose_sources_check_placeholders():
    numbers = {"g_365": {}}
    assert lint.check_prose("Supply grew {g_365}.", "copy", numbers) == []
    assert [f.problem for f in lint.check_prose("Supply grew {g_366}.", "copy", numbers)] == [
        "placeholder {g_366} is not a registered number"]
    assert [f.problem for f in lint.check_prose("In 2025 supply grew {g_365}.", "copy", numbers)] == [
        "digit typed outside the registry"]
    assert lint.check_prose("broken {g_365", "copy", numbers)[0].problem.startswith("malformed placeholder")


# ---------------------------------------------------------------- lint on a synthetic build

@pytest.fixture
def built(tmp_path, monkeypatch):
    repo = fx.make_repo(tmp_path, monkeypatch)
    b.build(repo.paths, models=repo.models, package=repo.package)
    yield repo
    fx.forget_package(repo.package)


def found(repo) -> list[str]:
    return [f"{f.where}: {f.problem}" for f in lint.run(repo.root)]


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new))


def test_clean_build_passes_lint(built):
    assert found(built) == []


def test_typed_digit_in_template_fails(built):
    edit(built.root / "templates" / "index.html.j2", "<h1>", "<h1>All 16 charts: ")
    assert found(built) == ["templates/index.html.j2 (lint render): digit typed outside the registry"]


def test_number_printed_without_num_fails(built):
    edit(built.root / "templates" / "data.html.j2", "</body>", "<p>{{ numbers.growth.value }}</p></body>")
    assert found(built) == ["templates/data.html.j2 (lint render): digit typed outside the registry"]


def test_number_through_text_and_num_passes(built):
    edit(built.root / "templates" / "data.html.j2", "</body>",
         "<p>{{ text('Growth was {growth} and volatility {vol:1}.') }} {{ num('vol') }}</p></body>")
    assert found(built) == []


def test_nan_and_brace_typed_in_markdown_template_fail(built):
    edit(built.root / "templates" / "THESIS.md.j2", "# Fixture thesis", "# Fixture thesis NaN {growth}")
    assert sorted(found(built)) == ["templates/THESIS.md.j2 (lint render): bad token 'NaN'",
                                    "templates/THESIS.md.j2 (lint render): unresolved brace"]


def test_unselected_copy_variant_with_digit_fails(built):
    edit(built.root / "thesis" / "copy.toml", 'text = "Not rendered by any template."',
         'text = "Seen in 2025 by {no_such_key}."')
    assert sorted(found(built)) == [
        "thesis/copy.toml [unused] variant 1: digit typed outside the registry",
        "thesis/copy.toml [unused] variant 1: placeholder {no_such_key} is not a registered number"]


def test_chart_title_template_with_typed_digit_fails(built):
    path = built.root / "site" / "data" / "charts" / "c01.json"
    chart = json.loads(path.read_text())
    chart["title_template"] = "Supply grew {growth} over 365 days"
    path.write_text(json.dumps(chart))
    assert sorted(found(built)) == [
        "site/data/charts/c01.json title_template: digit typed outside the registry",
        "templates/index.html.j2 (lint render): digit typed outside the registry"]


def test_chart_caveat_and_notes_with_typed_digit_fail(built):
    path = built.root / "site" / "data" / "charts" / "c01.json"
    chart = json.loads(path.read_text())
    chart["caveat_template"] = "Read {growth} over 365 days"
    chart["notes_templates"] = ["Sampled 12 times.", "Fine {growth}."]
    path.write_text(json.dumps(chart))
    got = found(built)
    assert "site/data/charts/c01.json caveat_template: digit typed outside the registry" in got
    assert "site/data/charts/c01.json notes_templates[0]: digit typed outside the registry" in got
    assert not any("notes_templates[1]" in f for f in got)


def test_glossary_and_metric_labels_with_typed_digit_fail(built):
    (built.root / "thesis" / "glossary.toml").write_text(
        '[[terms]]\nid = "pp"\nterm = "pp"\ntext = "A gap of 1 point."\n'
        '[[terms]]\nid = "g"\nterm = "Growth"\ntext = "Growth is {growth} now."\n')
    rules = built.root / "thesis" / "rules.toml"
    rules.write_text(rules.read_text() + '\n[labels]\ngrowth = "growth over 365 days"\n')
    assert sorted(found(built)) == ["thesis/glossary.toml [pp].text: digit typed outside the registry",
                                    "thesis/rules.toml [labels].growth: digit typed outside the registry"]


def test_tripwire_now_text_with_typed_digit_fails(built):
    edit(built.root / "thesis" / "rules.toml", 'meaning = "supply grows too fast"\n',
         'meaning = "supply grows too fast"\nnow_text = "{growth} over 90 days"\n')
    assert "thesis/rules.toml [tripwires.T1].now_text: digit typed outside the registry" in found(built)


def test_real_output_bad_token_fails(built):
    edit(built.root / "site" / "index.html", "</body>", "<table><tr><td>None</td></tr></table></body>")
    assert found(built) == ["site/index.html: bad token 'None'"]


def test_thesis_copies_must_match(built):
    (built.root / "site" / "THESIS.md").write_text((built.root / "THESIS.md").read_text() + "\nextra\n")
    assert found(built) == ["site/THESIS.md: differs from THESIS.md"]


def test_lint_needs_a_build(tmp_path):
    assert [f.problem for f in lint.run(tmp_path)] == ["cannot lint"]


def test_mask_keeps_placeholders_in_data_strings():
    assert b._strip_digits("Rule 2a-7: {nav_365} of {n:1} days") == "Rule a-: {nav_365} of {n:1} days"


def test_model_written_template_from_data_goes_through_text(built):
    path = built.root / "site" / "data" / "charts" / "c01.json"
    chart = json.loads(path.read_text())
    chart["panels"] = [{"id": "p", "row_templates": [{"near": "Within 2a-7 rules on {growth} of days"}]}]
    path.write_text(json.dumps(chart))
    edit(built.root / "templates" / "data.html.j2", "</body>",
         "{% for r in charts.c01.panels[0].row_templates %}<p>{{ text(r.near) }}</p>{% endfor %}</body>")
    assert found(built) == []
