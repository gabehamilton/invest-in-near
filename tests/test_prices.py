"""Offline tests for pipeline.sources.prices; fixtures are small real Binance NEARUSDT klines (2026-11-07..10)
and Coinbase NEAR-USD candles (2026-11-08..10), the 2026-11-10 candle being unfinished at capture time."""

from __future__ import annotations

import csv
import json
import sys
from datetime import date
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import config  # noqa: E402
from pipeline.sources import prices  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "prices"
TODAY = date(2026, 11, 10)


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text, self.status_code, self.headers = text, status_code, {}

    def json(self, **kwargs):
        return json.loads(self.text, **kwargs)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def http_error(status: int, body: str) -> requests.HTTPError:
    return requests.HTTPError(f"{status} error", response=FakeResponse(body, status))


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "data" / "raw")
    monkeypatch.setattr(prices.time, "sleep", lambda seconds: None)
    return tmp_path / "data" / "raw"


@pytest.fixture
def calls(monkeypatch):
    log: list[tuple[str, dict]] = []
    monkeypatch.setattr(prices.time, "sleep", lambda seconds: None)
    return log


def binance_route(log, fail_at: int | None = None):
    page1, page2 = json.loads(fixture("binance_page1.json")), json.loads(fixture("binance_page2.json"))

    def fake_get(url, params=None, **kwargs):
        log.append((url, dict(params)))
        if fail_at is not None and params["startTime"] == fail_at:
            raise http_error(500, '{"code":-1001,"msg":"Internal error."}')
        if params["startTime"] == 0:
            return FakeResponse(json.dumps(page1))
        if params["startTime"] == page1[-1][0] + 86_400_000:
            return FakeResponse(json.dumps(page2))
        return FakeResponse("[]")
    return fake_get


def test_plain_keeps_published_digits_and_rewrites_exponents():
    assert prices.plain("3.63000000") == "3.63000000"
    assert prices.plain(Decimal("2.2282306189661815E-05")) == "0.000022282306189661815"
    assert prices.plain(None) == "" and prices.plain("") == ""
    with pytest.raises(ValueError):
        prices.plain("NaN")


def test_binance_pages_forward_and_drops_unfinished_candle(calls, monkeypatch):
    monkeypatch.setattr(prices, "BINANCE_LIMIT", 2)
    monkeypatch.setattr(prices, "http_get", binance_route(calls))
    rows, pages, error = prices.fetch_binance("NEARUSDT", TODAY)
    assert error is None and pages == 3
    assert [call[1]["startTime"] for call in calls] == [0, 1794182400000, 1794355200000]
    assert [row["date"] for row in rows] == ["2026-11-07", "2026-11-08", "2026-11-09"]
    assert rows[-1] == {"date": "2026-11-09", "open": "3.58600000", "high": "3.68000000", "low": "3.45800000",
                        "close": "3.63000000", "volume_base": "16890928.40000000",
                        "volume_quote": "60402412.45510000"}


def test_coinbase_windows_are_contiguous_and_at_most_300_candles():
    windows = prices.coinbase_windows(date(2022, 9, 1), date(2026, 11, 9))
    assert windows[0] == (date(2022, 9, 1), date(2023, 6, 27))
    assert windows[-1][1] == date(2026, 11, 9)
    for (start, end), (next_start, _) in pairwise(windows):
        assert (end - start).days + 1 == 300 and (next_start - end).days == 1
    assert (windows[-1][1] - windows[-1][0]).days + 1 <= 300


def test_coinbase_rows_reordered_quote_volume_empty_and_today_dropped(calls, monkeypatch):
    def fake_get(url, params=None, **kwargs):
        calls.append((url, dict(params)))
        return FakeResponse(fixture("coinbase_candles.json"))
    monkeypatch.setattr(prices, "COINBASE_START", date(2026, 11, 8))
    monkeypatch.setattr(prices, "http_get", fake_get)
    rows, pages, error = prices.fetch_coinbase(TODAY)
    assert error is None and pages == 1
    assert calls[0][1] == {"granularity": 86400, "start": "2026-11-08T00:00:00Z", "end": "2026-11-09T00:00:00Z"}
    assert [row["date"] for row in rows] == ["2026-11-08", "2026-11-09"]
    assert rows[0] == {"date": "2026-11-08", "open": "3.7307", "high": "3.7438", "low": "3.4472",
                       "close": "3.5835", "volume_base": "7148752.818", "volume_quote": ""}


def test_mid_run_error_merges_over_cache_and_marks_meta(raw_dir, calls, monkeypatch):
    cached = [{"date": "2026-11-07", "open": "9", "high": "9", "low": "9", "close": "9", "volume_base": "9",
               "volume_quote": "9"},
              {"date": "2026-11-09", "open": "1", "high": "1", "low": "1", "close": "1", "volume_base": "1",
               "volume_quote": "1"}]
    config.write_csv("market", "NEARUSDT_binance_daily", cached, prices.FIELDNAMES)
    monkeypatch.setattr(prices, "BINANCE_LIMIT", 2)
    monkeypatch.setattr(prices, "http_get", binance_route(calls, fail_at=1794182400000))
    rows, pages, error = prices.fetch_binance("NEARUSDT", TODAY)
    assert pages == 1 and "HTTP 500" in error and "Internal error" in error
    saved = prices.save("market", "NEARUSDT_binance_daily", "date", rows, prices.FIELDNAMES, error)
    assert [row["date"] for row in saved] == ["2026-11-07", "2026-11-08", "2026-11-09"]
    assert saved[0]["close"] == "3.73600000" and saved[2]["close"] == "1"
    meta = json.loads((raw_dir / "market" / "NEARUSDT_binance_daily.meta.json").read_text())
    assert meta["complete"] is False and meta["rows_from_previous_file"] == 1 and meta["rows"] == 3
    with (raw_dir / "market" / "NEARUSDT_binance_daily.csv").open() as fh:
        assert [row["date"] for row in csv.DictReader(fh)] == ["2026-11-07", "2026-11-08", "2026-11-09"]


def test_total_failure_keeps_cached_files_and_raises(raw_dir, monkeypatch):
    def fake_get(url, params=None, **kwargs):
        raise http_error(451, '{"code":0,"msg":"Service unavailable from a restricted location"}')
    monkeypatch.setattr(prices, "http_get", fake_get)
    with pytest.raises(RuntimeError, match="4 series failed"):
        prices.pull()
    assert not (raw_dir / "market").exists()
