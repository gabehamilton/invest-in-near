"""Offline tests for pipeline.sources.derivatives; fixtures are small real responses captured 2026-11-10
(Deribit DVOL/instruments, OKX underlyings, Aevo markets, and the Bybit/Binance geoblock bodies exactly as
served to a US address)."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import config  # noqa: E402
from pipeline.sources import derivatives  # noqa: E402
from pipeline.sources.prices import day_ms  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "derivatives"
TODAY = date(2026, 11, 10)


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text, self.status_code, self.headers = text, status_code, {}

    def json(self, **kwargs):
        return json.loads(self.text, **kwargs)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def venue(name: str) -> derivatives.Venue:
    return next(v for v in derivatives.VENUES if v.name == name)


@pytest.fixture
def calls(monkeypatch):
    log: list[tuple[str, dict]] = []
    monkeypatch.setattr(derivatives.time, "sleep", lambda seconds: None)
    return log


def serve(monkeypatch, log, route):
    def fake_get(url, params=None, **kwargs):
        log.append((url, dict(params or {})))
        result = route(url, params or {})
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, FakeResponse) else FakeResponse(fixture(result))
    monkeypatch.setattr(derivatives, "http_get", fake_get)


def blocked(status: int, body: str) -> requests.HTTPError:
    return requests.HTTPError(f"{status}", response=FakeResponse(body, status))


def test_geoblocks_are_recorded_verbatim_with_unknown_answer(calls, monkeypatch):
    bodies = {"Bybit": (403, fixture("bybit_geoblock.txt")), "Binance": (451, fixture("binance_geoblock.json"))}
    serve(monkeypatch, calls, lambda url, params: blocked(*bodies["Bybit" if "bybit" in url else "Binance"]))
    for name, (status, body) in bodies.items():
        record = derivatives.check_venue(venue(name))
        assert record == {"venue": name, "endpoint": derivatives.endpoint(venue(name)), "ok": False,
                          "http_status": status, "has_near_options": None, "option_underlyings": None,
                          "error": body.strip()}
    assert "configured to block access from your country" in derivatives.check_venue(venue("Bybit"))["error"]


def test_connection_failure_has_no_status(calls, monkeypatch):
    serve(monkeypatch, calls, lambda url, params: requests.ConnectionError("Name or service not known"))
    record = derivatives.check_venue(venue("Aevo"))
    assert record["http_status"] is None and record["has_near_options"] is None
    assert record["error"] == "ConnectionError: Name or service not known"


def test_reachable_venues_list_underlyings_without_near(calls, monkeypatch):
    files = {"Deribit": "deribit_options.json", "OKX": "okx_underlying.json", "Aevo": "aevo_options.json"}
    expected = {"Deribit": ["AVAX", "BTC", "SOL"], "OKX": ["BTC-USD", "ETH-USD", "HYPE-USD", "SOL-USD", "XAU-USD"],
                "Aevo": ["BTC", "ETH", "HYPE", "SOL"]}
    for name, path in files.items():
        serve(monkeypatch, calls, lambda url, params, path=path: path)
        record = derivatives.check_venue(venue(name))
        assert record["ok"] is True and record["http_status"] == 200 and record["error"] is None
        assert record["option_underlyings"] == expected[name] and record["has_near_options"] is False
    assert derivatives.endpoint(venue("Deribit")) == \
        "https://www.deribit.com/api/v2/public/get_instruments?currency=any&kind=option"


def test_near_underlying_is_detected_and_venue_error_codes_kept(calls, monkeypatch):
    serve(monkeypatch, calls, lambda url, params: FakeResponse('{"code":"0","data":[["BTC-USD","NEAR-USD"]]}'))
    assert derivatives.check_venue(venue("OKX"))["has_near_options"] is True
    body = '{"retCode":10001,"retMsg":"params error","result":{}}'
    serve(monkeypatch, calls, lambda url, params: FakeResponse(body))
    record = derivatives.check_venue(venue("Bybit"))
    assert record["ok"] is False and record["http_status"] == 200 and record["error"] == body
    assert record["has_near_options"] is None


def test_venue_check_sidecar_has_contract_fields(calls, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "data" / "raw")
    files = {"deribit": "deribit_options.json", "okx": "okx_underlying.json", "aevo": "aevo_options.json"}
    blocks = {"bybit": blocked(403, fixture("bybit_geoblock.txt")),
              "binance": blocked(451, fixture("binance_geoblock.json"))}
    serve(monkeypatch, calls, lambda url, params: next(
        (files | blocks)[key] for key in files | blocks if key in url))
    venues = derivatives.write_venue_check()
    market = tmp_path / "data" / "raw" / "market"
    payload = json.loads((market / "options_venues.json").read_text())
    meta = json.loads((market / "options_venues.meta.json").read_text())
    assert payload["venues"] == venues and [v["ok"] for v in venues] == [True, True, True, False, False]
    assert {"fetched_at", "source_url", "description", "rows"} <= meta.keys()
    assert meta["rows"] == len(derivatives.VENUES) == 5
    assert meta["source_url"].split(" ") == [v["endpoint"] for v in venues]
    assert meta["file"] == "data/raw/market/options_venues.json" and "source_urls" not in meta


def test_dvol_walks_continuation_backwards_and_dedupes(calls, monkeypatch):
    serve(monkeypatch, calls, lambda url, params: "dvol_page1.json" if params["end_timestamp"] == day_ms(TODAY) - 1
          else "dvol_page2.json")
    rows, pages, error = derivatives.fetch_dvol("BTC", TODAY)
    assert error is None and pages == 2
    assert [call[1]["end_timestamp"] for call in calls] == [day_ms(TODAY) - 1, 1794096000000]
    assert rows == [{"date": "2026-11-07", "close": "40.2"}, {"date": "2026-11-08", "close": "40.99"},
                    {"date": "2026-11-09", "close": "39.87"}]
