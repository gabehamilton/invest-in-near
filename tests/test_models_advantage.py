"""Offline tests for pipeline.models.advantage (chart c24; claim H4) on synthetic raw files.

The synthetic world: monthly CPI levels on an uneven path, full-day RPC samples on the 1st of every month (supply and
both liquid-staking prices, no block time so each sample sits at 00:00 UTC), and a daily DFF that drifts. A month's
advantage is checked against a hand computation; the side and blank rules, the H4 boundary and the chart run on the
same files or on hand-built months.
"""

from __future__ import annotations

import csv
import json
import math
import shutil
import tomllib
from datetime import date, timedelta
from pathlib import Path

import pytest

from pipeline import config, numbers
from pipeline.chartspec import dump
from pipeline.models import advantage, load, macro, verdicts
from pipeline.models.params import RULES_PATH
from pipeline.models.staking import Window
from pipeline.numbers import Registry

REPO = Path(__file__).resolve().parents[1]
STAMP = "2026-11-10T00:00:00+00:00"
RPC_COLS = ["date", "height", "block_time_utc", "epoch_id", "total_supply_near", "validator_stake_near",
            "n_validators", "stake_epoch_start_height", "staked_ratio", "stnear_price", "linear_price",
            "treasury_near", "protocol_version", "max_inflation_rate"]
S0 = date(2021, 1, 1)      # first RPC sample
S1 = date(2026, 11, 1)     # last RPC sample


@pytest.fixture
def raw(tmp_path, monkeypatch):
    raw_dir = tmp_path / "data" / "raw"
    raw_dir.mkdir(parents=True)
    for mod in (config, load, numbers):
        monkeypatch.setattr(mod, "ROOT", tmp_path)
        monkeypatch.setattr(mod, "RAW_DIR", raw_dir)
    (tmp_path / "thesis").mkdir()
    for name in ("constants.toml", "sources.toml"):
        shutil.copy(REPO / "thesis" / name, tmp_path / "thesis" / name)
    return tmp_path


def put_csv(root: Path, rel: str, header: list[str], rows: list[list]) -> None:
    path = root / "data" / "raw" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows([["" if v is None else v for v in r] for r in rows])
    path.with_suffix(".meta.json").write_text(json.dumps({"fetched_at": STAMP, "source_url": "test"}))


def cpi_level(m: date) -> float:
    k = (m.year - 2019) * 12 + m.month - 1
    return 100 * math.exp(0.0028 * k + 0.004 * math.sin(k / 3))


def supply_at(d: date) -> float:
    t = (d - S0).days
    return 1e9 * math.exp(0.022 * t / 365.25 + 0.003 * math.sin(t / 40))


def price_at(d: date) -> float:
    t = (d - S0).days
    return math.exp(0.05 * t / 365.25 + 0.002 * math.cos(t / 50))


def dff_at(d: date) -> float:
    return 2.5 + 2 * math.sin(d.toordinal() / 90)


def world(root: Path, blank_cpi: tuple[date, ...] = ()) -> None:
    cpi = [[m.isoformat(), None if m in blank_cpi else round(cpi_level(m), 10)]
           for m in macro.months(date(2019, 1, 1), date(2026, 10, 1))]
    put_csv(root, "fred/CPIAUCSL.csv", ["date", "value"], cpi)
    dff, d = [], date(2020, 1, 1)
    while d <= date(2026, 11, 30):
        dff.append([d.isoformat(), round(dff_at(d), 10)])
        d += timedelta(days=1)
    put_csv(root, "fred/DFF.csv", ["date", "value"], dff)
    rows = []
    for k, m in enumerate(macro.months(S0, S1)):
        p = round(price_at(m), 12)
        rows.append([m.isoformat(), 1000 + k, None, "E", round(supply_at(m), 6), None, None, None, None, p, p,
                     None, 80, 0.025])
    put_csv(root, "rpc/history_samples.csv", RPC_COLS, rows)


def registry(cut: str | None = "2025-10-15") -> Registry:
    reg = Registry()
    if cut is not None:
        reg.put("cut_date", cut, "date", "FAKE")
    return reg


def h4_rule() -> dict:
    with RULES_PATH.open("rb") as fh:
        return tomllib.load(fh)["H4"]


def by_month(data: advantage.AdvData) -> dict[date, advantage.Month]:
    return {m.month: m for m in data.months}


# --------------------------------------------------------------------------- F-adv on one month

def test_one_month_matches_the_hand_computation(raw):
    world(raw)
    mo = by_month(advantage.analyze(registry()))[date(2025, 6, 1)]
    start, end = date(2024, 7, 1), date(2025, 7, 1)         # the trailing year that ends at the month's close
    span = (end - start).days
    cpi = cpi_level(date(2025, 6, 1)) / cpi_level(date(2024, 6, 1)) - 1
    g = (supply_at(end) / supply_at(start)) ** (365.25 / span) - 1
    y = (price_at(end) / price_at(start)) ** (365.25 / span) - 1
    acc = 1.0
    for k in range(span):
        acc *= 1 + dff_at(start + timedelta(days=k)) / 36000
    ff = acc ** (365 / span) - 1
    assert mo.close == end and mo.w.r0["date"] == start and mo.w.r1["date"] == end
    assert mo.infl_pp == pytest.approx(100 * (cpi - g), abs=1e-6)     # inputs are rounded on disk
    assert mo.stake_pp == pytest.approx(100 * (y - ff), abs=1e-6)
    assert mo.adv_pp == pytest.approx(mo.infl_pp + mo.stake_pp, abs=1e-9)
    # exact to 1e-9 against the hand formula on the values the model read
    assert mo.adv_pp == pytest.approx(100 * (mo.cpi - mo.g) + 100 * (mo.w.y - mo.w.ff), abs=1e-9)


# --------------------------------------------------------------------------- the side of the cut

def test_a_month_closing_the_day_after_the_cut_counts_after(raw):
    world(raw)
    months = by_month(advantage.analyze(registry("2025-10-31")))
    assert months[date(2025, 10, 1)].side == "after"        # closes 2025-11-01, the day after the cut
    assert months[date(2025, 9, 1)].side == "before"        # closes 2025-10-01


def test_a_month_closing_on_the_cut_counts_before(raw):
    world(raw)
    reg = registry("2025-11-01")
    months = by_month(advantage.analyze(reg))
    assert months[date(2025, 10, 1)].side == "before"       # closes on the cut day itself
    assert months[date(2025, 11, 1)].side == "after"
    advantage.compute(reg)
    data = advantage.analyze(reg)
    assert reg.value("adv_months_before") == sum(1 for m in data.counted if m.close <= date(2025, 11, 1))
    assert reg.value("adv_months_after") == sum(1 for m in data.counted if m.close > date(2025, 11, 1))
    # clean after: the whole trailing year starts on or after the cut
    assert reg.value("adv_clean_from") == "2026-10"


# --------------------------------------------------------------------------- blank CPI

def test_a_blank_cpi_month_has_no_advantage_and_is_listed(raw):
    world(raw, blank_cpi=(date(2025, 10, 1),))
    reg = registry()
    advantage.compute(reg)
    data = advantage.analyze(reg)
    months = by_month(data)
    assert months[date(2025, 10, 1)].adv_pp is None                 # CPI not published
    assert months[date(2026, 10, 1)].adv_pp is None                 # its year-ago CPI is the blank one
    assert months[date(2025, 10, 1)].stake_pp is not None           # the staking part alone is never shown as A
    blank = reg.value("adv_months_blank")
    assert "2025-10 (CPI not published)" in blank and "2026-10 (no year-ago CPI)" in blank
    counted = [m for m in data.months if m.adv_pp is not None]
    assert reg.value("adv_months") == len(counted)
    assert date(2025, 10, 1) not in {m.month for m in counted}
    assert reg.value("adv_months_le0") == sum(1 for m in counted if m.adv_pp <= 0)


# --------------------------------------------------------------------------- hand-built months

def hand_month(m: date, adv: tuple[float, float, float, float], side: str) -> advantage.Month:
    """One month with chosen parts: (cpi, g, y, ff) as fractions."""
    cpi, g, y, ff = adv
    close = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    w = Window({"date": close - timedelta(days=365), "height": 1}, {"date": close, "height": 2}, y=y, ff=ff)
    return advantage.Month(m, close, cpi, g, w, side, False)


def hand_data(monkeypatch, months: list[advantage.Month], cut: date = date(2025, 10, 15)) -> None:
    data = advantage.AdvData(cut, months, ["data/raw/fred/CPIAUCSL.csv"], None)
    monkeypatch.setattr(advantage, "analyze", lambda reg: data)


def month_list(n: int, n_le0: int, n_after: int) -> list[advantage.Month]:
    start, out = date(2017, 1, 1), []
    for i in range(n):
        m = date(start.year + (start.month - 1 + i) // 12, (start.month - 1 + i) % 12 + 1, 1)
        parts = (0.02, 0.03, 0.04, 0.045) if i < n_le0 else (0.04, 0.02, 0.05, 0.04)
        out.append(hand_month(m, parts, "after" if i >= n - n_after else "before"))
    return out


def test_an_advantage_of_exactly_zero_counts_at_or_below_zero(monkeypatch):
    zero = hand_month(date(2016, 12, 1), (0.03, 0.03, 0.04, 0.04), "before")
    assert zero.adv_pp == 0
    hand_data(monkeypatch, [zero, *month_list(3, 0, 2)])
    reg = Registry()
    advantage.compute(reg)
    assert reg.value("adv_months") == 4 and reg.value("adv_months_le0") == 1
    assert reg.value("adv_share_le0") == pytest.approx(25)


def test_no_month_after_the_cut_leaves_h4_not_computed(raw):
    world(raw)
    reg = registry("2027-01-01")
    advantage.compute(reg)
    assert reg.value("adv_months_after") == 0 and reg.value("adv_months_before") > 0
    assert verdicts.evaluate_claim("H4", h4_rule(), reg)["verdict"] == verdicts.NOT_COMPUTED


def test_no_cut_date_leaves_h4_not_computed(raw):
    world(raw)
    reg = registry(None)
    advantage.compute(reg)
    assert reg.value("adv_months_after") is None
    assert verdicts.evaluate_claim("H4", h4_rule(), reg)["verdict"] == verdicts.NOT_COMPUTED


@pytest.mark.parametrize("n_le0, verdict", [(41, verdicts.REFUTED), (40, verdicts.SUPPORTED)])
def test_h4_boundary_at_forty_percent(monkeypatch, n_le0, verdict):
    hand_data(monkeypatch, month_list(100, n_le0, 30))
    reg = Registry()
    advantage.compute(reg)
    assert reg.value("adv_share_le0") == pytest.approx(n_le0)
    assert reg.value("adv_le0_max_pct") == advantage.ADV_LE0_MAX_PCT
    assert verdicts.evaluate_claim("H4", h4_rule(), reg)["verdict"] == verdict


# --------------------------------------------------------------------------- c24

def test_c24_validates_with_the_blank_month_and_the_cut(raw):
    world(raw, blank_cpi=(date(2025, 10, 1),))
    reg = registry()
    advantage.compute(reg)
    chart = advantage.advantage_chart(reg)
    dump(chart.to_json())
    assert chart.claims == ["H4"] and chart.status in ("ok", "partial")
    adv = dict(map(tuple, next(s for s in chart.series if s.id == "adv").points))
    assert adv["2025-10-01"] is None and adv["2025-09-01"] is not None
    cuts = [a for a in chart.annotations if a["kind"] == "ceiling_change"]
    assert [a["x"] for a in cuts] == ["2025-10-15"]
    assert "2025-10" in chart.coverage["gaps"]
    assert len(chart.table["rows"]) == len(chart.series[0].points)


def test_c24_is_unavailable_on_empty_raw(raw):
    reg = registry()
    advantage.compute(reg)
    assert reg.value("adv_share_le0") is None and reg.get("adv_share_le0").null_reason
    chart = advantage.advantage_chart(reg)
    dump(chart.to_json())
    assert chart.status == "unavailable" and chart.empty_state
