"""Offline tests for pipeline.sources.perps. Fixtures are small real responses captured 2026-11-13: Hyperliquid
fundingHistory (the first five hours after the listing, split into two pages), candleSnapshot 1d (one pre-listing
zero-trade day, the first two traded days, and 2026-11-12/13) and 1h (2026-11-13 15:00-17:00 UTC),
metaAndAssetCtxs trimmed to BTC and NEAR; Kraken historicalfundingrates (first three rows), the first and last two
daily trade candles (served as two pages), the PF_XBTUSD and PF_NEARUSD instrument records and the PF_NEARUSD
ticker; and the first rows of Binance NEARUSDT bulk files (2020-10 funding with a header, 2020-10 klines without one,
2026-10 klines and funding with one, the last two rows of the 2022-02 klines file, which ends on 2022-02-25, and the
2022-02-26 daily file without a header), zipped in the test with their sha256 served as the checksum; Coinbase's
NER-20DEC30-CDE product record as published (captured 2026-11-14 03:11 UTC) and its daily candles for the first two
days (2026-02-28, 2026-03-01) and for 2026-11-11 to 2026-11-13, newest first as served, as two pages (at the tests'
clock the 2026-11-13 candle is the unfinished one)."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import requests

from pipeline import config
from pipeline.sources import perps

FIXTURES = Path(__file__).parent / "fixtures" / "perps"
NOW = datetime(2026, 11, 13, 17, 30, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
TODAY = NOW.date()


class FakeResponse:
    def __init__(self, body: bytes | str, status_code: int = 200):
        self.content = body if isinstance(body, bytes) else body.encode()
        self.text, self.status_code, self.headers = self.content.decode("utf-8", "replace"), status_code, {}

    def json(self, **kwargs):
        return json.loads(self.text, **kwargs)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def http_error(status: int, body: str = "") -> requests.HTTPError:
    return requests.HTTPError(f"{status}", response=FakeResponse(body, status))


def zipped(name: str, text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, text)
    return buffer.getvalue()


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "data" / "raw")
    monkeypatch.setattr(perps.time, "sleep", lambda seconds: None)
    return tmp_path / "data" / "raw" / "perps"


def read_rows(path: Path) -> list[dict]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def meta_of(path: Path) -> dict:
    return json.loads(path.with_suffix(".meta.json").read_text())


# ------------------------------------------------------------------------------------------------- Hyperliquid

def hl_route(log: list, fail_page: int | None = None):
    pages = [fixture("hl_funding_page1.json"), fixture("hl_funding_page2.json")]

    def post(url, payload, **kwargs):
        log.append(payload)
        kind = payload["type"]
        if kind == "fundingHistory":
            index = sum(1 for p in log if p["type"] == "fundingHistory") - 1
            if index == fail_page:
                raise http_error(429, "rate limited")
            return FakeResponse(pages[index])
        if kind == "candleSnapshot":
            return FakeResponse(fixture(f"hl_candles_{payload['req']['interval']}.json"))
        if kind == "metaAndAssetCtxs":
            return FakeResponse(fixture("hl_meta_ctxs.json"))
        raise AssertionError(payload)
    return post


def test_hl_funding_pages_forward_and_labels_each_hour(raw_dir, monkeypatch):
    log: list = []
    monkeypatch.setattr(perps, "HL_PAGE_ROWS", 3)
    monkeypatch.setattr(perps, "http_post", hl_route(log))
    out = perps.fetch_hl_funding()
    page1 = json.loads(fixture("hl_funding_page1.json"))
    assert out.error is None and out.pages == 2
    assert [p["startTime"] for p in log] == [0, page1[-1]["time"] + 1]
    assert [r["time"] for r in out.rows] == [f"2023-11-01T{h}:00:00Z" for h in (17, 18, 19, 20, 21)]
    assert out.rows[0] == {"time": "2023-11-01T17:00:00Z", "time_ms": str(page1[0]["time"]),
                           "funding_rate": page1[0]["fundingRate"], "premium": page1[0]["premium"]}
    body = (fixture("hl_funding_page1.json") + fixture("hl_funding_page2.json")).encode()
    assert out.sidecar()["response_sha256"] == hashlib.sha256(body).hexdigest()


def test_hl_funding_error_mid_run_merges_over_the_cached_file(raw_dir, monkeypatch):
    log: list = []
    monkeypatch.setattr(perps, "HL_PAGE_ROWS", 3)
    monkeypatch.setattr(perps, "http_post", hl_route(log, fail_page=1))
    raw_dir.mkdir(parents=True)
    cached = {"time": "2023-11-01T21:00:00Z", "time_ms": "1698872400000", "funding_rate": "0.1", "premium": "0.2"}
    config.write_csv("perps", "near_funding_hyperliquid_hourly", [cached], perps.FUNDING_HL)
    out = perps.fetch_hl_funding()
    assert out.error.startswith("fundingHistory startTime=") and "HTTP 429: rate limited" in out.error
    rows = perps.store("near_funding_hyperliquid_hourly", "time", out, perps.FUNDING_HL, accumulate=False)
    assert [r["time"] for r in rows][-1] == cached["time"] and len(rows) == 4
    meta = meta_of(raw_dir / "near_funding_hyperliquid_hourly.csv")
    assert meta["complete"] is False and meta["rows_from_previous_file"] == 1 and meta["error"] == out.error


def test_hl_daily_candles_drop_pre_trading_and_unfinished_days(raw_dir, monkeypatch):
    monkeypatch.setattr(perps, "http_post", hl_route([]))
    out = perps.fetch_hl_candles("1d", NOW_MS)
    assert out.error is None
    assert [r["date"] for r in out.rows] == ["2023-11-01", "2023-11-02", "2026-11-12"]
    assert all(int(r["trades"]) > 0 for r in out.rows) and out.rows[0]["volume_quote"] == ""
    assert out.meta["excluded"] == ("1 candle(s) 2021-02-13..2021-02-13 with zero volume and zero trades, before "
                                    "the first traded candle; 1 unfinished candle(s) at fetch time")


def test_hl_hourly_candles_accumulate_over_the_cached_window(raw_dir, monkeypatch):
    monkeypatch.setattr(perps, "http_post", hl_route([]))
    raw_dir.mkdir(parents=True)
    old = {"time": "2026-04-01T00:00:00Z", "open": "1", "high": "1", "low": "1", "close": "1", "volume_base": "1",
           "volume_quote": "", "trades": "1"}
    stale = dict(old, time="2026-11-13T15:00:00Z", close="9")
    config.write_csv("perps", "near_perp_hyperliquid_hourly", [old, stale], perps.CANDLES_HOURLY)
    out = perps.fetch_hl_candles("1h", NOW_MS)
    rows = perps.store("near_perp_hyperliquid_hourly", "time", out, perps.CANDLES_HOURLY, accumulate=True)
    assert [r["time"] for r in rows] == ["2026-04-01T00:00:00Z", "2026-11-13T15:00:00Z", "2026-11-13T16:00:00Z"]
    assert rows[1]["close"] != "9"
    meta = meta_of(raw_dir / "near_perp_hyperliquid_hourly.csv")
    assert meta["complete"] is True and meta["rows_from_previous_file"] == 1 and meta["rows"] == 3
    assert meta["first_time"] == "2026-04-01T00:00:00Z" and meta["last_time"] == "2026-11-13T16:00:00Z"


# ------------------------------------------------------------------------------------------------------ Kraken

def kraken_route(log: list):
    def get(url, params=None, **kwargs):
        log.append((url, dict(params or {})))
        if url == perps.KRAKEN_FUNDING_URL:
            return FakeResponse(fixture("kraken_funding.json"))
        if url == perps.KRAKEN_CANDLES_URL:
            return FakeResponse(fixture("kraken_candles_page1.json" if params["from"] == perps.KRAKEN_FROM
                                        else "kraken_candles_page2.json"))
        if url == perps.KRAKEN_INSTRUMENTS_URL:
            return FakeResponse(fixture("kraken_instruments.json"))
        if url == perps.KRAKEN_TICKER_URL:
            return FakeResponse(fixture("kraken_ticker.json"))
        raise AssertionError(url)
    return get


def test_kraken_funding_keeps_published_digits_as_plain_decimals(raw_dir, monkeypatch):
    monkeypatch.setattr(perps, "http_get", kraken_route([]))
    out = perps.fetch_kraken_funding()
    assert out.error is None and out.pages == 1
    assert out.rows[0] == {"time": "2025-11-11T23:00:00Z", "funding_rate": "-0.000018712319444444",
                           "funding_usd": "-0.000048464907361110"}
    assert [r["time"] for r in out.rows] == ["2025-11-11T23:00:00Z", "2025-11-12T00:00:00Z", "2025-11-12T01:00:00Z"]


def test_kraken_candles_page_while_more_candles_and_drop_today(raw_dir, monkeypatch):
    log: list = []
    monkeypatch.setattr(perps, "http_get", kraken_route(log))
    out = perps.fetch_kraken_candles(TODAY)
    page1 = json.loads(fixture("kraken_candles_page1.json"))["candles"]
    assert out.error is None and out.pages == 2
    assert [p["from"] for _, p in log] == [perps.KRAKEN_FROM, page1[-1]["time"] // 1000 + 86_400]
    assert [r["date"] for r in out.rows] == ["2022-06-13", "2022-06-14", "2026-11-12"]
    assert out.rows[-1]["volume_base"] == "3692136.00000000000" and out.rows[-1]["trades"] == ""


def test_terms_snapshot_holds_the_near_contract_with_number_text(raw_dir, monkeypatch):
    monkeypatch.setattr(perps, "http_post", hl_route([]))
    monkeypatch.setattr(perps, "http_get", kraken_route([]))
    terms, errors = perps.fetch_terms()
    assert errors == []
    hl, kraken = terms["venues"]
    assert hl["contract"] == {"szDecimals": 1, "name": "NEAR", "maxLeverage": 10, "marginTableId": 52}
    assert hl["margin_table"]["marginTiers"][0] == {"lowerBound": "0.0", "maxLeverage": 10}
    assert hl["context"]["funding"] == "0.0000125"
    assert kraken["contract"]["symbol"] == "PF_NEARUSD" and kraken["contract"]["tickSize"] == "0.0001"
    assert kraken["contract"]["marginLevels"][0]["numNonContractUnits"] == "0E-20"
    assert kraken["context"]["symbol"] == "PF_NEARUSD"


def test_terms_snapshot_reports_an_unreachable_venue(raw_dir, monkeypatch):
    monkeypatch.setattr(perps, "http_post", hl_route([]))
    monkeypatch.setattr(perps, "http_get", lambda url, **kwargs: (_ for _ in ()).throw(http_error(403, "blocked")))
    terms, errors = perps.fetch_terms()
    assert [v["venue"] for v in terms["venues"]] == ["Hyperliquid"]
    assert errors == ["Kraken instruments/ticker: HTTP 403: blocked"]


# ---------------------------------------------------------------------------------------------------- Coinbase

def coinbase_route(log: list, fail_page: int | None = None, product: str | None = None):
    """Serve the product record and the two candle pages (the first window gets page 1, later windows page 2)."""
    def get(url, params=None, **kwargs):
        log.append((url, dict(params or {})))
        if url == perps.COINBASE_PRODUCT_URL:
            return FakeResponse(product if product is not None else fixture("coinbase_product.json"))
        if url == perps.COINBASE_CANDLES_URL:
            index = sum(1 for u, _ in log if u == perps.COINBASE_CANDLES_URL) - 1
            if index == fail_page:
                raise http_error(503, "upstream unavailable")
            first = params["start"] == str(perps.unix_day(perps.COINBASE_FROM))
            return FakeResponse(fixture("coinbase_candles_page1.json" if first else "coinbase_candles_page2.json"))
        raise AssertionError(url)
    return get


def unix(day: str) -> str:
    return str(perps.unix_day(date.fromisoformat(day)))


def test_coinbase_candles_page_forward_in_inclusive_windows_and_drop_today(raw_dir, monkeypatch):
    log: list = []
    monkeypatch.setattr(perps, "COINBASE_PAGE_DAYS", 200)
    monkeypatch.setattr(perps, "http_get", coinbase_route(log))
    out = perps.fetch_coinbase_candles(TODAY)
    assert out.error is None and out.pages == 2
    # 200-day windows, both ends inclusive, from COINBASE_FROM until a window starts after today
    assert [(p["start"], p["end"]) for _, p in log] == [(unix("2026-02-01"), unix("2026-08-19")),
                                                        (unix("2026-08-20"), unix("2027-03-07"))]
    assert all(p["granularity"] == "ONE_DAY" for _, p in log)
    assert [r["date"] for r in out.rows] == ["2026-02-28", "2026-03-01", "2026-11-11", "2026-11-12"]
    assert out.rows[0] == {"date": "2026-02-28", "open": "1.0499", "high": "1.1438", "low": "1.0497",
                           "close": "1.1435", "volume_contracts": "67"}
    assert out.meta["excluded"] == "unfinished candle for 2026-11-13 (UTC)"
    body = (fixture("coinbase_candles_page1.json") + fixture("coinbase_candles_page2.json")).encode()
    assert out.sidecar()["response_sha256"] == hashlib.sha256(body).hexdigest()


def test_coinbase_candles_error_mid_run_merges_over_the_cached_file(raw_dir, monkeypatch):
    monkeypatch.setattr(perps, "COINBASE_PAGE_DAYS", 200)
    monkeypatch.setattr(perps, "http_get", coinbase_route([], fail_page=1))
    raw_dir.mkdir(parents=True)
    cached = {"date": "2026-11-12", "open": "1", "high": "1", "low": "1", "close": "1", "volume_contracts": "1"}
    config.write_csv("perps", "near_perp_coinbase_daily", [cached], perps.CANDLES_COINBASE)
    out = perps.fetch_coinbase_candles(TODAY)
    assert out.error == "candles ONE_DAY start=2026-08-20: HTTP 503: upstream unavailable"
    rows = perps.store("near_perp_coinbase_daily", "date", out, perps.CANDLES_COINBASE, accumulate=False)
    assert [r["date"] for r in rows] == ["2026-02-28", "2026-03-01", "2026-11-12"] and rows[-1] == cached
    meta = meta_of(raw_dir / "near_perp_coinbase_daily.csv")
    assert meta["complete"] is False and meta["rows_from_previous_file"] == 1 and meta["error"] == out.error


def test_coinbase_terms_keep_the_product_record_as_published(raw_dir, monkeypatch):
    monkeypatch.setattr(perps, "http_get", coinbase_route([]))
    terms, error = perps.fetch_coinbase_terms()
    assert error is None
    assert (terms["venue"], terms["product_id"], terms["source_url"]) == (
        "Coinbase Derivatives", "NER-20DEC30-CDE", perps.COINBASE_PRODUCT_URL)
    product = terms["product"]
    assert product == json.loads(fixture("coinbase_product.json"))
    details = product["future_product_details"]
    assert (product["display_name"], product["new_at"], details["venue"]) == ("NEAR Perp", "2026-02-28T14:00:00Z",
                                                                               "cde")
    assert (details["contract_size"], details["contract_root_unit"], details["funding_interval"]) == (
        "500", "NEAR", "3600s")
    assert details["overnight_margin_rate"]["short_margin_rate"] == "0.561625"
    assert details["intraday_margin_rate"]["short_margin_rate"] == "0.1999385"
    assert (details["open_interest"], details["funding_rate"]) == ("4457", "-0.00001")
    assert terms["response_sha256"] == hashlib.sha256(fixture("coinbase_product.json").encode()).hexdigest()


@pytest.mark.parametrize("body, expected", [
    ('{"product_id": "BIP-20DEC30-CDE", "future_product_details": {"contract_size": "0.01"}}', "VenueError"),
    ('{"product_id": "NER-20DEC30-CDE", "future_product_details": {}}', "VenueError"),
    ('{"error": "NOT_FOUND", "message": "product not found"}', "VenueError"),
    ("not json", "JSONDecodeError"),
])
def test_coinbase_terms_refuse_anything_but_the_near_perp_record(raw_dir, monkeypatch, body, expected):
    monkeypatch.setattr(perps, "http_get", coinbase_route([], product=body))
    terms, error = perps.fetch_coinbase_terms()
    assert terms is None and error.startswith(f"product NER-20DEC30-CDE: {expected}")


# ----------------------------------------------------------------------------------------------------- Binance

def binance_route(log: list, files: dict[str, str], bad_checksum: bool = False):
    """Serve the named files (file name -> CSV text) as zips with their checksum; everything else is a 404."""
    def get(url, params=None, **kwargs):
        log.append(url)
        name = url.rsplit("/", 1)[1]
        stem = name.removesuffix(".CHECKSUM")
        if stem not in files:
            raise http_error(404, "<Error><Code>NoSuchKey</Code></Error>")
        body = zipped(stem.replace(".zip", ".csv"), files[stem])
        if name.endswith(".CHECKSUM"):
            sha = "0" * 64 if bad_checksum else hashlib.sha256(body).hexdigest()
            return FakeResponse(f"{sha}  {stem}\n")
        return FakeResponse(body)
    return get


def test_binance_reads_headerless_and_header_files_and_lists_unpublished_days(raw_dir, monkeypatch):
    log: list = []
    files = {"NEARUSDT-1d-2020-10-15.zip": fixture("binance_klines_2020-10.csv")}
    monkeypatch.setattr(perps, "http_get", binance_route(log, files))
    monkeypatch.setattr(perps, "BINANCE_FIRST_MONTH", date(2020, 10, 1))
    out = perps.fetch_binance("klines", date(2020, 11, 2))
    assert out.error is None and out.pages == 1
    # last month's monthly file is not published yet, so its days are tried one by one, then the current month's
    assert out.meta["not_published"][0] == "NEARUSDT-1d-2020-10.zip"
    assert len(out.meta["not_published"]) == 1 + 30 + 1 and "NEARUSDT-1d-2020-11-01.zip" in out.meta["not_published"]
    assert [r["date"] for r in out.rows] == ["2020-10-15", "2020-10-16", "2020-10-17"]
    assert out.rows[0] == {"date": "2020-10-15", "open": "1.0625", "high": "1.2231", "low": "1.0625",
                           "close": "1.1220", "volume_base": "16485482", "volume_quote": "19016012.2335",
                           "trades": "98931"}
    assert list(out.meta["zip_sha256"]) == ["NEARUSDT-1d-2020-10-15.zip"]

    files = {"NEARUSDT-1d-2026-10.zip": fixture("binance_klines_2026-10.csv")}
    monkeypatch.setattr(perps, "http_get", binance_route([], files))
    monkeypatch.setattr(perps, "BINANCE_FIRST_MONTH", date(2026, 10, 1))
    out = perps.fetch_binance("klines", date(2026, 11, 1))
    assert out.error is None and [r["date"] for r in out.rows] == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert out.rows[0]["close"] == "4.8030"
    # the trimmed monthly file holds three days, so the rest of October is asked for day by day
    assert out.meta["not_published"] == [f"NEARUSDT-1d-2026-10-{d:02d}.zip" for d in range(4, 32)]


def test_binance_funding_reuses_archived_months_and_never_asks_for_daily_files(raw_dir, monkeypatch):
    log: list = []
    monkeypatch.setattr(perps, "BINANCE_FIRST_MONTH", date(2020, 10, 1))
    files = {"NEARUSDT-fundingRate-2020-10.zip": fixture("binance_funding_2020-10.csv")}
    monkeypatch.setattr(perps, "http_get", binance_route(log, files))
    first = perps.fetch_binance("funding", date(2020, 11, 20))
    assert first.error is None and first.meta["not_published"] == []
    assert first.rows[0] == {"time": "2020-10-14T16:00:00Z", "time_ms": "1602691200001", "interval_hours": "8",
                             "funding_rate": "0.00010000"}
    perps.store("near_funding_binance_8h", "time", first, perps.FUNDING_BINANCE, accumulate=True)
    log.clear()
    second = perps.fetch_binance("funding", date(2020, 12, 2))
    assert second.meta["files_reused"] == 1 and second.meta["not_published"] == ["NEARUSDT-fundingRate-2020-11.zip"]
    assert [u.rsplit("/", 1)[1] for u in log] == ["NEARUSDT-fundingRate-2020-11.zip"]
    rows = perps.store("near_funding_binance_8h", "time", second, perps.FUNDING_BINANCE, accumulate=True)
    assert len(rows) == 4
    meta = meta_of(raw_dir / "near_funding_binance_8h.csv")
    assert meta["complete"] is True and meta["rows_from_previous_file"] == 4 and "response_sha256" not in meta
    assert meta["zip_sha256"] == first.meta["zip_sha256"]


def test_binance_klines_fill_days_a_monthly_file_lacks_and_keep_them_on_reuse(raw_dir, monkeypatch):
    log: list = []
    monkeypatch.setattr(perps, "BINANCE_FIRST_MONTH", date(2022, 2, 1))
    files = {"NEARUSDT-1d-2022-02.zip": fixture("binance_klines_2022-02.csv"),
             "NEARUSDT-1d-2022-02-26.zip": fixture("binance_klines_2022-02-26.csv")}
    monkeypatch.setattr(perps, "http_get", binance_route(log, files))
    first = perps.fetch_binance("klines", date(2022, 3, 1))
    assert first.error is None and [r["date"] for r in first.rows] == ["2022-02-24", "2022-02-25", "2022-02-26"]
    assert first.meta["not_published"] == ["NEARUSDT-1d-2022-02-27.zip", "NEARUSDT-1d-2022-02-28.zip"]
    assert sorted(first.meta["zip_sha256"]) == sorted(files)
    perps.store("near_perp_binance_daily", "date", first, perps.CANDLES_DAILY, accumulate=True)
    log.clear()
    second = perps.fetch_binance("klines", date(2022, 3, 1))
    # nothing archived is downloaded again; only the days still missing are asked for
    assert [u.rsplit("/", 1)[1] for u in log] == ["NEARUSDT-1d-2022-02-27.zip", "NEARUSDT-1d-2022-02-28.zip"]
    assert second.meta["zip_sha256"] == first.meta["zip_sha256"] and second.meta["files_reused"] == 2


def test_binance_checksum_mismatch_is_an_error_and_keeps_the_cache(raw_dir, monkeypatch):
    monkeypatch.setattr(perps, "BINANCE_FIRST_MONTH", date(2026, 10, 1))
    files = {"NEARUSDT-fundingRate-2026-10.zip": fixture("binance_funding_2026-10.csv")}
    monkeypatch.setattr(perps, "http_get", binance_route([], files, bad_checksum=True))
    out = perps.fetch_binance("funding", date(2026, 11, 13))
    assert "differs from the published" in out.error and out.rows == []
    assert perps.store("near_funding_binance_8h", "time", out, perps.FUNDING_BINANCE, accumulate=True) is None
    assert not (raw_dir / "near_funding_binance_8h.csv").exists()


def test_binance_older_missing_month_is_an_error(raw_dir, monkeypatch):
    monkeypatch.setattr(perps, "BINANCE_FIRST_MONTH", date(2026, 9, 1))
    files = {"NEARUSDT-fundingRate-2026-10.zip": fixture("binance_funding_2026-10.csv")}
    monkeypatch.setattr(perps, "http_get", binance_route([], files))
    out = perps.fetch_binance("funding", date(2026, 11, 13))
    assert out.error.startswith("data/futures/um/monthly/fundingRate/NEARUSDT/NEARUSDT-fundingRate-2026-09.zip: "
                                "HTTP 404")


# -------------------------------------------------------------------------------------------------------- pull

@pytest.fixture
def fixed_now(monkeypatch):
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(perps, "datetime", Fixed)


def venue_get(kraken, binance, coinbase):
    """One http_get for every GET venue, chosen by host."""
    def get(url, **kwargs):
        return (binance if "binance" in url else coinbase if "coinbase" in url else kraken)(url, **kwargs)
    return get


def test_pull_writes_every_output_with_its_provenance(raw_dir, fixed_now, monkeypatch):
    monkeypatch.setattr(perps, "HL_PAGE_ROWS", 3)
    monkeypatch.setattr(perps, "COINBASE_PAGE_DAYS", 200)
    monkeypatch.setattr(perps, "BINANCE_FIRST_MONTH", date(2026, 10, 1))
    monkeypatch.setattr(perps, "http_post", hl_route([]))
    binance = binance_route([], {"NEARUSDT-fundingRate-2026-10.zip": fixture("binance_funding_2026-10.csv"),
                                 "NEARUSDT-1d-2026-10.zip": fixture("binance_klines_2026-10.csv")})
    monkeypatch.setattr(perps, "http_get", venue_get(kraken_route([]), binance, coinbase_route([])))
    perps.pull()
    names = {p.name for p in raw_dir.iterdir()}
    outputs = ["near_funding_hyperliquid_hourly.csv", "near_perp_hyperliquid_daily.csv",
               "near_perp_hyperliquid_hourly.csv", "near_funding_kraken_hourly.csv", "near_perp_kraken_daily.csv",
               "near_funding_binance_8h.csv", "near_perp_binance_daily.csv", "near_perp_coinbase_daily.csv",
               "near_perp_terms.json", "near_perp_coinbase_terms.json"]
    assert names == set(outputs) | {Path(o).with_suffix(".meta.json").name for o in outputs}
    for output in outputs:
        meta = meta_of(raw_dir / output)
        assert {"source_url", "fetched_at", "description", "rows", "file"} <= meta.keys(), output
        assert meta["file"] == f"data/raw/perps/{output}"
        if output.endswith(".csv"):
            assert meta["complete"] is True and meta["error"] is None and meta["params"], output
            assert ("zip_sha256" if "binance" in output else "response_sha256") in meta, output
            assert meta["rows"] == len(read_rows(raw_dir / output)), output
    daily = meta_of(raw_dir / "near_perp_binance_daily.csv")
    assert len(daily["not_published"]) == 28 + 12 and daily["last_date"] == "2026-10-03"
    terms = json.loads((raw_dir / "near_perp_terms.json").read_text())
    assert terms["checked_at"] and [v["venue"] for v in terms["venues"]] == ["Hyperliquid", "Kraken Derivatives"]
    coinbase = meta_of(raw_dir / "near_perp_coinbase_daily.csv")
    assert coinbase["first_date"] == "2026-02-28" and coinbase["last_date"] == "2026-11-12"
    assert coinbase["columns"] == perps.CANDLES_COINBASE


def test_pull_fails_loudly_and_keeps_going_after_a_venue_error(raw_dir, fixed_now, monkeypatch):
    monkeypatch.setattr(perps, "BINANCE_FIRST_MONTH", date(2026, 10, 1))
    monkeypatch.setattr(perps, "http_post", lambda url, payload, **kw: (_ for _ in ()).throw(http_error(503)))
    monkeypatch.setattr(perps, "http_get", venue_get(kraken_route([]), binance_route([], {}), coinbase_route([])))
    with pytest.raises(RuntimeError, match="perps: .* series failed"):
        perps.pull()
    names = {p.name for p in raw_dir.iterdir()}
    assert "near_funding_kraken_hourly.csv" in names and "near_funding_hyperliquid_hourly.csv" not in names
    assert "near_perp_terms.json" not in names
    assert {"near_perp_coinbase_daily.csv", "near_perp_coinbase_terms.json"} <= names


def test_coinbase_venue_run_fetches_only_coinbase(raw_dir, fixed_now, monkeypatch):
    log: list = []
    monkeypatch.setattr(perps, "COINBASE_PAGE_DAYS", 200)
    monkeypatch.setattr(perps, "http_post", lambda *a, **kw: pytest.fail("no Hyperliquid call expected"))
    monkeypatch.setattr(perps, "http_get", coinbase_route(log))
    perps.main(["--venue", "coinbase"])
    assert {u for u, _ in log} == {perps.COINBASE_CANDLES_URL, perps.COINBASE_PRODUCT_URL}
    outputs = ["near_perp_coinbase_daily.csv", "near_perp_coinbase_terms.json"]
    assert {p.name for p in raw_dir.iterdir()} == set(outputs) | {Path(o).with_suffix(".meta.json").name
                                                                  for o in outputs}
    daily = meta_of(raw_dir / "near_perp_coinbase_daily.csv")
    assert daily["complete"] is True and daily["rows"] == 4 and daily["source_url"] == perps.COINBASE_CANDLES_URL
    assert daily["params"] == {"granularity": "ONE_DAY", "from": "2026-02-01", "page_days": 200}
    terms = meta_of(raw_dir / "near_perp_coinbase_terms.json")
    assert terms["rows"] == 1 and terms["source_url"] == perps.COINBASE_PRODUCT_URL and terms["fetched_at"]


def test_coinbase_venue_run_keeps_the_cached_snapshot_and_fails_loudly(raw_dir, fixed_now, monkeypatch):
    raw_dir.mkdir(parents=True)
    config.write_json("perps", "near_perp_coinbase_terms", {"cached": True}, rows=1)
    monkeypatch.setattr(perps, "http_get", coinbase_route([], product='{"error": "NOT_FOUND"}'))
    with pytest.raises(RuntimeError, match="perps: 1 series failed .*near_perp_coinbase_terms"):
        perps.main(["--venue", "coinbase"])
    assert json.loads((raw_dir / "near_perp_coinbase_terms.json").read_text()) == {"cached": True}
    assert (raw_dir / "near_perp_coinbase_daily.csv").exists()
