"""Offline tests for pipeline.sources.bis_fx; the fixture is a slice of a real WS_DER_OTC_TOV response
(13 currencies incl. the TO1 total, survey years 2022, 2023 and 2025)."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import config  # noqa: E402
from pipeline.models import load  # noqa: E402
from pipeline.sources import bis_fx  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "bis_fx" / "fx_turnover.csv"
TOP_TEN = ["USD", "EUR", "JPY", "GBP", "CNY", "CHF", "AUD", "CAD", "HKD", "SGD"]


def text() -> str:
    return FIXTURE.read_text()


def test_parse_keeps_only_the_survey_year_values_as_published():
    rows = bis_fx.parse(text())
    assert {r["period"] for r in rows} == {"2025"}
    by = {r["currency"]: r for r in rows}
    assert by["TO1"] == {"currency": "TO1", "period": "2025", "value": "9510240.332703", "obs_status": "A"}
    assert by["USD"]["value"] == "8471519.373276"
    assert [r["currency"] for r in rows] == sorted(by)


def test_parse_other_year():
    rows = bis_fx.parse(text(), "2022")
    assert {r["currency"]: r["value"] for r in rows}["HKD"] == "193176.526829"


def test_nan_value_is_blank_not_dropped():
    lines = text().splitlines(keepends=True)
    changed = [line.replace(",2025,232347.228106,A,", ",2025,NaN,M,") for line in lines]
    assert changed != lines
    rows = {r["currency"]: r for r in bis_fx.parse("".join(changed))}
    assert rows["SGD"]["value"] == "" and rows["SGD"]["obs_status"] == "M"


@pytest.mark.parametrize("old,new", [(",C,3,USD,6,", ",B,3,USD,6,"), (",C,3,USD,6,", ",C,3,USD,3,"),
                                     (",A,USD,TO1,", ",A,USD,EUR,")])
def test_unexpected_dimensions_raise(old, new):
    lines = text().splitlines(keepends=True)
    i = next(i for i, line in enumerate(lines) if ",USD,TO1," in line and ",2025," in line)
    lines[i] = lines[i].replace(old, new, 1)
    with pytest.raises(ValueError):
        bis_fx.parse("".join(lines))


def test_duplicate_missing_total_and_bad_body_raise():
    lines = text().splitlines(keepends=True)
    usd = next(line for line in lines if ",USD,TO1," in line and ",2025," in line)
    with pytest.raises(ValueError, match="two rows"):
        bis_fx.parse(text() + usd)
    with pytest.raises(ValueError, match="TO1"):
        bis_fx.parse("".join(line for line in lines if ",TO1,TO1," not in line))
    with pytest.raises(ValueError, match="lacks columns"):
        bis_fx.parse("<html>blocked</html>\n")
    with pytest.raises(ValueError, match="TO1"):
        bis_fx.parse(text(), "2023")  # every 2023 value is NaN (not a survey year)
    with pytest.raises(ValueError, match="too few"):
        bis_fx.parse("".join(lines[:1] + [line for line in lines if ",USD,TO1," in line or ",TO1,TO1," in line]))


def test_source_url_matches_contract():
    assert bis_fx.source_url() == ("https://stats.bis.org/api/v2/data/dataflow/BIS/WS_DER_OTC_TOV/1.0/"
                                   "A.U.A.B.5J.A.5J.A..TO1.A.A.3.C?format=csv&startPeriod=2025&endPeriod=2025")


@pytest.fixture
def raw_root(tmp_path, monkeypatch):
    for mod in (config, load):
        monkeypatch.setattr(mod, "ROOT", tmp_path)
        monkeypatch.setattr(mod, "RAW_DIR", tmp_path / "data" / "raw")
    return tmp_path / "data" / "raw" / "bis_fx"


def test_pull_writes_file_and_loader_ranks(raw_root, monkeypatch):
    def http_get(url, *, params=None, **_):
        assert url == bis_fx.endpoint() and params == bis_fx.params()
        return SimpleNamespace(content=text().encode(), headers={})
    monkeypatch.setattr(bis_fx, "http_get", http_get)
    bis_fx.pull()
    with (raw_root / "fx_turnover_2025.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 13 and set(rows[0]) == {"currency", "period", "value", "obs_status"}
    meta = json.loads((raw_root / "fx_turnover_2025.meta.json").read_text())
    assert meta["source_url"] == bis_fx.source_url() and meta["units"] == "USD million per day"
    assert meta["currencies"] == 12 and meta["last_observed_date"] == "2025-04-01"
    assert meta["published_cross_check"]["document"].endswith("statistics-rpfx25-fx-annex.pdf")

    data, files = load.bis_fx_turnover()
    assert files == ["data/raw/bis_fx/fx_turnover_2025.csv"]
    assert [r["currency"] for r in data["currencies"][:10]] == TOP_TEN
    assert data["currencies"][10]["currency"] == "INR" and data["currencies"][10]["rank"] == 11
    assert data["total"] == float("9510240.332703")
    assert all(r["currency"] != "TO1" for r in data["currencies"])


def test_currency_codes_are_letters_and_non_currencies_are_kept_but_never_ranked(raw_root, monkeypatch):
    lines = text().splitlines(keepends=True)
    usd = next(line for line in lines if ",USD,TO1," in line and ",2025," in line)
    with pytest.raises(ValueError, match="unexpected currency code"):
        bis_fx.parse(text() + usd.replace(",USD,TO1,", ",U5D,TO1,"))
    gold = usd.replace(",USD,TO1,", ",XAU,TO1,")
    rows = {r["currency"]: r for r in bis_fx.parse(text() + gold)}
    assert "XAU" in rows   # kept as published

    def http_get(url, *, params=None, **_):
        return SimpleNamespace(content=(text() + gold).encode(), headers={})
    monkeypatch.setattr(bis_fx, "http_get", http_get)
    bis_fx.pull()
    meta = json.loads((raw_root / "fx_turnover_2025.meta.json").read_text())
    assert meta["currencies"] == 12 and meta["not_ranked"] == ["XAU"]
    data, _ = load.bis_fx_turnover()
    assert [r["currency"] for r in data["currencies"][:10]] == TOP_TEN
    assert all(r["currency"] != "XAU" for r in data["currencies"])


def test_ranked_panel_codes_have_money_specs():
    from pipeline.sources import money
    for code in TOP_TEN:
        assert code not in bis_fx.NON_CURRENCY and code.lower() in money.CURRENCIES


def test_loader_without_file_is_empty(raw_root):
    assert load.bis_fx_turnover() == ({"total": None, "currencies": []}, [])
