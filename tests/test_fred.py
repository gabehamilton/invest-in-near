"""Offline tests for pipeline.sources.fred; fixtures are small real fredgraph.csv slices (cosd/coed)."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import config  # noqa: E402
from pipeline.sources import fred  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "fred"


def fixture(series_id: str) -> str:
    return (FIXTURES / f"{series_id}.csv").read_text()


def test_blank_month_is_kept_as_empty():
    rows = fred.parse_series(fixture("CPIAUCSL"), "CPIAUCSL")
    assert [row["date"] for row in rows] == ["2025-08-01", "2025-09-01", "2025-10-01", "2025-11-01", "2025-12-01"]
    assert rows[2] == {"date": "2025-10-01", "value": ""}
    assert rows[1]["value"] == "324.245"


def test_holiday_blanks_and_dot_marker_become_empty():
    rows = fred.parse_series(fixture("EFFR"), "EFFR")
    assert {row["date"] for row in rows if row["value"] == ""} == {"2025-12-25", "2026-01-01"}
    dotted = fred.parse_series("observation_date,DFF\n2026-01-02,3.5\n2026-01-01,.\n", "DFF")
    assert dotted == [{"date": "2026-01-01", "value": ""}, {"date": "2026-01-02", "value": "3.5"}]


def test_values_are_kept_as_published_and_exponents_made_plain():
    assert fred.plain_decimal("21.480") == "21.480"
    assert fred.plain_decimal("1.5E-2") == "0.015"
    for bad in ("NaN", "n/a", "inf"):
        with pytest.raises(ValueError):
            fred.plain_decimal(bad)


@pytest.mark.parametrize("body", [
    "observation_date,EFFR\n2026-01-02,3.5\n",
    "<html>Not found</html>\n",
    "observation_date,DFF\n",
    "observation_date,DFF\n2026-01-02,3.5\n2026-01-02,3.6\n",
    "observation_date,DFF\n01/02/2026,3.5\n",
    "observation_date,DFF\n2026-01-02,3.5,x\n",
])
def test_malformed_bodies_raise(body):
    with pytest.raises(ValueError):
        fred.parse_series(body, "DFF")


def test_summary_counts_blanks_and_scheduled_rows():
    rows = fred.parse_series(fixture("EFFR"), "EFFR")
    summary = fred.summarize(rows, fetch_day="2025-12-31")
    assert summary == {"first_date": "2025-12-23", "last_date": "2026-01-02", "last_observed_date": "2026-01-02",
                       "blank_rows": 2, "rows_after_fetch_date": 2}


@pytest.fixture
def raw_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "data" / "raw")
    monkeypatch.setattr(fred, "PAUSE_SECONDS", 0)
    return tmp_path / "data" / "raw" / "fred"


def fake_http_get(bodies: dict[str, str]):
    def http_get(url, *, params=None, **_):
        series_id = params["id"]
        if series_id not in bodies:
            raise requests.HTTPError(f"503 for {series_id}")
        return SimpleNamespace(content=bodies[series_id].encode(), headers={"Last-Modified": "fixture"})
    return http_get


def test_pull_writes_every_series_with_meta(raw_root, monkeypatch):
    bodies = {sid: fixture("CPIAUCSL").replace("CPIAUCSL", sid) for sid in fred.SERIES}
    monkeypatch.setattr(fred, "http_get", fake_http_get(bodies))
    fred.pull()
    assert sorted(p.stem for p in raw_root.glob("*.csv")) == sorted(fred.SERIES)
    with (raw_root / "CPIAUCSL.csv").open(newline="") as fh:
        assert list(csv.reader(fh))[:4] == [["date", "value"], ["2025-08-01", "323.291"],
                                            ["2025-09-01", "324.245"], ["2025-10-01", ""]]
    meta = json.loads((raw_root / "CPIAUCSL.meta.json").read_text())
    assert meta["source_url"] == "https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL"
    assert (meta["rows"], meta["blank_rows"], meta["file"]) == (5, 1, "data/raw/fred/CPIAUCSL.csv")
    assert meta["description"] and meta["refresh"] and meta["http_last_modified"] == "fixture"


def test_pull_keeps_partial_progress_and_cached_file_on_failure(raw_root, monkeypatch):
    raw_root.mkdir(parents=True)
    (raw_root / "DFF.csv").write_text("cached copy\n")
    bodies = {sid: fixture("CPIAUCSL").replace("CPIAUCSL", sid) for sid in fred.SERIES if sid != "DFF"}
    monkeypatch.setattr(fred, "http_get", fake_http_get(bodies))
    with pytest.raises(RuntimeError, match=rf"1 of {len(fred.SERIES)} FRED series failed.*DFF"):
        fred.pull()
    assert (raw_root / "DFF.csv").read_text() == "cached copy\n"
    assert (raw_root / "M2SL.csv").exists()
