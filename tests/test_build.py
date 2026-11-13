"""pipeline/build.py on a synthetic repo (fake models, raw files, rules and templates in tmp). Offline."""

from __future__ import annotations

import json
import socket
import sys
from datetime import date
from pathlib import Path

import pytest

from pipeline import build as b
from pipeline import numbers as numbers_mod

sys.path.insert(0, str(Path(__file__).parent))
import _build_fixture as fx  # noqa: E402


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = fx.make_repo(tmp_path, monkeypatch)
    yield r
    fx.forget_package(r.package)


def run_build(repo, **kw):
    return b.build(repo.paths, models=kw.pop("models", repo.models), package=repo.package, **kw)


def outputs(root: Path) -> dict[str, bytes]:
    files = {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / "site").rglob("*") if p.is_file()}
    files["THESIS.md"] = (root / "THESIS.md").read_bytes()
    return files


def load(root: Path, name: str):
    return json.loads((root / "site" / "data" / f"{name}.json").read_text())


# ---------------------------------------------------------------- outputs and verdicts

def test_build_writes_every_output(repo):
    result = run_build(repo)
    root = repo.root
    for name in b.DATA_FILES:
        assert (root / "site" / "data" / f"{name}.json").exists(), name
    assert (root / "site" / "data" / "charts" / "c01.json").exists()
    assert (root / "site" / "data" / "csv" / "c01.csv").read_text().splitlines()[0] == "date,value"
    for rel in ("site/index.html", "site/data.html", "site/THESIS.md", "THESIS.md"):
        assert (root / rel).exists(), rel
    assert (root / "THESIS.md").read_bytes() == (root / "site" / "THESIS.md").read_bytes()
    assert (root / "site" / "llms.txt").read_text().startswith("# "), "llms.txt is built with the site"
    manifest = load(root, "manifest")
    listed = {o["path"] for o in manifest["outputs"]}
    assert {"site/index.html", "site/llms.txt", "THESIS.md", "site/data/numbers.json", "site/data/charts/c01.json"} <= listed
    assert manifest["build"]["git_sha"] == ""
    assert result.manifest == manifest


def test_verdicts_tally_and_registered_counts(repo):
    run_build(repo)
    v = load(repo.root, "verdicts")
    assert {cid: v[cid]["verdict"] for cid in ("H1", "H8", "H9", "H9_reframe", "H10")} == {
        "H1": "Refuted", "H8": "Not testable", "H9": "Partly", "H9_reframe": "Partly", "H10": "Not computed"}
    assert v["H10"]["null_reason"] == "gap: no comparison series in this fixture"
    assert v["H1"]["margin"] == "2.00 pp" and v["H1"]["margin_detail"]["keys"] == ["growth"]
    assert v["H1"]["margin_detail"]["diff"] > 0
    tally = v["tally"]
    assert (tally["supported"], tally["partly"], tally["refuted"], tally["not_testable"],
            tally["not_computed"], tally["claims"]) == (0, 1, 1, 1, 1, 2)
    assert tally["separate"] == ["H9_reframe"]   # a reframe sits beside the count
    n = load(repo.root, "numbers")
    assert n["n_claims"]["value"] == 2 and n["n_claims"]["formula_id"] == "F-tally"
    assert n["n_untestable"]["value"] == 1 and n["n_not_computed"]["value"] == 1
    trip = load(repo.root, "tripwires")["T1"]
    assert trip["state"] == "clear" and trip["metric_label"] == "Supply growth"   # words for the metric key


def test_pages_use_helpers_and_selected_copy(repo):
    run_build(repo)
    index = (repo.root / "site" / "index.html").read_text()
    assert 'Supply grows by <span class="num" data-key="growth"' in index      # first matching variant
    assert ">2.00%</span>" in index
    assert '<q class="claimed">NEAR is deflationary</q>' in index
    assert 'data-verdict="refuted"' in index and 'data-verdict="not-testable"' in index
    assert '<span class="rule">growth above five</span>' in index
    assert '<time datetime="2026-03-10T12:00:00+00:00">2026-03-10 12:00 UTC</time>' in index
    assert '<data value="102.0">102.0</data>' in index
    thesis = (repo.root / "THESIS.md").read_text()
    assert "**Supply grows by 2.00%, slowly.**" in thesis
    assert "- H8: ? Not testable" in thesis


# ---------------------------------------------------------------- determinism and provenance

def test_two_builds_in_two_roots_are_byte_identical(tmp_path, monkeypatch):
    r1 = fx.make_repo(tmp_path, monkeypatch, name="one")
    b.build(r1.paths, models=r1.models, package=r1.package)
    fx.forget_package(r1.package)
    r2 = fx.make_repo(tmp_path, monkeypatch, name="two")
    b.build(r2.paths, models=r2.models, package=r2.package)
    fx.forget_package(r2.package)
    o1, o2 = outputs(r1.root), outputs(r2.root)
    assert o1.keys() == o2.keys()
    assert [k for k in o1 if o1[k] != o2[k]] == []


def test_rebuild_in_place_is_identical_and_clears_old_files(repo):
    run_build(repo)
    first = outputs(repo.root)
    stray = repo.root / "site" / "data" / "charts" / "c99.json"
    stray.write_text("{}")
    run_build(repo)
    assert not stray.exists()
    assert outputs(repo.root) == first


def test_as_of_comes_from_sidecars_and_paths_are_relative(repo, tmp_path):
    result = run_build(repo)
    assert result.as_of == fx.AS_OF
    assert load(repo.root, "manifest")["build"]["as_of"] == fx.AS_OF
    growth = load(repo.root, "numbers")["growth"]
    assert growth["source_files"] == ["data/raw/demo/series.csv"] and growth["as_of"] == fx.AS_OF
    for rel, data in outputs(repo.root).items():
        assert str(tmp_path).encode() not in data, f"absolute path in {rel}"


# ---------------------------------------------------------------- source status and cross-checks

def test_source_states(repo):
    run_build(repo)
    states = {s["id"]: s["state"] for s in load(repo.root, "sources")}
    assert states == {"demo_ok": "ok", "demo_stale": "stale", "demo_lag": "lagging", "demo_failed": "failed",
                      "gated": "not_configured", "intents_explorer": "withheld", "constants": "partial"}
    by_id = {s["id"]: s for s in load(repo.root, "sources")}
    assert by_id["demo_ok"]["lag_days"] == 1 and by_id["demo_ok"]["last_obs"] == "2026-03-09"
    assert by_id["demo_stale"]["lag_days"] == 68
    assert "DEMO_TOKEN" in by_id["gated"]["note"]
    assert by_id["intents_explorer"]["sha256"] is None
    n = load(repo.root, "numbers")
    assert (n["n_sources"]["value"], n["n_sources_ok"]["value"], n["n_sources_stale"]["value"],
            n["n_sources_missing"]["value"]) == (7, 1, 4, 2)
    # the status pill: ok + needs attention + not configured = all sources (withheld and partial need attention)
    assert (n["n_sources_attention"]["value"], n["n_sources_not_configured"]["value"]) == (5, 1)
    assert n["n_sources_ok"]["value"] + n["n_sources_attention"]["value"] + \
        n["n_sources_not_configured"]["value"] == n["n_sources"]["value"]


def test_x4_compares_bis_with_unadjusted_cpi(repo):
    """BIS US CPI is not seasonally adjusted, so X4 reads CPIAUCNS; the adjusted CPIAUCSL would fail here."""
    months = [date(2024 + k // 12, k % 12 + 1, 1) for k in range(24)]
    nsa = {m: 100 * 1.003 ** k for k, m in enumerate(months)}
    fx.raw(repo.root, "fred/CPIAUCNS.csv", "date,value\n" + "".join(f"{m},{v:.6f}\n" for m, v in nsa.items()))
    fx.raw(repo.root, "fred/CPIAUCSL.csv",
           "date,value\n" + "".join(f"{m},{100 * 1.002 ** k:.6f}\n" for k, m in enumerate(months)))
    bis = [(m, (nsa[m] / nsa[date(m.year - 1, m.month, 1)] - 1) * 100) for m in months[12:]]
    fx.raw(repo.root, "bis/cpi_yoy_monthly.csv",
           "period,area,value\n" + "".join(f"{m:%Y-%m},US,{v:.6f}\n" for m, v in bis))
    reg = numbers_mod.Registry()
    out = b._x4(reg, {"months": 12, "max_abs_pp": 0.001}, repo.paths)
    assert out["status"] == "pass" and reg.value("xchk_x4_n_months") == 12
    assert reg.value("xchk_x4_max_abs_pp") < 0.001
    assert any(f.endswith("fred/CPIAUCNS.csv") for f in reg.get("xchk_x4_max_abs_pp").source_files)
    assert not any("CPIAUCSL" in f for f in reg.get("xchk_x4_max_abs_pp").source_files)


def test_text_and_num_print_magnitudes_on_request():
    def n(value, unit, display, **kw):
        return {"value": value, "unit": unit, "display": display, "formula_id": "F", "inputs": [], **kw}
    data = {"rules": {}, "numbers": {"gap": n(-1.0449, "pp", "-1.04 pp"), "gap_dir": n("faster", "text", "faster"),
                                     "sd": n(2.5, "pp", "2.50 pp", signed=False),
                                     "vol": n(3727751510.0, "USD", "$3.73 billion")}}
    h = b.Helpers(data, "md")
    assert h.text("{gap}") == "-1.04 pp" and h.text("{gap:abs} {gap_dir}") == "1.04 pp faster"
    assert h.text("{gap:abs,1}") == "1.0 pp" and h.num("gap", 1, absolute=True) == "1.0 pp"
    assert h.num("sd", 1) == "2.5 pp"                     # unsigned figures stay unsigned when re-rounded
    assert h.text("{vol}") == "$3.73 billion"             # compact in prose
    assert h.datum(3727751510.0, "USD") == "$3,727,751,510"   # exact in tables
    with pytest.raises(b.BuildError, match="spec"):
        h.text("{gap:bogus}")


def _helper_data(numbers: dict) -> dict:
    return {"rules": {}, "numbers": numbers, "verdicts": {}, "tripwires": {}, "crosschecks": {}, "sources": [],
            "charts": {}}


def test_ident_links_to_the_published_page_and_rejects_other_schemes():
    md, html = b.Helpers(_helper_data({}), "md"), b.Helpers(_helper_data({}), "html")
    assert md.ident("c02") == "c02"
    assert md.ident("c02", "https://x.test/site/index.html#") == "[c02](https://x.test/site/index.html#c02)"
    assert md.ident("H3", "site/index.html#claim-") == "[H3](site/index.html#claim-H3)"
    assert str(html.ident("c02", "https://x.test/#")) == '<a class="id" href="https://x.test/#c02">c02</a>'
    with pytest.raises(b.BuildError, match="link"):
        md.ident("c02", "javascript:alert(1)#")


def test_copy_conditions_read_registry_text_keys():
    def n(value, unit="text"):
        return {"value": value, "unit": unit, "display": value, "formula_id": "F", "inputs": []}
    h = b.Helpers(_helper_data({"method": n("sampled"), "missing": n(None), "share": n(1.5, "%")}), "md")
    assert h.condition("method=sampled") and h.condition("method!=exact") and h.condition("method=exact|sampled")
    assert h.condition("missing=n/a")
    with pytest.raises(b.BuildError, match="share"):      # a numeric key is not a state
        h.condition("share=1.5")
    with pytest.raises(b.BuildError, match="nope"):
        h.condition("nope=sampled")


def _result(verdicts: dict, trips: dict, xchk: dict):
    return b.BuildResult(paths=None, as_of=None, numbers={}, verdicts=verdicts, charts={}, sources=[],
                         crosschecks=xchk, tripwires=trips, manifest={})


def test_change_report_lists_verdict_tripwire_and_crosscheck_changes(tmp_path):
    import json
    paths = b.Paths(root=tmp_path, out=tmp_path)
    now = _result({"H1": {"verdict": "Refuted"}, "H9": {"verdict": "Partly"}, "tally": None},
                  {"T1": {"state": "clear"}}, {"X1": {"status": "pass"}, "X3": {"status": "pass"}})
    assert b.previous_site_data(paths) is None
    assert b.change_report(b.previous_site_data(paths), now) == ["no previous build"]
    paths.data.mkdir(parents=True)
    for name, payload in (("verdicts", {"H1": {"verdict": "Refuted"}, "H9": {"verdict": "Partly"}, "tally": None}),
                          ("tripwires", {"T1": {"state": "clear"}}),
                          ("crosschecks", {"X1": {"status": "pass"}, "X3": {"status": "pass"}})):
        (paths.data / f"{name}.json").write_text(json.dumps(payload))
    assert b.change_report(b.previous_site_data(paths), now) == ["none"]
    changed = _result({"H1": {"verdict": "Not computed"}, "H9": {"verdict": "Partly"}, "H2": {"verdict": "Open"},
                       "tally": None},
                      {"T1": {"state": "tripped"}}, {"X1": {"status": "pass"}})
    lines = b.change_report(b.previous_site_data(paths), changed)
    assert "verdict H1: Refuted → Not computed" in lines
    assert "verdict H2: new (Open)" in lines
    assert "tripwire T1: clear → tripped" in lines
    assert "cross-check X3: removed (was pass)" in lines
    assert len(lines) == 4 and not any("H9" in x or "X1:" in x for x in lines)


def test_trip_now_prints_the_now_text_or_the_metric():
    def n(value, unit="%"):
        return {"value": value, "unit": unit, "display": f"{value:.2f}%", "formula_id": "F", "inputs": []}
    data = _helper_data({"a": n(1.5), "b": n(-2.0)})
    data["rules"] = {"tripwires": {"T1": {"metric": "a"}, "T2": {"metric": "a", "now_text": "{a} now, {b} before"}}}
    h = b.Helpers(data, "md")
    assert h.trip_now("T1") == "1.50%" and h.trip_now("T2") == "1.50% now, -2.00% before"
    with pytest.raises(b.BuildError, match="no such tripwire"):
        h.trip_now("T77")


def test_copy_chart_follows_the_chosen_variant_and_checks_the_chart_exists():
    def n(value):
        return {"value": value, "unit": "text", "display": value, "formula_id": "F", "inputs": []}
    data = _helper_data({"mode": n("x")})
    data["charts"] = {"c01": {}, "c02": {}}
    data["copy"] = {"strongest": {"variants": [{"when": ["mode=x"], "text": "t", "chart": "c02"},
                                               {"when": [], "text": "u"}]},
                    "broken": {"variants": [{"when": [], "text": "t", "chart": "c77"}]}}
    h = b.Helpers(data, "md")
    assert h.copy_chart("strongest", "c01") == "c02"
    data["numbers"]["mode"] = n("y")
    assert b.Helpers(data, "md").copy_chart("strongest", "c01") == "c01"
    with pytest.raises(b.BuildError, match="c77"):
        h.copy_chart("broken", "c01")


def test_withheld_explorer_becomes_ok_with_permission(repo):
    sources = repo.root / "thesis" / "sources.toml"
    sources.write_text(sources.read_text().replace("publish = false", "publish = true"))
    run_build(repo)
    states = {s["id"]: s["state"] for s in load(repo.root, "sources")}
    assert states["intents_explorer"] == "ok"
    x6 = load(repo.root, "crosschecks")["X6"]
    assert x6["status"] == "pass" and "xchk_x6_share_pct" in x6["observed"]


def test_crosschecks(repo, capsys):
    run_build(repo)
    x = load(repo.root, "crosschecks")
    assert x["X3"]["status"] == "pass" and x["X3"]["blocks_verify"] is True
    assert x["X3"]["observed"].startswith("xchk_x3_median_abs_pct ")
    assert x["X6"]["status"] == "withheld" and x["X6"]["observed"] is None
    n = load(repo.root, "numbers")
    assert n["xchk_x6_share_pct"]["value"] is None and n["xchk_x6_share_pct"]["source_files"] == []
    assert "X6 (local only, withheld from the site): pass" in capsys.readouterr().err


def test_x7_exact_burn_share_within_the_upper_bound(tmp_path):
    """X7: the every-epoch burn share must sit at or below the upper bound; it reports the gap to the sample."""
    paths = b.Paths(root=tmp_path, out=tmp_path)
    cfg = {"max_excess_pp": 0}

    def reg_with(exact, ub, sampled):
        reg = numbers_mod.Registry()
        for key, v in (("burn_share_365_epochs", exact), ("burn_share_365_ub", ub), ("burn_share_365_sampled", sampled)):
            reg.put(key, v, "%", "F", null_reason=None if v is not None else "not covered")
        return reg

    reg = reg_with(1.70, 2.00, 1.52)
    out = b._x7(reg, cfg, paths)
    assert out["status"] == "pass" and out["reason"] is None
    assert reg.value("xchk_x7_ub_margin_pp") == pytest.approx(0.30)
    assert reg.value("xchk_x7_vs_sampled_pp") == pytest.approx(0.18)
    assert b._x7(reg_with(2.10, 2.00, 1.52), cfg, paths)["status"] == "fail"
    out = b._x7(reg_with(None, 2.00, 1.52), cfg, paths)
    assert out["status"] == "not run" and "not covered" in out["reason"]
    reg = reg_with(1.70, 2.00, None)
    out = b._x7(reg, cfg, paths)
    assert out["status"] == "pass" and reg.value("xchk_x7_vs_sampled_pp") is None and "no sampled" in out["reason"]


def test_blocking_failures_only_for_failed_blocking_checks():
    xs = {"X1": {"blocks_verify": True, "status": "fail", "title": "a", "tolerance_text": "t"},
          "X2": {"blocks_verify": False, "status": "fail"},
          "X3": {"blocks_verify": True, "status": "not run"}}
    assert [m.split(" ")[1] for m in b.blocking_failures(xs)] == ["X1"]


def test_diff_trees_reports_changed_missing_and_extra(tmp_path):
    e, a = tmp_path / "e", tmp_path / "a"
    for root, files in ((e, {"site/x.txt": "1", "site/gone.txt": "g", "THESIS.md": "t"}),
                        (a, {"site/x.txt": "2", "site/new.txt": "n", "THESIS.md": "t"})):
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
    problems = b._diff_trees(e, a, ["site", "THESIS.md"])
    assert any(p.startswith("site/gone.txt: committed but not produced") for p in problems)
    assert any(p.startswith("site/new.txt: produced by the rebuild but not committed") for p in problems)
    assert any(p.startswith("site/x.txt: differs") for p in problems)
    assert not any(p.startswith("THESIS.md") for p in problems)


# ---------------------------------------------------------------- failing closed

def test_model_order_matches_contract():
    # a model reads only keys registered by the models before it; sweep reads every earlier one, and the desk
    # simulation (portfolio) reads sweep's exit-liquidity keys; pay (the policy rates the title compares with staking)
    # reads money's panel and staking's yield, and runs last so it changes nothing the others read
    assert b.MODEL_ORDER == ("supply", "staking", "holders", "macro", "market", "demand", "derivatives", "currencies",
                             "money", "sweep", "portfolio", "pay")


def test_missing_model_is_a_clear_error(repo):
    with pytest.raises(b.BuildError, match=r"missing model module\(s\): .*/gamma\.py.*--allow-missing-models"):
        run_build(repo, models=("alpha", "gamma", "beta"))


def test_allow_missing_models_builds_without_them(repo, capsys):
    run_build(repo, models=("alpha", "gamma", "beta"), allow_missing=True)
    assert "gamma.py" in capsys.readouterr().err
    assert (repo.root / "site" / "index.html").exists()


def test_model_with_broken_import_is_not_treated_as_missing(repo):
    (repo.root.parent / "pkgs" / repo.package / "delta.py").write_text("import a_module_that_does_not_exist\n")
    with pytest.raises(b.BuildError, match="failed to import"):
        run_build(repo, models=("alpha", "delta"), allow_missing=True)


def test_network_is_blocked_while_models_run(repo, monkeypatch):
    original = socket.create_connection
    monkeypatch.setattr(numbers_mod, "FAKE_TRY_NETWORK", True, raising=False)
    with pytest.raises(b.BuildError, match="network access during the build is forbidden"):
        run_build(repo)
    assert socket.create_connection is original


def test_rules_key_no_model_registers_fails_strict_build(repo):
    rules = repo.root / "thesis" / "rules.toml"
    rules.write_text(rules.read_text().replace('metrics = ["vol"]\nsupported = "vol >= 50"',
                                               'metrics = ["vol", "nobody_registers_this"]\nsupported = "vol >= 50"'))
    with pytest.raises(b.BuildError, match="nobody_registers_this"):
        run_build(repo)


def test_copy_without_match_or_fallback_fails_closed(repo):
    copy = repo.root / "thesis" / "copy.toml"
    copy.write_text(copy.read_text().replace('[[headline.variants]]\nwhen = []\ntext = "The ledger."\n', "")
                    .replace('"H1=Refuted", "H9=Supported|Partly"', '"H1=Refuted", "H9=Refuted"'))
    with pytest.raises(b.BuildError, match=r"copy\('headline'\): no variant matches .* fails closed"):
        run_build(repo)


def test_copy_condition_on_unknown_id_is_an_error(repo):
    copy = repo.root / "thesis" / "copy.toml"
    copy.write_text(copy.read_text().replace('when = ["H1=Supported"]', 'when = ["H77=Supported"]'))
    with pytest.raises(b.BuildError, match="H77"):
        run_build(repo)


def test_template_printing_none_fails(repo):
    index = repo.root / "templates" / "index.html.j2"
    index.write_text(index.read_text().replace("</body>", "{{ verdicts['H10'].margin }}</body>"))
    with pytest.raises(b.BuildError, match="printed None"):
        run_build(repo)


def test_num_of_unknown_key_fails(repo):
    index = repo.root / "templates" / "index.html.j2"
    index.write_text(index.read_text().replace("</body>", "{{ num('no_such_key') }}</body>"))
    with pytest.raises(b.BuildError, match="no_such_key"):
        run_build(repo)


def test_missing_template_fails_strict_build(repo):
    (repo.root / "templates" / "data.html.j2").unlink()
    with pytest.raises(b.BuildError, match="data.html.j2 is missing"):
        run_build(repo)


def test_failed_render_leaves_previous_outputs_untouched(repo):
    run_build(repo)
    before = outputs(repo.root)
    edit = repo.root / "templates" / "index.html.j2"
    edit.write_text(edit.read_text().replace("</body>", "{{ num('no_such_key') }}</body>"))
    growth = repo.root / "data" / "raw" / "demo" / "series.csv"
    growth.write_text(growth.read_text().replace("2026-03-09,102", "2026-03-09,150"))
    with pytest.raises(b.BuildError):
        run_build(repo)
    assert outputs(repo.root) == before
