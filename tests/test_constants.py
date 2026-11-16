"""thesis/constants.toml: every declared constant carries its provenance (a source, a verbatim quote and a retrieval
date), as the file's header promises."""

from __future__ import annotations

import re
import tomllib
from datetime import date

import pytest

from pipeline import config

CONSTANTS = tomllib.loads((config.ROOT / "thesis" / "constants.toml").read_text())


def test_the_file_declares_constants():
    assert CONSTANTS, "thesis/constants.toml declares at least one constant"


@pytest.mark.parametrize("name", sorted(CONSTANTS))
def test_each_constant_has_a_source_a_quote_and_a_retrieval_date(name):
    c = CONSTANTS[name]
    assert c.get("unit"), f"{name}: a unit"
    if c.get("kind") == "author scenario":
        # a value the author chose: the quote is the author's request, there is no outside source to cite
        assert "source" not in c, f"{name}: an author scenario cites no outside source"
    else:
        assert re.match(r"https://\S+$", c.get("source", "")), f"{name}: a source URL"
    assert c.get("quote", "").strip(), f"{name}: a verbatim quote"
    assert date.fromisoformat(c["retrieved"]) <= date.today(), f"{name}: a retrieval date"
    for key in c:
        if key.startswith("source_"):
            assert c.get("quote_" + key.removeprefix("source_")), f"{name}: {key} has its own quote"


SCENARIOS = sorted(n for n, c in CONSTANTS.items() if c.get("kind") == "author scenario")


@pytest.mark.parametrize("name", SCENARIOS)
def test_each_author_scenario_part_has_a_unit_a_value_and_a_note(name):
    parts = {k: v for k, v in CONSTANTS[name].items() if isinstance(v, dict)}
    assert parts, f"{name}: an author scenario declares its parts as sub-tables"
    for part, c in parts.items():
        assert c.get("unit"), f"{name}.{part}: a unit"
        assert c.get("note", "").strip(), f"{name}.{part}: a note saying what the value is for"
        assert any(k not in ("unit", "note") for k in c), f"{name}.{part}: a value"
