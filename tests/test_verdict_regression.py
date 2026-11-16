"""The verdicts, tripwire states, tile figures and chart text that this repository's data/raw produces.

A verdict change must update the expected file in the same commit.

site/data/ is what the committed data/raw produces (the offline rebuild diff in `make verify` holds it there), so these
comparisons pin each verdict, its deciding rule and margin, the tally, every tripwire state, the Intents and tile
figures, and the resolved text of the charts listed in expected_chart_text.json to the raw data.
"""

from __future__ import annotations

import json

import pytest

from pipeline import config

DATA = config.ROOT / "site" / "data"
FIXTURES = config.ROOT / "tests" / "fixtures"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


EXPECTED_VERDICTS = load(FIXTURES / "expected_verdicts.json")
EXPECTED_TRIPWIRES = load(FIXTURES / "expected_tripwires.json")
EXPECTED_NUMBERS = load(FIXTURES / "expected_numbers.json")
EXPECTED_CHART_TEXT = load(FIXTURES / "expected_chart_text.json")
CLAIM_IDS = sorted(k for k in EXPECTED_VERDICTS if k != "tally")


@pytest.fixture(scope="module")
def verdicts():
    return load(DATA / "verdicts.json")


@pytest.fixture(scope="module")
def numbers():
    return load(DATA / "numbers.json")


@pytest.mark.parametrize("cid", CLAIM_IDS)
def test_verdict_rule_and_margin(verdicts, cid):
    want, got = EXPECTED_VERDICTS[cid], verdicts[cid]
    assert {f: got[f] for f in ("verdict", "binding_condition", "margin")} == want


def test_tally(verdicts, numbers):
    want = dict(EXPECTED_VERDICTS["tally"])
    assert numbers["thinnest_supported_id"]["value"] == want.pop("thinnest_supported_id")
    assert {f: verdicts["tally"][f] for f in want} == want


def test_tripwire_states():
    got = load(DATA / "tripwires.json")
    assert {tid: {"state": t["state"]} for tid, t in got.items()} == EXPECTED_TRIPWIRES


@pytest.mark.parametrize("key", sorted(EXPECTED_NUMBERS["full"]))
def test_number_entry(numbers, key):
    want = EXPECTED_NUMBERS["full"][key]
    assert {f: numbers[key].get(f) for f in want} == want


@pytest.mark.parametrize("key", sorted(EXPECTED_NUMBERS["value_only"]))
def test_number_value(numbers, key):
    want = EXPECTED_NUMBERS["value_only"][key]
    assert {f: numbers[key].get(f) for f in want} == want


@pytest.mark.parametrize("cid", sorted(EXPECTED_CHART_TEXT))
def test_chart_text(cid):
    got = load(DATA / "charts" / f"{cid}.json")
    want = EXPECTED_CHART_TEXT[cid]
    assert {f: got.get(f) for f in want} == want
