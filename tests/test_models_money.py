"""Offline tests for pipeline.models.money on synthetic raw files (no network, no real data).

Every series here is made up for the test: smooth geometric paths so each formula can be checked against a hand
computation. They test the latest twelve-month money growth and its null rules (a blank month is skipped, never
filled; a missing file gives its reason), the euro's published rate, the H1 gaps (each currency's latest year against
NEAR's latest year, g_365), their median and count, the same-months check, the X9 mirror, and an empty raw directory
leaving c21 unavailable and H1 not computed.
"""

from __future__ import annotations

import csv
import json
import shutil
import tomllib
from datetime import date
from pathlib import Path

import pytest

from pipeline import config, numbers
from pipeline.models import load, macro, money, verdicts
from pipeline.models.params import RULES_PATH
from pipeline.numbers import Registry

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def raw(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    raw_dir = root / "data" / "raw"
    raw_dir.mkdir(parents=True)
    for mod in (config, load, numbers):
        monkeypatch.setattr(mod, "ROOT", root)
        monkeypatch.setattr(mod, "RAW_DIR", raw_dir)
    (root / "thesis").mkdir()
    shutil.copy(REPO / "thesis" / "constants.toml", root / "thesis" / "constants.toml")
    return raw_dir


# --------------------------------------------------------------------------- synthetic writers

def months(first: tuple[int, int], last: tuple[int, int]) -> list[str]:
    (y, m), out = first, []
    while (y, m) <= last:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def write(raw: Path, ccy: str, fld: str, rows: list[tuple[str, float | None]], **meta) -> None:
    folder = raw / "money" / ccy.lower()
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"{fld}.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["period", "value"])
        w.writerows([[p, "" if v is None else repr(v)] for p, v in rows])
    side = {"source_url": f"https://example.invalid/{ccy}/{fld}", **meta}
    (folder / f"{fld}.meta.json").write_text(json.dumps(side))


MONEY_RATE = 1.004   # per month


def money_rows(last=(2025, 12), blank=(), rate=MONEY_RATE):
    ps = months((2021, 1), last)
    return [(p, None if p in blank else 1000.0 * rate ** i) for i, p in enumerate(ps)]


def currency(raw: Path, ccy: str = "AUD", *, money_last=(2025, 12), money_blank=(), money_units=None,
             rate=MONEY_RATE) -> None:
    write(raw, ccy, "money", money_rows(money_last, money_blank, rate),
          units=money_units or f"{ccy} million", aggregate="M3", series_id=f"TEST-{ccy}-M3")


def analyze(ccy="AUD"):
    return money.analyze_currency(ccy, 1, 100.0)


def write_bis(raw: Path, rows: list[tuple[str, float]]) -> None:
    folder = raw / "bis_fx"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "fx_turnover_2025.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["period", "currency", "value", "obs_status"])
        w.writerow(["2025", "TO1", "1000", "A"])
        w.writerows([["2025", c, repr(v), "A"] for c, v in rows])
    (folder / "fx_turnover_2025.meta.json").write_text(json.dumps(
        {"source_url": "https://example.invalid/bis", "units": "USD million per day", "first_date": "2025-04-01"}))


def near_growth(monkeypatch, by_month: dict[str, float]) -> None:
    """NEAR's month-end supply growth, one fraction per closing month, in place of the RPC samples."""
    points = {date.fromisoformat(m + "-01"): macro.GPoint(date.fromisoformat(m + "-01"), g, date(2024, 12, 1),
                                                          date(2025, 12, 1)) for m, g in by_month.items()}
    monkeypatch.setattr(money, "near_g_monthly", lambda: (points, ["data/raw/rpc/history_samples.csv"]))


def x9_tolerance() -> float:
    with RULES_PATH.open("rb") as fh:
        return tomllib.load(fh)["crosschecks"]["X9"]["max_rel_pct"]


# --------------------------------------------------------------------------- the latest twelve months

def test_latest_money_growth_is_twelve_months_apart(raw):
    currency(raw)
    f = analyze()
    assert f.val("gm_month") == "2025-12"
    assert f.val("gm_latest") == pytest.approx(MONEY_RATE ** 12 - 1, rel=1e-12)
    assert f.val("aggregate") == "M3"


def test_a_blank_month_inside_the_year_leaves_the_latest_reading(raw):
    currency(raw, money_blank=("2025-05",))
    f = analyze()
    # 2025-12 against 2024-12 are both published, so the blank month is not needed
    assert f.val("gm_month") == "2025-12"


def test_a_blank_latest_month_moves_the_reading_back_without_filling(raw):
    currency(raw, money_blank=("2025-12",))
    f = analyze()
    assert f.val("gm_month") == "2025-11"
    assert f.val("gm_latest") == pytest.approx(MONEY_RATE ** 12 - 1, rel=1e-12)


def test_a_blank_month_a_year_earlier_moves_the_reading_back(raw):
    currency(raw, money_blank=("2024-12",))
    f = analyze()
    assert f.val("gm_month") == "2025-11"


def test_a_missing_file_gives_its_reason(raw):
    f = analyze()
    assert f.val("gm_latest") is None and "no raw file data/raw/money/aud/money.csv" in f.r["gm_latest"].reason


def test_euro_uses_the_published_rate(raw):
    currency(raw, "EUR", money_units="Index")   # the ECB index of notional stocks: growth only, no level
    write(raw, "EUR", "money_growth_published", [("2025-11", 2.9), ("2025-12", 3.1)], units="percent")
    f = analyze("EUR")
    assert f.val("gm_latest") == pytest.approx(0.031) and f.val("gm_month") == "2025-12"
    assert "ECB published annual growth rate" in " ".join(f.r["gm_latest"].inputs)


# --------------------------------------------------------------------------- panel, medians and the H1 gaps

def test_panel_medians_count_only_currencies_with_a_value(raw):
    write_bis(raw, [("AUD", 300.0), ("CAD", 200.0), ("CHF", 100.0)])
    currency(raw, "AUD")
    currency(raw, "CAD", rate=1.006)
    reg = Registry()
    money.compute(reg)
    assert reg.value("n_fiat_panel") == 3 and reg.value("fx_aud_rank") == 1
    assert reg.value("n_fiat_money") == 2
    assert reg.get("mon_chf_gm_latest").value is None and "no raw file" in reg.get("mon_chf_gm_latest").null_reason
    latest = [reg.value("mon_aud_gm_latest"), reg.value("mon_cad_gm_latest")]
    assert reg.value("fiat_gm_latest_med") == pytest.approx(sum(latest) / 2, abs=1e-3)   # registered values are rounded
    # no NEAR supply growth in this registry, so the gaps are null with the upstream reason
    assert reg.get("h1_gap_aud").value is None and "g_365" in reg.get("h1_gap_aud").null_reason
    assert reg.get("h1_gap_med").value is None and reg.get("h1_n_faster").value is None


def near_latest_year(reg: Registry, g: float) -> None:
    """NEAR's supply growth over its latest year (percent), as the supply model registers it."""
    reg.put("g_365", g, "%", "F-growth", source_files=["data/raw/rpc/dense_samples.csv"],
            inputs=["total_supply_near 2024-12-20", "total_supply_near 2025-12-20"])


def test_h1_gaps_set_each_currencys_latest_year_against_nears_latest_year(raw, monkeypatch):
    """Every gap reads g_365, NEAR's latest year, whatever month the currency's money ends; the month-end growth over
    each currency's own twelve months only feeds the same-months count, which can differ."""
    write_bis(raw, [("AUD", 300.0), ("CAD", 200.0), ("CHF", 100.0)])
    currency(raw, "AUD")                                    # latest month 2025-12
    currency(raw, "CAD", money_last=(2025, 11), rate=1.006)  # latest month 2025-11
    currency(raw, "CHF", rate=1.0005)
    near_growth(monkeypatch, {"2025-11": 0.03, "2025-12": 0.02})
    reg = Registry()
    near_latest_year(reg, 5.0)
    money.compute(reg)
    money_g = {c: 100 * (r ** 12 - 1) for c, r in (("aud", MONEY_RATE), ("cad", 1.006), ("chf", 1.0005))}
    gaps = {c: money_g[c] - 5.0 for c in money_g}
    for c, gap in gaps.items():
        assert reg.value(f"h1_gap_{c}") == pytest.approx(gap, abs=1e-3), c
        assert "g_365" in reg.get(f"h1_gap_{c}").inputs[0], c
    assert reg.value("h1_gap_med") == pytest.approx(sorted(gaps.values())[1], abs=1e-3)
    assert reg.value("h1_n_faster") == sum(1 for g in gaps.values() if g > 0) == 1   # CAD only
    assert reg.value("h1_gap_aud") < 0   # money that grew slower than NEAR's supply over NEAR's latest year
    # the same-months check: AUD against NEAR's year to 2025-12 (2 %), CAD against its year to 2025-11 (3 %)
    same = {"aud": money_g["aud"] - 2.0, "cad": money_g["cad"] - 3.0, "chf": money_g["chf"] - 2.0}
    assert reg.value("h1_n_faster_same") == sum(1 for g in same.values() if g > 0) == 2
    assert reg.get("h1_n_faster_same").formula_id == "F-h1-gap"
    assert not any(k.startswith("g_365_at_") for k, _n in reg.items()), "no per-currency NEAR figure is registered"


def test_a_month_without_nears_month_end_growth_leaves_the_gaps_and_drops_it_from_the_check(raw, monkeypatch):
    write_bis(raw, [("AUD", 300.0), ("CAD", 200.0)])
    currency(raw, "AUD")
    currency(raw, "CAD", money_last=(2025, 11))
    near_growth(monkeypatch, {"2025-12": 0.02})
    reg = Registry()
    near_latest_year(reg, 2.5)
    money.compute(reg)
    assert reg.value("h1_gap_aud") is not None and reg.value("h1_gap_cad") is not None
    assert reg.value("h1_n_faster") == 2
    assert reg.value("h1_n_faster_same") == 1 and reg.get("h1_n_faster_same").inputs == [
        "AUD 2025-12: NEAR 2024-12-01 -> 2025-12-01"]


def test_empty_raw_leaves_c21_unavailable_and_h1_not_computed(raw):
    reg = Registry()
    money.compute(reg)
    assert reg.get("n_fiat_panel").value is None and "no BIS turnover file" in reg.get("n_fiat_panel").null_reason
    assert reg.value("n_fiat_money") == 0
    chart = money.chart_c21(reg)
    assert chart.status == "unavailable" and chart.series == []
    assert chart.empty_state
    with RULES_PATH.open("rb") as fh:
        rule = tomllib.load(fh)["H1"]
    assert verdicts.evaluate_claim("H1", rule, reg)["verdict"] == verdicts.NOT_COMPUTED


@pytest.mark.parametrize("n_money, verdict", [(6, verdicts.NOT_COMPUTED), (7, verdicts.SUPPORTED)])
def test_h1_needs_seven_of_the_ten_currencies(n_money, verdict):
    """The floor in H1's rule is the literal seven; one currency short of it leaves H1 without a verdict."""
    with RULES_PATH.open("rb") as fh:
        rule = tomllib.load(fh)["H1"]
    assert rule["not_computed_if"] == "n_fiat_money < 7"
    reg = Registry()
    reg.put("n_fiat_money", n_money, "count", "F-money-growth-latest")
    reg.put("gap_m2", 3.0, "pp", "F-gap")
    reg.put("h1_gap_med", 2.0, "pp", "F-h1-gap")
    out = verdicts.evaluate_claim("H1", rule, reg)
    assert out["verdict"] == verdict
    if verdict == verdicts.NOT_COMPUTED:
        assert "fewer than seven" in out["null_reason"]


# --------------------------------------------------------------------------- X9

def test_x9_mirror_compares_the_latest_common_month_in_one_unit(raw):
    currency(raw, "USD", money_units="USD billion")
    m = dict(money_rows())
    folder = raw / "money" / "oecd"
    folder.mkdir(parents=True)
    with (folder / "monagg.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["area", "adjustment", "period", "value"])
        w.writerow(["USA", "Y", "2025-11", repr(m["2025-11"] * 1000)])          # million: equal to the national
        w.writerow(["USA", "Y", "2025-12", repr(m["2025-12"] * 1000 * 1.002)])  # latest common month, 0.2% off
        w.writerow(["USA", "N", "2025-12", repr(m["2025-12"] * 1000)])          # the other adjustment is ignored
    rows, files = money.mirror({"USD": analyze("USD")})
    assert [(r["code"], r["month"]) for r in rows] == [("USD", date(2025, 12, 1))]
    assert rows[0]["rel_pct"] == pytest.approx(0.2 / 1.002, rel=1e-9)
    assert "data/raw/money/oecd/monagg.csv" in files


# --------------------------------------------------------------------------- the real panel (sanity bounds)

def test_real_panel_sanity():
    """On this repository's data/raw: the panel is the survey's ten largest, most currencies have a latest money
    growth, every computed reading sits in a plausible band, and X9 passes where it runs. Bounds, not figures."""
    if not (REPO / "data" / "raw" / "bis_fx" / "fx_turnover_2025.csv").exists():
        pytest.skip("no BIS survey file in data/raw")
    d = money.analyze(Registry())
    assert len(d["panel"]) == money.PANEL_SIZE and "USD" in d["panel"]
    fiats = [d["fiats"][c] for c in d["panel"]]
    latest = [f.val("gm_latest") for f in fiats if f.val("gm_latest") is not None]
    assert len(latest) >= 7
    for v in latest:
        assert -0.2 < v < 0.5, v
    rows, _ = money.mirror(d["fiats"])
    assert rows and all(r["rel_pct"] <= x9_tolerance() for r in rows)
