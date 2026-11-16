"""The desk simulation's Python twin, run once per test session from site/data/sim.json.

Both test_portfolio.py and test_sim_parity.py read it: the twin simulates from the payload, not from raw files, so
it starts from the same doubles as site/assets/sim.js (spec section 9).
"""

from __future__ import annotations

import json
import struct
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

from pipeline.models import portfolio as pf

ROOT = Path(__file__).resolve().parents[1]
SIM_JSON = ROOT / "site" / "data" / "sim.json"
SIM_JS = ROOT / "site" / "assets" / "sim.js"


@lru_cache(maxsize=None)
def payload() -> dict | None:
    return json.loads(SIM_JSON.read_text(encoding="utf-8")) if SIM_JSON.exists() else None


def run(P: dict) -> list[dict]:
    """F-sim-path for a payload: both windows from one stream, with each window's period products (F-sim-rebal)."""
    return pf.simulate([(w["a"], w["b"]) for w in P["windows"]], P["paths"], P["horizon"], P["seed"], P["half"],
                       P["rebal_days"])


@lru_cache(maxsize=None)
def twin() -> tuple[dict, list[dict], pf.Inputs]:
    P = payload()
    return P, run(P), pf.inputs_from_payload(P)


SMALL = 2000     # paths of the small twin: a multiple of twice sim_dots, for the tests that bisect


@lru_cache(maxsize=None)
def small() -> tuple[dict, list[dict], pf.Inputs]:
    """The same payload at SMALL paths: the first SMALL years of the full stream (path-major), matched on their own."""
    P = {**payload(), "paths": SMALL}
    return P, run(P), pf.inputs_from_payload(P)


def digest(xs) -> str:
    """sha256 of the doubles, little-endian (what node hashes from a Float64Array)."""
    return sha256(struct.pack(f"<{len(xs)}d", *xs)).hexdigest()


def ctrl(P: dict, **over) -> dict:
    return {**P["default"], **over}


def states(P: dict) -> list[dict]:
    """The default, every grid state (share grid x the three drift presets), and each control off its default; all
    in the default mode (the NEAR part reset, rb = 1), then the held mode (rb = 0) and the reset mode at the largest
    share and with both costs (spec 8.1). A state listed twice runs once."""
    ds = pf.scenario()
    out = [ctrl(P)]
    out += [ctrl(P, w=bp / P["bp"], mu=mu) for bp in ds["share"]["grid_bp"] for mu in ("value", "flat", "thesis")]
    out += [ctrl(P, w=0.0), ctrl(P, w=P["share"]["max_bp"] / P["bp"]), ctrl(P, s=0.0), ctrl(P, s=1.0), ctrl(P, s=0.5),
            ctrl(P, mu="own", own=0.25), ctrl(P, mu="own", own=-0.5), ctrl(P, mu="own", own=P["mu"]["hi"]),
            ctrl(P, mu="own", own=P["mu"]["lo"]), ctrl(P, k=0), ctrl(P, win=1), ctrl(P, venue="all"),
            ctrl(P, p=0.5), ctrl(P, p=0.01), ctrl(P, rt=25), ctrl(P, cu=10),
            ctrl(P, k=0, win=1, venue="all", rt=40, cu=15, mu="value", s=0.6, w=0.08)]
    out += [ctrl(P, rb=0), ctrl(P, rb=0, win=1), ctrl(P, rb=0, rt=25), ctrl(P, rb=0, w=0.08, mu="thesis"),
            ctrl(P, rb=1, w=min(1.0, P["share"]["max_bp"] / P["bp"])), ctrl(P, rb=1, rt=40, cu=15)]
    seen, uniq = set(), []
    for c in out:
        key = json.dumps(c, sort_keys=True)
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    return uniq
