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
    """F-sim-path for a payload: both windows from one stream."""
    return pf.simulate([(w["a"], w["b"]) for w in P["windows"]], P["paths"], P["horizon"], P["seed"], P["half"])


@lru_cache(maxsize=None)
def twin() -> tuple[dict, list[dict], pf.Inputs]:
    P = payload()
    return P, run(P), pf.inputs_from_payload(P)


def digest(xs) -> str:
    """sha256 of the doubles, little-endian (what node hashes from a Float64Array)."""
    return sha256(struct.pack(f"<{len(xs)}d", *xs)).hexdigest()


def ctrl(P: dict, **over) -> dict:
    return {**P["default"], **over}


def states(P: dict) -> list[dict]:
    """The default, every grid state (share grid x the three drift presets), and each control off its default."""
    ds = pf.scenario()
    out = [ctrl(P)]
    out += [ctrl(P, w=bp / P["bp"], mu=mu) for bp in ds["share"]["grid_bp"] for mu in ("value", "flat", "thesis")]
    out += [ctrl(P, w=0.0), ctrl(P, w=P["share"]["max_bp"] / P["bp"]), ctrl(P, s=0.0), ctrl(P, s=1.0), ctrl(P, s=0.5),
            ctrl(P, mu="own", own=0.25), ctrl(P, mu="own", own=-0.5), ctrl(P, mu="own", own=P["mu"]["hi"]),
            ctrl(P, mu="own", own=P["mu"]["lo"]), ctrl(P, k=0), ctrl(P, win=1), ctrl(P, venue="all"),
            ctrl(P, p=0.5), ctrl(P, p=0.01), ctrl(P, rt=25), ctrl(P, cu=10),
            ctrl(P, k=0, win=1, venue="all", rt=40, cu=15, mu="value", s=0.6, w=0.08)]
    return out
