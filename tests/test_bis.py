"""Offline tests for pipeline.sources.bis; fixtures are small real SDMX CSV slices (startPeriod/endPeriod)."""

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
from pipeline.sources import bis  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "bis"
POLICY, CPI, POLICY_CN_HK = bis.FLOWS


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.csv").read_text()


def test_policy_rows_sorted_by_period_then_area():
    rows, dropped = bis.to_rows(bis.read_records(fixture("policy_rates_monthly")), POLICY)
    assert dropped == []
    assert [(row["period"], row["area"]) for row in rows[:4]] == [
        ("2025-05", "AR"), ("2025-05", "US"), ("2025-06", "AR"), ("2025-06", "US")]
    assert {"area": "US", "period": "2025-08", "value": "4.375"} in rows
    assert not any(row["area"] == "AR" and row["period"] > "2025-06" for row in rows)


def test_nan_rows_are_dropped_and_reported():
    rows, dropped = bis.to_rows(bis.read_records(fixture("cpi_yoy_monthly")), CPI)
    assert dropped == ["US 2025-10"]
    assert ("US", "2025-10") not in {(row["area"], row["period"]) for row in rows}
    assert {"area": "US", "period": "2025-11", "value": "2.735084"} in rows
    assert len(rows) == 5


def test_attributes_keep_multiline_compilation_and_title():
    attributes = bis.series_attributes(bis.read_records(fixture("policy_rates_monthly")))
    assert list(attributes) == ["AR", "US"]
    assert attributes["AR"]["COMPILATION"].startswith("From 10 July 2025 onwards: no policy rate adopted")
    assert attributes["US"]["TITLE"] == "Central bank policy rates - United States - Monthly - End of period"


def replace_first_record(text: str, old: str, new: str) -> str:
    header, first, *rest = text.splitlines(keepends=True)
    return "".join([header, first.replace(old, new, 1), *rest])


@pytest.mark.parametrize("old,new", [("M,US,", "Q,US,"), ("M,US,", "M,DE,"), ("M,US,771", "M,US,628"),
                                     ("2025-09", "2025-Q3"), ("3.012677", "n/a")])
def test_unexpected_records_raise(old, new):
    records = bis.read_records(replace_first_record(fixture("cpi_yoy_monthly"), old, new))
    with pytest.raises(ValueError):
        bis.to_rows(records, CPI)


def test_duplicate_and_missing_columns_raise():
    text = fixture("cpi_yoy_monthly")
    with pytest.raises(ValueError, match="duplicate"):
        bis.to_rows(bis.read_records(text + text.splitlines(keepends=True)[1]), CPI)
    with pytest.raises(ValueError, match="missing columns"):
        bis.read_records("<html>NoResultsFound</html>\n")


def test_coverage_spans_per_area():
    rows, _ = bis.to_rows(bis.read_records(fixture("cpi_yoy_monthly")), CPI)
    assert bis.coverage(rows) == {"AU": {"first_period": "2025-09", "last_period": "2025-11", "rows": 3},
                                  "US": {"first_period": "2025-09", "last_period": "2025-11", "rows": 2}}


def test_source_url_matches_contract():
    assert bis.source_url(POLICY) == ("https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/"
                                      "M.US+XM+GB+JP+CH+CA+AU+TR+BR+IN+AR?format=csv&startPeriod=2015-01")
    assert bis.endpoint(CPI).endswith("/WS_LONG_CPI/1.0/M.US+XM+GB+JP+CH+CA+AU+TR+BR+IN+AR.771")


@pytest.fixture
def raw_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "data" / "raw")
    monkeypatch.setattr(bis, "PAUSE_SECONDS", 0)
    return tmp_path / "data" / "raw" / "bis"


def fake_http_get(bodies: dict[str, str]):
    def http_get(url, *, params=None, **_):
        assert params == {"format": "csv", "startPeriod": "2015-01"}
        flow, _version, key = url.split("/BIS/")[1].split("/")[:3]
        body = bodies.get(f"{flow}/{key}", bodies.get(flow))
        if body is None:
            raise requests.HTTPError(f"500 for {flow}")
        return SimpleNamespace(content=body.encode(), headers={})
    return http_get


def test_pull_writes_both_files_with_meta(raw_root, monkeypatch):
    bodies = {"WS_CBPOL": fixture("policy_rates_monthly"), "WS_LONG_CPI": fixture("cpi_yoy_monthly"),
              f"WS_CBPOL/{POLICY_CN_HK.key}": fixture("policy_rates_monthly_cn_hk")}
    monkeypatch.setattr(bis, "http_get", fake_http_get(bodies))
    bis.pull()
    with (raw_root / "cpi_yoy_monthly.csv").open(newline="") as fh:
        assert list(csv.reader(fh))[:3] == [["area", "period", "value"], ["AU", "2025-09", "3.218765"],
                                            ["US", "2025-09", "3.012677"]]
    meta = json.loads((raw_root / "cpi_yoy_monthly.meta.json").read_text())
    assert meta["dropped_nan"] == ["US 2025-10"]
    assert meta["rows"] == 5 and meta["flow"] == "WS_LONG_CPI" and meta["key"].endswith(".771")
    assert "AR" in meta["areas_without_data"] and "US" not in meta["areas_without_data"]
    assert meta["source_url"] == bis.source_url(CPI)
    assert (raw_root / "policy_rates_monthly.meta.json").exists()


def test_pull_keeps_partial_progress_and_cached_file_on_failure(raw_root, monkeypatch):
    raw_root.mkdir(parents=True)
    (raw_root / "policy_rates_monthly.csv").write_text("cached copy\n")
    monkeypatch.setattr(bis, "http_get", fake_http_get({"WS_LONG_CPI": fixture("cpi_yoy_monthly")}))
    with pytest.raises(RuntimeError, match="2 of 3 BIS flows failed.*policy_rates_monthly"):
        bis.pull()
    assert (raw_root / "policy_rates_monthly.csv").read_text() == "cached copy\n"
    assert (raw_root / "cpi_yoy_monthly.csv").exists()


def test_cn_hk_flow_keeps_its_own_areas_and_file(raw_root, monkeypatch):
    rows, dropped = bis.to_rows(bis.read_records(fixture("policy_rates_monthly_cn_hk")), POLICY_CN_HK)
    assert dropped == [] and {row["area"] for row in rows} == {"CN", "HK"}
    with pytest.raises(ValueError, match="unexpected series"):
        bis.to_rows(bis.read_records(fixture("policy_rates_monthly")), POLICY_CN_HK)
    monkeypatch.setattr(bis, "http_get", fake_http_get({f"WS_CBPOL/{POLICY_CN_HK.key}": fixture("policy_rates_monthly_cn_hk")}))
    bis.pull_flow(POLICY_CN_HK)
    meta = json.loads((raw_root / "policy_rates_monthly_cn_hk.meta.json").read_text())
    assert meta["areas_requested"] == ["CN", "HK"] and meta["areas_without_data"] == []
    assert "Loan Prime Rate" in meta["series_attributes"]["CN"]["COMPILATION"]
    assert not (raw_root / "policy_rates_monthly.csv").exists()
