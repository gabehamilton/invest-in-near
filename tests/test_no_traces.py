"""The repository and its published pages describe the thesis as it stands, and nothing else.

Two word lists, held as hashes so this file never spells them out:
- BANNED_ANY: identifiers and words that are not part of the thesis, or that describe how the work was done. No text
  file in the repository may hold one (the walk skips .git, .venv, .context, data/raw, tests/fixtures, caches and
  binaries; for site/data/csv it reads the header line only). The raw sidecars (data/raw/**/*.meta.json) are read in
  full; the raw data files are not.
- BANNED_PAGES: words about producing the site. The visible text of the pages (and their title, aria-label and alt
  attributes), THESIS.md and llms.txt may not hold one.

Text is lowercased, URLs are dropped, tokens are runs of [a-z0-9], and every run of one to four tokens is hashed
(sha256, first sixteen hex digits). ALLOW lists the (path, hash) pairs that are plain English rather than a leftover;
each carries its reason.

The positive lists below pin what the site, the pipeline and data/raw hold, so nothing outside them can appear under
a new name.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path

from pipeline import config, pull
from pipeline.build import MODEL_ORDER
from pipeline.sources import bigquery, near_rpc

ROOT = config.ROOT
SITE = ROOT / "site"
DATA = SITE / "data"

BANNED_ANY = frozenset({
    "78e61a47f600a0b4", "116a2b57691ab00d", "6665bc2f57c4a704", "e00cb9b69591aa35", "ae4be40cad5250aa",
    "14cc3e8d95ceb60a", "987c74f717c468e7", "7d2857159e309122", "b7212ecf0b0da245", "1f0fab756a0107a5",
    "18544eb38b6456d2", "a8b8c8fe86c77bb6", "a496b88247711041", "72d8d10863238361", "fa03a34ee287432d",
    "d2d4a592700b0920", "f30c534da958a753", "88a49ddf988b599a", "eb3558c58486bae2", "99d1f47bcd2166dc",
    "44adb4a956affa7d", "f19694d36724a9d6", "ece8bb218378a6af", "9cfe61bbc32ece1d", "7f08416e4ef7dfc2",
    "5371a9a19d7ef886", "ca13b71db585fe48", "a79fd74dcca4a383", "5483004893eecb6b", "7d4b5f730e281578",
    "32ee73c79f2be06a", "b54d2688b8e16f8a", "42662d9ce1e4e28f", "f3375291b9d8ed6c", "716ca0cb895efaee",
    "e8417522e5795bfd", "8b75219d3263cc75", "9bdb9881dda78127", "bf7dca50e02fbb93", "9f8376dc7180278d",
    "d309a83f87073d50", "4c99da8f254d687a", "6430ee5fe632946a", "e25eae6e36929e18", "6f5505d3fada6a1f",
    "39dcb70d9ad07bb7", "d70757eee6fc348a", "c97ace4c8fef2cee", "7feaaff7b59a06ea", "b767cb12cf75304d",
    "64c790bed679530e", "730300ba138d1ed3", "973b98d4ef3aac8c", "f6fbcc7773c3a8f6", "ef46a230cfb0c087",
    "fda9f04c2ded0176", "7f7ef9c9a88fd0a9", "c2d6ae581bd7abe7", "240a42374fa81d24", "96a610aee3735ad6",
    "fe372293ac6fc876", "5e4a4501365904b9", "ff62523e04ae48ba", "3c44f43ed3a9baa9", "089ae52fdf81a5ee",
    "63a98318c415bf5b", "fc7fe9c12d74a424", "44bac339ae3a5d63", "4615e7830fdb944d", "a9971e59df72fd08",
    "639ad709729ff4c9", "fcce90d0dbde2a06", "e5baedaa0ebdacd9", "04247a41cafe3e36", "1dfccc0bdd531d96",
    "31d59728fff06361", "c50c7bb5c7f6a0d0", "a499068b8f456a03", "2e3b7023266053bd", "a7520d22f0d5742a",
    "e28d3c99d7149788", "5b3056ee5acbac1a", "6d43da8093b9af4d", "99970e46174cdc6b", "a551bead2df88d02",
    "8b5cc4df7eec7d32", "ac0f09c0f8bf5e7a", "cef7fc13a3818093", "f3e2e400523c5068",
})

BANNED_PAGES = frozenset({
    "1c7797c956a50a57", "62f18b95504b18e0", "8209261ac9b8cd25", "cf44886e54f424ce", "9ae832844d70dd5a",
    "fbb1ddddcb02920c", "6fb42ae1565f4dad", "ed70de070ba6b927", "79a9c6e1a14acc1b", "5b81e1c7326fcf6c",
    "ed9247a897838c27", "f64551fcd6f07823", "3946ca64ff78d93c", "43bb00d0ce7790a5", "ab71fc4c8a1c4d62",
})

_TEMPLATE_SUFFIX = "a499068b8f456a03"
ALLOW = {
    # the Jinja template file suffix (index.html + that suffix), named where templates are loaded or imported
    ("pipeline/build.py", _TEMPLATE_SUFFIX): "template file names",
    ("templates/index.html.j2", _TEMPLATE_SUFFIX): "imports the macro template by file name",
    ("templates/data.html.j2", _TEMPLATE_SUFFIX): "imports the macro template by file name",
    ("templates/_macros.html.j2", _TEMPLATE_SUFFIX): "its usage note names its own file",
    ("templates/_film.html.j2", _TEMPLATE_SUFFIX): "imports the macro template by file name",
    ("tests/test_frontend.py", _TEMPLATE_SUFFIX): "renders the templates by file name",
    ("tests/test_build.py", _TEMPLATE_SUFFIX): "edits the synthetic templates by file name",
    ("tests/test_lint.py", _TEMPLATE_SUFFIX): "edits the synthetic templates by file name",
    ("tests/_build_fixture.py", _TEMPLATE_SUFFIX): "writes the synthetic templates by file name",
    # nearcore's reward source file, cited where the epoch reward formula is reproduced
    ("pipeline/sources/near_rpc.py", "d2d4a592700b0920"): "nearcore reward source file name",
    ("data/raw/rpc/epoch_history.meta.json", "d2d4a592700b0920"): "nearcore reward source file name, as the puller wrote it",
    ("data/raw/rpc/snapshot_now.meta.json", "d2d4a592700b0920"): "nearcore reward source file name, as the puller wrote it",
    # a sidecar key, incremental_rule, followed by its text, which opens with "Change rows"
    ("data/raw/rpc/protocol_changes.meta.json", "ae4be40cad5250aa"): "a sidecar key followed by its text",
    # markup, not prose: the class of the rule span runs into the T2 metric label that opens with a noun
    ("site/index.html", "ae4be40cad5250aa"): "a span class followed by the T2 metric label",
}

SKIP_DIRS = {".git", ".venv", ".context", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}
SKIP_PATHS = {"data/raw", "tests/fixtures"}
BINARY = {".png", ".svg", ".ico", ".woff2", ".xls", ".tgz", ".lock"}
TOKEN = re.compile(r"[a-z0-9]+")
URL = re.compile(r"https?://\S+")


def digest(gram: str) -> str:
    return hashlib.sha256(gram.encode()).hexdigest()[:16]


def hits(lines: list[tuple[int, str]], banned: frozenset[str]) -> list[tuple[int, str]]:
    """(line number, hash) for every run of one to four tokens whose hash is banned; runs cross line breaks."""
    toks = [(t, no) for no, line in lines for t in TOKEN.findall(URL.sub(" ", line.lower()))]
    out = set()
    for i in range(len(toks)):
        for n in range(1, 5):
            if i + n > len(toks):
                break
            h = digest(" ".join(t for t, _ in toks[i:i + n]))
            if h in banned:
                out.add((toks[i][1], h))
    return sorted(out)


def repo_files() -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel = Path(dirpath).relative_to(ROOT)
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_DIRS and (rel / d).as_posix() not in SKIP_PATHS)
        for name in sorted(filenames):
            # local environment files hold credentials and are never committed (.env.example is)
            if name.startswith(".env") and name != ".env.example":
                continue
            if Path(name).suffix in BINARY or name == ".DS_Store":
                continue
            out.append(Path(dirpath) / name)
    return out


def report(rel: str, lines: list[tuple[int, str]], found: list[tuple[int, str]]) -> list[str]:
    text = dict(lines)
    return [f"{rel}:{no}: {text.get(no, '').strip()[:160]}" for no, h in found if (rel, h) not in ALLOW]


def without_the_tables(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """This file without its hash tables (from BANNED_ANY to the end of ALLOW), the one part it may not scan."""
    first = next(no for no, line in lines if line.startswith("BANNED_ANY = "))
    allow = next(no for no, line in lines if line.startswith("ALLOW = "))
    last = next(no for no, line in lines if no > allow and line == "}")
    return [(no, line) for no, line in lines if not first <= no <= last]


def test_no_text_file_holds_a_banned_term():
    problems = []
    for path in repo_files() + sorted((ROOT / "data" / "raw").glob("**/*.meta.json")):
        rel = path.relative_to(ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        lines = list(enumerate(text.splitlines(), 1))
        if rel.startswith("site/data/csv/") and rel.endswith(".csv"):
            lines = lines[:1]
        if path == Path(__file__).resolve():
            lines = without_the_tables(lines)
        problems += report(rel, lines, hits(lines, BANNED_ANY))
    assert not problems, "\n".join(problems[:60])


def test_every_allowed_pair_is_still_needed():
    """An ALLOW entry whose file no longer holds the term must go, so the list never outgrows the repository."""
    stale = []
    for rel, h in ALLOW:
        path = ROOT / rel
        lines = list(enumerate(path.read_text(encoding="utf-8").splitlines(), 1)) if path.exists() else []
        if h not in {x for _, x in hits(lines, BANNED_ANY)}:
            stale.append(rel)
    assert not stale, stale


class _Visible(HTMLParser):
    """Visible text plus title, aria-label and alt attributes, with line numbers; script and style are skipped."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[tuple[int, str]] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        for key, value in attrs:
            if key in ("title", "aria-label", "alt") and value:
                self.out.append((self.getpos()[0], value))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.out.append((self.getpos()[0], data))


def test_pages_use_no_production_vocabulary():
    problems = []
    for rel in ("site/index.html", "site/data.html"):
        parser = _Visible()
        parser.feed((ROOT / rel).read_text(encoding="utf-8"))
        problems += report(rel, parser.out, hits(parser.out, BANNED_PAGES))
    for rel in ("site/THESIS.md", "THESIS.md", "site/llms.txt"):
        lines = list(enumerate((ROOT / rel).read_text(encoding="utf-8").splitlines(), 1))
        problems += report(rel, lines, hits(lines, BANNED_PAGES))
    assert not problems, "\n".join(problems[:60])


# --------------------------------------------------------------------------- positive lists

CLAIMS = ("H1", "H1b", "H2", "H3", "H4", "H5", "H6", "H7", "H8", "H9", "H10", "H12", "H6_near")
CHARTS = ("c01", "c02", "c03", "c05", "c06", "c07", "c08", "c09", "c10", "c11", "c12", "c13", "c14", "c15", "c16",
          "c21")
SIM_CHARTS = ("c30", "c31", "c32", "c33")   # the desk simulation's charts: all of them or none
VERDICT_FIELDS = {"binding_condition", "chart_ids", "id", "kind", "margin", "margin_detail", "margin_key",
                  "metric_keys", "null_reason", "reason", "rule_text", "section", "title", "trigger", "unregistered",
                  "values", "verdict"}
NUMBER_FIELDS = {"as_of", "decimals", "display", "formula_id", "inputs", "key", "null_reason", "signed",
                 "source_files", "status", "unit", "value"}


def site_json(name: str):
    return json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))


def test_verdicts_are_the_claims_with_their_fields():
    verdicts = site_json("verdicts")
    assert set(verdicts) == {*CLAIMS, "tally"}
    for cid in CLAIMS:
        assert set(verdicts[cid]) == VERDICT_FIELDS, cid
    tally = verdicts["tally"]
    assert sorted(tally["counted"] + tally["separate"]) == sorted(CLAIMS)


def test_tripwires_and_crosschecks_are_the_listed_ids():
    assert sorted(site_json("tripwires")) == [f"T{i}" for i in range(1, 9)]
    assert sorted(site_json("crosschecks")) == [f"X{i}" for i in (1, 2, 3, 4, 5, 6, 7, 9)]


def test_charts_are_the_listed_ids_and_name_only_the_claims():
    manifest = site_json("manifest")
    ids = sorted(c["id"] for c in manifest["charts"])
    charts = list(CHARTS) + (list(SIM_CHARTS) if set(SIM_CHARTS) & set(ids) else [])
    assert ids == sorted(charts)
    for c in manifest["charts"]:
        assert set(c["claims"]) <= set(CLAIMS), c["id"]
    assert sorted(p.stem for p in (DATA / "charts").glob("*.json")) == sorted(charts)
    assert sorted(p.stem for p in (DATA / "csv").glob("*.csv")) == sorted(charts)
    for cid in charts:
        spec = json.loads((DATA / "charts" / f"{cid}.json").read_text(encoding="utf-8"))
        assert set(spec.get("claims", [])) <= set(CLAIMS) and set(spec.get("verdicts", [])) <= set(CLAIMS), cid


def test_numbers_carry_only_the_registry_fields():
    for key, n in site_json("numbers").items():
        assert set(n) <= NUMBER_FIELDS, (key, sorted(set(n) - NUMBER_FIELDS))


def test_data_page_sections():
    page = (SITE / "data.html").read_text(encoding="utf-8")
    assert re.findall(r'<section[^>]*\bid="([^"]+)"', page) == [
        "status", "sources", "formulas", "numbers", "rules", "xchk", "gaps", "licences", "reconciliation"]
    assert page.count('id="constants"') == 1


FONTS = ["InstrumentSerif-Italic.woff2", "MartianMono-var-latin.woff2", "MonaSans-var-latin.woff2",
         "OFL-InstrumentSerif.txt", "OFL-MartianMono.txt", "OFL-MonaSans.txt"]


def test_site_assets():
    """The page's assets: the chart renderer, the film's player and styles, the desk simulation once it ships, and
    the three self-hosted fonts with their licences."""
    names = sorted(p.name for p in (SITE / "assets").iterdir() if not p.name.startswith("."))
    base = ["charts.js", "favicon.svg", "film.css", "film.js", "fonts", "style.css"]
    assert names in (base, sorted(base + ["sim.js"]), sorted(base + ["sim.css", "sim.js"])), names
    assert sorted(p.name for p in (SITE / "assets" / "fonts").iterdir() if not p.name.startswith(".")) == FONTS


def test_model_modules_are_the_build_order_and_its_helpers():
    modules = {p.stem for p in (ROOT / "pipeline" / "models").glob("*.py")}
    assert modules == set(MODEL_ORDER) | {"formulas", "load", "params", "verdicts", "__init__"}


def test_pullers_queries_and_sql():
    assert sorted(pull.SOURCES) == sorted(["fred", "bis", "bis_fx", "money", "prices", "derivatives", "near_rpc",
                                           "defillama", "intents", "bigquery"])
    assert {q.name for q in bigquery.QUERIES} == {"daily_supply_flows", "fee_burn"}
    assert near_rpc.DENSE_PARTS == ("changes", "epoch_history", "dense")
    assert sorted(p.stem for p in (ROOT / "sql").glob("*.sql")) == [
        "daily_supply_flows", "fee_burn", "freshness"]


def test_raw_layout():
    raw = ROOT / "data" / "raw"
    if not raw.is_dir():
        return
    assert {p.name for p in raw.iterdir() if p.is_dir()} <= {
        "bigquery", "bis", "bis_fx", "fred", "intents", "market", "money", "rpc"}
    money = raw / "money"
    for folder in (p for p in money.iterdir() if p.is_dir()) if money.is_dir() else ():
        stems = {p.name.split(".")[0] for p in folder.iterdir() if p.is_file()}
        assert stems <= {"money", "money_alt", "money_growth_published", "monagg"}, (folder.name, stems)
        assert {p.name for p in folder.iterdir() if p.is_dir()} <= {"_raw"}, folder.name
    bq = raw / "bigquery"
    if bq.is_dir():
        assert {p.stem for p in bq.glob("*.csv")} == {
            "daily_supply_flows", "fee_burn_sampled", "freshness"}
        chunks = bq / "_chunks"
        if chunks.is_dir():
            assert {p.name for p in chunks.iterdir() if p.is_dir()} <= {q.name for q in bigquery.QUERIES}


def test_every_manifest_raw_input_exists():
    missing = [r["path"] for r in site_json("manifest")["raw_inputs"] if not (ROOT / r["path"]).exists()]
    assert not missing, missing
