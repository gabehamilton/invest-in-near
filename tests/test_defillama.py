"""Offline tests for pipeline.sources.defillama using a small fake summary payload (no network, no data/raw/)."""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
import requests  # noqa: E402

from pipeline import config  # noqa: E402
from pipeline.sources import defillama  # noqa: E402

FIXTURE = (ROOT / "tests/fixtures/defillama/summary_small.json").read_text()


def payload() -> dict:
    return defillama.decode(FIXTURE)


def test_parse_chart_keeps_published_values_and_blanks_nulls():
    rows, dropped = defillama.parse_chart(payload(), "volume_usd", date(2026, 1, 6))
    assert rows == [{"date": "2025-12-31", "volume_usd": "1000"},
                    {"date": "2026-01-01", "volume_usd": "0.1"},
                    {"date": "2026-01-03", "volume_usd": ""},
                    {"date": "2026-01-04", "volume_usd": "600.65"},
                    {"date": "2026-01-05", "volume_usd": "0.00000015"}]
    assert dropped == []


def test_parse_chart_drops_the_unfinished_fetch_day():
    rows, dropped = defillama.parse_chart(payload(), "fees_usd", date(2026, 1, 5))
    assert rows[-1] == {"date": "2026-01-04", "fees_usd": "600.65"}
    assert dropped == ["2026-01-05"]


def test_summarize_reports_gaps_blanks_and_totals():
    data = payload()
    rows, _ = defillama.parse_chart(data, "volume_usd", date(2026, 1, 5))
    summary = defillama.summarize(rows, "volume_usd", data)
    assert summary["missing_dates"] == ["2026-01-02"]
    assert (summary["first_date"], summary["last_date"], summary["blank_rows"]) == ("2025-12-31", "2026-01-04", 1)
    assert summary["sum_of_rows_usd"] == "1600.75" and summary["reported_total_all_time_usd"] == "1600.75"
    assert summary["methodology"] is None and summary["methodology_url"].endswith("dexs/near-intents")


def test_provisional_flags_trailing_days_and_keeps_the_latest_value_as_published():
    rows, _ = defillama.parse_chart(payload(), "fees_usd", date(2026, 1, 6))
    flags = defillama.provisional(rows, "fees_usd", date(2026, 1, 6))
    assert flags["provisional_dates"] == ["2026-01-03", "2026-01-04", "2026-01-05"]
    assert flags["latest_day_provisional"] is True
    assert flags["latest_day"] == {"date": "2026-01-05", "value": "0.00000015",
                                   "previous_date": "2026-01-04", "previous_value": "600.65"}
    assert rows[-1] == {"date": "2026-01-05", "fees_usd": "0.00000015"}


def test_provisional_keeps_a_blank_previous_value_blank_and_skips_the_gap():
    rows, _ = defillama.parse_chart(payload(), "fees_usd", date(2026, 1, 5))
    flags = defillama.provisional(rows, "fees_usd", date(2026, 1, 5))
    assert flags["provisional_dates"] == ["2026-01-03", "2026-01-04"]
    assert flags["latest_day"] == {"date": "2026-01-04", "value": "600.65",
                                   "previous_date": "2026-01-03", "previous_value": ""}


def test_a_stale_series_has_no_provisional_days():
    rows, _ = defillama.parse_chart(payload(), "fees_usd", date(2026, 2, 1))
    flags = defillama.provisional(rows, "fees_usd", date(2026, 2, 1))
    assert flags["provisional_dates"] == [] and flags["latest_day_provisional"] is False
    single = defillama.provisional(rows[:1], "fees_usd", date(2026, 1, 2))
    assert single["latest_day"] == {"date": "2025-12-31", "value": "1000", "previous_date": None,
                                    "previous_value": None}
    assert single["latest_day_provisional"] is True


@pytest.mark.parametrize(("point", "error"), [
    ([1767139200, 1], "duplicate date"),
    ([1767225601, 2], "UTC-midnight"),
    ([1767225600.0, 3], "UTC-midnight"),
    ([1767225600, True], "not a number"),
    ([1767225600, "4"], "not a number"),
    ([1767225600], "malformed"),
])
def test_parse_chart_rejects_malformed_points(point, error):
    data = {"totalDataChart": [[1767139200, 1], point]}
    with pytest.raises(ValueError, match=error):
        defillama.parse_chart(data, "volume_usd", date(2026, 2, 1))


def test_plain_decimal_never_goes_through_float_or_exponents():
    assert defillama.plain_decimal(Decimal("59750528.980000004")) == "59750528.980000004"
    assert defillama.plain_decimal(Decimal("1.5E+3")) == "1500"
    assert defillama.plain_decimal(166233299) == "166233299"
    assert defillama.plain_decimal(None) == ""


def test_series_match_the_contract():
    by_name = {series.name: series for series in defillama.SERIES}
    assert by_name["defillama_volume_daily"].column == "volume_usd"
    assert by_name["defillama_volume_daily"].source_url == (
        "https://api.llama.fi/summary/dexs/near-intents?excludeTotalDataChartBreakdown=true")
    assert by_name["defillama_fees_daily"].column == "fees_usd"
    assert by_name["defillama_fees_daily"].url == "https://api.llama.fi/summary/fees/near-intents"
    assert defillama.REQUIRES == [] and defillama.OUTPUT_SOURCE == "intents"


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "data" / "raw")
    monkeypatch.setattr(defillama.time, "sleep", lambda seconds: None)
    return tmp_path / "data" / "raw"


def test_pull_writes_both_contract_files(raw_dir, monkeypatch):
    monkeypatch.setattr(defillama, "fetch_payload", lambda series: payload())
    defillama.pull()
    volume = config.read_csv("intents", "defillama_volume_daily")
    fees = config.read_csv("intents", "defillama_fees_daily")
    assert list(volume[0]) == ["date", "volume_usd"] and list(fees[0]) == ["date", "fees_usd"]
    assert len(volume) == len(fees) == 5
    meta = config.read_meta("intents", "defillama_fees_daily")
    assert meta["source_url"].endswith("dataType=dailyFees") and meta["rows"] == 5
    assert meta["missing_dates"] == ["2026-01-02"]
    assert meta["latest_day"] == {"date": "2026-01-05", "value": "0.00000015",
                                  "previous_date": "2026-01-04", "previous_value": "600.65"}
    assert isinstance(meta["latest_day_provisional"], bool) and "provisional_rule" in meta


def test_a_failed_series_keeps_its_cached_file_and_fails_the_run(raw_dir, monkeypatch, capsys):
    def fetch(series):
        if series.kind == "fees":
            raise requests.ConnectionError("network down")
        return payload()

    monkeypatch.setattr(defillama, "fetch_payload", fetch)
    with pytest.raises(RuntimeError, match="1 of 2 DefiLlama series failed"):
        defillama.pull()
    assert (raw_dir / "intents" / "defillama_volume_daily.csv").exists()
    assert not (raw_dir / "intents" / "defillama_fees_daily.csv").exists()
    assert "defillama_fees_daily FAILED" in capsys.readouterr().out
