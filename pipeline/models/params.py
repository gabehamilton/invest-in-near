"""Author-chosen model parameters, read from the [params] table of thesis/rules.toml.

These are thresholds and scenario inputs the author set (a band, a cut-off, a hypothetical share). They are not
external facts, so they do not live in thesis/constants.toml (which needs a source, a quote and a retrieval date).
Keeping them next to the verdict rules means a rule and the model read one file; tests/test_params.py pins every
parameter that also appears as a literal in a rule expression to that literal, so the two cannot drift apart.

The file is read from this package's own repository (not config.ROOT), so tests that point ROOT at a temporary
tree still get the real parameters. A missing parameter raises at import: the build fails closed.
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path

RULES_PATH = Path(__file__).resolve().parents[2] / "thesis" / "rules.toml"


@lru_cache(maxsize=1)
def params() -> dict:
    with RULES_PATH.open("rb") as fh:
        return dict(tomllib.load(fh).get("params") or {})


def param(name: str) -> float:
    table = params()
    if name not in table:
        raise KeyError(f"thesis/rules.toml [params] has no {name!r}")
    return table[name]
