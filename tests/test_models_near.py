"""Tests for the NEAR models (group A): pipeline/models/{supply,staking,holders}.py.

Synthetic raw directories (built in tmp_path, with pipeline.config / load / numbers pointed at them) exercise every
formula path: the merged history + dense sample loader (dedupe, conflict, the tip row), exact growth, edge tolerance,
nulls, the ceiling bracket vs an exact change height, the four burn methods (exact from BigQuery, exact from every
epoch boundary in epoch_mints, sampled from epoch_history, upper bound), the realized-mint check, BigQuery absent/present, LST yields and spreads, the DFF publication lag,
quarterly statistics under the rule in force, the c10 grid and the holder windows. Sanity bounds run against the real
data/raw/ and skip (with the reason) only when the input they need is not on disk yet.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import config, numbers
from pipeline.chartspec import dump
from pipeline.models import holders, load, staking, supply
from pipeline.numbers import Registry

REPO = Path(__file__).resolve().parents[1]
UTC = timezone.utc
T0 = datetime(2020, 10, 1, tzinfo=UTC)
YEAR_S = 365 * 86400

GROUP_A_KEYS = {
    "supply": ["g_365", "g_365_neg_run_days", "i_max_now", "cut_date", "cut_height", "supply_now", "s_now", "s_max",
               "unstaked_now", "burn_share_365", "burn_method", "mint_per_day_365", "burn_per_day_365", "bq_max_date",
               "max_abs_diff_near", "n_overlap", "latest_rpc_date", "epoch_hours_median_30d", "rpc_tip_date",
               "rpc_weekly_from", "rpc_daily_from", "burn_share_365_ub", "n_burn_epochs_365",
               "burn_share_365_sampled", "n_burn_epochs_365_sampled", "burn_share_365_epochs",
               "mint_vs_ceiling_median_365_pp", "mint_vs_ceiling_min_365_pp", "mint_vs_ceiling_max_365_pp",
               "n_mint_epochs_365", "mint_check_from", "hsp027_from", "hsp027_height", "hsp027_protocol"],
    "staking": ["lst_start_date", "y_lst_365", "y_lst_90", "y_st_365", "y_li_365", "y_formula_365", "lst_haircut",
                "effr_mean_365", "effr_latest", "spread_adj_365", "spread_naive_365", "spread_adj_90",
                "n_q_rule", "spread_q_sd_rule", "corr_levels_rule", "fed_range_q_bp_rule", "sep_year", "sep_median",
                "sep_longer_run", "y_s60"],
    "holders": ["staker_share_365", "nonstaker_365"],
}
ALL_KEYS = [k for keys in GROUP_A_KEYS.values() for k in keys]
HISTORY_FIELDS = ["date", "height", "block_time_utc", "epoch_id", "total_supply_near", "validator_stake_near",
                  "n_validators", "stake_epoch_start_height", "staked_ratio", "stnear_price", "linear_price",
                  "treasury_near", "protocol_version", "max_inflation_rate"]


# ---------------------------------------------------------------------------------------------------------
# synthetic raw directory
# ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def raw(tmp_path, monkeypatch):
    """An empty repo root in tmp_path with every module's RAW_DIR / ROOT pointed at it (constants copied)."""
    root = tmp_path.resolve()
    raw_dir = root / "data" / "raw"
    raw_dir.mkdir(parents=True)
    for mod in (config, load, numbers):
        monkeypatch.setattr(mod, "ROOT", root)
        monkeypatch.setattr(mod, "RAW_DIR", raw_dir)
    (root / "thesis").mkdir()
    shutil.copy(REPO / "thesis" / "constants.toml", root / "thesis" / "constants.toml")
    return raw_dir


def write_csv(raw_dir: Path, source: str, name: str, fields: list[str], rows: list[dict]) -> None:
    path = raw_dir / source / f"{name}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    (raw_dir / source / f"{name}.meta.json").write_text(json.dumps(
        {"fetched_at": "2026-11-10T23:00:00+00:00", "source_url": f"https://example.test/{source}/{name}"}))


def years(t: datetime) -> float:
    return (t - T0).total_seconds() / 86400 / 365.25


def target_time(d: date) -> datetime:
    """A month-start sample: the last block before 00:00 UTC of `d` (as the real backfill records it)."""
    return datetime(d.year, d.month, d.day, tzinfo=UTC) - timedelta(milliseconds=500)


def month_starts(first: date, last: date) -> list[date]:
    out, d = [], first
    while d <= last:
        out.append(d)
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return out


LATEST = date(2026, 11, 10)
LATEST_T = datetime(2026, 11, 9, 23, 59, 59, 500000, tzinfo=UTC)   # target_time(LATEST): a full-day sample
TIP_D, TIP_T = date(2026, 11, 11), datetime(2026, 11, 11, 2, 58, 40, tzinfo=UTC)   # the history file's latest block
K = math.log(1.0245)       # supply grows 2.45 %/yr (F-growth on 365.25 days)
A_ST = math.log(1.045)     # stNEAR yields 4.5 %
A_LI = math.log(1.040)     # LiNEAR yields 4.0 %
LST_FROM = date(2022, 4, 1)
CUT_OLD, CUT_NEW = date(2025, 10, 1), date(2025, 11, 1)   # rate 0.05 through CUT_OLD, 0.025 from CUT_NEW


def history_row(d: date, t: datetime, height: int, *, k: float = K, s: float = 0.40, rate: float | None = None,
                lst: bool | None = None, supply_override: float | None = None) -> dict:
    supply_v = supply_override if supply_override is not None else 1e9 * math.exp(k * years(t))
    if rate is None:
        rate = 0.05 if d <= CUT_OLD else 0.025
    has_lst = d >= LST_FROM if lst is None else lst
    return {
        "date": d.isoformat(), "height": height, "block_time_utc": t.isoformat(), "epoch_id": f"E{height}",
        "total_supply_near": f"{supply_v:.9f}", "validator_stake_near": f"{s * supply_v:.9f}",
        "n_validators": 100, "stake_epoch_start_height": height - 10, "staked_ratio": f"{s:.12f}",
        "stnear_price": f"{math.exp(A_ST * years(t)):.15f}" if has_lst else "",
        "linear_price": f"{math.exp(A_LI * years(t)):.15f}" if has_lst else "",
        "treasury_near": "1", "protocol_version": 70, "max_inflation_rate": f"{rate}",
    }


def baseline_dates() -> list[date]:
    # monthly from 2020-11, plus edges for the trailing year (2025-11-08) and the trailing 90 days (2026-08-10)
    return sorted(set(month_starts(date(2020, 11, 1), date(2026, 11, 1)) + [date(2025, 11, 8), date(2026, 8, 10)]))


def write_history(raw_dir: Path, rows: list[dict]) -> None:
    write_csv(raw_dir, "rpc", "history_samples", HISTORY_FIELDS, rows)


def baseline_history(raw_dir: Path, *, s_bump: date | None = date(2023, 6, 1), tip: bool = False, **kw) -> list[dict]:
    rows = []
    for d in baseline_dates():
        t = target_time(d)
        rows.append(history_row(d, t, int((t - T0).total_seconds()), s=0.45 if d == s_bump else 0.40, **kw))
    rows.append(history_row(LATEST, LATEST_T, int((LATEST_T - T0).total_seconds()), **kw))
    if tip:   # the latest-block row the backfill appends: after 00:00 UTC of its date, so not a full-day sample
        rows.append(history_row(TIP_D, TIP_T, int((TIP_T - T0).total_seconds()), s=0.42, **kw))
    write_history(raw_dir, rows)
    return rows


DENSE_FIELDS = ["date", "height", "block_time_utc", "total_supply_near", "stnear_price", "linear_price"]


def dense_row(d: date, **kw) -> dict:
    """A dense_samples.csv row: the same 00:00 UTC block (height, time, values) a history row on `d` would hold."""
    t = target_time(d)
    r = history_row(d, t, int((t - T0).total_seconds()), **kw)
    return {k: r[k] for k in DENSE_FIELDS}


def write_dense(raw_dir: Path, rows: list[dict]) -> None:
    write_csv(raw_dir, "rpc", "dense_samples", DENSE_FIELDS, rows)


EPOCH_FIELDS = ["sample_date", "epoch_id", "start_height", "start_time_utc", "end_height", "end_time_utc",
                "next_start_height", "epoch_seconds", "supply_start_near", "supply_end_near",
                "minted_next_boundary_near", "burnt_in_epoch_near", "annualized_mint_rate", "annualized_burn_rate",
                "protocol_version", "max_inflation_rate"]


def epoch_row(d: date, minted: float, burnt: float, *, protocol: int = 70, rate: float = 0.025,
              mint_rate: float | None = None, seconds: float = 25000.0) -> dict:
    """An epoch_history.csv row for the epoch containing the 00:00 UTC sample of `d`."""
    t = target_time(d)
    h = int((t - T0).total_seconds())
    supply0 = 1e9
    return {"sample_date": d.isoformat(), "epoch_id": f"E{h}", "start_height": h - 100,
            "start_time_utc": (t - timedelta(seconds=seconds / 2)).isoformat(), "end_height": h + 100,
            "end_time_utc": (t + timedelta(seconds=seconds / 2)).isoformat(), "next_start_height": h + 101,
            "epoch_seconds": seconds, "supply_start_near": supply0, "supply_end_near": supply0 - burnt,
            "minted_next_boundary_near": minted, "burnt_in_epoch_near": burnt,
            "annualized_mint_rate": mint_rate if mint_rate is not None else minted * YEAR_S / (supply0 * seconds),
            "annualized_burn_rate": burnt * YEAR_S / (supply0 * seconds), "protocol_version": protocol,
            "max_inflation_rate": rate}


def write_epochs(raw_dir: Path, rows: list[dict]) -> None:
    write_csv(raw_dir, "rpc", "epoch_history", EPOCH_FIELDS, rows)


CHANGE_FIELDS = ["param", "old_value", "new_value", "first_height", "block_time_utc", "protocol_version",
                 "prev_height"]


def daily(first: date, last: date, value_fn) -> list[dict]:
    out, d = [], first
    while d <= last:
        v = value_fn(d)
        out.append({"date": d.isoformat(), "value": "" if v is None else v})
        d += timedelta(days=1)
    return out


def write_fred(raw_dir: Path, series_id: str, rows: list[dict]) -> None:
    write_csv(raw_dir, "fred", series_id, ["date", "value"], rows)


DFF_SWITCH = date(2023, 1, 1)


def dff_value(d: date) -> float:
    return 1.0 if d < DFF_SWITCH else 6.0


def cpi_index(m: date, base: float = 100.0, rate: float = 0.03) -> float:
    months = (m.year - 2020) * 12 + (m.month - 1)
    return base * (1 + rate) ** (months / 12)


def baseline_macro(raw_dir: Path, dff_last: date = date(2026, 11, 9)) -> None:
    write_fred(raw_dir, "DFF", daily(date(2020, 1, 1), dff_last, dff_value))
    write_fred(raw_dir, "EFFR", daily(date(2020, 1, 1), dff_last,
                                      lambda d: None if d.weekday() >= 5 else dff_value(d)))
    cpi = [{"date": m.isoformat(), "value": "" if m == date(2025, 10, 1) else f"{cpi_index(m):.6f}"}
           for m in month_starts(date(2020, 1, 1), date(2026, 10, 1))]
    write_fred(raw_dir, "CPIAUCSL", cpi)
    m2 = [{"date": m.isoformat(), "value": f"{cpi_index(m, 15000, 0.05):.4f}"}
          for m in month_starts(date(2020, 1, 1), date(2026, 9, 1))]
    write_fred(raw_dir, "M2SL", m2)
    steps = [(date(2020, 3, 16), 0.25), (date(2022, 3, 17), 0.50), (date(2022, 6, 16), 1.75),
             (date(2023, 7, 27), 5.50), (date(2024, 9, 19), 5.00), (date(2025, 12, 11), 3.75),
             (date(2026, 9, 17), 4.00)]

    def target(d: date) -> float:
        return [v for s, v in steps if s <= d][-1]
    write_fred(raw_dir, "DFEDTARU", daily(date(2020, 3, 16), date(2026, 11, 10), target))
    write_fred(raw_dir, "FEDTARMD", [{"date": "2026-01-01", "value": 4.1}, {"date": "2027-01-01", "value": 3.9},
                                     {"date": "2028-01-01", "value": 3.6}])
    write_fred(raw_dir, "FEDTARMDLR", [{"date": "2026-06-17", "value": 3.1}, {"date": "2026-09-16", "value": 3.2}])


def run_all() -> tuple[Registry, dict]:
    reg = Registry()
    for mod in (supply, staking, holders):
        mod.compute(reg)
    charts = {}
    for mod in (supply, staking, holders):
        for chart in mod.charts(reg):
            charts[chart.id] = chart
    return reg, charts


def ffc(rate_pct: float) -> float:
    """F-ffcomp of a constant DFF, annualized on 365 days."""
    return (1 + rate_pct / 36000) ** 365 - 1


# ---------------------------------------------------------------------------------------------------------
# empty raw directory: every key registered as null with a reason; every chart degrades
# ---------------------------------------------------------------------------------------------------------


def said(chart) -> str:
    """Everything a chart card says in words: the sentence, the caveat and the method notes."""
    return " ".join([chart.sentence, chart.caveat, *chart.notes])

def test_empty_raw_registers_every_key_null_and_charts_degrade(raw):
    (raw.parent.parent / "thesis" / "constants.toml").unlink()
    reg, charts = run_all()
    for key in ALL_KEYS:
        assert key in reg, key
        n = reg.get(key)
        assert n.value is None and n.null_reason, (key, n.value)
    assert set(charts) == {"c01", "c02", "c03", "c05", "c10", "c12", "c16"}
    for chart in charts.values():
        chart.to_json()
        assert chart.status == "unavailable", chart.id
        assert all(chart.empty_state.get(k) for k in ("reason", "needs", "fix_command"))


def test_every_group_a_key_registered_on_baseline(raw):
    baseline_history(raw)
    baseline_macro(raw)
    reg, _ = run_all()
    for key in ALL_KEYS:
        assert key in reg, key


# ---------------------------------------------------------------------------------------------------------
# supply: growth, windows, nulls
# ---------------------------------------------------------------------------------------------------------

def test_growth_is_exact_and_uses_block_times(raw):
    baseline_history(raw)
    baseline_macro(raw)
    reg, _ = run_all()
    assert reg.value("g_365") == pytest.approx(2.45, abs=1e-6)
    series = supply.build_model().series
    assert all(w["g"] * 100 == pytest.approx(2.45, abs=1e-6) for w in series)
    assert reg.value("g_365_neg_run_days") == 0
    # first window end: the first sample whose date - 365 has a sample within 7 days (2021-11-01 -> 2020-11-01)
    assert series[0]["r1"]["date"] == date(2021, 11, 1)
    assert reg.value("latest_rpc_date") == LATEST.isoformat()
    assert reg.value("supply_now") == pytest.approx(1e9 * math.exp(K * years(LATEST_T)), abs=0.01)
    # one window per sample date with an edge 365 days back
    dates = baseline_dates() + [LATEST]
    expected = sum(1 for d in dates
                   if any(abs((d - timedelta(days=365) - e).days) <= 7 for e in dates if e < d))
    assert len(series) == expected


def test_tip_row_sets_now_figures_but_no_window_ends_there(raw):
    baseline_history(raw, tip=True)
    baseline_macro(raw)
    reg, charts = run_all()
    assert reg.value("latest_rpc_date") == LATEST.isoformat()           # every trailing window ends here
    assert reg.value("rpc_tip_date") == TIP_D.isoformat()
    assert reg.value("supply_now") == pytest.approx(1e9 * math.exp(K * years(TIP_T)), abs=0.01)
    assert reg.value("s_now") == pytest.approx(42.0, abs=1e-6)           # stake is a "now" figure: the tip
    assert any(f"{LATEST.isoformat()} (block" in i for i in reg.get("g_365").inputs)
    assert staking.build_model().latest["date"] == LATEST
    assert supply.build_model().series[-1]["r1"]["date"] == LATEST
    assert reg.value("g_365") == pytest.approx(2.45, abs=1e-6)


def test_merged_samples_dedupe_identical_dates_and_add_dense_dates(raw):
    baseline_history(raw, tip=True)
    write_dense(raw, [dense_row(date(2026, 11, 1)), dense_row(date(2026, 11, 3))])   # 11-01 is also in history
    rows, files = load.rpc_merged()
    on = {d: [r for r in rows if r["date"] == d] for d in (date(2026, 11, 1), date(2026, 11, 3), TIP_D)}
    assert len(on[date(2026, 11, 1)]) == 1 and on[date(2026, 11, 1)][0]["sources"] == "history+dense"
    assert on[date(2026, 11, 1)][0]["validator_stake_near"] is not None      # the history row is the one kept
    dense_only = on[date(2026, 11, 3)][0]
    assert dense_only["sources"] == "dense" and dense_only["validator_stake_near"] is None and dense_only["full_day"]
    assert len(on[TIP_D]) == 1 and not on[TIP_D][0]["full_day"]
    assert any(f.endswith("dense_samples.csv") for f in files) and any(f.endswith("history_samples.csv") for f in files)
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)
    series = load.rpc_merged_series("total_supply_near")
    assert TIP_D not in {d for d, _ in series.points} and date(2026, 11, 3) in {d for d, _ in series.points}


def test_merged_samples_raise_when_the_files_disagree(raw):
    baseline_history(raw)
    bad = dense_row(date(2026, 11, 1))
    bad["total_supply_near"] = f"{float(bad['total_supply_near']) + 1:.9f}"
    write_dense(raw, [bad])
    with pytest.raises(load.SampleConflict, match="2026-11-01"):
        load.rpc_merged()
    late = dense_row(date(2026, 11, 3))
    late["block_time_utc"] = datetime(2026, 11, 3, 0, 0, 5, tzinfo=UTC).isoformat()   # after 00:00 UTC
    write_dense(raw, [late])
    with pytest.raises(load.SampleConflict, match="00:00 UTC"):
        load.rpc_merged()


def test_daily_dense_samples_make_trailing_windows_exact(raw):
    # monthly history without the hand-placed edges; dense daily samples for the trailing 400 days
    write_history(raw, [r for r in baseline_history(raw) if r["date"] not in ("2025-11-08", "2026-08-10")])
    baseline_macro(raw)
    write_dense(raw, [dense_row(LATEST - timedelta(days=k)) for k in range(400)])
    reg, charts = run_all()
    assert any("2025-11-10 (block" in i for i in reg.get("g_365").inputs)       # exactly LATEST - 365
    assert "365.000 days" in reg.get("y_lst_365").inputs and "90.000 days" in reg.get("y_lst_90").inputs
    assert "365.000 days" in reg.get("spread_adj_365").inputs and "90.000 days" in reg.get("spread_adj_90").inputs
    assert reg.value("g_365") == pytest.approx(2.45, abs=1e-6)
    assert reg.value("y_lst_90") == pytest.approx(4.25, abs=1e-6)
    assert reg.value("rpc_weekly_from") == (LATEST - timedelta(days=399)).isoformat()
    assert reg.value("rpc_daily_from") == (LATEST - timedelta(days=399)).isoformat()
    # c02: a window at every full-day sample date; the CSV lists each one, the page table the month starts
    c02 = charts["c02"]
    n_windows = len(supply.build_model().series)
    pts = [p for p in next(s for s in c02.series if s.id == "g_365").points if p[1] is not None]
    assert len(pts) == n_windows
    windows = [r for r in c02.csv_table["rows"] if r[0] == "trailing year"]
    assert len(windows) == n_windows
    shown = [r for r in c02.table["rows"] if r[0] == "trailing year"]
    assert all(r[1].endswith("-01") for r in shown[:-1]) and shown[-1][1] == LATEST.isoformat()
    # c01: the ninety-day LST line has a point at every full-day sample date with a start edge
    lst90 = next(s for s in charts["c01"].series if s.id == "lst_90").points
    assert sum(1 for x, y in lst90 if y is not None and x >= "2025-11-01") >= 300
    assert "monthly until" in said(c02) and "daily" in said(c02)


def test_g365_null_when_window_start_has_no_sample_within_seven_days(raw):
    rows = [r for r in baseline_history(raw) if r["date"] not in ("2025-11-08", "2025-11-01")]
    write_history(raw, rows)
    reg, charts = run_all()
    assert reg.value("g_365") is None
    assert "2025-11-10" in reg.get("g_365").null_reason
    assert reg.value("g_365_neg_run_days") is None
    assert charts["c02"].status == "partial"
    charts["c02"].to_json()


def test_negative_growth_run_and_shrink_title(raw):
    rows = []
    for d in baseline_dates():
        t = target_time(d)
        k = -0.01 if d >= date(2025, 6, 1) else K   # supply starts shrinking in mid 2025
        base = 1e9 * math.exp(K * years(datetime(2025, 6, 1, tzinfo=UTC)))
        sup = base * math.exp(k * (years(t) - years(datetime(2025, 6, 1, tzinfo=UTC)))) if d >= date(2025, 6, 1) \
            else None
        rows.append(history_row(d, t, int((t - T0).total_seconds()), supply_override=sup))
    sup = 1e9 * math.exp(K * years(datetime(2025, 6, 1, tzinfo=UTC))) * math.exp(
        -0.01 * (years(LATEST_T) - years(datetime(2025, 6, 1, tzinfo=UTC))))
    rows.append(history_row(LATEST, LATEST_T, int((LATEST_T - T0).total_seconds()), supply_override=sup))
    write_history(raw, rows)
    reg, charts = run_all()
    assert reg.value("g_365") < 0
    series = supply.build_model().series
    # consecutive negative windows back to the first negative one, no gap > 35 days in between
    negatives = [w["r1"]["date"] for w in series if w["g"] < 0]
    assert reg.value("g_365_neg_run_days") == (LATEST - negatives[0]).days
    assert "shrank" in charts["c02"].title and "-" not in charts["c02"].title


def test_window_series_breaks_across_sampling_gaps(raw):
    rows = [r for r in baseline_history(raw) if not ("2023-03-01" <= r["date"] <= "2023-09-01")]
    write_history(raw, rows)
    _, charts = run_all()
    pts = next(s for s in charts["c02"].series if s.id == "g_365").points
    assert any(y is None for _, y in pts)
    assert ["2023-02-01", "2023-10-01"] in charts["c02"].coverage["gaps"]


# ---------------------------------------------------------------------------------------------------------
# supply: the issuance ceiling
# ---------------------------------------------------------------------------------------------------------

def test_ceiling_cut_bracketed_without_protocol_changes(raw):
    rows = baseline_history(raw)
    baseline_macro(raw)
    reg, charts = run_all()
    assert reg.value("i_max_now") == pytest.approx(2.5)
    assert reg.value("cut_date") == CUT_NEW.isoformat()
    assert reg.value("cut_height") is None
    before = next(r for r in rows if r["date"] == CUT_OLD.isoformat())
    after = next(r for r in rows if r["date"] == CUT_NEW.isoformat())
    reason = reg.get("cut_height").null_reason
    assert str(before["height"]) in reason and str(after["height"]) in reason
    assert charts["c02"].bands and charts["c02"].bands[0]["from"] == CUT_OLD.isoformat()
    # a bracketed change makes that year's ceiling an upper bound, so the calendar-year direction is not exact: null
    assert reg.value("ceiling_gap_dir") is None
    assert "bracketed" in reg.get("ceiling_gap_dir").null_reason and "2025" in reg.get("ceiling_gap_dir").null_reason
    basis = {r[1]: r[7] for r in charts["c02"].table["rows"] if r[0] == "calendar year"}
    assert basis["2024"] == "exact" and basis["2025"] == "upper bound"


CHANGE_T = datetime(2025, 10, 20, 7, 30, tzinfo=UTC)
GAS_T = datetime(2026, 11, 4, 11, 46, 29, tzinfo=UTC)


def write_changes(raw_dir: Path) -> None:
    # the schema pipeline/sources/near_rpc.py writes (CHANGE_FIELDS); other params are ignored by the ceiling model
    write_csv(raw_dir, "rpc", "protocol_changes", CHANGE_FIELDS,
              [{"param": "max_inflation_rate", "old_value": 0.05, "new_value": 0.025, "first_height": 170000123,
                "block_time_utc": CHANGE_T.isoformat(), "protocol_version": 80, "prev_height": 170000122},
               {"param": "burnt_gas_reward", "old_value": 0.3, "new_value": 0, "first_height": 223012337,
                "block_time_utc": GAS_T.isoformat(), "protocol_version": 87, "prev_height": 223012336}])


def test_ceiling_gap_exact_per_calendar_year(raw):
    baseline_history(raw)
    baseline_macro(raw)
    write_changes(raw)
    reg, charts = run_all()
    ceil5 = math.exp(0.05 * 365.25 / 365) - 1
    gaps_pp = {}
    for y in range(2021, 2026):
        t0, t1 = target_time(date(y, 1, 1)), target_time(date(y + 1, 1, 1))
        if y < 2025:
            ceil = ceil5
        else:   # 2025 switches rate at the exact instant of the change block
            log = (0.05 * (CHANGE_T - t0).total_seconds() + 0.025 * (t1 - CHANGE_T).total_seconds()) / YEAR_S
            ceil = math.exp(log * 365.25 / ((t1 - t0).total_seconds() / 86400)) - 1
        gaps_pp[y] = (ceil - 0.0245) * 100
    assert reg.value("ceiling_gap_dir") == "below"   # growth sat under the ceiling in every calendar year
    rows = {r[1]: r for r in charts["c02"].table["rows"] if r[0] == "calendar year"}
    assert all(r[7] == "exact" for r in rows.values())
    for y, gap in gaps_pp.items():
        assert rows[str(y)][8] == pytest.approx(gap, abs=1e-4), y


def test_exact_cut_from_protocol_changes(raw):
    baseline_history(raw)
    baseline_macro(raw)
    write_changes(raw)
    write_csv(raw, "rpc", "protocol_config_samples",
              ["height", "date", "protocol_version", "max_inflation_rate", "burnt_gas_reward"],
              [{"height": 169000000, "date": "2025-10-01", "protocol_version": 80, "max_inflation_rate": 0.05,
                "burnt_gas_reward": 0.3},
               {"height": 171000000, "date": "2025-11-01", "protocol_version": 81, "max_inflation_rate": 0.025,
                "burnt_gas_reward": 0.3},
               {"height": 224000000, "date": "2026-11-11", "protocol_version": 87, "max_inflation_rate": 0.025,
                "burnt_gas_reward": 0}])
    reg, charts = run_all()
    assert reg.value("cut_height") == 170000123
    assert reg.value("cut_date") == "2025-10-20"
    assert not charts["c02"].bands
    basis = {r[1]: r[7] for r in charts["c02"].table["rows"] if r[0] == "calendar year"}
    assert basis["2025"] == "exact"
    # the bisected bracket is confirmed by the sampled config reads either side of it
    assert any("protocol_config_samples.csv reads max_inflation_rate 0.05 at block 169000000" in i
               for i in reg.get("cut_height").inputs)
    # every HSP-027 figure comes from the burnt_gas_reward row (no typed constant)
    assert reg.value("hsp027_height") == 223012337 and reg.value("hsp027_from") == "2026-11-04"
    assert reg.value("hsp027_protocol") == 87 and reg.get("hsp027_height").formula_id == "D5"
    assert any("burnt_gas_reward 0.3 at block 171000000" in i for i in reg.get("hsp027_from").inputs)
    assert "hsp027_release" not in reg and "hsp027_activation" not in supply.constants()[0]
    assert "rebate ended at block 223,012,337 on 2026-11-04" in said(charts["c05"])


def test_hsp027_null_without_protocol_changes(raw):
    baseline_history(raw)
    reg, _ = run_all()
    for key in ("hsp027_from", "hsp027_height", "hsp027_protocol"):
        assert reg.value(key) is None and "burnt_gas_reward" in reg.get(key).null_reason


def test_staked_share(raw):
    baseline_history(raw)
    reg, _ = run_all()
    assert reg.value("s_now") == pytest.approx(40.0, abs=1e-6)
    assert reg.value("s_max") == pytest.approx(45.0, abs=1e-6)
    assert reg.value("unstaked_now") == pytest.approx(60.0, abs=1e-6)


# ---------------------------------------------------------------------------------------------------------
# supply: burn (exact > sampled > upper bound) and the BigQuery reconciliation
# ---------------------------------------------------------------------------------------------------------

def test_burn_upper_bound_without_exact_sources(raw):
    rows = baseline_history(raw)
    baseline_macro(raw)
    reg, charts = run_all()
    r0 = next(r for r in rows if r["date"] == "2025-11-08")
    t0 = datetime.fromisoformat(r0["block_time_utc"])
    s0, s1 = float(r0["total_supply_near"]), 1e9 * math.exp(K * years(LATEST_T))
    minted = s0 * math.expm1(0.025 * (LATEST_T - t0).total_seconds() / YEAR_S)
    share = (minted - (s1 - s0)) / minted
    days = (LATEST_T - t0).total_seconds() / 86400
    assert reg.value("burn_method") == "upper bound"
    assert reg.get("burn_share_365").formula_id == "F-burn-ub"
    assert reg.value("burn_share_365") == pytest.approx(share * 100, abs=1e-4)
    assert reg.value("mint_per_day_365") == pytest.approx(minted / days, abs=0.01)
    assert reg.value("burn_per_day_365") == pytest.approx((minted - (s1 - s0)) / days, abs=0.01)
    c05 = charts["c05"]
    assert c05.status in ("fallback", "partial")
    assert "(upper bound)" in c05.title and "at most" in c05.title and "the most fees can have burned" in c05.sentence
    methods = {r[0]: r[3] for r in c05.table["rows"]}
    assert methods["Trailing year"] == "upper bound" and methods["2021"] == "upper bound"
    c05.to_json()


def monthly_epochs(spike: date | None = date(2023, 12, 1), skip: date | None = None) -> list[dict]:
    """One epoch per month from 2020-11 to 2026-11: 100 NEAR minted, 2 burnt; one spike epoch burns 60."""
    return [epoch_row(d, 100.0, 60.0 if d == spike else 2.0, protocol=40 if d < date(2021, 6, 1) else 70,
                      rate=0.05 if d <= CUT_OLD else 0.025)
            for d in month_starts(date(2020, 11, 1), date(2026, 11, 1)) if d != skip]


def test_burn_sampled_from_epoch_history(raw):
    baseline_history(raw)
    baseline_macro(raw)
    write_epochs(raw, monthly_epochs())
    reg, charts = run_all()
    assert reg.value("burn_method") == "sampled: one epoch per month"
    assert reg.get("burn_share_365").formula_id == "F-burn-sampled"
    # trailing year [2025-11-10, 2026-11-10): the epochs sampled 2025-12-01 .. 2026-11-01
    assert reg.value("n_burn_epochs_365") == 12
    assert reg.value("burn_share_365") == pytest.approx(2.0, abs=1e-9)
    days = 12 * 25000 / 86400                       # the sampled epochs' own durations, not the window
    assert reg.value("mint_per_day_365") == pytest.approx(1200 / days, abs=0.01)
    assert reg.value("burn_per_day_365") == pytest.approx(24 / days, abs=0.01)
    # the upper bound stays alongside, over the same edges
    assert reg.value("burn_share_365_ub") is not None and reg.get("burn_share_365_ub").formula_id == "F-burn-ub"
    c05 = charts["c05"]
    cols = c05.table["columns"]
    rows = {r[0]: r for r in c05.table["rows"]}
    # per calendar year; the spike epoch is kept, so it weighs a twelfth of 2023
    assert rows["2022"][cols.index("sampled_share_pct")] == pytest.approx(2.0)
    assert rows["2023"][cols.index("sampled_share_pct")] == pytest.approx((11 * 2 + 60) / 1200 * 100, abs=1e-4)
    t = rows["Trailing year"]
    assert t[cols.index("method")] == "sampled: one epoch per month" and t[cols.index("sampled_epochs")] == 12
    assert t[cols.index("upper_bound_share_pct")] == pytest.approx(reg.value("burn_share_365_ub"), abs=1e-4)
    assert rows["2023"][cols.index("sampled_epochs")] == 12
    assert rows["epoch 2023-12-01"][cols.index("burn_share_pct")] == pytest.approx(60.0)   # the outlier is listed
    # the title carries the epoch count and the hard upper bound beside the sampled share
    assert "(sampled: one epoch per month, 12 epochs) and at most" in c05.title
    assert "F-burn-sampled" in c05.formula_ids and "F-burn-ub" in c05.formula_ids
    assert any(q.file == "data/raw/rpc/epoch_history.csv" for q in c05.queries)
    c05.to_json()


MINT_FIELDS = ["boundary_height", "block_time_utc", "minted_near", "epoch_seconds", "supply_before_near",
               "annualized_mint_rate", "prev_boundary_height"]
WIN0 = LATEST - timedelta(days=365)       # the trailing year's start sample date


def boundary_rows(first: datetime, last: datetime, minted: float, *, every_s: int = 25000,
                  linked: bool = True) -> list[dict]:
    """epoch_mints.csv rows: a boundary every `every_s` seconds from `first` through `last` (height = seconds since
    T0, as in the synthetic samples), each linked to the boundary before it."""
    h0, h1 = int((first - T0).total_seconds()), int((last - T0).total_seconds())
    heights = list(range(h0, h1 + 1, every_s))
    return [{"boundary_height": h, "block_time_utc": (T0 + timedelta(seconds=h)).isoformat(),
             "minted_near": f"{minted:.6f}", "epoch_seconds": every_s, "supply_before_near": "1000000000",
             "annualized_mint_rate": "0.025", "prev_boundary_height": prev if linked else ""}
            for prev, h in zip([h0 - every_s] + heights, heights)]


def epoch_window(raw_dir: Path) -> tuple[int, int, float, float]:
    """Monthly history plus daily dense samples for the trailing 400 days; (start height, end height, S0, S1) of the
    trailing year's two 00:00 UTC samples."""
    write_history(raw_dir, [r for r in baseline_history(raw_dir) if r["date"] not in ("2025-11-08", "2026-08-10")])
    write_dense(raw_dir, [dense_row(LATEST - timedelta(days=k)) for k in range(400)])
    r0, r1 = dense_row(WIN0), dense_row(LATEST)
    return r0["height"], r1["height"], float(r0["total_supply_near"]), float(r1["total_supply_near"])


def write_mints(raw_dir: Path, rows: list[dict]) -> None:
    write_csv(raw_dir, "rpc", "epoch_mints", MINT_FIELDS, rows)


def every_epoch_setup(raw_dir: Path) -> tuple[list[dict], int, float, float]:
    """A boundary every 25,000 s from a week before the window to the tip, each minting 2 % more than its share of
    the year's supply growth; returns (rows, boundaries inside, minted per boundary, supply growth)."""
    lo, hi, s0, s1 = epoch_window(raw_dir)
    rows = boundary_rows(target_time(WIN0 - timedelta(days=7)), TIP_T, 1.0)
    n = sum(1 for r in rows if lo < r["boundary_height"] <= hi)
    per = float(f"{(s1 - s0) / n * 1.02:.6f}")
    for r in rows:
        r["minted_near"] = f"{per:.6f}"
    write_mints(raw_dir, rows)
    return rows, n, per, s1 - s0


def test_burn_exact_from_every_epoch_boundary(raw):
    baseline_macro(raw)
    rows, n, per, growth_near = every_epoch_setup(raw)
    write_epochs(raw, monthly_epochs())            # the monthly sample stays beside it for comparison
    reg, charts = run_all()
    minted = n * per
    assert reg.value("burn_method") == "exact: every epoch boundary" and reg.value("burn_method_short") == "exact"
    assert reg.get("burn_share_365").formula_id == "F-burn-epochs"
    assert reg.value("burn_share_365") == pytest.approx((minted - growth_near) / minted * 100, abs=1e-4)
    assert reg.value("burn_share_365_epochs") == reg.value("burn_share_365")
    assert reg.value("n_burn_epochs_365") == n and n > 1200
    assert reg.value("mint_per_day_365") == pytest.approx(minted / 365.0, abs=0.01)   # the exact window, not epochs
    assert reg.value("burn_per_day_365") == pytest.approx((minted - growth_near) / 365.0, abs=0.01)
    # the sampled share and the upper bound stay registered beside it
    assert reg.value("burn_share_365_sampled") == pytest.approx(2.0) and reg.value("n_burn_epochs_365_sampled") == 12
    assert reg.get("burn_share_365_sampled").formula_id == "F-burn-sampled"
    assert reg.value("burn_share_365_ub") is not None
    c05 = charts["c05"]
    assert "(exact: every epoch boundary, " in c05.title and " epochs)" in c05.title
    assert "exact over every epoch for the trailing year" in c05.sentence
    assert any("one-epoch-a-month sample read" in note for note in c05.notes)
    cols = c05.table["columns"]
    t = {r[0]: r for r in c05.table["rows"]}["Trailing year"]
    assert t[cols.index("method")] == "exact: every epoch boundary" and t[cols.index("epoch_boundaries")] == n
    assert t[cols.index("sampled_share_pct")] == pytest.approx(2.0) and t[cols.index("days")] == 365.0
    series = {se.id: dict((x, y) for x, y in se.points) for se in c05.series}
    assert series["burn_epochs"]["Trailing year"] is not None and series["burn_sampled"]["Trailing year"] is None
    assert series["burn_epochs"]["2024"] is None and series["burn_sampled"]["2024"] == pytest.approx(2.0)
    assert c05.x.get("pack") is True
    assert c05.status == "fallback" and "trailing year is exact" in c05.status_banner
    assert any(q.file == "data/raw/rpc/epoch_mints.csv" for q in c05.queries)
    assert "F-burn-epochs" in c05.formula_ids
    c05.to_json()


@pytest.mark.parametrize("break_it, why", [
    (lambda rows, lo, hi: [r for r in rows if r["boundary_height"] != next(
        x["boundary_height"] for x in rows if lo < x["boundary_height"] <= hi) + 25000 * 100], "gap"),
    (lambda rows, lo, hi: [dict(r, prev_boundary_height="") for r in rows], "no prev_boundary_height"),
    (lambda rows, lo, hi: [r for r in rows if r["boundary_height"] > lo + 30000], "starts at"),
    (lambda rows, lo, hi: [r for r in rows if r["boundary_height"] <= hi], "ends at"),
])
def test_every_epoch_burn_falls_back_unless_the_chain_covers_the_window(raw, break_it, why):
    baseline_macro(raw)
    rows, _, _, _ = every_epoch_setup(raw)
    lo, hi = dense_row(WIN0)["height"], dense_row(LATEST)["height"]
    write_mints(raw, break_it(rows, lo, hi))
    write_epochs(raw, monthly_epochs())
    reg, charts = run_all()
    assert reg.value("burn_method") == "sampled: one epoch per month"
    assert reg.value("burn_share_365") == pytest.approx(2.0) and reg.value("n_burn_epochs_365") == 12
    assert reg.value("burn_share_365_epochs") is None and why in reg.get("burn_share_365_epochs").null_reason
    c05 = charts["c05"]
    assert "every epoch boundary of the year" in c05.status_banner
    t = {r[0]: r for r in c05.table["rows"]}["Trailing year"]
    assert "every epoch: " in t[c05.table["columns"].index("note")] and why in t[c05.table["columns"].index("note")]


def test_every_epoch_burn_is_the_exact_block_identity():
    """On a toy chain (mint at each epoch's first block, a burn in every block), Σ boundary mints - ΔS over the
    window equals the burn of every block after the start sample through the end sample."""
    import random
    rnd = random.Random(7)
    supply_v, prev_boundary, chain, mints = 1000.0, None, [], []
    for h in range(1, 2001):
        burn = rnd.uniform(0, 0.5)
        mint = 30.0 + rnd.uniform(0, 1) if h % 97 == 0 else 0.0
        supply_v += mint - burn
        chain.append((h, supply_v, burn))
        if mint:
            mints.append({"height": h, "minted": mint, "epoch_seconds": 1.0, "time": None, "prev": prev_boundary,
                          "mint_rate": None})
            prev_boundary = h
    sample = {h: {"height": h, "total_supply_near": s, "time": T0 + timedelta(seconds=h)} for h, s, _ in chain}
    r0, r1 = sample[150], sample[1900]
    out, why = supply.epoch_burn(mints, r0, r1)
    assert why == "" and out["n"] == sum(1 for m in mints if 150 < m["height"] <= 1900)
    assert out["burnt"] == pytest.approx(sum(b for h, _, b in chain if 150 < h <= 1900), abs=1e-9)
    assert out["share"] == pytest.approx(out["burnt"] / out["minted"])
    # a boundary missing in the middle breaks the chain: no partial sum
    holed = [m for m in mints if m["height"] != 97 * 10]
    assert supply.epoch_burn(holed, r0, r1)[0] is None


def test_sampled_burn_needs_an_epoch_in_every_month(raw):
    # 2026-03 has no sampled epoch: the trailing year is not "the year" from the samples; it falls back to the
    # upper bound, while calendar years with every month sampled stay sampled
    baseline_history(raw)
    baseline_macro(raw)
    write_epochs(raw, monthly_epochs(skip=date(2026, 3, 1)))
    reg, charts = run_all()
    assert reg.value("burn_method") == "upper bound"
    assert reg.get("burn_share_365").formula_id == "F-burn-ub"
    assert reg.value("n_burn_epochs_365_sampled") is None and "gap" in reg.get("n_burn_epochs_365_sampled").null_reason
    assert reg.value("n_burn_epochs_365") is None and "upper bound" in reg.get("n_burn_epochs_365").null_reason
    cols = charts["c05"].table["columns"]
    assert {r[0]: r for r in charts["c05"].table["rows"]}["2025"][cols.index("sampled_share_pct")] == \
        pytest.approx(2.0)
    methods = {r[0]: r[3] for r in charts["c05"].table["rows"]}
    assert methods["Trailing year"] == "upper bound" and methods["2025"] == "sampled: one epoch per month"


def test_sampled_share_above_the_upper_bound_is_counted_not_clamped(raw):
    baseline_history(raw)
    baseline_macro(raw)
    eps = monthly_epochs()
    for e in eps:
        if e["sample_date"].startswith("2024"):
            e["burnt_in_epoch_near"] = 90.0      # every 2024 epoch burns 90 % of its mint
    write_epochs(raw, eps)
    reg, charts = run_all()
    c05 = charts["c05"]
    rows = {r[0]: r for r in c05.table["rows"]}
    assert rows["2024"][c05.table["columns"].index("sampled_share_pct")] == pytest.approx(90.0)
    above = [a["x"] for a in c05.annotations if a["label"] == "Sample above the bound"]
    assert above == ["2024"]


def test_mint_check_compares_time_based_epochs_only(raw):
    baseline_history(raw)
    write_epochs(raw, [
        epoch_row(date(2021, 4, 1), 100.0, 1.0, protocol=42, rate=0.05, mint_rate=0.0631),   # block-count reward
        epoch_row(date(2021, 7, 1), 100.0, 1.0, protocol=45, rate=0.05, mint_rate=0.0500),
        epoch_row(date(2023, 12, 1), 100.0, 1.0, protocol=63, rate=0.05, mint_rate=0.0391),
        epoch_row(date(2026, 1, 1), 100.0, 1.0, protocol=82, rate=0.025, mint_rate=0.0249)])
    reg, charts = run_all()
    assert reg.value("mint_check_from") == "2021-07-01"
    # the trailing year holds one of the sampled epochs (2026-01-01)
    assert reg.value("n_mint_epochs_365") == 1
    assert reg.value("mint_check_365_method") == "sampled: one epoch per month"
    assert reg.value("mint_vs_ceiling_median_365_pp") == pytest.approx(-0.01, abs=1e-6)
    assert reg.get("n_mint_epochs_365").formula_id == "F-mint-check"
    rows = {r[1]: r for r in charts["c02"].table["rows"] if r[0] == "epoch mint"}
    assert rows["2021-04-01"][7].startswith("block-count") and rows["2021-04-01"][8] is None
    assert rows["2021-07-01"][8] == pytest.approx(0.0, abs=1e-4)          # ceiling minus mint
    assert rows["2023-12-01"][8] == pytest.approx(1.09, abs=1e-4)
    assert rows["2026-01-01"][8] == pytest.approx(0.01, abs=1e-4)
    assert "epochs minted at the ceiling" in said(charts["c02"])


def test_epoch_hours_from_epoch_mints(raw):
    baseline_history(raw)
    last = datetime(2026, 11, 10, 20, tzinfo=UTC)
    mints = [{"boundary_height": 1000 + i, "block_time_utc": (last - timedelta(hours=7 * i)).isoformat(),
              "minted_near": "100", "epoch_seconds": 25000 + 100 * i, "supply_before_near": "1000000000",
              "annualized_mint_rate": "0.025"} for i in range(60)]
    mints.append({"boundary_height": 1, "block_time_utc": (last - timedelta(days=40)).isoformat(),
                  "minted_near": "100", "epoch_seconds": 99999, "supply_before_near": "1",
                  "annualized_mint_rate": "0.025"})     # older than thirty days: not in the median
    write_csv(raw, "rpc", "epoch_mints", ["boundary_height", "block_time_utc", "minted_near", "epoch_seconds",
                                          "supply_before_near", "annualized_mint_rate"], mints)
    reg, _ = run_all()
    assert reg.value("epoch_hours_median_30d") == pytest.approx(statistics.median(25000 + 100 * i for i in range(60))
                                                                / 3600, abs=0.01)
    assert "60 rpc/epoch_mints.csv boundaries" in reg.get("epoch_hours_median_30d").inputs[0]


def test_burn_exact_from_bigquery_and_reconciliation(raw):
    rows = baseline_history(raw)
    baseline_macro(raw)
    supply_by_day = {datetime.fromisoformat(r["block_time_utc"]).date(): float(r["total_supply_near"])
                     for r in rows}
    flows = []
    d = date(2024, 1, 1)
    while d < LATEST:   # every day has an end-of-day supply: the curve at the day's last block, plus 0.5 NEAR
        eod = 1e9 * math.exp(K * years(target_time(d + timedelta(days=1)))) + 0.5
        flows.append({"block_date": d.isoformat(), "eod_supply_near": f"{eod:.6f}",
                      "minted_near": "100", "burnt_near": "2", "epoch_boundaries": 2})
        d += timedelta(days=1)
    write_csv(raw, "bigquery", "daily_supply_flows",
              ["block_date", "eod_supply_near", "minted_near", "burnt_near", "epoch_boundaries"], flows)
    write_csv(raw, "bigquery", "fee_burn_sampled", ["block_date", "gross_fee_burn_near", "gas_burnt_tgas"],
              [{"block_date": "2024-03-01", "gross_fee_burn_near": "3", "gas_burnt_tgas": "1"},
               {"block_date": "2024-09-01", "gross_fee_burn_near": "5", "gas_burnt_tgas": "1"}])
    reg, charts = run_all()
    assert reg.value("burn_method") == "exact"
    assert reg.get("burn_share_365").formula_id == "F-burnshare"
    assert reg.value("burn_share_365") == pytest.approx(2.0)
    assert reg.value("mint_per_day_365") == pytest.approx(100.0)
    assert reg.value("bq_max_date") == (LATEST - timedelta(days=1)).isoformat()
    matched = [k for k in supply_by_day if date(2024, 1, 1) <= k < LATEST and k != LATEST]
    assert reg.value("n_overlap") == len(matched)
    assert reg.value("max_abs_diff_near") == pytest.approx(0.5, abs=1e-3)
    c16 = charts["c16"]
    assert c16.status == "ok" and c16.series and c16.to_json()
    methods = {r[0]: r[3] for r in charts["c05"].table["rows"]}
    assert methods["2024"] == "exact" and methods["2023"] == "upper bound"
    cols = charts["c05"].table["columns"]
    gross = {r[0]: (r[cols.index("bq_gross_fee_burn_per_day_near")], r[cols.index("bq_gross_fee_burn_days_sampled")])
             for r in charts["c05"].table["rows"]}
    assert gross["2024"] == (4.0, 2) and gross["2023"] == (None, 0)     # cross-check only: the method stays exact
    assert any(q.file == "sql/fee_burn.sql" for q in charts["c05"].queries)


def test_c16_empty_state_without_bigquery(raw):
    baseline_history(raw)
    reg, charts = run_all()
    c16 = charts["c16"]
    assert c16.status == "unavailable"
    assert c16.empty_state == {"reason": "BigQuery not configured",
                               "needs": "GCP_PROJECT in .env + gcloud auth application-default login",
                               "fix_command": "make check-bq && make data && make charts"}
    assert c16.empty_state == {"reason": supply.BQ_NOT_CONFIGURED, "needs": supply.BQ_NEEDS,
                               "fix_command": supply.BQ_FIX}
    sql = [q for q in c16.queries if q.kind == "sql"]
    assert sql and "MAX(block_date)" in sql[0].body_or_sql and sql[0].file == "sql/freshness.sql"
    for key in ("bq_max_date", "max_abs_diff_near", "n_overlap"):
        assert reg.value(key) is None and "BigQuery not configured" in reg.get(key).null_reason
    c16.to_json()


# ---------------------------------------------------------------------------------------------------------
# staking
# ---------------------------------------------------------------------------------------------------------

def test_lst_yields_and_spreads(raw):
    baseline_history(raw)
    baseline_macro(raw)
    reg, _ = run_all()
    y_st, y_li = 4.5, 4.0
    assert reg.value("lst_start_date") == LST_FROM.isoformat()
    assert reg.value("y_st_365") == pytest.approx(y_st, abs=1e-6)
    assert reg.value("y_li_365") == pytest.approx(y_li, abs=1e-6)
    assert reg.value("y_lst_365") == pytest.approx((y_st + y_li) / 2, abs=1e-6)
    assert reg.value("y_lst_90") == pytest.approx((y_st + y_li) / 2, abs=1e-6)
    # like-for-like: compounded DFF (6 % all year) over the same dates, annualized on 365 days
    assert reg.value("spread_adj_365") == pytest.approx(4.25 - ffc(6.0) * 100, abs=1e-4)
    assert reg.value("spread_adj_90") == pytest.approx(4.25 - ffc(6.0) * 100, abs=1e-4)
    assert reg.value("effr_mean_365") == pytest.approx(6.0)
    assert reg.value("spread_naive_365") == pytest.approx(4.25 - 6.0, abs=1e-6)
    assert reg.value("effr_latest") == pytest.approx(6.0)


def test_lst_mean_falls_back_to_the_one_lst_that_exists(raw):
    rows = baseline_history(raw)
    for r in rows:
        r["linear_price"] = ""
    write_history(raw, rows)
    reg, _ = run_all()
    assert reg.value("y_lst_365") == pytest.approx(4.5, abs=1e-6)
    assert reg.value("y_li_365") is None


def test_spread_window_ends_where_dff_ends(raw):
    baseline_history(raw)
    baseline_macro(raw, dff_last=date(2026, 11, 5))     # FRED lags: no DFF for Nov 6..9
    reg, _ = run_all()
    n = reg.get("spread_adj_365")
    assert n.value == pytest.approx(4.25 - ffc(6.0) * 100, abs=1e-4)
    assert any("2026-11-01" in i for i in n.inputs)      # window ends at the latest sample DFF covers
    assert reg.value("y_lst_365") is not None


def test_yields_and_spreads_share_one_window(raw):
    """y_lst_x and spread_adj_x describe the same dates: both end at the latest LST sample DFF covers."""
    baseline_history(raw)
    baseline_macro(raw, dff_last=date(2026, 11, 5))
    m = staking.build_model()
    assert m.end["date"] == date(2026, 11, 1) and m.latest["date"] == LATEST
    for w, adj in ((m.y365, m.adj365), (m.y90, m.adj90)):
        assert (w.r0["date"], w.r1["date"]) == (adj.r0["date"], adj.r1["date"])
    reg, _ = run_all()
    for days in ("365", "90"):
        y, sp = reg.get(f"y_lst_{days}"), reg.get(f"spread_adj_{days}")
        assert y.value is not None and set(y.inputs) <= set(sp.inputs)    # same edge samples, same length
    refs = {key: {re.sub(r"^(LST prices|total_supply_near) ", "", i) for i in reg.get(key).inputs
                  if re.match(r"(LST prices|total_supply_near) ", i)} for key in ("y_lst_365", "g_y_lst_365")}
    assert len(refs["y_lst_365"]) == 2 and refs["g_y_lst_365"] == refs["y_lst_365"]   # supply growth, same samples


def test_without_dff_yields_end_at_the_latest_sample(raw):
    baseline_history(raw)
    baseline_macro(raw)
    (raw / "fred" / "DFF.csv").unlink()
    m = staking.build_model()
    assert m.end is m.latest and m.y365 is not None and m.adj365 is None


def test_dff_gap_makes_spread_null_not_approximated(raw):
    baseline_history(raw)
    baseline_macro(raw)
    rows = daily(date(2020, 1, 1), date(2026, 11, 9), lambda d: None if d == date(2026, 3, 3) else dff_value(d))
    write_fred(raw, "DFF", rows)
    reg, _ = run_all()
    assert reg.value("spread_adj_365") is None
    assert reg.value("spread_naive_365") is not None   # uses EFFR, unaffected


def test_formula_yield_haircut_and_c10_grid(raw):
    baseline_history(raw)
    baseline_macro(raw)
    reg, charts = run_all()
    formula = 0.9 * 0.025 / 0.40
    h = 0.0425 / formula
    assert reg.value("y_formula_365") == pytest.approx(formula * 100, abs=1e-6)
    assert reg.value("lst_haircut") == pytest.approx(h, abs=1e-4)
    assert reg.value("sep_year") == "2027"
    assert reg.value("sep_median") == pytest.approx(3.9)
    assert reg.value("sep_longer_run") == pytest.approx(3.2)
    y_s60 = 0.9 * 0.025 / 0.60 * h * 100
    assert reg.value("y_s60") == pytest.approx(y_s60, abs=1e-3)
    c10 = charts["c10"]
    assert c10.status == "ok" and len(c10.table["rows"]) == 3
    assert {r[0] for r in c10.table["rows"]} == {"On chain now"}
    cell = {r[2]: r[4] for r in c10.table["rows"]}
    assert cell["Staked share now"] == pytest.approx(0.9 * 0.025 / 0.40 * h * 100, abs=1e-3)
    assert cell["Highest staked share observed"] == pytest.approx(0.9 * 0.025 / 0.45 * h * 100, abs=1e-3)
    assert cell["Hypothetical staked share"] == pytest.approx(y_s60, abs=1e-3)
    assert {r[2]: r[5] for r in c10.table["rows"]}["Staked share now"] == \
        pytest.approx(cell["Staked share now"] - 3.9, abs=1e-3)
    c10.to_json()


def test_formula_yield_null_when_samples_leave_a_gap(raw):
    rows = [r for r in baseline_history(raw) if not ("2026-02-01" <= r["date"] <= "2026-05-01")]
    write_history(raw, rows)
    reg, charts = run_all()
    assert reg.value("y_formula_365") is None and "gap" in reg.get("y_formula_365").null_reason
    assert reg.value("lst_haircut") is None and reg.value("y_s60") is None
    assert charts["c10"].status == "unavailable"
    charts["c10"].to_json()


def test_quarterly_spread_statistics(raw):
    # samples only at quarter starts, with chosen LST prices and a DFF constant within each quarter; the issuance
    # rule changes between the first two samples, so the statistics read the four quarters from cut_date on
    quarters = [date(2022, 4, 1), date(2022, 7, 1), date(2022, 10, 1), date(2023, 1, 1), date(2023, 4, 1),
                date(2023, 7, 1)]
    q_rates = [0.5, 2.0, 3.5, 4.5, 5.0]                 # DFF within each complete quarter
    q_yield = [0.090, 0.085, 0.070, 0.045, 0.052]      # stNEAR = LiNEAR annual yield per quarter
    price, rows = 1.0, []
    for i, q in enumerate(quarters):
        t = target_time(q)
        r = history_row(q, t, int((t - T0).total_seconds()), lst=True, rate=0.05 if i == 0 else 0.025)
        if i:
            span = (t - target_time(quarters[i - 1])).total_seconds() / 86400
            price *= (1 + q_yield[i - 1]) ** (span / 365.25)
        r["stnear_price"] = r["linear_price"] = f"{price:.15f}"
        rows.append(r)
    write_history(raw, rows)

    def dff(d: date) -> float:
        for q0, q1, v in zip(quarters, quarters[1:], q_rates):
            if q0 <= d < q1:
                return v
        return 5.0
    write_fred(raw, "DFF", daily(date(2022, 1, 1), date(2023, 7, 31), dff))
    write_fred(raw, "DFEDTARU", daily(date(2022, 1, 1), date(2023, 7, 31),
                                      lambda d: 0.25 if d < date(2022, 5, 5) else 3.25 if d < date(2022, 12, 15)
                                      else 5.25))
    reg, charts = run_all()
    assert reg.value("cut_date") == quarters[1].isoformat()
    spreads = [(y - ffc(r)) * 100 for y, r in zip(q_yield, q_rates)][1:]
    assert reg.value("n_q_rule") == 4
    assert reg.value("spread_q_sd_rule") == pytest.approx(statistics.stdev(spreads), abs=1e-4)
    assert reg.value("corr_levels_rule") == pytest.approx(
        statistics.correlation(q_yield[1:], [r / 100 for r in q_rates[1:]]), abs=1e-4)
    assert reg.value("fed_range_q_bp_rule") == pytest.approx(200)   # the step before the cut is outside the range
    c12 = charts["c12"]
    assert [p[0] for p in c12.series[0].points] == ["2022Q3", "2022Q4", "2023Q1", "2023Q2"]
    assert [p[1] for p in c12.series[0].points] == pytest.approx(spreads, abs=1e-3)
    c12.to_json()


def test_quarter_missing_an_edge_is_listed_not_bridged(raw):
    rows = [r for r in baseline_history(raw) if not ("2026-03-25" <= r["date"] <= "2026-04-07")]
    write_history(raw, rows)
    baseline_macro(raw)
    reg, charts = run_all()
    labels = [p[0] for p in charts["c12"].series[0].points]
    assert labels and "2026Q1" not in labels and "2026Q2" not in labels
    assert "2026Q1" in charts["c12"].coverage["gaps"]
    assert charts["c12"].status == "partial"
    assert reg.value("n_q_rule") == len(labels)


def test_fed_turns_and_annotation_merge():
    items = [{"x": "2022-03-17", "label": "a", "kind": "fed_turn"},
             {"x": "2022-04-01", "label": "b", "kind": "data_boundary"},
             {"x": "2024-09-19", "label": "c", "kind": "fed_turn"}]
    merged = staking.merge_annotations(items)
    assert merged[0]["label"] == "a; b" and merged[0]["merged"] and len(merged) == 2


# ---------------------------------------------------------------------------------------------------------
# holders
# ---------------------------------------------------------------------------------------------------------

def test_share_yield_windows(raw):
    baseline_history(raw)
    baseline_macro(raw)
    reg, charts = run_all()
    staker = 1.0425 / 1.0245 - 1
    assert reg.value("staker_share_365") == pytest.approx(staker * 100, abs=1e-4)
    assert reg.value("nonstaker_365") == pytest.approx((1 / 1.0245 - 1) * 100, abs=1e-4)
    c03 = charts["c03"]
    assert c03.status == "ok" and "transfer" in c03.title
    assert c03.coverage["n"] == 5 and c03.coverage["gaps"] == []
    assert all(y is not None and y > 0 for _, y in c03.series[0].points)     # every window: the staker gained share
    assert [p[0] for p in c03.series[0].points][-1] == "Since the ceiling cut"
    assert {r[0]: r for r in c03.table["rows"]}["Since the ceiling cut"][2] == LATEST.isoformat()


def test_wide_cut_bracket_leaves_cut_windows_out(raw):
    rows = [r for r in baseline_history(raw) if not ("2025-06-01" <= r["date"] <= "2025-11-30")]
    write_history(raw, rows)
    baseline_macro(raw)
    reg, charts = run_all()
    assert reg.value("cut_height") is None
    rows = {r[0]: r for r in charts["c03"].table["rows"]}
    assert rows["Since the ceiling cut"][6] is None and "bracketed" in rows["Since the ceiling cut"][-1]
    assert charts["c03"].status == "partial"
    for c in charts.values():
        c.to_json()


# ---------------------------------------------------------------------------------------------------------
# chart contract
# ---------------------------------------------------------------------------------------------------------

# Secrets a chart's reproduce panel (Query.body_or_sql, printed verbatim on the site) must never carry. The check looks
# for credential shapes and for the values of the secret env vars themselves, so a plain word such as the column
# tokens_burnt or the contract usdt.tether-token.near does not trip it.
SECRET_ENV = ("GCP_PROJECT", "NEAR_INTENTS_JWT", "NEAR_AI_API_KEY")
SECRET_ENV_NAME = re.compile(r"(?:KEY|TOKEN|JWT|SECRET|PASSWORD)$")
CREDENTIAL_SHAPES = (
    ("bearer", re.compile(r"bearer", re.IGNORECASE)),   # unchanged: no Authorization scheme at all in a NEAR chart
    ("authorization header", re.compile(r"\bauthorization\s*:", re.IGNORECASE)),
    ("credential assignment", re.compile(
        r"(?<![a-z0-9])[a-z0-9_-]*(?:token|api[_-]?key|apikey|secret|password|passwd|jwt|credentials?)"
        r"[\"']?\s*[:=]\s*[\"']?[a-z0-9._~+/=-]{8,}", re.IGNORECASE)),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("Google OAuth token", re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("NEAR private key", re.compile(r"\bed25519:[1-9A-HJ-NP-Za-km-z]{80,}")),
)


def credential_leaks(text: str) -> list[str]:
    """Names of the credential shapes and secret env vars found in text; never the matched text itself."""
    found = [name for name, pattern in CREDENTIAL_SHAPES if pattern.search(text)]
    names = sorted(set(SECRET_ENV) | {name for name in os.environ if SECRET_ENV_NAME.search(name)})
    return found + [f"${name}" for name in names if len(os.environ.get(name) or "") >= 6 and os.environ[name] in text]


def _check_chart_contract(charts: dict) -> None:
    for c in charts.values():
        data = c.to_json()
        assert "y2" not in data["y"]
        typed = re.sub(r"\{[a-z0-9_]+(?::[a-z0-9,]+)?\}", "", c.title_template + c.sentence_template)   # placeholders are keys
        assert not re.search(r"\d", typed), (c.id, "digit typed into a template")
        assert not re.search(r"\d", c.status_banner or ""), c.id
        for s in c.series:
            assert not re.search(r"\d", s.label), (c.id, s.label)
        assert c.code_ref.startswith("pipeline/models/") and "::" in c.code_ref
        assert c.queries and c.sources and c.sentence
        assert len(dump(data)) <= 60_000, (c.id, len(dump(data)))
        for q in c.queries:
            if q.kind == "jsonrpc":
                body = json.loads(q.body_or_sql)
                assert body["jsonrpc"] == "2.0" and body["method"]
            leaks = credential_leaks(q.body_or_sql)
            assert not leaks, (c.id, q.kind, leaks)


@pytest.mark.parametrize("text", [
    "GET https://api.example.org/v1/volume?token=3f9a8b7c6d5e4f30a1b2",
    'curl -H "Authorization: Bearer 3f9a8b7c6d5e4f30" https://api.example.org/v1/volume',
    'Authorization: Basic dXNlcjpwYXNzd29yZA==',
    '{"jsonrpc": "2.0", "api_key": "3f9a8b7c6d5e4f30a1b2", "method": "query"}',
    "NEAR_INTENTS_JWT=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0LXVzZXIifQ.c2lnbmF0dXJlLXZhbHVlLXg",
    "-- partner key eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0LXVzZXIifQ.c2lnbmF0dXJlLXZhbHVlLXg",
    "x-goog-api-key AIzaSyA1234567890abcdefghijklmnopqrstuv",
    "access_token: ya29.a0AfH6SMBabcdefghijklmnopqrstuvwxyz",
    "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----",
    "signer ed25519:" + "3vQB7B6MrGQZaxCuFg4oh" * 4,
    "SELECT 1 -- client_secret = 'Zq8xT2pL9vR4mN6b'",
])
def test_credential_check_still_flags_secret_like_strings(text):
    assert credential_leaks(text)


def test_credential_check_flags_the_value_of_a_secret_env_var(monkeypatch):
    monkeypatch.setenv("NEAR_INTENTS_JWT", "opaque-partner-credential-42")
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "zz-other-credential-17")
    assert credential_leaks("GET /v1/volume HTTP/1.1\nX-Auth opaque-partner-credential-42") == ["$NEAR_INTENTS_JWT"]
    assert credential_leaks("select 'zz-other-credential-17'") == ["$SOME_SERVICE_TOKEN"]


def test_credential_check_passes_the_names_the_substring_search_tripped_on():
    sql = ("SELECT block_date, SUM(CAST(tokens_burnt AS FLOAT64)) AS tokens_burnt, "
           "SUM(CAST(receipt_conversion_tokens_burnt AS FLOAT64)) FROM t WHERE contract IN ('usdt.tether-token.near')")
    rpc = json.dumps({"jsonrpc": "2.0", "id": "dontcare", "method": "query",
                      "params": {"request_type": "call_function", "finality": "final",
                                 "account_id": "usdt.tether-token.near", "method_name": "ft_metadata",
                                 "args_base64": "e30=", "token_id": "nep141:wrap.near"}})
    assert credential_leaks(sql) == [] and credential_leaks(rpc) == []


def test_chart_contract_rejects_a_chart_query_that_carries_a_secret(raw):
    baseline_history(raw)
    baseline_macro(raw)
    _, charts = run_all()
    chart = next(iter(charts.values()))
    chart.queries = [*chart.queries, type(chart.queries[0])("http", "https://api.example.org/v1/volume",
                                                            "GET /v1/volume?api_key=3f9a8b7c6d5e4f30a1b2")]
    with pytest.raises(AssertionError, match="credential assignment"):
        _check_chart_contract({chart.id: chart})


def test_chart_contract_on_synthetic_data(raw):
    baseline_history(raw)
    baseline_macro(raw)
    _, charts = run_all()
    _check_chart_contract(charts)
    assert charts["c01"].status == "ok"
    lst_q = [q for q in charts["c01"].queries if "call_function" in q.body_or_sql]
    assert {json.loads(q.body_or_sql)["params"]["method_name"] for q in lst_q} == {"get_st_near_price", "ft_price"}
    assert all(json.loads(q.body_or_sql)["params"]["args_base64"] == "e30=" for q in lst_q)


def test_models_are_deterministic(raw):
    baseline_history(raw)
    baseline_macro(raw)
    reg1, charts1 = run_all()
    reg2, charts2 = run_all()
    assert dump(reg1.to_json()) == dump(reg2.to_json())
    assert all(dump(charts1[k].to_json()) == dump(charts2[k].to_json()) for k in charts1)


# ---------------------------------------------------------------------------------------------------------
# real data/raw/: sanity bounds. Skip only when the input is not on disk yet.
# ---------------------------------------------------------------------------------------------------------

REAL_SAMPLES = REPO / "data" / "raw" / "rpc" / "history_samples.csv"


@pytest.fixture(scope="module")
def real():
    if not REAL_SAMPLES.exists():
        pytest.skip("data/raw/rpc/history_samples.csv absent")
    reg, charts = run_all()
    return reg, charts


def _bounded(reg: Registry, key: str, lo: float | None, hi: float | None) -> None:
    n = reg.get(key)
    if n.value is None:
        pytest.skip(f"{key} not computable from the data on disk yet: {n.null_reason}")
    if lo is not None:
        assert n.value >= lo, (key, n.value, n.inputs)
    if hi is not None:
        assert n.value <= hi, (key, n.value, n.inputs)


def test_real_g_365_bound(real):
    _bounded(real[0], "g_365", 2.2, 2.8)


def test_real_y_lst_bound(real):
    _bounded(real[0], "y_lst_365", 3.5, 4.8)


def test_real_s_now_bound(real):
    _bounded(real[0], "s_now", 38, 53)


def test_real_burn_share_bound(real):
    _bounded(real[0], "burn_share_365", None, 10)


def test_real_rule_quarters_start_on_or_after_the_cut(real):
    reg = real[0]
    cut = reg.value("cut_date")
    if cut is None:
        pytest.skip(f"cut_date not computable from the data on disk yet: {reg.get('cut_date').null_reason}")
    quarters = staking.rule_quarters(staking.build_model(), cut)
    assert reg.value("n_q_rule") == len(quarters)
    assert all(w.r0["date"].isoformat() >= cut for w in quarters)


def test_real_effr_latest_is_the_last_fred_row(real):
    rows = [r for r in csv.DictReader((REPO / "data" / "raw" / "fred" / "EFFR.csv").open()) if r["value"].strip()]
    assert real[0].value("effr_latest") == pytest.approx(float(rows[-1]["value"]))


def test_real_ceiling_reads(real):
    reg = real[0]
    assert reg.value("i_max_now") == pytest.approx(2.5)


def test_real_charts_meet_the_contract(real):
    _check_chart_contract(real[1])


def test_real_trailing_windows_end_at_the_latest_full_day_and_span_exactly(real):
    reg = real[0]
    rows, _ = load.rpc_merged()
    end = max(r["date"] for r in rows if r["full_day"]).isoformat()
    assert reg.value("latest_rpc_date") == end
    assert any(f"{end} (block" in i for i in reg.get("g_365").inputs)
    for key, span in (("y_lst_365", "365.000 days"), ("y_lst_90", "90.000 days"), ("spread_adj_365", "365.000 days"),
                      ("spread_adj_90", "90.000 days")):
        n = reg.get(key)
        if n.value is None:
            pytest.skip(f"{key} not computable from the data on disk yet: {n.null_reason}")
        assert span in n.inputs, (key, n.inputs)


def test_real_cut_and_hsp027_come_from_protocol_changes(real):
    path = REPO / "data" / "raw" / "rpc" / "protocol_changes.csv"
    if not path.exists():
        pytest.skip("data/raw/rpc/protocol_changes.csv absent")
    reg, charts = real
    rows = list(csv.DictReader(path.open()))
    cut = [r for r in rows if r["param"] == "max_inflation_rate"][-1]
    assert reg.value("cut_height") == int(cut["first_height"])
    assert reg.value("cut_date") == cut["block_time_utc"][:10]
    gas = [r for r in rows if r["param"] == "burnt_gas_reward" and float(r["new_value"]) == 0][-1]
    assert reg.value("hsp027_height") == int(gas["first_height"])
    assert reg.value("hsp027_from") == gas["block_time_utc"][:10]
    assert reg.value("ceiling_gap_dir") is not None
    assert all(r[7] == "exact" for r in charts["c02"].table["rows"] if r[0] == "calendar year")


def test_real_burn_is_exact_with_the_sample_outliers_and_the_upper_bound_kept(real):
    if (REPO / "data" / "raw" / "bigquery" / "daily_supply_flows.csv").exists():
        pytest.skip("BigQuery daily_supply_flows on disk: the exact method takes precedence")
    if not (REPO / "data" / "raw" / "rpc" / "epoch_history.csv").exists():
        pytest.skip("data/raw/rpc/epoch_history.csv absent")
    reg, charts = real
    if reg.value("burn_share_365_epochs") is None:
        pytest.skip("rpc/epoch_mints.csv does not cover the trailing year yet: "
                    + reg.get("burn_share_365_epochs").null_reason)
    assert reg.value("burn_method") == "exact: every epoch boundary"
    # ~6.9-hour epochs: a year holds about 1,270 boundaries
    assert 1200 <= reg.value("n_burn_epochs_365") <= 1340
    assert 12 <= reg.value("n_burn_epochs_365_sampled") <= 13
    _bounded(reg, "burn_share_365", 0.5, 5)
    # the exact share cannot exceed the bound from supply alone (it counts unminted rewards as burn)
    assert reg.value("burn_share_365") <= reg.value("burn_share_365_ub")
    assert abs(reg.value("burn_share_365") - reg.value("burn_share_365_sampled")) < 1.5
    # the mint a day is the ceiling's 2.5 % of supply less a little for offline validators
    assert 0.97 < reg.value("mint_per_day_365") * 365 / (0.025 * reg.value("supply_now")) < 1.0
    labels = {r[0] for r in charts["c05"].table["rows"]}
    assert {"epoch 2023-12-01", "epoch 2024-09-01"} <= labels


def test_real_epoch_mints_chain_covers_the_window(real):
    path = REPO / "data" / "raw" / "rpc" / "epoch_mints.csv"
    rows = list(csv.DictReader(path.open())) if path.exists() else []
    if not rows or not rows[0].get("prev_boundary_height"):
        pytest.skip("rpc/epoch_mints.csv absent or written before the chain links")
    heights = [int(r["boundary_height"]) for r in rows]
    assert heights == sorted(heights)
    assert all(int(r["prev_boundary_height"]) == below for r, below in zip(rows[1:], heights))
    # every epoch's seconds add up to the time the chain spans, and each mint is at the rate in force (or below it,
    # when validators fell under the online threshold)
    times = [datetime.fromisoformat(r["block_time_utc"]) for r in rows]
    secs = sum(float(r["epoch_seconds"]) for r in rows[1:])
    assert abs(secs - (times[-1] - times[0]).total_seconds()) < 60
    # the ceiling in force is the one during the epoch a boundary pays for: the window now reaches back across the
    # 2025-10 ceiling cut, so each rate is held to its own ceiling (same 0.5 pp floor and 2e-4 median as before)
    segs = supply.build_model().segs
    at = dict(zip(heights, times))
    gaps, unobserved = [], []
    for r in rows[1:]:
        t, t_prev = at[int(r["boundary_height"])], at[int(r["prev_boundary_height"])]
        mid = t_prev + (t - t_prev) / 2
        if mid >= segs[-1].t1:   # paid for an epoch after the latest sample that read the ceiling
            unobserved.append(r["boundary_height"])
            continue
        ceil = supply.ceiling_at(segs, mid)
        assert ceil is not None, r["boundary_height"]
        x = float(r["annualized_mint_rate"])
        assert ceil - 0.005 < x <= ceil + 1e-9, (r["boundary_height"], x, ceil)
        gaps.append(x - ceil)
    assert statistics.median(gaps) == pytest.approx(0, abs=2e-4)
    assert len(unobserved) <= 5, unobserved   # at most about a day and a half of epochs past the history tip


def test_real_mint_check_365_uses_every_boundary(real):
    """The mint check over the trailing year: every epoch boundary when epoch_mints covers g_365's window. The epoch counts
    must agree whenever the burn counts epochs; the exact BigQuery burn counts days, not epochs (n_burn_epochs_365 is
    null by design), so the mint check is then held to the every-boundary burn share computed beside it."""
    reg = real[0]
    _bounded(reg, "mint_vs_ceiling_median_365_pp", -0.1, 0.05)
    _bounded(reg, "mint_vs_ceiling_max_365_pp", None, 0.05)
    if reg.value("mint_check_365_method") == "every epoch boundary":
        _check_mint_epochs_match_burn(reg, supply.build_model().burn_trailing)


def _check_mint_epochs_match_burn(reg: Registry, burn: supply.BurnCol | None) -> None:
    method, n_mint = reg.value("burn_method"), reg.value("n_mint_epochs_365")
    if method == supply.METHOD_LABEL["epochs"]:
        assert n_mint == reg.value("n_burn_epochs_365"), (n_mint, reg.value("n_burn_epochs_365"))
    elif method == supply.METHOD_LABEL["exact"]:
        assert reg.value("n_burn_epochs_365") is None
        assert "does not count epochs" in reg.get("n_burn_epochs_365").null_reason
        assert burn is not None and burn.epochs_share is not None, "the every-boundary burn share is missing"
        assert n_mint == len(burn.boundaries), (n_mint, len(burn.boundaries))


def test_mint_epoch_check_catches_a_count_mismatch_under_either_exact_method():
    def registry(method: str, n_mint: int, n_burn: int | None) -> Registry:
        reg = Registry()
        supply.put(reg, "burn_method", supply.METHOD_LABEL[method], "text", "F-burnshare", [], [])
        supply.put(reg, "n_mint_epochs_365", n_mint, "count", "F-mint-check", [], [])
        supply.put(reg, "n_burn_epochs_365", n_burn, "count", "F-burn-epochs", [], [],
                   reason=f"the {supply.METHOD_LABEL[method]} method does not count epochs")
        return reg

    burn = supply.BurnCol("trailing", date(2025, 11, 10), date(2026, 11, 10), method="exact", epochs_share=0.015)
    burn.boundaries = [{}] * 1201
    _check_mint_epochs_match_burn(registry("epochs", 1201, 1201), burn)
    _check_mint_epochs_match_burn(registry("exact", 1201, None), burn)
    with pytest.raises(AssertionError):
        _check_mint_epochs_match_burn(registry("epochs", 1201, 1200), burn)
    with pytest.raises(AssertionError):
        _check_mint_epochs_match_burn(registry("exact", 1200, None), burn)
    with pytest.raises(AssertionError):
        _check_mint_epochs_match_burn(registry("exact", 1201, None), None)


def test_real_mint_check_and_epoch_hours(real):
    reg = real[0]
    assert reg.value("mint_check_from") == "2021-06-01"
    _bounded(reg, "epoch_hours_median_30d", 5, 9)
