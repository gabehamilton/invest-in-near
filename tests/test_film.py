"""The film at the top of index.html, checked on the real site data (site/data and the built site/index.html).

These are the checks the film's direction lists for the page: the thesis comes before any script that is not the
class setter; every shot has a duration and one caption, and every scene names claims and charts that exist on the
page; every figure in the film is a registry figure and the number lint passes; all thirteen claim ids appear with
their verdicts.json word; the runtime and the caption reading speed hold; the turn and the callback fall back to
their neutral variants when the literal reading is not refuted; the desk scene and its skip link exist only with the
simulation's keys; the film's assets stay inside their budgets and load nothing from the network; and the film's
geometry is a pure function of the data, so `make verify` reproduces the page byte for byte.
The desk checks take the simulation's keys away, or null one of them, on a copy of the data; none of it
reaches site/.
"""

from __future__ import annotations

import copy
import gzip
import html
import json
import os
import re
from pathlib import Path

import pytest

from pipeline import build as b
from pipeline import lint

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
ASSETS = SITE / "assets"
PATHS = b.Paths(root=ROOT, out=ROOT)
CLAIMS = ("H1", "H1b", "H2", "H3", "H4", "H5", "H6", "H6_near", "H7", "H8", "H9", "H10", "H12")
SCENES = ["title", "word", "money", "yield", "cost", "ledger"]
MAX_CPS = 17.0          # caption characters per second of its shot: every caption is timed to seventeen at most
RUNTIME = (150.0, 200.0)
SIM_CHARTS = ("c30", "c31", "c32", "c33")
SVG_NS = "http://www.w3.org/2000/svg"
CSS_RULE = re.compile(r"[#.\w][^{}\n]*\{-{0,2}[a-z][a-z-]*:[^{}]*\}")   # a stylesheet a script injects is not prose

SHOT = re.compile(r'<div class="shot[^"]*" id="([^"]+)" data-shot="([^"]+)" data-dur="([^"]+)"[^>]*>')
SCENE = re.compile(r'<section class="scene" id="[^"]+" data-scene="([a-z]+)" data-claims="([^"]*)" '
                   r'data-charts="([^"]*)"')


def page_html() -> str:
    path = SITE / "index.html"
    if not path.exists():
        pytest.skip("site/index.html is not built (run make charts)")
    return path.read_text(encoding="utf-8")


def film_of(page: str) -> str:
    return page[page.index('<div class="film" id="film">'):page.index('<section id="evidence"')]


def visible(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def shots_of(film: str) -> list[tuple[str, float, str]]:
    """(shot id, duration, shot markup) in document order."""
    out = []
    marks = list(SHOT.finditer(film))
    for i, mt in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(film)
        out.append((mt.group(2), float(mt.group(3)), film[mt.start():end]))
    return out


def caption_of(shot: str) -> str:
    caps = re.findall(r'<p class="cap">(.*?)</p>', shot, re.S)
    assert len(caps) == 1, f"a shot holds exactly one caption, found {len(caps)}"
    return visible(caps[0])


def site_data() -> dict:
    return b.load_site_data(PATHS)


def render_index(data: dict) -> str:
    for _template, _mode, outs, text in b.render_all(data, PATHS):
        if "site/index.html" in outs:
            return text
    raise AssertionError("no index page rendered")


# ---------------------------------------------------------------------------------------------- 1 first paint

def test_thesis_precedes_every_script_but_the_class_setter():
    page = page_html()
    thesis = page.index('<h1 class="thesis">')
    scripts = [mt.start() for mt in re.finditer(r"<script\b", page)]
    setters = [s for s in scripts if page[s:page.index("</script>", s)].count("classList.add('js')")]
    assert len(setters) == 1 and setters[0] < thesis, "one inline class setter, in the head"
    others = [s for s in scripts if s not in setters]
    assert others and min(others) > thesis, "every other script comes after the thesis"
    for s in others:
        tag = page[s:page.index(">", s)]
        assert " defer" in tag and "src=" in tag, tag
    assert page.count("<h1") == 1


# ---------------------------------------------------------------------------------------------- 2 contract

def test_every_shot_has_a_duration_and_one_caption_and_scenes_name_what_exists():
    page = page_html()
    film = film_of(page)
    scenes = SCENE.findall(film)
    assert [s[0] for s in scenes][:len(SCENES)] == SCENES
    for name, claims, charts in scenes:
        for cid in claims.split():
            assert f'id="claim-{cid}"' in page, f"{name} names claim {cid}, which has no block on the page"
        for cid in charts.split():
            assert re.search(rf'<figure class="card" id="{cid}"', page), f"{name} names chart {cid}, not on the page"
    shots = shots_of(film)
    assert len({s for s, _d, _m in shots}) == len(shots), "shot ids are unique"
    for sid, dur, markup in shots:
        assert dur > 0, sid
        assert caption_of(markup), f"{sid}: empty caption"
    assert 'data-orbit-slot' in film and film.count('id="orbit"') == 1


# ---------------------------------------------------------------------------------------------- 3 figures

def test_every_film_figure_is_a_registry_number_and_the_lint_passes():
    page = page_html()
    film = film_of(page)
    numbers = json.loads((SITE / "data" / "numbers.json").read_text(encoding="utf-8"))
    keys = re.findall(r'<span class="num[^"]*" data-key="([^"]+)"', film)
    assert keys and all(k in numbers for k in keys)
    data = site_data()
    findings = [f for f in lint.lint_render(PATHS, data) if "index" in f.where]
    assert not findings, "\n".join(map(str, findings[:20]))
    # no key names, formula ids or file paths in the film's visible text (they stay on data.html)
    text = visible(re.sub(r"<(code|span) class=\"(raw|rule)\">.*?</\1>", "", film, flags=re.S))
    assert not re.search(r"\b[a-z]+_[a-z0-9_]+\b", text), "a registry key name is visible in the film"
    assert "F-" not in text and "data/raw" not in film
    assert 'class="verdict"' not in film and '<code class="k">' not in film


# ---------------------------------------------------------------------------------------------- 4 verdicts

def test_all_claim_ids_appear_with_their_verdict_words():
    page = page_html()
    film = film_of(page)
    verdicts = json.loads((SITE / "data" / "verdicts.json").read_text(encoding="utf-8"))
    for cid in CLAIMS:
        word = verdicts[cid]["verdict"]
        marks = re.findall(rf'class="(chip vchip|stamp)"[^>]*data-claim="{cid}" data-verdict="([^"]+)"|'
                           rf'class="(stamp)" data-verdict="([^"]+)" data-claim="{cid}"', film)
        found = [m[1] or m[3] for m in marks]
        assert found, f"{cid} does not appear in the film"
        assert set(found) == {word}, (cid, found, word)
        chip = re.search(rf'data-claim="{cid}" data-verdict="[^"]+">.*?<span class="chip-v">([^<]+)</span>', film,
                         re.S)
        assert chip and chip.group(1) == word, cid
    for word in ("Supported", "Partly", "Refuted", "Not testable"):
        for stamp in re.findall(rf'<div class="stamp" data-verdict="{word}"[^>]*>(.*?)</div>', film, re.S):
            assert '<svg class="glyph"' in stamp and f'<span class="stamp-word">{word}</span>' in stamp


# ---------------------------------------------------------------------------------------------- 5 timing

def test_runtime_and_caption_speed():
    film = film_of(page_html())
    shots = shots_of(film)
    total = sum(d for _s, d, _m in shots)
    assert RUNTIME[0] <= total <= RUNTIME[1], total
    for sid, dur, markup in shots:
        if sid == "d5":       # the held last frame: playback stops, the caption is not timed
            continue
        cps = len(caption_of(markup)) / dur
        assert cps <= MAX_CPS, f"{sid}: {cps:.1f} characters a second"


# ---------------------------------------------------------------------------------------------- 6 variants

def test_turn_and_callback_render_neutral_when_the_word_is_not_refuted():
    data = site_data()
    real = film_of(render_index(data))
    turned = data["verdicts"]["H1"]["verdict"] == "Refuted" and data["verdicts"]["H1b"]["verdict"] == "Supported"
    assert ('data-variant="turn"' in real) == turned
    data = copy.deepcopy(data)
    data["verdicts"]["H1"]["verdict"] = "Supported"
    film = film_of(render_index(data))
    w5 = dict((s, m) for s, _d, m in shots_of(film))["w5"]
    l2 = dict((s, m) for s, _d, m in shots_of(film))["l2"]
    assert 'data-variant="neutral"' in w5 and 'data-variant="neutral"' in l2
    assert "the word fails" not in w5 + l2 and "survive" not in caption_of(l2)
    assert '<li class="survive"' not in l2


# ---------------------------------------------------------------------------------------------- 7 the desk

def without_sim(data: dict) -> dict:
    """The site data with every simulation key and chart taken away, as before the simulation has run."""
    data = copy.deepcopy(data)
    for k in [k for k in data["numbers"] if k.startswith("sim_")]:
        del data["numbers"][k]
    for cid in SIM_CHARTS:          # the simulation's charts come and go with its keys
        data["charts"].pop(cid, None)
    data["film"] = b.film_context(data["numbers"], data["charts"])
    return data


def with_null(data: dict, key: str) -> dict:
    """The site data with one registry figure null, the way the registry records a figure it could not compute."""
    data = copy.deepcopy(data)
    data["numbers"][key] = {**data["numbers"][key], "value": None, "display": "n/a", "status": "null",
                            "null_reason": "test: a missing figure"}
    data["film"] = b.film_context(data["numbers"], data["charts"])
    return data


def test_desk_scene_and_its_skip_link_exist_only_with_the_simulation_keys():
    full = site_data()
    plain_data = without_sim(full)
    page = render_index(plain_data)
    film = film_of(page)
    assert 'data-scene="desk"' not in film and 'href="#desk"' not in page and 'id="sim"' not in page
    for word in ("pending", "not yet"):
        assert word not in visible(film).lower(), word
    total = sum(d for _s, d, _m in shots_of(film))
    assert RUNTIME[0] <= total <= RUNTIME[1], total
    plain = next(text for _t, _m, outs, text in b.render_all(plain_data, PATHS) if "THESIS.md" in outs)
    assert "## Desk simulation" not in plain
    if b.film_desk(full["numbers"], full["charts"]) is None:
        pytest.skip("the simulation's keys are not in site/data (run make sim)")
    desk = film_of(render_index(full))
    assert 'data-scene="desk"' in desk and 'href="#desk"' in desk and 'id="sim"' in desk
    ids = [s for s, _d, _m in shots_of(desk)]
    assert ids[-5:] == ["d1", "d2", "d3", "d4", "d5"]
    total = sum(d for _s, d, _m in shots_of(desk))
    assert RUNTIME[0] <= total <= RUNTIME[1], total
    thesis = next(text for _t, _m, outs, text in b.render_all(full, PATHS) if "THESIS.md" in outs)
    assert "## Desk simulation" in thesis, "THESIS.md gains the desk section with the simulation's keys"
    for key in ("sim_carry_usd", "sim_split_usd", "sim_dots"):   # any figure the frame prints: no scene without it
        assert 'data-scene="desk"' not in film_of(render_index(with_null(full, key))), key
    charts = copy.deepcopy(full)
    charts["charts"]["c30"] = {**charts["charts"]["c30"], "x": {}}
    assert b.film_desk(charts["numbers"], charts["charts"]) is None, "no baked plot, no scene"


def test_desk_plot_is_baked_from_c30_and_sim_js_owns_the_rest():
    full = site_data()
    if b.film_desk(full["numbers"], full["charts"]) is None:
        pytest.skip("the simulation's keys are not in site/data (run make sim)")
    page = page_html()
    sim = page[page.index('<div id="sim"'):page.index("</section>", page.index('<div id="sim"'))]
    lay = full["charts"]["c30"]["x"]["layout"]
    fig = re.search(r'<figure class="sim-fig"[^>]*>(.*?)</figure>', sim, re.S)
    assert fig, "the dot plot sits in figure.sim-fig, which sim.js reuses and redraws"
    dots = re.findall(r"<circle class=\"dot( is-ahead)?\"", fig.group(1))
    assert len(dots) == len(lay["dots"]) == full["numbers"]["sim_dots"]["value"]
    assert sum(1 for d in dots if d) == sum(1 for d in lay["dots"] if d[2]), "filled is ahead, as in c30"
    assert len(re.findall(r'<g class="sim-col"[^>]*data-fx="drop"', fig.group(1))) == len({d[0] for d in lay["dots"]})
    marks = re.findall(r'<span class="sim-lab" data-mark="([a-z0-9]+)"', fig.group(1))
    assert sorted(marks) == sorted(["zero", *lay["marks"]]), marks
    # what sim.js creates itself is in the page only for readers without JavaScript
    bare = re.sub(r"<noscript>.*?</noscript>", "", sim, flags=re.S)
    for cls in ("sim-headline", "sim-hurdle", "sim-left"):
        assert f'class="{cls}' not in bare and f' {cls}"' not in bare, cls
    assert re.search(r'<ul class="sim-tiles">', bare) and 'class="note sim-note"' in bare
    keys = re.findall(r'<li class="sim-tile" data-key="([a-z0-9_]+)"', bare)
    assert keys and all(full["numbers"][k]["value"] is not None for k in keys)


# ---------------------------------------------------------------------------------------------- 8, 9 assets

def kb(path: Path) -> float:
    return path.stat().st_size / 1024


def test_asset_budgets():
    film_js, film_css, sim_js = ASSETS / "film.js", ASSETS / "film.css", ASSETS / "sim.js"
    if not film_js.exists() or not film_css.exists():
        pytest.skip("film.js and film.css are not in site/assets")
    # direction 13.1 item 5 and section 10, raised by the film polish pass (the desk plot's label rows, the short
    # stage's title layout): film.js 60 KB, film.js with sim.js 88 KB, film.css 34 KB. Page JavaScript stays at
    # 60 KB gzip (charts.js, film.js and sim.js). An overrun is met by the cut order (direction section 12), never
    # by a minifier
    assert kb(film_js) <= 60, f"film.js {kb(film_js):.1f} KB"
    assert kb(film_js) + (kb(sim_js) if sim_js.exists() else 0) <= 88
    page_js = [ASSETS / "charts.js", film_js] + ([sim_js] if sim_js.exists() else [])
    zipped = sum(len(gzip.compress(p.read_bytes(), 9)) for p in page_js) / 1024
    assert zipped <= 60, f"page JavaScript {zipped:.1f} KB gzip"
    assert kb(film_css) <= 34, f"film.css {kb(film_css):.1f} KB"
    fonts = sum(kb(p) for p in (ASSETS / "fonts").glob("*.woff2"))
    assert fonts <= 160, f"fonts {fonts:.1f} KB"


def test_film_assets_load_nothing_and_hold_no_figures():
    present = [p for p in (ASSETS / "film.js", ASSETS / "film.css", ASSETS / "sim.js", ASSETS / "sim.css")
               if p.exists()]
    if not present:
        pytest.skip("no film assets in site/assets")
    for p in present:
        text = p.read_text(encoding="utf-8").replace(SVG_NS, "")   # a namespace name, not an address
        assert not re.search(r"https?://", text), f"{p.name} names a network address"
        if p.suffix == ".js":
            code = re.sub(r"/\*.*?\*/|(?<![:\\])//[^\n]*", "", text, flags=re.S)   # comments are not shown
            strings = re.findall(r"'([^'\n]*)'|\"([^\"\n]*)\"|`([^`]*)`", code)
            prose = [s for t in strings for s in t if re.search(r"[A-Za-z]{3,} [a-z]{2,}", s) and re.search(r"\d", s)
                     and not ("${" not in s and CSS_RULE.search(s))]
            assert not prose, f"{p.name}: prose with a figure in a string: {prose[:3]}"


def test_fonts_ship_with_their_licences():
    fonts = ASSETS / "fonts"
    names = sorted(p.name for p in fonts.iterdir())
    for face in ("InstrumentSerif", "MonaSans", "MartianMono"):
        assert any(n.startswith(face) and n.endswith(".woff2") for n in names), face
        licence = fonts / f"OFL-{face}.txt"
        assert licence.exists() and "SIL OPEN FONT LICENSE" in licence.read_text(encoding="utf-8").upper(), face
    data_page = (SITE / "data.html").read_text(encoding="utf-8")
    for face in ("Instrument Serif", "Mona Sans", "Martian Mono"):
        assert face in data_page, f"data.html lists no licence for {face}"


# ---------------------------------------------------------------------------------------------- 11 purity

def test_film_geometry_is_a_pure_function_of_the_data():
    data = site_data()
    first = b.film_context(data["numbers"], data["charts"])
    again = b.film_context(copy.deepcopy(data["numbers"]), copy.deepcopy(data["charts"]))
    assert json.dumps(first, sort_keys=True) == json.dumps(again, sort_keys=True)
    assert len(first["sky"]) == b.SKY["n"]
    orbit = first["orbit"]
    table = data["charts"]["c21"]["table"]
    ix = {c: i for i, c in enumerate(table["columns"])}
    inside = {r[ix["code"]] for r in table["rows"] if isinstance(r[ix["gap_pp"]], (int, float)) and r[ix["gap_pp"]] < 0}
    assert {bd["code"] for bd in orbit["inside"]} == inside, "hollow bodies follow the sign of c21's gap column"
    for body in orbit["bodies"]:
        assert body["label"]["style"].startswith("left:") and "%" in body["label"]["style"]


def test_inside_ring_caption_names_each_hollow_body():
    film = film_of(page_html())
    t3 = caption_of(dict((s, m) for s, _d, m in shots_of(film))["t3"])
    data = site_data()
    for body in data["film"]["orbit"]["inside"]:
        assert body["name"] in t3, body["code"]
    assert "Inside the blue ring: ." not in t3 and not t3.endswith(": .")


def test_radial_edge_cases_negative_growth_and_inside_outside_disagreement():
    """A negative money growth sits at the centre with its figure; a body can be drawn outside the blue ring while its
    matched gap is negative, and it is still hollow (the gap's sign decides, not the pixels)."""
    data = site_data()
    charts = copy.deepcopy(data["charts"])
    table = charts["c21"]["table"]
    ix = {c: i for i, c in enumerate(table["columns"])}
    rows = table["rows"]
    rows[-1][ix["money_growth_latest_pct"]] = -1.5
    rows[-1][ix["gap_pp"]] = -4.0
    rows[-2][ix["money_growth_latest_pct"]] = 7.5
    rows[-2][ix["gap_pp"]] = -0.2
    orbit = b.film_orbit(data["numbers"], charts["c21"])
    by = {bd["code"]: bd for bd in orbit["bodies"]}
    neg, far = by[rows[-1][ix["code"]]], by[rows[-2][ix["code"]]]
    assert neg["negative"] and neg["r"] == "0.0" and neg["valued"] and neg["inside"]
    assert far["inside"] and float(far["r"]) > float(orbit["r_near"])


# ---------------------------------------------------------------------------------------------- 12 what holding pays

PAY_KEYS = ("pay_n_below", "pay_n_with_rate")
# the thesis's earlier wording and the phrase the first caption dropped, assembled so this file never spells them
OLD_THESIS = " ".join(["relative", "to", "the", "dollar", "or", "other", "currency"])
AUTHOR_WORDS = "author" + "'s words"
SKIP_DIRS = {".git", ".venv", ".context", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", "raw"}


def test_policy_rate_keys_exist_with_their_sources():
    numbers = site_data()["numbers"]
    panel = [c for c in ("usd", "eur", "jpy", "gbp", "cny", "chf", "aud", "cad", "hkd", "sgd", "inr", "krw", "mxn",
                         "nzd", "sek", "nok") if numbers.get(f"fx_{c}_rank", {}).get("value") is not None]
    for key in PAY_KEYS:
        n = numbers[key]
        assert n["formula_id"] == "F-pay-count" and n["source_files"] and n["value"] is not None, key
    with_rate = 0
    for c in panel:
        n, month = numbers[f"pol_{c}_latest"], numbers[f"pol_{c}_month"]
        assert n["formula_id"] == month["formula_id"] == "F-policy-latest", c
        if n["value"] is None:
            assert n["null_reason"] and "regime" in n["null_reason"], c
        else:
            assert n["source_files"] and n["inputs"] and month["value"], c
            with_rate += 1
    assert numbers["pay_n_with_rate"]["value"] == with_rate
    y = numbers["y_lst_365"]["value"]
    below = sum(1 for c in panel if (numbers[f"pol_{c}_latest"]["value"] is not None
                                     and numbers[f"pol_{c}_latest"]["value"] < y))
    assert numbers["pay_n_below"]["value"] == below
    assert numbers["pol_usd_latest"]["source_files"] == ["data/raw/fred/DFF.csv"], "the dollar's rate is fed funds"


def test_pay_beat_renders_after_the_money_beat_with_both_radii_baked():
    """The pay beat follows the money beat and emits exactly the hooks film.js reads: the shot is
    div.shot[data-orbit="pay"]; the svg holds data-ring-money, data-ring-pay and data-usd-pay; each body holds
    data-r-money and either data-r-pay (data-inside-pay when its policy rate is below staking) or data-norate; every
    element of one reading carries data-when, never data-mode or the hidden attribute; the pay figures arrive two
    seconds or more into the beat, once the morph has landed."""
    page = page_html()
    film = film_of(page)
    shots = shots_of(film)
    order = [s for s, _d, _m in shots]
    rank = site_data()["film"]["orbit"]["rank"]
    title_order = ["t1", "t2", "t3", "tpay"] + (["trank"] if rank else []) + ["t4"]
    assert order[:len(title_order)] == title_order, order[:len(title_order)]
    tpay_open = re.search(r'<div class="shot[^"]*" id="tpay"[^>]*>', film).group(0)
    assert 'data-orbit="pay"' in tpay_open
    assert len(re.findall(r'<div class="shot[^>]*data-orbit="pay"', film)) == 1
    svg = re.search(r'<svg class="o-svg"[^>]*>', film).group(0)
    for attr in ("data-ring-money", "data-ring-pay", "data-usd-pay"):
        assert f'{attr}="' in svg, attr
    title = film[film.index('data-scene="title"'):film.index('data-scene="word"')]
    for gone in ("data-ring-usd-pay", "data-hollow-pay", "data-mode", "data-wash-pay"):
        assert gone not in title, gone
    data = site_data()
    pay = data["film"]["orbit"]["pay"]
    bodies = re.findall(r'<g class="o-body"([^>]*)>', film)
    assert len(bodies) == len(data["film"]["orbit"]["bodies"])
    y = data["numbers"]["y_lst_365"]["value"]
    for attrs in bodies:
        code = re.search(r'data-code="([A-Z]+)"', attrs).group(1)
        assert "data-r-money=" in attrs, code
        pol = data["numbers"][f"pol_{code.lower()}_latest"]["value"]
        if pol is None:
            assert "data-norate" in attrs and "data-r-pay" not in attrs and "data-inside-pay" not in attrs, code
        else:
            assert "data-r-pay=" in attrs and ("data-inside-pay" in attrs) == (pol < y), code
    assert {bd["code"] for bd in pay["norate"]} == {bd["code"] for bd in data["film"]["orbit"]["bodies"]
                                                    if data["numbers"][f"pol_{bd['code'].lower()}_latest"]["value"] is None}
    # every crossfading element names its reading; the off-stage set is hidden by film.css, never by markup
    orbit = title[title.index('<figure class="orbit"'):title.index("</figure>")]
    for tag in re.findall(r'<(?:span class="o-lab[^"]*"|li data-entity|line class="o-leader"|g class="o-bracket"|'
                          r'g class="o-ladder")[^>]*>', orbit):
        assert re.search(r'data-when="(money|pay|rank)"', tag), tag
        assert " hidden" not in tag and 'display="none"' not in tag, tag
    # the notes under the legend share one line: one paragraph, its text swapped by reading
    norate = re.findall(r'<p class="o-norate"[^>]*>(.*?)</p>', orbit, re.S)
    assert len(norate) <= 1 and not re.search(r'<p class="o-norate" data-when', orbit)
    if pay["norate"]:
        assert norate and re.search(r'<span data-when="pay">', norate[0]), "the pay reading names who has no rate"
    labels = re.findall(r'<span class="o-lab[^"]*" data-when="pay"[^>]*>', orbit)
    assert len(labels) == len(pay["bodies"]) + 1
    assert re.search(r'<figcaption class="o-scale"><span data-when="money">.+?</span><span data-when="pay">', orbit)
    assert len(re.findall(r'<li data-entity="[a-z]+" data-when="pay">', orbit)) == \
        len(re.findall(r'<li data-entity="[a-z]+" data-when="money">', orbit))
    # the pay figures land after the morph (scene clock: the beat starts after t1, t2 and t3)
    start = sum(d for s, d, _m in shots[:3])
    pay_heroes = re.findall(r'<div class="hero is-pay" data-at="([0-9.]+)"', title)
    assert len(pay_heroes) == 2 and all(float(at) >= start + 2.0 - 1e-9 for at in pay_heroes), pay_heroes
    tpay = dict((s, m) for s, _d, m in shots)["tpay"]
    assert "blue ring" in caption_of(tpay)
    for key in ("spread_adj_365", "pay_n_below", "pay_n_with_rate"):
        assert f'data-key="{key}"' in title, key
    for cid in ("H3", "H5"):
        assert f'<a class="chip vchip" href="#claim-{cid}" data-claim="{cid}"' in title, cid
    for body in pay["norate"]:
        assert body["name"] in visible(tpay), body["code"]
    assert "paid in NEAR" in visible(title) or "pays in NEAR" in visible(title), "the clause on what staking pays in"


def test_pay_beat_is_absent_without_the_policy_rates():
    data = site_data()
    numbers = {k: v for k, v in data["numbers"].items() if not k.startswith("pol_")}
    orbit = b.film_orbit(numbers, data["charts"]["c21"])
    assert orbit is not None and orbit["pay"] is None
    stripped = copy.deepcopy(data)
    stripped["numbers"]["pay_n_below"]["value"] = None
    stripped["numbers"]["pay_n_below"]["null_reason"] = "test: no staking yield"
    film = film_of(render_index(stripped))
    assert 'data-shot="tpay"' not in film and "data-ring-pay" not in film


def test_the_thesis_reads_in_its_current_words_everywhere():
    thesis = site_data()["rules"]["meta"]["thesis"]
    assert thesis == "NEAR is deflationary relative to the dollar and other currencies."
    page = page_html()
    assert "relative to the dollar and other currencies." in visible(film_of(page))
    assert "author" not in visible(film_of(page)).lower(), "nothing in the film refers to the author"
    hits = []
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in files:
            path = Path(folder) / name
            if path.suffix in {".woff2", ".png", ".ico", ".pyc"}:
                continue
            try:
                low = path.read_text(encoding="utf-8").lower()
            except (UnicodeDecodeError, OSError):
                continue
            rel = path.relative_to(ROOT)
            if OLD_THESIS in low:
                hits.append(f"{rel}: the earlier thesis wording")
            if AUTHOR_WORDS in low:
                hits.append(f"{rel}: the author's-words caption")
    assert not hits, "\n".join(hits)


# ---------------------------------------------------------------------------------------------- 13 both at once

HOLD_KEYS = ("hold_near_net", "hold_near_rank", "hold_n_ranked")
MONTH = re.compile(r"\d{4}-\d{2}")


def test_the_first_caption_and_the_title_tell_three_figures_in_order():
    """t1 says what the page is; the title then tells the money figure (t3), the rate figure (tpay) and both at once
    (trank), each in words and in the picture, and no verdict word or chip sits beside a figure: the claims the
    title touches are named once, on the tested line."""
    film = film_of(page_html())
    shots = dict((s, m) for s, _d, m in shots_of(film))
    assert caption_of(shots["t1"]) == "A straightforward thesis, tested against public data."
    title = film[film.index('data-scene="title"'):film.index('data-scene="word"')]
    heroes = title[title.index('<div class="heroes">'):title.index('<p class="tested"')]
    assert "chip" not in heroes and "eyebrow" not in heroes, "no verdict chip or eyebrow beside a title figure"
    assert "mixed" not in visible(title).lower()
    tested = re.search(r'<p class="tested"[^>]*>(.*?)</p>', title, re.S).group(1)
    assert re.findall(r'data-claim="([A-Za-z0-9_]+)"', tested) == ["H1b", "H3", "H5"]
    # money, then pay, then rank: the figures arrive in that order on the scene clock
    at = lambda cls: [float(x) for x in re.findall(rf'<div class="hero{cls}" data-at="([0-9.]+)"', title)]  # noqa: E731
    money, pay, rank = at(""), at(" is-pay"), at(" is-rank")
    assert money and pay and max(money) < min(pay)
    if site_data()["film"]["orbit"]["rank"]:
        assert rank and max(pay) < min(rank)


def test_ranking_keys_exist_with_their_formula_and_sources():
    numbers = site_data()["numbers"]
    panel = [c for c in ("usd", "eur", "jpy", "gbp", "cny", "chf", "aud", "cad", "hkd", "sgd", "inr", "krw", "mxn",
                         "nzd", "sek", "nok") if numbers.get(f"fx_{c}_rank", {}).get("value") is not None]
    near = numbers["hold_near_net"]
    assert near["formula_id"] == "F-hold-net" and near["source_files"] and near["value"] is not None
    assert len(near["inputs"]) == 2 and all(MONTH.search(i) for i in near["inputs"]), "the windows are named"
    y, g = numbers["y_lst_365"]["value"], numbers["g_365"]["value"]
    assert near["value"] == pytest.approx(((1 + y / 100) / (1 + g / 100) - 1) * 100, abs=1e-3)
    nets = {}
    for c in panel:
        n = numbers[f"hold_{c}_net"]
        assert n["formula_id"] == "F-hold-net" and n["source_files"], c
        pol, mon = numbers[f"pol_{c}_latest"]["value"], numbers[f"mon_{c}_gm_latest"]["value"]
        if pol is None or mon is None:
            assert n["value"] is None and n["null_reason"], c
            if pol is None:
                assert n["null_reason"].startswith("no policy rate") and "regime" in n["null_reason"], c
            continue
        assert n["value"] == pytest.approx(((1 + pol / 100) / (1 + mon / 100) - 1) * 100, abs=1e-3), c
        assert len(n["inputs"]) == 2 and all(MONTH.search(i) for i in n["inputs"]), c
        nets[c] = n["value"]
    for key in ("hold_near_rank", "hold_n_ranked"):
        assert numbers[key]["formula_id"] == "F-hold-rank" and numbers[key]["source_files"], key
    assert numbers["hold_n_ranked"]["value"] == 1 + len(nets)
    assert numbers["hold_near_rank"]["value"] == 1 + sum(1 for v in nets.values() if v > near["value"])
    # China's and Hong Kong's rates come from their own BIS file, with what each series is
    for c, what in (("cny", "Loan Prime Rate"), ("hkd", "peg")):
        if c in panel and numbers[f"pol_{c}_latest"]["value"] is not None:
            n = numbers[f"pol_{c}_latest"]
            assert n["source_files"] == ["data/raw/bis/policy_rates_monthly_cn_hk.csv"], c
            assert any(what in i for i in n["inputs"]), c


def test_rank_beat_resolves_the_orbit_into_a_ladder():
    """div.shot[data-orbit="rank"] follows the pay beat; every ranked body and the core carry their ladder place;
    the ladder and every rank label carry data-when="rank"; the figures arrive once the move has landed; the text
    names both inputs and the honest clause; a currency without a rate is named as not ranked."""
    data = site_data()
    rank = data["film"]["orbit"]["rank"]
    if not rank:
        pytest.skip("the ranking keys are not in site/data")
    film = film_of(page_html())
    shots = shots_of(film)
    assert len(re.findall(r'<div class="shot[^>]*data-orbit="rank"', film)) == 1
    title = film[film.index('data-scene="title"'):film.index('data-scene="word"')]
    orbit = title[title.index('<figure class="orbit"'):title.index("</figure>")]
    assert re.search(r'<g class="o-ladder" data-when="rank">', orbit)
    assert re.search(r'<g class="o-core" data-rank-x="[0-9.]+" data-rank-y="[0-9.]+"', orbit)
    ranked = {bd["code"] for bd in rank["bodies"]}
    for attrs in re.findall(r'<g class="o-body"([^>]*)>', orbit):
        code = re.search(r'data-code="([A-Z]+)"', attrs).group(1)
        assert ("data-rank-x=" in attrs) == (code in ranked), code
    labels = re.findall(r'<span class="o-lab[^"]*" data-when="rank" data-for="([a-zA-Z]+)"', orbit)
    assert sorted(labels) == sorted([*ranked, "near", "up", "down"])
    for bd in rank["bodies"]:
        assert f'data-key="hold_{bd["code"].lower()}_net"' in orbit, bd["code"]
    # the ladder's two directions are words from the copy, and its tick values are drawn by film.js
    assert re.search(r'data-for="up"[^>]*>your share grows<', orbit) and re.search(r'data-for="down"[^>]*>shrinks<',
                                                                                    orbit)
    assert len(re.findall(r'<line class="o-rt" data-tick="-?[0-9.]+"', orbit)) == len(rank["ticks"])
    # figures land after the move: the rank beat starts after t1, t2, t3 and tpay; the morph takes about two seconds
    start = sum(d for s, d, _m in shots[:4])
    heroes = re.findall(r'<div class="hero is-rank" data-at="([0-9.]+)"', title)
    assert len(heroes) == 2 and all(float(at) >= start + 2.2 - 1e-9 for at in heroes), heroes
    for key in HOLD_KEYS:
        assert f'data-key="{key}"' in title, key
    words = visible(title)
    assert "staking" in words and "supply growth" in words and "policy rate" in words and "money growth" in words
    assert "benchmark" in words and "paid in NEAR" in words, "the honest clause"
    trank = dict((s, m) for s, _d, m in shots)["trank"]
    for bd in rank["unranked"]:
        assert bd["name"] in visible(trank) and bd["code"] in visible(orbit), bd["code"]


def test_rank_beat_is_absent_without_the_ranking_keys():
    data = site_data()
    numbers = {k: v for k, v in data["numbers"].items() if not k.startswith("hold_")}
    orbit = b.film_orbit(numbers, data["charts"]["c21"])
    assert orbit is not None and orbit["rank"] is None and orbit["pay"] is not None
    stripped = copy.deepcopy(data)
    stripped["numbers"]["hold_near_rank"] = {**stripped["numbers"]["hold_near_rank"], "value": None,
                                             "display": "n/a", "status": "null", "null_reason": "test: no rank"}
    film = film_of(render_index(stripped))
    assert 'data-shot="trank"' not in film and 'class="o-ladder"' not in film and 'data-when="rank"' not in film
    assert 'data-shot="tpay"' in film
    total = sum(d for _s, d, _m in shots_of(film))
    assert RUNTIME[0] <= total <= RUNTIME[1], total
