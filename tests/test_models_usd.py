"""Group B model tests (macro, market, demand, derivatives, currencies, sweep).

Synthetic tests write small raw files into a tmp ROOT (config/load/numbers paths monkeypatched) and register
fake upstream (group A) keys in a fresh Registry. Sanity-bound tests read the real data/raw/ and skip when a
file is absent. Offline only.
"""

from __future__ import annotations

import csv
import json
import math
import re
import shutil
import statistics
import string
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import config, numbers
from pipeline.chartspec import dump
from pipeline.models import currencies, demand, derivatives, formulas as F, load, macro, market, sweep
from pipeline.numbers import Registry

REPO = Path(__file__).resolve().parents[1]
MODULES = (macro, market, demand, derivatives, currencies, sweep)
CONTRACT_KEYS = {
    "macro": "m2_yoy m2_month cpi_yoy cpi_month core_yoy pce_yoy pce_month gap_cpi gap_m2 gap_core gap_pce "
             "h1b_months_with_data h1b_months_expected h1b_months_below h1b_below_run h2_months_in_band_12 "
             "h2_months_with_data_12",
    "market": "rv_365_near rv_365_btc rv_365_eth rv_365_eur rv_90_near x_btc x_eur rv30_median mdd_365 sigma_d "
              "worst_day worst_day_date ratio_gap_to_day share_days_gt_spread n_windows_usd_1y pct_within_2pp_1y "
              "pct_beat_cash_1y near_close_latest latest_common_date",
    "demand": "intents_vol_30d intents_vol_90d_seq_ratio intents_vol_365d intents_series_start intents_fees_365d "
              "intents_latest_date",
    "derivatives": "n_venues_with_near_options n_option_venues_checked venues_unchecked vrp_btc vrp_eth",
    "currencies": "n_panel near_reading_a near_reading_b panel_median_abs_a panel_median_abs_b h5_now_narrower "
                  "h5_now_wider",
    "sweep": "nav_days_at_par_365 stnear_down_days n_lst_days unstake_hours_lo unstake_hours_hi "
             "unstake_hours_2021_lo unstake_hours_2021_hi nslp_near nslp_usd nslp_fee_now nslp_fee_min nslp_fee_max "
             "h6_rows_passed h6_rows_total h6_critical_failed h6n_rows13_pass h6n_row2_fail",
}
ALL_KEYS = {k for v in CONTRACT_KEYS.values() for k in v.split()}
STAMP = "2026-11-10T00:00:00+00:00"


# --- fixtures and synthetic raw writers ---------------------------------------------------------------------

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


def put_csv(root: Path, rel: str, header: list[str], rows: list[list], **meta) -> None:
    path = root / "data" / "raw" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows([["" if v is None else v for v in r] for r in rows])
    path.with_suffix(".meta.json").write_text(json.dumps({"fetched_at": STAMP, "source_url": "test", **meta}))


def put_json(root: Path, rel: str, payload) -> None:
    path = root / "data" / "raw" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    path.with_suffix(".meta.json").write_text(json.dumps({"fetched_at": STAMP, "source_url": "test"}))


def days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def month_starts(start: date, end: date):
    return macro.months(start, end)


def fred_monthly(root, sid, start, end, growth, blanks=()):
    rows = []
    for k, m in enumerate(month_starts(start, end)):
        rows.append([m.isoformat(), None if m in blanks else round(100 * (1 + growth) ** k, 8)])
    put_csv(root, f"fred/{sid}.csv", ["date", "value"], rows)


def fred_daily(root, sid, start, end, value, blanks=()):
    put_csv(root, f"fred/{sid}.csv", ["date", "value"],
            [[d.isoformat(), None if d in blanks else (value(d) if callable(value) else value)]
             for d in days(start, end)])


RPC_COLS = ["date", "height", "block_time_utc", "epoch_id", "total_supply_near", "validator_stake_near",
            "n_validators", "stake_epoch_start_height", "staked_ratio", "stnear_price", "linear_price",
            "treasury_near", "protocol_version", "max_inflation_rate"]
D0 = date(2020, 11, 1)


def rpc_history(root, dates, g=0.025, y=0.04, lst_from=date(2022, 4, 1), skip=(), rate_fn=None):
    rows = []
    for d in dates:
        if d in skip:
            continue
        t = (d - D0).days
        supply = 1e9 * (1 + g) ** (t / 365.25)
        rate = rate_fn(d) if rate_fn else (1 + y) ** ((d - lst_from).days / 365.25)
        lst = d >= lst_from
        rows.append([d.isoformat(), 1000 + t * 86400,
                     (datetime(d.year, d.month, d.day, tzinfo=timezone.utc) - timedelta(seconds=1)).isoformat(),
                     "E", supply, None, None, None, None, rate if lst else None, rate if lst else None,
                     None, 80, 0.025])
    put_csv(root, "rpc/history_samples.csv", RPC_COLS, rows)


DENSE_COLS = ["date", "height", "block_time_utc", "total_supply_near", "stnear_price", "linear_price"]


def rpc_dense(root, dates, g=0.025, y=0.04, lst_from=date(2022, 4, 1)):
    """rpc/dense_samples.csv rows: the same 00:00 UTC blocks rpc_history writes (identical on a shared date)."""
    rows = []
    for d in dates:
        t = (d - D0).days
        supply = 1e9 * (1 + g) ** (t / 365.25)
        rate = (1 + y) ** ((d - lst_from).days / 365.25)
        rows.append([d.isoformat(), 1000 + t * 86400,
                     (datetime(d.year, d.month, d.day, tzinfo=timezone.utc) - timedelta(seconds=1)).isoformat(),
                     supply, rate if d >= lst_from else None, rate if d >= lst_from else None])
    put_csv(root, "rpc/dense_samples.csv", DENSE_COLS, rows)


EPOCH_COLS = ["sample_date", "epoch_seconds", "minted_next_boundary_near", "burnt_in_epoch_near",
              "annualized_mint_rate", "protocol_version", "max_inflation_rate"]


def rpc_epochs(root, seconds_by_date: dict):
    put_csv(root, "rpc/epoch_history.csv", EPOCH_COLS,
            [[d.isoformat(), secs, 100.0, 1.0, 0.05, 45, 0.05] for d, secs in sorted(seconds_by_date.items())])


def closes(root, name, start, end, fn, skip=()):
    put_csv(root, f"market/{name}.csv", ["date", "open", "high", "low", "close", "volume_base", "volume_quote"],
            [[d.isoformat(), 1, 1, 1, fn(d), 1, 1] for d in days(start, end) if d not in skip])


def wiggle(scale: float, base: float = 10.0):
    """Deterministic, non-trivial daily price path."""
    return lambda d: round(base * math.exp(scale * math.sin(d.toordinal() * 1.7) + 0.0001 * d.toordinal() % 1), 8)


def fake_upstream(reg: Registry, **over) -> Registry:
    vals = {"g_365": (2.5, "%"), "g_y_lst_365": (2.5, "%"), "y_lst_365": (4.0, "%"), "spread_adj_365": (0.3, "pp"),
            "spread_adj_365_dir": ("above", "text"),
            "effr_mean_365": (3.6, "%"), "n_pos": (5, "count"), "n_w": (5, "count"),
            "cut_date": ("2025-10-30", "date"), "epoch_hours_median_30d": (7.0, "hours"),
            "lst_start_date": ("2022-04-01", "date"), "first_window_end": ("2021-11-01", "date"),
            "staker_share_365": (0.4, "%")}
    for k, v in over.items():
        vals[k] = v
    for k, (v, unit) in vals.items():
        if v is None:
            reg.put(k, None, unit, "FAKE", null_reason="fake null")
        else:
            reg.put(k, v, unit, "FAKE")
    return reg


def run_all(reg: Registry):
    for m in MODULES:
        m.compute(reg)
    charts = [c for m in MODULES for c in m.charts(reg)]
    for c in charts:
        dump(c.to_json())  # validates; raises on NaN / missing table / unresolved placeholders
    return {c.id: c for c in charts}


# --- empty data/raw -----------------------------------------------------------------------------------------


def said(chart) -> str:
    """Everything a chart card says in words: sentence, caveat and method notes."""
    return " ".join([chart.sentence, chart.caveat, *chart.notes])

def test_empty_raw_registers_every_key_and_charts_degrade(raw):
    reg = Registry()
    charts = run_all(reg)
    assert ALL_KEYS <= {k for k, _ in reg.items()}
    nulls = {k for k, n in reg.items() if n.value is None}
    assert {"rv_365_near", "gap_cpi", "intents_vol_30d", "vrp_btc", "h5_now_narrower", "nslp_near"} <= nulls
    for k, n in reg.items():
        assert n.value is not None or n.null_reason, k
    assert set(charts) == {"c06", "c07", "c08", "c09", "c11", "c13", "c14", "c15"}
    for cid in ("c06", "c07", "c08", "c09", "c11", "c13", "c15"):
        assert charts[cid].status == "unavailable", cid
        assert all(charts[cid].empty_state.values())
    assert charts["c14"].status in ("partial", "unavailable")


def test_empty_raw_with_fake_upstream_does_not_raise(raw):
    reg = fake_upstream(Registry())
    run_all(reg)
    assert reg.value("near_reading_a") == pytest.approx(1.5)
    assert reg.value("h5_now_narrower") is None  # no BIS panel


# --- macro ----------------------------------------------------------------------------------------------------

def macro_raw(root, blank_oct=True, skip=()):
    start, end = date(2019, 1, 1), date(2026, 10, 1)
    oct25 = (date(2025, 10, 1),) if blank_oct else ()
    fred_monthly(root, "M2SL", start, date(2026, 9, 1), 0.005)
    fred_monthly(root, "CPIAUCSL", start, end, 0.004, blanks=oct25)
    fred_monthly(root, "PCEPI", start, date(2026, 9, 1), 0.002)
    fred_monthly(root, "CPILFESL", start, end, 0.0025, blanks=oct25)
    rpc_history(root, month_starts(D0, date(2026, 11, 1)), skip=skip)


def test_macro_yoy_gaps_and_oct_2025_blank(raw):
    macro_raw(raw)
    reg = Registry()
    macro.compute(reg)
    assert reg.value("m2_yoy") == pytest.approx(100 * (1.005 ** 12 - 1), abs=1e-3)
    assert reg.value("m2_month") == "2026-09"
    # CPI 2026-10 exists but 2025-10 is blank: YoY for 2026-10 is null, so the latest CPI month is 2026-09.
    assert reg.value("cpi_month") == "2026-09"
    assert reg.value("cpi_yoy") == pytest.approx(100 * (1.004 ** 12 - 1), abs=1e-3)
    data = macro.analyze()
    assert data.yoy["cpi"][date(2025, 10, 1)] is None
    assert data.yoy["cpi"][date(2026, 10, 1)] is None
    assert data.yoy["core"][date(2026, 10, 1)] is None
    # NEAR g for month 2026-09 closes at the 2026-10-01 sample: exactly 2.5 %.
    assert data.g[date(2026, 9, 1)].value == pytest.approx(0.025, abs=1e-9)
    assert data.g[date(2026, 9, 1)].end == date(2026, 10, 1)
    assert reg.value("gap_cpi") == pytest.approx(100 * (1.004 ** 12 - 1) - 2.5, abs=1e-3)
    assert reg.value("gap_m2") == pytest.approx(100 * (1.005 ** 12 - 1) - 2.5, abs=1e-3)
    assert reg.value("gap_pce") == pytest.approx(100 * (1.002 ** 12 - 1) - 2.5, abs=1e-3)
    # H1b: M2 6.17 % > g 2.5 % in every month since the first window.
    assert reg.value("h1b_months_with_data") == reg.value("h1b_months_expected")
    assert reg.value("h1b_months_below") == reg.value("h1b_months_with_data")
    # H2: gap 2.41 pp is outside 1.5 +/- 0.5; 2025-10 has no data, so 11 of 12 months have data.
    assert reg.value("h2_months_with_data_12") == 11
    assert reg.value("h2_months_in_band_12") == 0
    chart = macro.inflation_measures(reg)
    dump(chart.to_json())
    assert chart.status == "ok"
    assert any(a["x"] == "2025-10-01" and a["kind"] == "data_boundary" for a in chart.annotations)
    cpi_pts = dict(map(tuple, next(s for s in chart.series if s.id == "usd_cpi").points))
    assert cpi_pts["2025-10-01"] is None  # the line breaks; never filled


def test_macro_h2_band_counts(raw):
    start = date(2019, 1, 1)
    fred_monthly(raw, "M2SL", start, date(2026, 9, 1), 0.005)
    fred_monthly(raw, "CPIAUCSL", start, date(2026, 9, 1), 0.0033, blanks=(date(2025, 10, 1),))
    fred_monthly(raw, "PCEPI", start, date(2026, 9, 1), 0.002)
    fred_monthly(raw, "CPILFESL", start, date(2026, 9, 1), 0.0025)
    rpc_history(raw, month_starts(D0, date(2026, 11, 1)))
    reg = Registry()
    macro.compute(reg)
    gap = 100 * (1.0033 ** 12 - 1) - 2.5
    assert abs(gap - 1.5) <= 0.5
    assert reg.value("h2_months_in_band_12") == 11
    assert reg.value("h2_months_with_data_12") == 11


def test_macro_missing_rpc_sample_nulls_gap_and_share(raw):
    macro_raw(raw, skip=(date(2026, 10, 1), date(2024, 3, 1)))
    reg = Registry()
    macro.compute(reg)
    assert reg.value("gap_cpi") is None and "2026-10-01" in reg.get("gap_cpi").null_reason
    assert reg.value("h2_months_in_band_12") is None
    assert reg.value("h1b_months_below") <= reg.value("h1b_months_with_data") < reg.value("h1b_months_expected")
    chart = macro.inflation_measures(reg)
    dump(chart.to_json())
    assert chart.status == "partial" and chart.status_banner


def test_macro_alt_title_when_near_grows_faster(raw):
    macro_raw(raw)
    path = raw / "data" / "raw" / "rpc" / "history_samples.csv"
    path.unlink()
    rpc_history(raw, month_starts(D0, date(2026, 11, 1)), g=0.08)
    reg = Registry()
    macro.compute(reg)
    assert reg.value("gap_cpi") < 0 and reg.value("gap_cpi_dir") == "faster"
    chart = macro.inflation_measures(reg)
    # the sign is carried by words: the magnitude prints unsigned ({gap_cpi:abs})
    # (a hyphen inside the month label is not a sign: only a +/- that is not preceded by a digit counts)
    assert "faster than CPI" in chart.title and not re.search(r"(?<!\d)[-+−]\s?\d", chart.title)
    assert chart.values["gap_cpi"]["display"] == reg.display("gap_cpi", "abs")


def test_macro_annotations_are_per_panel_and_short(raw):
    macro_raw(raw)
    reg = Registry()
    macro.compute(reg)
    chart = macro.inflation_measures(reg)
    dump(chart.to_json())
    assert chart.annotations and {a["panel"] for a in chart.annotations} == {"cpi", "core"}
    assert all(len(a["label"]) <= 28 for a in chart.annotations)
    # the full statement lives in the method notes, through the registered blank month
    assert reg.value("cpi_blank_month") == "2025-10" and "2025-10" in said(chart)


# --- market ---------------------------------------------------------------------------------------------------

def market_raw(root, near=True, binance=True, skip_near=()):
    start, end = date(2020, 10, 14), date(2026, 11, 9)
    if near and binance:
        closes(root, "NEARUSDT_binance_daily", start, end, wiggle(0.05), skip=skip_near)
    if near:
        closes(root, "near_usd_coinbase_daily", date(2022, 9, 1), end, wiggle(0.05))
    closes(root, "BTCUSDT_binance_daily", date(2017, 8, 17), end, wiggle(0.02, 1000))
    closes(root, "ETHUSDT_binance_daily", date(2017, 8, 17), end, wiggle(0.03, 100))
    weekdays = [d for d in days(date(2019, 1, 1), date(2026, 11, 6)) if d.weekday() < 5]
    put_csv(root, "fred/DEXUSEU.csv", ["date", "value"],
            [[d.isoformat(), None if d == date(2026, 1, 1) else wiggle(0.003, 1.1)(d)] for d in weekdays])
    fred_daily(root, "DFF", date(2019, 1, 1), end, 3.6)
    fred_daily(root, "DFEDTARU", date(2019, 1, 1), end,
               lambda d: 2.0 if d < date(2022, 3, 17) else 3.0 if d < date(2024, 9, 19) else 2.5)


def expected_rv(fn, end: date, n: int = 365, periods: int = 365) -> float:
    rets = [math.log(fn(d) / fn(d - timedelta(days=1))) for d in days(end - timedelta(days=n - 1), end)]
    return statistics.stdev(rets) * math.sqrt(periods) * 100


def test_market_rv_drawdown_and_worst_day(raw):
    market_raw(raw)
    reg = fake_upstream(Registry())
    market.compute(reg)
    end = date(2026, 11, 9)
    assert reg.value("latest_common_date") == "2026-11-09"
    assert reg.value("rv_365_near") == pytest.approx(expected_rv(wiggle(0.05), end), abs=1e-3)
    assert reg.value("rv_365_btc") == pytest.approx(expected_rv(wiggle(0.02, 1000), end), abs=1e-3)
    assert reg.value("x_btc") == pytest.approx(reg.value("rv_365_near") / reg.value("rv_365_btc"), abs=1e-3)
    assert reg.value("sigma_d") == pytest.approx(reg.value("rv_365_near") / math.sqrt(365), abs=1e-3)
    # EUR: business-day observations, the 2026-01-01 blank is skipped (one return spans the holiday), sqrt(252).
    eur = [(d, wiggle(0.003, 1.1)(d)) for d in days(date(2025, 11, 10), date(2026, 11, 6)) if d.weekday() < 5
           and d != date(2026, 1, 1)]  # the synthetic DEXUSEU series ends on Friday 2026-11-06
    prev = (date(2025, 11, 7), wiggle(0.003, 1.1)(date(2025, 11, 7)))
    rets = []
    for d, v in eur:
        rets.append(math.log(v / prev[1]))
        prev = (d, v)
    assert reg.value("rv_365_eur") == pytest.approx(statistics.stdev(rets) * math.sqrt(252) * 100, abs=1e-3)
    pts = load.price_close("NEARUSDT_binance_daily").points
    # F-mdd-365: the running max starts inside the trailing 365 days, which end the day after latest_common_date
    mdd = F.max_drawdown([(d, v) for d, v in pts if end - timedelta(days=364) <= d <= end])
    assert reg.value("mdd_365") == pytest.approx(100 * mdd[0], abs=1e-3)
    assert f"high {mdd[1].isoformat()}, low {mdd[2].isoformat()}" in reg.get("mdd_365").inputs
    worst = min(((d1, p1 / p0 - 1) for (d0, p0), (d1, p1) in zip(pts, pts[1:])), key=lambda x: x[1])
    assert reg.value("worst_day") == pytest.approx(100 * worst[1], abs=1e-3)
    assert reg.value("worst_day_date") == worst[0].isoformat()
    assert reg.value("ratio_gap_to_day") == pytest.approx(0.3 / reg.value("sigma_d"), abs=1e-3)
    window = [(d1, p1 / p0 - 1) for (d0, p0), (d1, p1) in zip(pts, pts[1:]) if d1 > end - timedelta(days=365)]
    share = 100 * sum(1 for _, r in window if abs(r) * 100 > 0.3) / len(window)
    assert reg.value("share_days_gt_spread") == pytest.approx(share, abs=1e-3)
    assert reg.value("near_close_latest") == pytest.approx(wiggle(0.05)(end), abs=0.01)


def test_market_rv_window_must_be_complete(raw):
    market_raw(raw, skip_near=(date(2026, 6, 1),))
    reg = Registry()
    market.compute(reg)
    assert reg.value("rv_365_near") is None
    assert "364 of 365" in reg.get("rv_365_near").null_reason or "of 365" in reg.get("rv_365_near").null_reason
    assert reg.value("rv_365_btc") is not None


def test_market_without_spread_nulls_dependents(raw):
    market_raw(raw)
    reg = Registry()
    market.compute(reg)
    assert reg.value("ratio_gap_to_day") is None
    assert "spread_adj_365" in reg.get("ratio_gap_to_day").null_reason
    chart = market.carry_vs_noise(reg)
    dump(chart.to_json())
    assert chart.status == "partial"


def test_rolling_usd_uses_close_of_day_before_and_same_days_cash(raw):
    # NEAR close is 2.0 every day except on the 1st of each month (5.0): V must use the day before.
    end = date(2026, 11, 9)
    closes(raw, "NEARUSDT_binance_daily", date(2022, 1, 1), end, lambda d: 5.0 if d.day == 1 else 2.0)
    fred_daily(raw, "DFF", date(2021, 1, 1), end, 3.6)
    fred_daily(raw, "DFEDTARU", date(2021, 1, 1), end, 4.0)
    rpc_history(raw, month_starts(date(2022, 4, 1), date(2026, 11, 1)), y=0.04)
    reg = fake_upstream(Registry())
    market.compute(reg)
    samples, _ = market.stnear_usd(market.near_prices())
    assert all(s["close"] == 2.0 for s in samples if s["close"] is not None)
    wins = market.usd_windows(samples, load.fred("DFF").as_dict())
    assert wins and all(w["start"] == w["date"] - timedelta(days=w["days"]) for w in wins)
    w = wins[-1]
    assert w["ret"] == pytest.approx(100 * (1.04 ** (w["days"] / 365.25) - 1), abs=1e-6)
    assert w["cash"] == pytest.approx(100 * ((1 + 3.6 / 36000) ** w["days"] - 1), abs=1e-6)
    hi = date.fromisoformat(reg.value("latest_common_date")) + timedelta(days=1)
    in_year = [w for w in wins if hi - timedelta(days=365) < w["date"] <= hi]
    assert in_year and reg.value("n_windows_usd_1y") == len(in_year)
    assert reg.value("pct_beat_cash_1y") == pytest.approx(100.0)
    assert reg.value("pct_within_2pp_1y") == pytest.approx(100.0)
    chart = market.rolling_usd(reg)
    dump(chart.to_json())
    assert chart.status == "ok" and "two percentage points" in chart.sentence


def test_rolling_usd_windows_are_weekly(raw):
    # history monthly; dense samples weekly on Mondays from 2022-04-04, daily from 2025-10-06. Window ends are the
    # dense Mondays plus the latest sample, never the other daily samples; starts may be any sample.
    end = date(2026, 11, 9)
    closes(raw, "NEARUSDT_binance_daily", date(2022, 1, 1), end, lambda d: 2.0)
    fred_daily(raw, "DFF", date(2021, 1, 1), end, 3.6)
    fred_daily(raw, "DFEDTARU", date(2021, 1, 1), end, 4.0)
    rpc_history(raw, month_starts(date(2022, 4, 1), date(2026, 11, 1)), y=0.04)
    weekly = [date(2022, 4, 4) + timedelta(weeks=k) for k in range(300) if
              date(2022, 4, 4) + timedelta(weeks=k) < date(2025, 10, 6)]
    rpc_dense(raw, weekly + list(days(date(2025, 10, 6), date(2026, 11, 10))))
    reg = fake_upstream(Registry())
    market.compute(reg)
    samples, _ = market.stnear_usd(market.near_prices())
    wins = market.usd_windows(samples, load.fred("DFF").as_dict())
    ends = [w["date"] for w in wins]
    assert all(d.weekday() == 0 for d in ends[:-1]) and ends[-1] == date(2026, 11, 10)
    assert all((b - a).days == 7 for a, b in zip(ends, ends[1:-1]))
    # the first Monday whose start (d - 365) has a sample within seven days: 2023-03-27 -> 2022-04-01
    assert ends[0] == date(2023, 3, 27) and wins[0]["start"] == date(2022, 4, 1)
    mondays = [d for d in days(date(2023, 3, 27), date(2026, 11, 10)) if d.weekday() == 0]
    assert len(mondays) + 1 == len(wins)
    # the trailing-year keys count the windows that end in the 365 days up to the latest sample
    assert reg.value("n_windows_usd_1y") == sum(1 for d in ends if d > date(2026, 11, 10) - timedelta(days=365))
    # once samples are daily, the start is exactly d - 365
    assert wins[-1]["days"] == 365 and wins[-1]["start"] == date(2025, 11, 10)
    assert any("weekly windows" in i for i in reg.get("pct_within_2pp_1y").inputs)
    chart = market.rolling_usd(reg)
    dump(chart.to_json())
    # the title counts the weekly windows that ended in the trailing year
    assert chart.status == "ok" and "Windows end each Monday" in said(chart)
    assert "weekly one-year windows that ended in the last year" in chart.title
    # the page table keeps the first window of each month and the latest; the CSV keeps every window
    assert len(chart.csv_table["rows"]) == len(wins) and chart.table["rows"][-1][0] == "2026-11-10"
    months = [r[0][:7] for r in chart.table["rows"][:-1]]
    assert len(months) == len(set(months)) and len(chart.table["rows"]) < len(wins)
    assert not chart.coverage["gaps"]


def test_rolling_usd_needs_sample_within_seven_days(raw):
    end = date(2026, 11, 9)
    closes(raw, "NEARUSDT_binance_daily", date(2022, 1, 1), end, lambda d: 2.0)
    fred_daily(raw, "DFF", date(2021, 1, 1), end, 3.6)
    rpc_history(raw, [date(2023, 4, 1), date(2024, 4, 10)])  # 375 days apart: 10 days off the 365-day edge
    reg = Registry()
    market.compute(reg)
    assert reg.value("n_windows_usd_1y") is None
    chart = market.rolling_usd(reg)
    dump(chart.to_json())
    assert chart.status == "unavailable" and chart.empty_state["fix_command"]


def test_market_coinbase_fallback(raw):
    market_raw(raw, binance=False)
    reg = Registry()
    market.compute(reg)
    assert reg.value("rv_365_near") is not None
    assert any("coinbase" in f for f in reg.get("rv_365_near").source_files)
    charts = market.charts(reg)
    for c in charts:
        dump(c.to_json())
        assert c.status in ("fallback", "unavailable")
    assert all(c.status_banner and "Coinbase" in c.status_banner for c in charts if c.status == "fallback")


# --- demand ---------------------------------------------------------------------------------------------------

def demand_raw(root, start=date(2024, 12, 10), end=date(2026, 11, 9), gap=None, provisional=True):
    rows = [[d.isoformat(), None if d == gap else (d - start).days + 1] for d in days(start, end)]
    prov = [(end - timedelta(days=k)).isoformat() for k in range(2, -1, -1)] if provisional else []
    put_csv(root, "intents/defillama_volume_daily.csv", ["date", "volume_usd"], rows, provisional_dates=prov)
    put_csv(root, "intents/defillama_fees_daily.csv", ["date", "fees_usd"],
            [[d.isoformat(), 2] for d in days(date(2025, 5, 10), end)], provisional_dates=prov)


def test_demand_sums_ratio_months(raw):
    demand_raw(raw)
    reg = Registry()
    demand.compute(reg)
    start, end = date(2024, 12, 10), date(2026, 11, 9)
    idx = lambda d: (d - start).days + 1  # noqa: E731
    assert reg.value("intents_vol_30d") == pytest.approx(sum(idx(end - timedelta(days=k)) for k in range(30)))
    assert reg.value("intents_vol_365d") == pytest.approx(sum(idx(end - timedelta(days=k)) for k in range(365)))
    now = sum(idx(end - timedelta(days=k)) for k in range(90))
    before = sum(idx(end - timedelta(days=90 + k)) for k in range(90))
    assert reg.value("intents_vol_90d_seq_ratio") == pytest.approx(now / before, abs=1e-4)
    assert reg.value("intents_series_start") == "2024-12-10"
    assert reg.value("intents_latest_date") == "2026-11-09"
    assert reg.value("intents_fees_365d") == pytest.approx(2 * 365)
    assert reg.value("intents_n_provisional") == 3
    assert any("provisional" in i for i in reg.get("intents_vol_30d").inputs)
    assert reg.value("intents_vol_mtd") == pytest.approx(sum(idx(date(2026, 11, d)) for d in range(1, 10)))
    chart = demand.intents_volume(reg)
    dump(chart.to_json())
    months = [p[0] for p in chart.series[0].points]
    assert months[0] == "2025-01-01" and months[-1] == "2026-10-01"  # partial first and running month excluded
    assert "DefiLlama alone" in chart.status_banner and "JWT" not in chart.status_banner
    assert "<redacted" in chart.queries[-1].body_or_sql and "Bearer" in chart.queries[-1].body_or_sql


def test_demand_gap_blanks_every_window_it_falls_in(raw):
    demand_raw(raw, start=date(2025, 6, 1), gap=date(2026, 11, 1))
    reg = Registry()
    demand.compute(reg)
    assert reg.value("intents_vol_90d_seq_ratio") is None
    assert "missing" in reg.get("intents_vol_90d_seq_ratio").null_reason
    assert reg.value("intents_vol_30d") is None  # a missing day is never filled
    assert reg.value("intents_vol_365d") is None


def test_demand_explorer_overlay_needs_permission(raw):
    demand_raw(raw)
    put_csv(raw, "intents/explorer_daily.csv", ["date", "final", "volume_in_usd_success"],
            [[d.isoformat(), "true", 5] for d in days(date(2026, 9, 1), date(2026, 10, 31))])
    reg = Registry()
    demand.compute(reg)
    withheld = demand.intents_volume(reg)
    assert "permission" in withheld.status_banner and len(withheld.series) == 1
    sources = raw / "thesis" / "sources.toml"
    sources.write_text(sources.read_text().replace("publish = false", "publish = true"))
    shown = demand.intents_volume(reg)
    dump(shown.to_json())
    overlay = next(s for s in shown.series if s.id == "explorer_month")
    assert overlay.points == [["2026-09-01", 150], ["2026-10-01", 155]]


# --- derivatives ----------------------------------------------------------------------------------------------

def derivatives_raw(root):
    put_json(root, "market/options_venues.json", {"checked_at": STAMP, "venues": [
        {"venue": "Deribit", "endpoint": "https://d", "ok": True, "http_status": 200, "has_near_options": False,
         "option_underlyings": ["BTC", "ETH"], "error": None},
        {"venue": "Aevo", "endpoint": "https://a", "ok": True, "http_status": 200, "has_near_options": False,
         "option_underlyings": ["BTC"], "error": None},
        {"venue": "Bybit", "endpoint": "https://b", "ok": False, "http_status": 403, "has_near_options": None,
         "option_underlyings": None, "error": "blocked"}]})
    for sym, dv in (("BTC", 50.0), ("ETH", 70.0)):
        put_csv(root, f"market/dvol_deribit_{sym.lower()}_daily.csv", ["date", "close"],
                [[d.isoformat(), dv] for d in days(date(2025, 1, 1), date(2026, 11, 9))])
    closes(root, "BTCUSDT_binance_daily", date(2024, 12, 1), date(2026, 11, 9), wiggle(0.02, 1000))
    closes(root, "ETHUSDT_binance_daily", date(2024, 12, 1), date(2026, 11, 9), wiggle(0.03, 100))


def test_derivatives_counts_venues_and_vrp(raw):
    derivatives_raw(raw)
    reg = Registry()
    derivatives.compute(reg)
    assert reg.value("n_venues_with_near_options") == 0
    assert reg.value("n_option_venues_checked") == 2
    assert reg.value("venues_unchecked") == 1
    rets = dict(F.log_returns(load.price_close("BTCUSDT_binance_daily").points))
    diffs = []
    for t in days(date(2025, 1, 1), date(2026, 11, 9)):
        fwd = [rets.get(t + timedelta(days=k)) for k in range(1, 31)]
        if None not in fwd:
            diffs.append(50.0 - statistics.stdev(fwd) * math.sqrt(365) * 100)
    assert reg.value("vrp_btc") == pytest.approx(statistics.fmean(diffs), abs=1e-3)
    chart = derivatives.vol_market(reg)
    dump(chart.to_json())
    assert chart.status == "ok"
    labels = {r[0]: r[2] for r in chart.table["rows"]}
    assert labels["Bybit"] == "not checked: HTTP 403" and labels["Deribit"] == "no"
    assert chart.form == "table" and chart.series == [] and chart.panels[0]["table"] == chart.table
    assert "0 of the 2 options venues" in chart.title and "1 more could not be checked" in chart.title


def test_derivatives_no_venue_checked_is_partial(raw):
    put_json(raw, "market/options_venues.json", {"checked_at": STAMP, "venues": [
        {"venue": "Bybit", "endpoint": "https://b", "ok": False, "http_status": 403, "has_near_options": None,
         "option_underlyings": None, "error": "blocked"}]})
    reg = Registry()
    derivatives.compute(reg)
    assert reg.value("n_option_venues_checked") == 0 and reg.value("venues_unchecked") == 1
    assert reg.value("vrp_btc") is None
    chart = derivatives.vol_market(reg)
    dump(chart.to_json())
    assert chart.status == "partial"


# --- currencies -----------------------------------------------------------------------------------------------

def bis_raw(root):
    months = [m for m in month_starts(date(2015, 1, 1), date(2026, 9, 1))]
    pol, cpi = [], []
    for i, (area, _) in enumerate(currencies.PANEL):
        for k, m in enumerate(months):
            # reading A = base_i + swing (alternating), reading B = policy - US policy
            pol.append([area, m.strftime("%Y-%m"), 2.0 + i + (0.5 * i if k % 2 else 0.0)])
            if area == "AU":
                q = F.quarter_start(m)
                cpi.append([area, m.strftime("%Y-%m"), 1.0 + q.month / 10])
            elif not (area == "US" and m == date(2025, 10, 1)):
                cpi.append([area, m.strftime("%Y-%m"), round(2.0 + 0.01 * k, 2)])  # moves every month
    for m in month_starts(date(2015, 1, 1), date(2025, 6, 1)):
        pol.append(["AR", m.strftime("%Y-%m"), 40.0])
    put_csv(root, "bis/policy_rates_monthly.csv", ["area", "period", "value"], pol)
    put_csv(root, "bis/cpi_yoy_monthly.csv", ["area", "period", "value"], cpi)


def test_currencies_panel_counts(raw):
    bis_raw(raw)
    rpc_history(raw, month_starts(D0, date(2026, 11, 1)), g=0.025, y=0.04)
    fred_daily(raw, "DFF", date(2019, 1, 1), date(2026, 11, 9), 3.6)
    reg = fake_upstream(Registry())
    currencies.compute(reg)
    assert reg.value("n_panel") == 10
    assert reg.value("ar_policy_last_month") == "2025-06"
    assert reg.value("near_reading_a") == pytest.approx(1.5)
    assert reg.value("near_reading_b") == pytest.approx(0.4)
    # NEAR history is constant (y 4 %, g 2.5 %, DFF 3.6 %): its context range is ~0 over >= 12 months.
    d = currencies.analyze(reg)
    assert d["near_sum_a"]["range"] == pytest.approx(0.0, abs=1e-3)
    assert d["near_sum_a"]["n"] >= currencies.MIN_NEAR_RANGE_MONTHS
    # H5 reads today's readings against the panel median |latest reading|
    side = {}
    for r in ("a", "b"):
        med = statistics.median(abs(p[r]["value"]) for p in d["panel"].values() if p[r])
        assert reg.value(f"panel_median_abs_{r}") == pytest.approx(med, abs=1e-6)
        side[r] = abs(reg.value(f"near_reading_{r}")) - med
    assert reg.value("h5_now_narrower") == sum(1 for v in side.values() if v < 0)
    assert reg.value("h5_now_wider") == sum(1 for v in side.values() if v > 0)
    assert d["panel"]["AU"]["cpi_quarterly"] is True and d["panel"]["US"]["cpi_quarterly"] is False
    # US reading A month skips the 2025-10 CPI hole only if it is the latest; here latest common is 2026-09.
    assert d["panel"]["US"]["a"]["month"] == date(2026, 9, 1)
    assert d["panel"]["AU"]["a"]["month"] == date(2026, 9, 1)
    chart = currencies.other_currencies(reg)
    dump(chart.to_json())
    assert chart.status == "ok" and "Argentina is excluded" in said(chart) and "Australia" in said(chart)


def test_currencies_short_near_history_nulls_range(raw):
    bis_raw(raw)
    rpc_history(raw, month_starts(date(2024, 1, 1), date(2026, 11, 1)), lst_from=date(2025, 1, 1))
    fred_daily(raw, "DFF", date(2019, 1, 1), date(2026, 11, 9), 3.6)
    reg = fake_upstream(Registry())
    currencies.compute(reg)
    d = currencies.analyze(reg)
    assert d["near_sum_a"]["range"] is None and d["near_sum_a"]["n"] < currencies.MIN_NEAR_RANGE_MONTHS
    # the ranges are context: today's readings, and so H5's counts, are computed without them
    assert reg.value("h5_now_narrower") is not None and reg.value("h5_now_wider") is not None
    chart = currencies.other_currencies(reg)
    dump(chart.to_json())
    # the title compares today's readings, and a note says why NEAR's row has no range bar
    assert chart.status == "ok" and "NEAR's rate gap today" in chart.title
    assert "NEAR's row has no gray bar" in said(chart)


# --- sweep ----------------------------------------------------------------------------------------------------

def sweep_raw(root, rate_fn=None):
    end = date(2026, 11, 9)
    closes(root, "NEARUSDT_binance_daily", date(2020, 10, 14), end, lambda d: 2.0 if d < date(2026, 3, 1) else 2.5)
    fred_daily(root, "DFF", date(2019, 1, 1), end, 3.6)
    dates = month_starts(D0, date(2025, 11, 1)) + [date(2025, 11, 10) + timedelta(days=k) for k in range(366)]
    rpc_history(root, dates, rate_fn=rate_fn)
    # 2021 epochs (rpc/epoch_history.csv): median 12 h; 2020 and 2022 rows are outside the year
    secs = {m: 43200.0 if m.month <= 7 else 40000.0 for m in month_starts(date(2021, 1, 1), date(2021, 12, 1))}
    rpc_epochs(root, secs | {date(2020, 12, 1): 99999.0, date(2022, 1, 1): 1.0})
    put_json(root, "rpc/snapshot_now.json", {"height": 1, "block_time_utc": STAMP, "lst": {"metapool_state": {
        "nslp_liquidity_near": 75000.0, "nslp_current_discount_bp": 55, "nslp_min_discount_bp": 30,
        "nslp_max_discount_bp": 500}}})


def test_sweep_rows_and_outcomes(raw):
    sweep_raw(raw)
    reg = fake_upstream(Registry())
    market.compute(reg)
    sweep.compute(reg)
    # V = rate x close(d-1): the price step on 2026-03-01 (+25 %) pushes later samples outside the 0.5 % band.
    assert reg.value("mmf_penny_band") == pytest.approx(0.5)
    n, at_par = reg.value("n_nav_days"), reg.value("nav_days_at_par_365")
    # Daily samples 2025-11-10 .. 2026-11-10; the window is [last - 365, last]: its first sample (a full year back)
    # is the reference and each of the 365 later days is tested.
    assert n == 365 and 0 < at_par < n
    samples, _ = market.stnear_usd(market.near_prices())
    window = [s for s in samples if s["value"] and s["date"] >= date(2026, 11, 10) - timedelta(days=365)]
    assert window[0]["date"] == date(2025, 11, 10)
    t0 = window[0]["value"]
    assert at_par == sum(1 for s in window[1:] if abs(s["value"] / t0 - 1) <= 0.005)
    assert reg.value("stnear_down_days") == 0 and reg.value("n_lst_days") > 300
    assert reg.value("unstake_hours_lo") == pytest.approx(21.0) and reg.value("unstake_hours_hi") == pytest.approx(28.0)
    # 2021: median epoch_seconds of the sampled 2021 epochs (seven of twelve at 43,200 s) = 12 h
    assert reg.value("epoch_hours_2021") == pytest.approx(12.0, abs=1e-9)
    assert "12 complete epochs sampled in 2021" in reg.get("epoch_hours_2021").inputs[0]
    assert reg.value("unstake_hours_2021_lo") == pytest.approx(36.0, abs=0.01)
    assert reg.value("unstake_hours_2021_hi") == pytest.approx(48.0, abs=0.01)
    assert reg.value("nslp_near") == pytest.approx(75000.0)
    assert reg.value("nslp_fee_now") == pytest.approx(0.55) and reg.value("nslp_fee_max") == pytest.approx(5.0)
    assert reg.value("nslp_usd") == pytest.approx(75000.0 * reg.value("near_close_latest"))
    assert reg.value("h6_row1_usd") == "fail" and reg.value("h6_row2_usd") == "partial"
    assert reg.value("h6_row4_usd") == "fail"
    assert reg.value("h6_row3_usd") == "pass"  # stNEAR rises 4 % a year and the price steps up: every window beats cash
    assert reg.value("h6_rows_total") == 4 and reg.value("h6_rows_passed") == 1
    assert reg.value("h6_critical_failed") == 2
    assert reg.value("h6n_rows13_pass") == 2 and reg.value("h6n_row2_fail") == 0
    chart = sweep.checklist(reg)
    dump(chart.to_json())
    assert chart.title.startswith("NEAR fails the sweep-account tests that decide the verdict")
    # row 2 is partial here: the title says so and never claims that same-day cash fails
    assert "same-day cash is only partial" in chart.title and "same-day cash fails" not in chart.title
    assert "principal stability" in chart.title and "legal-vehicle" in chart.title
    assert len(chart.table["rows"]) == 4
    assert chart.panels[0]["row_templates"][0]["usd"].startswith("stNEAR in dollars")
    # the drawdown is NEAR spot (F-mdd on NEAR closes), and the row says so
    assert "NEAR's spot price (not stNEAR)" in chart.panels[0]["row_templates"][0]["usd"]


def test_sweep_title_follows_row_states():
    """c14's title names only the critical rows (1 and 4, the ones h6_critical_failed counts) that fail, and states
    row 2 as measured."""
    t = sweep.checklist_title
    assert t(["fail", "partial", "pass", "fail"]).endswith("principal stability and the legal-vehicle rule; "
                                                           "same-day cash is only partial.")
    assert t(["pass", "fail", "pass", "fail"]).endswith(": the legal-vehicle rule; same-day cash fails too.")
    assert t(["fail", "pass", "fail", None]).endswith(": principal stability; same-day cash passes.")
    assert t(["fail", None, "fail", "fail"]).endswith("legal-vehicle rule.")  # row 2 not computed: no claim about it
    assert t(["pass", "partial", "fail", "pass"]) == "Staked NEAR against the sweep-account checklist"
    for states in (["fail", "partial", "pass", "fail"], ["pass", "fail", "pass", "fail"]):
        assert not any(ch.isdigit() for ch in t(states))


def test_sweep_rate_drop_and_fast_unstake(raw):
    def rate(d):
        return 1.0 + (d - date(2022, 4, 1)).days * 1e-4 - (0.01 if d == date(2026, 1, 1) else 0.0)
    sweep_raw(raw, rate_fn=rate)
    reg = fake_upstream(Registry(), epoch_hours_median_30d=(6.0, "hours"), n_pos=(2, "count"),
                        staker_share_365=(-0.3, "%"))
    market.compute(reg)
    sweep.compute(reg)
    assert reg.value("stnear_down_days") == 1
    assert reg.value("h6n_row1") == "fail"
    assert reg.value("h6_row2_usd") == "pass"  # 4 x 6 h = 24 h is within a day
    # row 3 reads the trailing year only; a staker's share fell, so it fails (n_pos is history, unused)
    assert reg.value("h6n_row3") == "fail"
    assert reg.value("h6n_rows13_pass") == 0


def test_sweep_without_upstream(raw):
    sweep_raw(raw)
    reg = Registry()
    market.compute(reg)
    sweep.compute(reg)
    assert reg.value("unstake_hours_lo") is None
    assert "epoch_hours_median_30d" in reg.get("unstake_hours_lo").null_reason
    assert reg.value("h6n_rows13_pass") is None
    assert reg.value("h6_critical_failed") >= 1  # row 4 fails by definition
    chart = sweep.checklist(reg)
    dump(chart.to_json())
    assert chart.status == "partial"


# --- lint-friendliness: literal chart text carries no digits -----------------------------------------------

def _literal(template: str) -> str:
    return "".join(lit for lit, _, _, _ in string.Formatter().parse(template))


def test_chart_literal_text_has_no_digits(raw):
    macro_raw(raw)
    market_raw(raw)
    demand_raw(raw)
    derivatives_raw(raw)
    bis_raw(raw)
    put_json(raw, "rpc/snapshot_now.json", {"lst": {"metapool_state": {"nslp_liquidity_near": 1.0,
                                                                        "nslp_current_discount_bp": 55,
                                                                        "nslp_min_discount_bp": 30,
                                                                        "nslp_max_discount_bp": 500}}})
    for upstream in (True, False):
        reg = fake_upstream(Registry()) if upstream else Registry()
        charts = run_all(reg)
        for c in charts.values():
            texts = [c.status_banner or "", *(c.empty_state or {}).values()]
            texts += [_literal(c.title_template), _literal(c.sentence_template)] if c.title_template else [c.title]
            if not c.sentence_template:
                texts.append(c.sentence)
            for a in c.annotations:
                texts.append(a["label"])
            for t in texts:
                if t == (c.empty_state or {}).get("fix_command"):
                    continue
                assert not any(ch.isdigit() for ch in t), (c.id, t)


# --- sanity bounds on the real data/raw ------------------------------------------------------------

REAL = REPO / "data" / "raw"


def need(*rels):
    for rel in rels:
        if not (REAL / rel).exists():
            pytest.skip(f"{rel} not in data/raw")


@pytest.fixture(scope="module")
def real_reg():
    reg = Registry()
    for m in (macro, market, demand, derivatives, currencies, sweep):
        m.compute(reg)
    return reg


def test_real_rv_365_near_bounds(real_reg):
    need("market/NEARUSDT_binance_daily.csv")
    assert 70 <= real_reg.value("rv_365_near") <= 150


def test_real_mdd_365_bounds(real_reg):
    need("market/NEARUSDT_binance_daily.csv")
    # a drawdown whose running max starts inside the window can be no deeper than the one over the whole history
    deepest = 100 * F.max_drawdown(load.price_close("NEARUSDT_binance_daily").points)[0]
    assert deepest - 1e-6 <= real_reg.value("mdd_365") <= 0


def test_real_cpi_yoy_sep_2026_bounds():
    need("fred/CPIAUCSL.csv")
    yoy = macro.analyze().yoy["cpi"].get(date(2026, 9, 1))
    assert yoy is not None and 3.6 <= 100 * yoy <= 3.8


def test_real_cpi_oct_2025_gap_is_null():
    need("fred/CPIAUCSL.csv")
    yo = macro.analyze().yoy["cpi"]
    assert yo.get(date(2025, 10, 1)) is None and yo.get(date(2026, 10, 1)) is None


def test_real_m2_yoy_bounds(real_reg):
    need("fred/M2SL.csv")
    assert 5.5 <= real_reg.value("m2_yoy") <= 5.9


def test_real_nslp_near_bounds(real_reg):
    need("rpc/snapshot_now.json")
    assert 0 <= real_reg.value("nslp_near") <= 200_000


def test_real_intents_vol_365d_is_sum_of_last_365_points(real_reg):
    need("intents/defillama_volume_daily.csv")
    with (REAL / "intents" / "defillama_volume_daily.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    assert real_reg.value("intents_vol_365d") == pytest.approx(sum(float(r["volume_usd"]) for r in rows[-365:]),
                                                               abs=0.01)


def test_real_charts_validate(real_reg):
    need("market/NEARUSDT_binance_daily.csv")
    for m in MODULES:
        for c in m.charts(real_reg):
            dump(c.to_json())


def test_real_c09_json_within_budget_and_csv_full_resolution(real_reg):
    # chart JSON <= 60 KB, long daily series weekly for display, full resolution in csv/<id>.csv
    need("market/NEARUSDT_binance_daily.csv")
    c09 = market.rolling_vol(real_reg)
    body = dump(c09.to_json())
    assert len(body.encode()) <= 60 * 1024
    assert "csv_table" not in c09.to_json()
    csv_rows = c09.to_csv().strip().splitlines()[1:]
    assert len(csv_rows) > 3 * len(c09.table["rows"])                   # daily in the CSV, weekly in the JSON
    drawn = {x for s in c09.series for x, _ in s.points}
    assert {r[0] for r in c09.table["rows"]} <= drawn                  # every table date is a drawn weekly point


def test_chartspec_csv_table_feeds_csv_not_json():
    from pipeline.chartspec import Chart, Query
    c = Chart(id="cx", section="s", priority="lead", claims=[], title="t", sentence="s.", form="line",
              sources=["src"], queries=[Query("derived", "e", "b")], code_ref="x::y",
              table={"columns": ["date", "v"], "rows": [["2026-01-04", 1.0]]},
              csv_table={"columns": ["date", "v"], "rows": [["2026-01-01", 0.5], ["2026-01-04", 1.0]]})
    assert "csv_table" not in c.to_json()
    assert c.to_csv() == "date,v\n2026-01-01,0.5\n2026-01-04,1.0\n"
    c.csv_table = None
    assert c.to_csv() == "date,v\n2026-01-04,1.0\n"
