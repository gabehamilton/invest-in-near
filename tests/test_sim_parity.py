"""site/assets/sim.js against its Python twin (spec section 9, tests 1, 2 and the literal allow-list of test 9).

node runs sim.js on the real payload (site/data/sim.json) and on a small synthetic one; the Python twin
(pipeline/models/portfolio.py) runs the same states. The PRNG stream, the moment-matched price factors, the sorted
unit outcomes and every display string must be identical, and the default state must print exactly the registry's
displays. The node tests skip when node is absent, as the other front-end tests do.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline.models import portfolio as pf

sys.path.insert(0, str(Path(__file__).parent))
import _sim_twin as tw  # noqa: E402

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
needs_payload = pytest.mark.skipif(tw.payload() is None or not tw.SIM_JS.exists(), reason="no sim.json or sim.js")

# Runs sim.js's core on one job read from stdin and prints what it computed.
DRIVER = r"""
const S = require(process.argv[1]);
const crypto = require('crypto');
const hash = (a) => crypto.createHash('sha256').update(Buffer.from(a.buffer, a.byteOffset, a.byteLength)).digest('hex');
let raw = '';
process.stdin.on('data', (c) => { raw += c; }).on('end', () => {
  const job = JSON.parse(raw), P = job.payload, out = {};
  out.prng = job.prng.map(([seed, n]) => { const r = S.prng(seed); return Array.from({ length: n }, () => r()); });
  const X = S.makeFmt(P.fmt);
  out.fmt = job.fmt.map(([v, spec, abs]) => X.display(v, spec, abs));
  out.dp = job.dp.map(([v, dp]) => S.roundDp(v, dp, P.fmt.ten));
  out.step = job.step.map(([v, st]) => S.roundStep(v, st));
  const sims = S.simulate(P);
  out.gs = sims.map((s) => hash(s.Gs));
  out.half = sims.map((s) => s.H.reduce((a, b) => a + b, 0));
  out.states = job.states.map((c) => { const O = S.outcomes(sims, P, c); return { d: O.d, s: hash(O.S), dots: Array.from(O.dots) }; });
  out.copy = job.copy.map(([c, name]) => S.parts(P, S.outcomes(sims, P, c), name).join(''));
  process.stdout.write(JSON.stringify(out));
});
"""

FMT_VALUES = [0.0, -0.0, 1.0, -1.0, 0.004, -0.004, 0.005, 0.125, -0.125, 2.5, -2.5, 9.995, 10.0, 99.95, 123.456, 999.5,
              1234.5, 9999.99, 10000.0, 49999.0, 999999.0, 1e6, 1.5e6, 9.995e6, 1.234567e7, 99.95e6, 1.0e9, 2.5e12, -1.75e7]


def fmt_cases(P: dict) -> list:
    """Display specs of every kind the payload uses, on values across signs and magnitudes."""
    specs = {json.dumps(s) for s in P["specs"].values() if not s[0].endswith("*")}
    specs |= {json.dumps(["usd", s]) for s in P["steps"]["usd"]}
    specs |= {json.dumps(["pctstep", s]) for s in P["steps"]["pp"]} | {json.dumps(["countstep", s]) for s in P["steps"]["count"]}
    return [[v, json.loads(s), a] for s in sorted(specs) for v in FMT_VALUES for a in (False, True)]


def node(job: dict) -> dict:
    r = subprocess.run([NODE, "-e", DRIVER, str(tw.SIM_JS)], input=json.dumps(job), capture_output=True, text=True,
                       timeout=600)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout)


def python_side(P: dict, sims: list[dict], inp: pf.Inputs, states: list[dict]) -> list[dict]:
    out = []
    for c in states:
        st = pf.state_of(c, P)
        S, _ = pf.units(sims[st.win], st, inp.rates)
        ev = pf.evaluate(sims, inp, st, P["spread"], pf.cfg_payload(P))
        out.append({"d": pf.outcomes(sims, inp, c, P), "s": tw.digest(S), "dots": ev["dots"]})
    return out


def synthetic(P: dict) -> dict:
    """The real payload with small made-up windows (37 and 11 days) and 400 paths: a second, independent check."""
    Q = copy.deepcopy(P)
    rng = pf.Prng(7)
    for w, n in zip(Q["windows"], (37, 11)):
        a = [1 + (rng.next() / 2**32 - 0.5) / 5 for _ in range(n)]
        b = [1 + (rng.next() / 2**32 - 0.5) / 50 if k % 3 else 1.0 for k in range(n)]
        ma, mb = pf.mean(a), pf.mean(b)
        w["a"], w["b"] = [x / ma for x in a], [x / mb for x in b]
    Q["paths"] = 2 * Q["tail"]["dots"] * 2
    return Q


def compare(P: dict, sims: list[dict], inp: pf.Inputs, got: dict, states: list[dict]) -> None:
    assert got["gs"] == [tw.digest(s["Gs"]) for s in sims]
    assert got["half"] == [sum(s["H"]) for s in sims]
    for c, js, py in zip(states, got["states"], python_side(P, sims, inp, states)):
        assert js["s"] == py["s"], f"sorted outcomes differ for {c}"
        assert js["dots"] == py["dots"], f"dots differ for {c}"
        diff = {k: (js["d"].get(k), v) for k, v in py["d"].items() if js["d"].get(k) != v}
        assert not diff, f"{c}: {diff}"
        assert set(js["d"]) == set(py["d"])


@needs_node
@needs_payload
def test_node_twin_matches_python_on_the_real_payload():
    P, sims, inp = tw.twin()
    states = tw.states(P)
    copy_names = [n for n in P["copy"] if n.startswith("sim_")]
    job = {"payload": P, "prng": [[P["seed"], 16], [1, 16], [0, 4]], "fmt": fmt_cases(P),
           "dp": [[v, dp] for v in FMT_VALUES for dp in (0, 1, 2)], "step": [[v, s] for v in FMT_VALUES for s in (1, 5, 1e4)],
           "states": states, "copy": [[c, n] for c in states[:1] + states[-1:] for n in copy_names]}
    got = node(job)
    # test 1: the known-answer vector, from the published algorithm written out independently below
    assert got["prng"] == [reference_xoshiro(seed, n) for seed, n in job["prng"]]
    assert got["prng"][0] == pf.prng_vector(P["seed"], 16)
    # the display rules, including half-way ties (away from zero) and negative zero
    assert got["fmt"] == [pf.display(v, spec, a) for v, spec, a in job["fmt"]]
    assert got["dp"] == [pf.round_dp(v, dp) for v, dp in job["dp"]]
    assert got["step"] == [pf.round_step(v, s) for v, s in job["step"]]
    # test 2: the simulation, every state, and the default state against the registry
    compare(P, sims, inp, got, states)
    assert {k: got["states"][0]["d"][k] for k in P["expect"]} == P["expect"]
    # the copy: sim.js picks the same variant and fills the same figures as the Python twin
    for (c, name), text in zip(job["copy"], got["copy"]):
        assert "{" not in text and "}" not in text, f"{name}: unfilled placeholder in {text!r}"


@needs_node
@needs_payload
def test_node_twin_matches_python_on_a_synthetic_payload():
    Q = synthetic(tw.payload())
    sims, inp = tw.run(Q), pf.inputs_from_payload(Q)
    states = tw.states(Q)
    got = node({"payload": Q, "prng": [], "fmt": [], "dp": [], "step": [], "states": states, "copy": []})
    compare(Q, sims, inp, got, states)


def reference_xoshiro(seed: int, n: int) -> list[int]:
    """xoshiro128** (Blackman and Vigna) seeded by four splitmix32 outputs, written from the published C code."""
    mask = 0xFFFFFFFF
    x, s = seed & mask, []
    for _ in range(4):
        x = (x + 0x9E3779B9) & mask
        z = x
        z = ((z ^ (z >> 16)) * 0x21F0AAAD) & mask
        z = ((z ^ (z >> 15)) * 0x735A2D97) & mask
        s.append(z ^ (z >> 15))

    def rotl(v: int, k: int) -> int:
        return ((v << k) | (v >> (32 - k))) & mask

    out = []
    for _ in range(n):
        out.append((rotl((s[1] * 5) & mask, 7) * 9) & mask)
        t = (s[1] << 9) & mask
        s[2] ^= s[0]
        s[3] ^= s[1]
        s[1] ^= s[2]
        s[0] ^= s[3]
        s[2] ^= t
        s[3] = rotl(s[3], 11)
    return out


def test_prng_known_answer_in_python():
    seed = pf.params()["sim_seed"]
    assert pf.prng_vector(seed, 16) == reference_xoshiro(seed, 16)
    assert pf.prng_vector(1, 16) == reference_xoshiro(1, 16)


# ------------------------------------------------------------------------------------------- the literal allow-list

def code_numbers(src: str) -> list[str]:
    """Numeric literals in JavaScript code, with comments, strings, template literals and regex literals removed."""
    code, i, n, prev = [], 0, len(src), ""
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            i = src.find("\n", i) if src.find("\n", i) >= 0 else n
            continue
        if src.startswith("/*", i):
            i = src.index("*/", i) + 2
            continue
        if c in "'\"`":
            j = i + 1
            while src[j] != c:
                j += 2 if src[j] == "\\" else 1
            i, prev = j + 1, '"'
            code.append(' "" ')
            continue
        if c == "/" and (prev == "" or prev in "(,=:[!&|?{};+-*%<>~^"):
            j, cls = i + 1, False
            while cls or src[j] != "/":
                if src[j] == "\\":
                    j += 1
                elif src[j] == "[":
                    cls = True
                elif src[j] == "]":
                    cls = False
                j += 1
            j += 1
            while j < n and src[j].isalpha():
                j += 1
            i, prev = j, "/"
            code.append(" /re/ ")
            continue
        code.append(c)
        if not c.isspace():
            prev = c
        i += 1
    return re.findall(r"(?<![\w$.])(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+)", "".join(code))


def test_code_numbers_scanner_sees_through_strings_comments_and_regexes():
    src = "const a = 3; // 4\n/* 5 */ const b = '6' + `7` + \"8\"; const c = /[0-9]{9}/g.test(x) ? 1.5 : y / 2;"
    assert code_numbers(src) == ["3", "1.5", "2"]


@needs_payload
def test_sim_js_types_no_figure():
    """Outside the PRNG's published constants, sim.js's code holds no number but 0, 1 and 2 (spec section 9, test 9):
    every figure, step, scale and geometry comes from the payload."""
    src = tw.SIM_JS.read_text(encoding="utf-8")
    start, end = src.index("/* prng"), src.index("/* /prng */")
    found = code_numbers(src[:start] + src[end:])
    assert set(found) <= {"0", "1", "2"}, sorted(set(found) - {"0", "1", "2"})
    assert "Math.pow" not in src and "Math.exp" not in src and "Math.log" not in src and "**" not in src[end:]
