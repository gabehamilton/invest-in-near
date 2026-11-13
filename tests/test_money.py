"""Offline tests for pipeline.sources.money and the money loaders in pipeline.models.load.

Fixtures in tests/fixtures/money/ are small slices of real responses recorded from each publisher on
2026-11-11 (rows cut, nothing edited); synthetic bodies are used only to test failure paths.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import config  # noqa: E402
from pipeline.models import load  # noqa: E402
from pipeline.sources import money  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "money"
SPEC = {s.name: s for s in money.SPECS}


def fx(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fxt(name: str) -> str:
    return fx(name).decode("utf-8-sig")


def fxj(name: str):
    return json.loads(fx(name))


# =========================================================================== spec table

def test_panel_and_spec_table_are_consistent():
    assert set(money.CURRENCIES) == {s.ccy for s in money.SPECS}
    assert len(SPEC) == len(money.SPECS), "duplicate ccy/field"
    for ccy in money.CURRENCIES:
        assert f"{ccy}/money" in SPEC and SPEC[f"{ccy}/money"].role == "primary"
    for s in money.SPECS:
        assert s.publisher in money.FETCHERS and s.publisher in money.PUBLISHERS
        assert s.frequency == "monthly"
        assert s.role in ("primary", "alt", "headline", "level", "context")


def test_every_unit_string_maps_to_a_scale_or_is_an_index_or_percent():
    for s in money.SPECS:
        scale = load.unit_scale(s.units)
        if scale is None:
            assert s.units == "percent" or s.units.startswith("index ("), s.name
        else:
            assert load.unit_currency(s.units) == s.ccy.upper(), s.name


def test_unit_traps_are_flagged_in_the_specs():
    assert load.unit_scale(SPEC["aud/money"].units) == 1e9
    assert load.unit_scale(SPEC["jpy/money"].units) == 1e8


def test_unit_scale():
    assert load.unit_scale("AUD billion") == 1e9
    assert load.unit_scale("JPY 100 million") == 1e8
    assert load.unit_scale("CHF million (end of month)") == 1e6
    for text in ("index (2017=100)", "percent", "", None, "USD trillion", "usd million"):
        assert load.unit_scale(text) is None
    assert load.unit_currency("CAD million") == "CAD" and load.unit_currency("percent") is None


# =========================================================================== numbers and periods

def test_plain_decimal_and_xls_number():
    assert money.plain_decimal("689,219.1 ") == "689219.1"
    assert money.plain_decimal("1.5E+3") == "1500"
    for bad in ("n/a", "NaN", "inf", ""):
        with pytest.raises(ValueError):
            money.plain_decimal(bad)
    assert money.xls_number(8043994.055000001) == "8043994.055"
    assert money.xls_number(8830871.379999999) == "8830871.38"
    assert money.xls_number(2021.0) == "2021"
    with pytest.raises(ValueError):
        money.xls_number("N.A.")


def test_period_helpers():
    assert money.period_start("2026-09") == "2026-09-01"
    assert load.period_start("2015-01") == date(2015, 1, 1)
    for bad in ("2026Q3", "2026-Q3"):
        with pytest.raises(ValueError):
            money.period_start(bad)
        with pytest.raises(ValueError):
            load.period_start(bad)


def test_finish_trims_sorts_and_validates():
    spec = SPEC["usd/money"]
    rows = money.finish([("2015-02", "2"), ("2014-12", "0"), ("2015-01", "1"), ("2015-02", "2")], spec)
    assert rows == [{"period": "2015-01", "value": "1"}, {"period": "2015-02", "value": "2"}]
    with pytest.raises(ValueError, match="two values"):
        money.finish([("2015-01", "1"), ("2015-01", "3")], spec)
    with pytest.raises(ValueError, match="unexpected period"):
        money.finish([("2015-Q1", "1")], spec)
    with pytest.raises(ValueError, match="no observations"):
        money.finish([("2015-01", "")], spec)
    summary = money.summarize([{"period": "2026-06", "value": "1"}, {"period": "2026-07", "value": ""}])
    assert summary["latest_period"] == "2026-06" and summary["last_date"] == "2026-07-01"
    assert summary["last_observed_date"] == "2026-06-01" and summary["blank_rows"] == 1


# =========================================================================== parsers, one publisher each

def test_fred_blank_month():
    cpi = dict(money.parse_fred(fxt("fred_CPIAUCSL.csv"), "CPIAUCSL"))
    assert cpi["2025-10"] == "" and cpi["2025-11"] == "325.063"
    with pytest.raises(ValueError, match="first day of a month"):
        money.parse_fred("observation_date,M2SL\n2026-01-15,1\n", "M2SL")


def test_ecb_index_series_as_published_with_unit_checks():
    records = money.read_sdmx_csv(fxt("ecb_bsi_index.csv"))
    key = SPEC["eur/money"].series_id
    pairs = money.sdmx_pairs(records, {"KEY": key}, {"UNIT_INDEX_BASE": "Dec10 = 100"})
    assert pairs[-1] == ("2026-09", "187.6930926783734")
    with pytest.raises(ValueError, match="UNIT_MULT"):
        money.sdmx_pairs(records, {"KEY": key}, {"UNIT_MULT": "6"})
    with pytest.raises(ValueError, match="not an SDMX-CSV"):
        money.read_sdmx_csv("<html>error</html>")


def test_boj_csv():
    text = fxt("boj_md02.csv")
    pairs, meta = money.parse_boj(text, "MAM1XAM3M3MO", "100 million yen")
    assert pairs == [("2026-08", "16372604"), ("2026-09", "16381626"), ("2026-10", "16371215")]
    assert meta["boj_last_update"] == "20261111"
    with pytest.raises(ValueError, match="unit"):
        money.parse_boj(text, "MAM1XAM3M3MO", "billion yen")
    with pytest.raises(ValueError, match="paginated"):
        money.parse_boj(text.replace("NEXTPOSITION,", "NEXTPOSITION,256"), "MAM1XAM3M3MO", "100 million yen")


def test_boe_iadb():
    pairs = money.parse_iadb(fxt("boe_iadb_LPMAUYN.csv"), "LPMAUYN")
    assert pairs[0] == ("2015-01", "2090756") and pairs[-1] == ("2026-09", "3310558")
    with pytest.raises(ValueError, match="blocked page"):
        money.parse_iadb("<html><title>403 Forbidden</title></html>", "LPMAUYN")


def test_oecd_monagg_selection():
    records = money.read_sdmx_csv(fxt("oecd_monagg.csv"))
    gbr = money.sdmx_pairs(records, {"REF_AREA": "GBR", "ADJUSTMENT": "Y"}, {"UNIT_MULT": "6"})
    assert sorted(gbr)[-1] == ("2026-08", "3837682")


def test_snb_cube_and_label_check():
    pairs, meta = money.parse_snb(fxt("snb_snbmonagg.csv"), ("B", "GM3"))
    assert pairs == [("2026-08", "1239475"), ("2026-09", "1238936")]
    assert meta["publication_date"] == "2026-10-21 09:00"
    m3 = SPEC["chf/money"]
    labels = money.check_snb_labels(fxj("snb_snbmonagg_dimensions.json"), m3.param("match"), m3.param("labels"))
    assert "Monetary aggregate M3" in labels[1]
    with pytest.raises(ValueError, match="expected it to contain"):
        money.check_snb_labels(fxj("snb_snbmonagg_dimensions.json"), m3.param("match"), ("Level", "Monetary aggregate M9"))


def test_rba_d3():
    pairs, meta = money.parse_rba(fxt("rba_d3.csv"), "DMABMS", "$ billion")
    assert pairs == [("2026-07", "3504.3"), ("2026-08", "3506.9"), ("2026-09", "3513.2")]
    assert meta["type_published"] == "Seasonally adjusted"
    with pytest.raises(ValueError, match="units"):
        money.parse_rba(fxt("rba_d3.csv"), "DMABMS", "$ million")
    with pytest.raises(ValueError, match="no series"):
        money.parse_rba(fxt("rba_d3.csv"), "DMAXXX", "$ billion")


def test_valet():
    pairs, meta = money.parse_valet(fxj("valet_money.json"), "V41552801")
    assert pairs[-1] == ("2026-08", "5346672") and meta["label_published"].startswith("M2++")


def test_hkma_bulletin_hk_dollar_columns():
    pairs, meta = money.parse_hkma_t0101(fx("hkma_T0101.xls"), "M2")
    d = dict(pairs)
    assert d["2021-12"] == "8043994.055"           # a year-end row, stored as December
    assert d["2026-10"] == ""                      # N.A. in the bulletin
    assert meta["year_end_rows"] == 5 and meta["monthly_rows"] >= 12
    m3 = dict(money.parse_hkma_t0101(fx("hkma_T0101.xls"), "M3")[0])
    assert all(float(m3[p]) > float(d[p]) for p in d if d[p])


def test_hkma_api_shape_is_checked():
    doc = {"header": {"success": True}, "result": {"datasize": 2, "records": [
        {"end_of_month": "2026-08", "m2_hkd": 1.5, "m3_hkd": 2.5},
        {"end_of_month": "2026-09", "m2_hkd": "", "m3_hkd": 3}]}}
    assert money.parse_hkma_api(doc, "M2") == [("2026-08", "1.5"), ("2026-09", "")]
    with pytest.raises(ValueError, match="lacks"):
        money.parse_hkma_api({"header": {"success": True}, "result": {"records": [{"period": "2026-08"}]}}, "M2")
    with pytest.raises(ValueError, match="paginated"):
        money.parse_hkma_api({"header": {"success": True}, "result": {"datasize": 9, "records":
                                                                      doc["result"]["records"]}}, "M2")
    with pytest.raises(ValueError, match="unsuccessful"):
        money.parse_hkma_api({"header": {"success": False}}, "M2")


def test_singstat_rows_by_name():
    pairs, meta = money.parse_singstat(fxj("singstat_M701111.json"), "M2", "Million Dollars")
    assert pairs[-1] == ("2026-09", "903759.4") and meta["units_published"] == "Million Dollars"
    with pytest.raises(ValueError, match="rows named"):
        money.parse_singstat(fxj("singstat_M701111.json"), "M4", None)
    for bad in ("2026 2Q", "2026 Sept"):
        with pytest.raises(ValueError, match="unexpected"):
            money.singstat_period(bad)


# =========================================================================== client (pacing, cache, failures)

def test_client_caches_paces_per_host_and_remembers_failures(monkeypatch):
    calls, sleeps = [], []

    def http_get(url, *, params=None, headers=None, **_):
        calls.append(url)
        if "bad" in url:
            raise requests.HTTPError("405 Client Error")
        return SimpleNamespace(content=b"ok", headers={})
    monkeypatch.setattr(money, "http_get", http_get)
    client = money.Client(pause=5.0, host_pause={}, sleep=sleeps.append)
    a = client.get("https://a.example/x", {"id": "1"})
    assert a.url == "https://a.example/x?id=1"
    client.get("https://a.example/x", {"id": "1"})
    client.get("https://b.example/y")
    client.get("https://a.example/z")
    assert calls == ["https://a.example/x", "https://b.example/y", "https://a.example/z"]
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 5.0   # only the second request to a.example waited
    for _ in range(2):
        with pytest.raises(requests.HTTPError):
            client.get("https://bad.example/")
    assert calls.count("https://bad.example/") == 1 and client.requests == 4


# =========================================================================== pull (fake client, tmp raw dir)

ROUTES = [
    ("_iadb-FromShowColumns.asp", "boe_iadb_LPMAUYN.csv"),
    ("T0101.xls", "hkma_T0101.xls"),
    ("data-api.ecb.europa.eu", "ecb_bsi_index.csv"),
]


class FakeClient(money.Client):
    def __init__(self, fail: tuple[str, ...] = ()):
        super().__init__(pause=0, host_pause={}, sleep=lambda s: None)
        self.fail, self.seen = fail, []

    def _serve(self, url: str) -> money.Fetched:
        self.seen.append(url)
        self.last_url = url
        if any(f in url for f in self.fail):
            raise requests.HTTPError(f"403 Client Error for {url}")
        for fragment, name in ROUTES:
            if fragment in url:
                return money.Fetched(fx(name), url, {})
        raise requests.HTTPError(f"405 Not Allowed for {url}")

    def get(self, url, params=None, headers=None):
        items = list(params.items()) if isinstance(params, dict) else list(params or [])
        return self._serve(url + ("?" + money.urlencode(items) if items else ""))

    def curl(self, url):
        try:
            return self._serve(url)
        except requests.HTTPError:
            raise money.CurlError("403") from None


@pytest.fixture
def raw(tmp_path, monkeypatch):
    raw_dir = tmp_path / "data" / "raw"
    for mod in (config, load, money):
        monkeypatch.setattr(mod, "ROOT", tmp_path)
        monkeypatch.setattr(mod, "RAW_DIR", raw_dir)
    return raw_dir / "money"


REQUIRED_META = ("source_url", "fetched_at", "method", "units", "seasonal_adjustment", "series_id",
                 "latest_period", "first_date", "last_observed_date", "publisher", "rows")


def test_pull_writes_csv_sidecar_and_raw_archives(raw):
    specs = [SPEC["eur/money"], SPEC["gbp/money"], SPEC["hkd/money"]]
    status = money.pull(None, FakeClient(), specs)
    assert status == {"eur/money": "ok", "gbp/money": "ok", "hkd/money": "ok"}
    with (raw / "eur" / "money.csv").open(newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["period", "value"] and rows[-1] == ["2026-09", "187.6930926783734"]
    meta = json.loads((raw / "eur" / "money.meta.json").read_text())
    for key in REQUIRED_META:
        assert key in meta, key
    assert meta["units"] == SPEC["eur/money"].units
    assert meta["latest_period"] == "2026-09"
    assert meta["source_url"].startswith("https://data-api.ecb.europa.eu/service/data/BSI/M.U2.Y.V.M30.X.I.U2.2300.Z01.E?")
    assert meta["last_observed_date"] == "2026-09-01" and meta["file"] == "data/raw/money/eur/money.csv"
    gbp = json.loads((raw / "gbp" / "money.meta.json").read_text())
    assert gbp["archived_response"] == "data/raw/money/gbp/_raw/iadb_LPMAUYN.csv"
    assert (raw / "gbp" / "_raw" / "iadb_LPMAUYN.csv").read_bytes() == fx("boe_iadb_LPMAUYN.csv")
    hkd = json.loads((raw / "hkd" / "money.meta.json").read_text())
    assert "405" in hkd["hkma_api_error"] and hkd["source_url"].endswith("T0101.xls")
    assert (raw / "hkd" / "_raw" / "T0101.xls").exists()


def test_failure_keeps_previous_vintage_and_marks_sidecar(raw):
    spec = SPEC["gbp/money"]
    money.pull(None, FakeClient(), [spec])
    before_csv = (raw / "gbp" / "money.csv").read_text()
    before = json.loads((raw / "gbp" / "money.meta.json").read_text())
    with pytest.raises(RuntimeError, match="1 of 1 money series failed"):
        money.pull(None, FakeClient(fail=("_iadb-",)), [spec])
    assert (raw / "gbp" / "money.csv").read_text() == before_csv
    after = json.loads((raw / "gbp" / "money.meta.json").read_text())
    assert after["fetched_at"] == before["fetched_at"] and "403" in after["error"] and after["error_at"]
    assert load.money_null_reason("gbp", "money") is None   # the cached vintage still loads


def test_failure_without_vintage_writes_null_sidecar_only(raw):
    with pytest.raises(RuntimeError):
        money.pull(None, FakeClient(fail=("_iadb-",)), [SPEC["gbp/money"]])
    assert not (raw / "gbp" / "money.csv").exists()
    meta = json.loads((raw / "gbp" / "money.meta.json").read_text())
    assert meta["complete"] is False and meta["rows"] == 0 and meta["units"] == "GBP million"
    assert "_iadb-FromShowColumns.asp" in meta["source_url"]   # the URL attempted is recorded
    assert load.money_null_reason("gbp", "money") == "BoE IADB refused this network"
    assert load.money_series("gbp", "money").points == []


class HkmaApiClient(FakeClient):
    """Serves a synthetic HKMA API document unless the API host is in `fail`."""

    def __init__(self, doc: dict, fail: tuple[str, ...] = ()):
        super().__init__(fail)
        self.doc = doc

    def _serve(self, url: str) -> money.Fetched:
        if url.startswith(money.HKMA_API) and not any(f in url for f in self.fail):
            self.seen.append(url)
            self.last_url = url
            return money.Fetched(json.dumps(self.doc).encode(), url, {})
        return super()._serve(url)


def test_hkma_bulletin_never_replaces_a_fuller_api_vintage(raw):
    spec = SPEC["hkd/money"]
    months = [f"{y}-{m:02d}" for y in range(2015, 2027) for m in range(1, 13)][:141]
    doc = {"header": {"success": True}, "result": {"datasize": len(months), "records": [
        {"end_of_month": p, "m2_hkd": 1000 + i, "m3_hkd": 2000 + i} for i, p in enumerate(months)]}}
    money.pull(None, HkmaApiClient(doc), [spec])
    before = (raw / "hkd" / "money.csv").read_bytes()
    assert json.loads((raw / "hkd" / "money.meta.json").read_text())["source_url"].startswith(money.HKMA_API)
    with pytest.raises(RuntimeError, match="1 of 1 money series failed"):
        money.pull(None, HkmaApiClient(doc, fail=("api.hkma.gov.hk",)), [spec])
    assert (raw / "hkd" / "money.csv").read_bytes() == before
    after = json.loads((raw / "hkd" / "money.meta.json").read_text())
    assert "cached API vintage, which is kept" in after["error"] and after["error_url"].endswith("T0101.xls")
    assert not (raw / "hkd" / "_raw" / "T0101.xls").exists()


def test_rba_blank_after_the_first_observation_is_a_blank_row():
    lines = fxt("rba_d3.csv").splitlines(keepends=True)
    i = next(i for i, line in enumerate(lines) if line.startswith("31/08/2026"))
    cells = lines[i].rstrip("\n").split(",")
    cells[12] = ""   # DMABMS
    lines[i] = ",".join(cells) + "\n"
    pairs, _ = money.parse_rba("".join(lines), "DMABMS", "$ billion")
    assert pairs == [("2026-07", "3504.3"), ("2026-08", ""), ("2026-09", "3513.2")]
    j = next(i for i, line in enumerate(lines) if line.startswith("31/07/2026"))
    cells = lines[j].rstrip("\n").split(",")
    cells[12] = ""
    lines[j] = ",".join(cells) + "\n"
    pairs, _ = money.parse_rba("".join(lines), "DMABMS", "$ billion")
    assert pairs == [("2026-09", "3513.2")]   # blanks before the first observation are not rows


def test_monagg_and_series_failures_share_one_exception_tuple():
    assert money.CurlError in money.FETCH_ERRORS and money.ET.ParseError in money.FETCH_ERRORS
    assert TypeError in money.FETCH_ERRORS and OSError in money.FETCH_ERRORS


def test_panel_codes_have_specs():
    for ccy in ("usd", "eur", "jpy", "gbp", "cny", "chf", "aud", "cad", "hkd", "sgd"):
        assert ccy in money.CURRENCIES and f"{ccy}/money" in SPEC


def test_boe_is_requested_first_and_once(raw):
    client = FakeClient()
    money.pull(None, client, [SPEC["eur/money"], SPEC["gbp/money"]])
    assert "_iadb-" in client.seen[0] and sum("_iadb-" in u for u in client.seen) == 1


def test_a_shape_change_fails_the_series_not_the_run(raw):
    # the ECB route serves the index series, so the published growth series finds no matching key
    with pytest.raises(RuntimeError, match="1 of 2 money series failed.*eur/money_growth_published"):
        money.pull(None, FakeClient(), [SPEC["eur/money_growth_published"], SPEC["eur/money"]])
    meta = json.loads((raw / "eur" / "money_growth_published.meta.json").read_text())
    assert "no observations match" in meta["error"]
    assert (raw / "eur" / "money.csv").exists()


# =========================================================================== loaders

def test_money_series_and_meta_loaders(raw):
    money.pull(None, FakeClient(), [SPEC["eur/money"]])
    s = load.money_series("EUR", "money")
    assert s.files == ["data/raw/money/eur/money.csv"]
    assert s.points[-1] == (date(2026, 9, 1), 187.6930926783734) and s.first == date(2026, 7, 1)
    assert load.money_meta("eur", "money")["units"] == SPEC["eur/money"].units
    assert load.money_null_reason("eur", "money") is None
    assert "no raw file" in load.money_null_reason("sgd", "money")


def test_blank_values_load_as_none(raw):
    path = raw / "usd" / "money.csv"
    path.parent.mkdir(parents=True)
    path.write_text("period,value\n2025-09,324.245\n2025-10,\n2025-11,325.063\n")
    s = load.money_series("usd", "money")
    assert s.points == [(date(2025, 9, 1), 324.245), (date(2025, 10, 1), None), (date(2025, 11, 1), 325.063)]
    assert len(s.non_null()) == 2


def test_oecd_monagg_loader(raw):
    path = raw / "oecd" / "monagg.csv"
    path.parent.mkdir(parents=True)
    records = money.read_sdmx_csv(fxt("oecd_monagg.csv"))
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["area", "adjustment", "period", "value"])
        w.writerows([[r["REF_AREA"], r["ADJUSTMENT"], r["TIME_PERIOD"], r["OBS_VALUE"]] for r in records])
    gbr = load.oecd_monagg("GBR", "Y")
    assert [d for d, _ in gbr.points] == [date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1)]
    assert gbr.points[-1][1] == 3837682.0 and load.oecd_monagg("CHN", "Y").points == []
