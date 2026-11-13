"""Number formatting: signed / unsigned pp, {key:abs} specs, compact USD in prose and exact USD in tables."""

from __future__ import annotations

import pytest

from pipeline.numbers import MissingNumber, Registry, fill, fmt, parse_spec


def test_usd_is_compact_from_a_million_and_exact_on_request():
    assert fmt(3_727_751_510, "USD") == "$3.73 billion"
    assert fmt(6_410_000, "USD") == "$6.41 million"
    assert fmt(53_698_857, "USD") == "$53.7 million"
    assert fmt(123_400_000, "USD") == "$123 million"
    assert fmt(-2_500_000_000, "USD") == "-$2.50 billion"
    assert fmt(999_999, "USD") == "$999,999"
    assert fmt(3.63, "USD", 2) == "$3.63"
    # tables keep the exact figure
    assert fmt(3_727_751_510, "USD", compact=False) == "$3,727,751,510"


def test_registered_usd_value_stays_exact():
    reg = Registry()
    n = reg.put("vol", 3_727_751_510.4, "USD", "F")
    assert n.value == 3_727_751_510.4 and n.display == "$3.73 billion"


def test_pp_signed_by_default_unsigned_for_distances():
    assert fmt(1.04, "pp") == "+1.04 pp" and fmt(-1.04, "pp") == "-1.04 pp"
    assert fmt(2.57, "pp", signed=False) == "2.57 pp"
    assert fmt(-1.04, "pp", absolute=True) == "1.04 pp"
    assert fmt(-0.001, "pp") == "0.00 pp" and fmt(0.001, "pp") == "0.00 pp"   # never "-0.00" or "+0.00"
    reg = Registry()
    assert reg.put("sd", 2.57, "pp", "F", signed=False).display == "2.57 pp"


def test_fill_specs_abs_and_decimals():
    reg = Registry()
    reg.put("gap", -1.0449, "pp", "F")
    reg.put("rate", 4.1296, "%", "F", decimals=1)
    reg.put("dir", "faster", "text", "F")
    assert fill("{gap}", reg) == "-1.04 pp"
    assert fill("{gap:abs} {dir}", reg) == "1.04 pp faster"
    assert fill("{gap:abs,1}", reg) == "1.0 pp"
    assert fill("{gap:3}", reg) == "-1.045 pp"
    assert fill("{rate:abs}", reg) == "4.1%"   # keeps the registered decimals
    with pytest.raises(MissingNumber):
        fill("{nope:abs}", reg)


def test_parse_spec_rejects_unknown_specs():
    assert parse_spec("") == (None, False) and parse_spec(None) == (None, False)
    assert parse_spec("2") == (2, False) and parse_spec("abs") == (None, True) and parse_spec("abs,1") == (1, True)
    for bad in ("+", "abs2", "x", ".2f"):
        with pytest.raises(ValueError):
            parse_spec(bad)


def test_days_print_one_day_in_the_singular():
    # a run of days such as g_365_neg_run_days
    assert fmt(1, "days") == "1 day"
    assert fmt(1.0, "days") == "1 day"
    assert fmt(7, "days") == "7 days"
    assert fmt(22.3, "days") == "22.3 days"
    assert fmt(1.04, "days") == "1.0 days", "only an exact one day is singular"
    assert fmt(1, "days", decimals=1) == "1.0 days"


def test_seconds_unit():
    # seconds per block, two decimals
    assert fmt(0.6123, "seconds", decimals=2) == "0.61 s"
    assert fmt(2, "seconds") == "2 s"
    reg = Registry()
    reg.put("block_s", 0.6123456, "seconds", "F-blocktime", decimals=2)
    assert reg.get("block_s").display == "0.61 s"
    assert reg.value("block_s") == 0.6123456, "seconds are not rounded at registration"
