"""Group B, currencies: is NEAR's holding-rate spread narrower than other currencies'? (chart c13; claim H5).

The claim is ambiguous, so two readings are fixed:
  F-reading-a  own rate minus own inflation. Fiat: BIS policy rate - BIS CPI YoY at the latest month both exist
               (per area). NEAR: y_lst_365 - g_y_lst_365 (staking yield minus supply growth over the same two
               samples; a different kind of measure).
  F-reading-b  distance from the dollar's rate. Fiat: policy rate - US policy rate (latest common month).
               NEAR: y_lst_365 - effr_mean_365.
  Range        60-month min-max of the monthly reading ending at the latest month, drawn as context. NEAR's
               history is recomputed here at RPC month-start samples (LST trailing-365 yield, mean of stNEAR and
               LiNEAR where both exist, minus trailing-365 supply growth; minus the trailing-365 mean of DFF for
               reading B) and includes the latest reading; it needs at least MIN_NEAR_RANGE_MONTHS monthly readings.
  F-panel      the panel median of |latest| per reading; NEAR is "narrower" on a reading when its |latest| is below
               the median and "wider" when above; h5_now_narrower / h5_now_wider count the readings (0, 1, 2) on
               which it is narrower / wider (below on both, split, above on both).
Panel: US XM GB JP CH CA AU IN BR TR. Argentina is excluded: BIS reports no Argentine policy rate after mid-2025.
"""

from __future__ import annotations

import statistics
from datetime import date, timedelta

from pipeline.chartspec import Chart, Query, Series
from pipeline.models import formulas as F
from pipeline.models import load
from pipeline.models.macro import (coverage, derived_query, empty_state, files_as_of, fred_query, notes_for,
                                   ready, rnd,
                                   rpc_block_query, rpc_linear_query, rpc_stnear_query, upstream, ym)
from pipeline.numbers import Registry

PANEL = (("US", "United States"), ("XM", "Euro area"), ("GB", "United Kingdom"), ("JP", "Japan"),
         ("CH", "Switzerland"), ("CA", "Canada"), ("AU", "Australia"), ("IN", "India"), ("BR", "Brazil"),
         ("TR", "Turkey"))
RANGE_MONTHS = 60
MIN_NEAR_RANGE_MONTHS = 12   # a range needs at least a year of monthly NEAR readings
BIS_POLICY, BIS_CPI = "policy_rates_monthly", "cpi_yoy_monthly"


def _monthly(kind: str, area: str) -> tuple[dict[date, float], list[str]]:
    s = load.bis(kind, area)
    return {d: v for d, v in s.points if v is not None}, s.files


def _reading(a: dict[date, float], b: dict[date, float]) -> dict[date, float]:
    return {m: a[m] - b[m] for m in sorted(set(a) & set(b))}


def _summary(series: dict[date, float]) -> dict | None:
    if not series:
        return None
    latest = max(series)
    lo = F.add_months(latest, -(RANGE_MONTHS - 1))
    window = [v for m, v in series.items() if lo <= m <= latest]
    return {"month": latest, "value": series[latest], "min": min(window), "max": max(window),
            "range": max(window) - min(window), "n": len(window), "from": lo}


def _quarterly(series: dict[date, float]) -> bool:
    """True when the last 12 months carry at most 4 distinct values (a quarterly series repeated monthly)."""
    if not series:
        return False
    last = max(series)
    recent = [v for m, v in series.items() if m > F.add_months(last, -12)]
    return len(recent) >= 6 and len(set(recent)) <= 4


def fiat_panel() -> tuple[dict[str, dict], list[str], dict]:
    us_policy, files = _monthly(BIS_POLICY, "US")
    out, extra = {}, {}
    for area, label in PANEL:
        policy, f1 = _monthly(BIS_POLICY, area)
        cpi, f2 = _monthly(BIS_CPI, area)
        files += f1 + f2
        out[area] = {"label": label, "a": _summary(_reading(policy, cpi)), "b": _summary(_reading(policy, us_policy)),
                     "cpi_quarterly": _quarterly(cpi)}
    ar_policy, f3 = _monthly(BIS_POLICY, "AR")
    files += f3
    extra["ar_policy_last"] = max(ar_policy) if ar_policy else None
    return out, sorted(set(files)), extra


def near_history() -> tuple[dict[date, tuple[float, float | None]], list[str]]:
    """{month-start sample date: (reading A pp, reading B pp or None)} from RPC samples and DFF."""
    # merged full-day RPC samples (history_samples and dense_samples), evaluated at month starts
    supply = load.rpc_merged_series("total_supply_near")
    st, li = load.rpc_merged_series("stnear_price"), load.rpc_merged_series("linear_price")
    dff = load.fred("DFF")
    dff_pts = dff.non_null()
    dff_dict = dff.as_dict()
    out = {}
    dates = sorted({d for d, v in st.non_null()} | {d for d, v in li.non_null()})
    for d in dates:
        if d != F.month_start(d):
            continue
        ys = [y for y in (F.trailing_growth(st.points, d, 365, 7), F.trailing_growth(li.points, d, 365, 7))
              if y is not None]
        g = F.trailing_growth(supply.points, d, 365, 7)
        if not ys or g is None:
            continue
        y = statistics.fmean(ys)
        start = d - timedelta(days=365)
        complete = all(dff_dict.get(start + timedelta(days=k)) is not None for k in range(365))
        mean_dff = F.mean_between(dff_pts, start, d) if complete else None
        out[d] = (F.pct(y) - F.pct(g), F.pct(y) - mean_dff if mean_dff is not None else None)
    return out, supply.files + dff.files


def _near_summary(latest_value: float | None, latest_date: date | None, hist: dict[date, float | None]):
    if latest_value is None or latest_date is None:
        return None
    lo = F.add_months(latest_date, -RANGE_MONTHS)
    window = [v for m, v in hist.items() if lo < m <= latest_date and v is not None]
    if len(window) < MIN_NEAR_RANGE_MONTHS:
        return {"value": latest_value, "min": None, "max": None, "range": None, "n": len(window)}
    vals = window + [latest_value]
    return {"value": latest_value, "min": min(vals), "max": max(vals), "range": max(vals) - min(vals),
            "n": len(window)}


def analyze(reg: Registry) -> dict:
    panel, files, extra = fiat_panel()
    hist, hfiles = near_history()
    y, why_y = upstream(reg, "y_lst_365")
    g, why_g = upstream(reg, "g_y_lst_365")  # supply growth over the y_lst_365 window, not g_365's
    effr, why_e = upstream(reg, "effr_mean_365")
    latest_rpc = max(hist) if hist else None
    rpc_rows, _ = load.rpc_merged()
    full = [r for r in rpc_rows if r["full_day"]]
    if full:
        latest_rpc = full[-1]["date"]   # the latest full-day sample, where y_lst_365 and g_365 end
    near_a = y - g if (y is not None and g is not None) else None
    near_b = y - effr if (y is not None and effr is not None) else None
    return {
        "panel": panel, "files": files, "hfiles": hfiles, "extra": extra, "hist": hist,
        "near_a": near_a, "why_a": why_y or why_g, "near_b": near_b, "why_b": why_y or why_e,
        "near_sum_a": _near_summary(near_a, latest_rpc, {m: v[0] for m, v in hist.items()}),
        "near_sum_b": _near_summary(near_b, latest_rpc, {m: v[1] for m, v in hist.items()}),
    }


def _median_abs(panel: dict, reading: str) -> float | None:
    """The panel median of |latest reading|, over the economies with a reading."""
    rows = [p[reading] for p in panel.values() if p[reading]]
    return statistics.median(abs(s["value"]) for s in rows) if rows else None


def compute(reg: Registry) -> None:
    d = analyze(reg)
    panel, files = d["panel"], d["files"]
    near_files = d["hfiles"] + [f for k in ("y_lst_365", "g_y_lst_365", "effr_mean_365") if k in reg
                                for f in reg.get(k).source_files]
    complete = [a for a, p in panel.items() if p["a"] and p["b"]]
    reg.put("n_panel", len(complete) if complete else None, "count", "F-panel", source_files=files,
            inputs=[f"{a}: A {ym(panel[a]['a']['month'])}, B {ym(panel[a]['b']['month'])}" for a in complete],
            null_reason=None if complete else "no BIS policy and CPI data for the panel in data/raw/bis")
    ar = d["extra"]["ar_policy_last"]
    reg.put("ar_policy_last_month", ym(ar), "month", "F-panel", source_files=files,
            inputs=["last month with an Argentine policy rate in BIS WS_CBPOL"],
            null_reason=None if ar else "no Argentine policy rate in data/raw/bis")

    for reading, key, why in (("a", "near_reading_a", d["why_a"]), ("b", "near_reading_b", d["why_b"])):
        v = d[f"near_{reading}"]
        formula = "y_lst_365 - g_y_lst_365" if reading == "a" else "y_lst_365 - effr_mean_365"
        reg.put(key, v, "pp", f"F-reading-{reading}", source_files=near_files, inputs=[formula], null_reason=why)

    medians = {}
    for reading in ("a", "b"):
        med = _median_abs(panel, reading)
        medians[reading] = med
        reg.put(f"panel_median_abs_{reading}", med, "pp", "F-panel",
                source_files=files, inputs=[f"median |latest reading {reading.upper()}| over the panel"],
                null_reason=None if med is not None else "no panel data", signed=False)

    # H5 reads today's readings: on each reading NEAR is narrower when its |latest| is below the panel median
    # |latest|, wider when above
    now = {}
    for reading in ("a", "b"):
        x, med = d[f"near_{reading}"], medians[reading]
        now[reading] = None if (x is None or med is None) else ("narrower" if abs(x) < med else
                                                                 "wider" if abs(x) > med else "level")
    if all(now[r] is not None for r in ("a", "b")):
        inputs = [f"reading {r.upper()}: |near_reading_{r}| against panel_median_abs_{r}: {now[r]}" for r in ("a", "b")]
        for key, flag in (("h5_now_narrower", "narrower"), ("h5_now_wider", "wider")):
            reg.put(key, sum(1 for r in ("a", "b") if now[r] == flag), "count", "F-panel",
                    source_files=files + near_files, inputs=inputs)
    else:
        missing = [r.upper() for r in ("a", "b") if now[r] is None]
        for key in ("h5_now_narrower", "h5_now_wider"):
            reg.put(key, None, "count", "F-panel", source_files=files + near_files,
                    null_reason=f"NEAR's latest reading or the panel median is not computed for reading "
                                f"{', '.join(missing)}")


def charts(reg: Registry) -> list[Chart]:
    return [other_currencies(reg)]


def other_currencies(reg: Registry) -> Chart:
    """c13 other-currencies: dot (latest) and range (60-month min-max) per economy, two readings."""
    d = analyze(reg)
    panel = d["panel"]
    series, rows = [], []
    near_rows = {"a": d["near_sum_a"], "b": d["near_sum_b"]}
    for reading in ("a", "b"):
        s = near_rows[reading]
        series.append(Series(f"{reading}_NEAR", "NEAR", "near",
                             [["NEAR", rnd(s["value"]) if s else None, rnd(s["min"]) if s else None,
                               rnd(s["max"]) if s else None]], unit="pp", panel=reading))
        for area, label in PANEL:
            p = panel[area][reading]
            series.append(Series(f"{reading}_{area}", label, "usd" if area == "US" else "context",
                                 [[label, rnd(p["value"]) if p else None, rnd(p["min"]) if p else None,
                                   rnd(p["max"]) if p else None]],
                                 role="emphasis" if area == "US" else "context", unit="pp", panel=reading))
    sa, sb = d["near_sum_a"], d["near_sum_b"]
    rows.append(["NEAR", "NEAR", rnd(d["near_a"]), None, rnd(sa["min"]) if sa else None,
                 rnd(sa["max"]) if sa else None, sa["n"] if sa else None, rnd(d["near_b"]), None,
                 rnd(sb["min"]) if sb else None, rnd(sb["max"]) if sb else None, sb["n"] if sb else None,
                 "staking yield minus supply growth; a different kind of measure"])
    for area, label in PANEL:
        a, b = panel[area]["a"], panel[area]["b"]
        note = "CPI is quarterly (each quarter's value repeats across its months)" if panel[area]["cpi_quarterly"] else ""
        rows.append([area, label, rnd(a["value"]) if a else None, ym(a["month"]) if a else None,
                     rnd(a["min"]) if a else None, rnd(a["max"]) if a else None, a["n"] if a else None,
                     rnd(b["value"]) if b else None, ym(b["month"]) if b else None,
                     rnd(b["min"]) if b else None, rnd(b["max"]) if b else None, b["n"] if b else None, note])
    bis_q = [Query("http", load.meta("bis", k).get("source_url", "https://stats.bis.org/api/v2/data/dataflow/BIS"),
                   f"GET {load.meta('bis', k).get('source_url', 'https://stats.bis.org/api/v2/data/dataflow/BIS')}",
                   f"data/raw/bis/{k}.csv") for k in (BIS_POLICY, BIS_CPI)]
    files = d["files"] + d["hfiles"]
    chart = Chart(
        id="c13", section="A3", priority="supporting", claims=["H5"], verdicts=["H5"], title="", sentence="",
        form="dot-range",
        sources=["BIS central bank policy rates (WS_CBPOL) and consumer prices (WS_LONG_CPI), stats.bis.org",
                 "NEAR archival RPC (stNEAR and LiNEAR rates, total supply)", "FRED (fredgraph.csv: DFF)"],
        queries=bis_q + [rpc_stnear_query(), rpc_linear_query(), rpc_block_query(), fred_query("DFF"),
                         derived_query("A: policy - CPI YoY (NEAR: y_lst_365 - g_y_lst_365); B: policy - US policy (NEAR: "
                                       "y_lst_365 - effr_mean_365); range = min..max over the last 60 months")],
        table={"columns": ["area", "label", "rate_minus_own_inflation_pp", "rate_minus_own_inflation_month",
                           "rate_minus_own_inflation_min_60m", "rate_minus_own_inflation_max_60m",
                           "rate_minus_own_inflation_months", "rate_minus_us_rate_pp", "rate_minus_us_rate_month",
                           "rate_minus_us_rate_min_60m", "rate_minus_us_rate_max_60m", "rate_minus_us_rate_months",
                           "note"], "rows": rows},
        code_ref="pipeline/models/currencies.py::other_currencies",
        formula_ids=["F-reading-a", "F-reading-b", "F-panel"],
        x={"type": "linear", "unit": "pp", "domain": [-12, 12], "clamp": True}, y={"type": "category", "unit": "pp"},
        panels=[{"id": "a", "label": "Rate minus own inflation", "series_ids": [s.id for s in series if s.panel == "a"]},
                {"id": "b", "label": "Rate minus US rate",
                 "series_ids": [s.id for s in series if s.panel == "b"]}],
        series=series, reference_lines=[{"x": 0, "label": "zero"}],
        coverage=coverage([p["a"]["month"] for p in panel.values() if p["a"]], monthly=True),
        as_of=files_as_of(files),
    )
    if not ready(reg, "n_panel"):
        chart.status = "unavailable"
        chart.title = "NEAR's holding-rate spread against other currencies"
        chart.sentence = "Not computed: there is no BIS policy rate and consumer price data."
        chart.series = []
        chart.empty_state = empty_state("No BIS policy rate and consumer price data in data/raw/bis.",
                                        "BIS WS_CBPOL and WS_LONG_CPI for the panel economies.",
                                        "uv run python -m pipeline.pull --only bis && make charts")
        return chart
    chart.sentence_template = ("Each dot is a currency's rate gap today, its policy rate minus its own inflation on "
                               "the left and minus the US rate on the right, and the gray bar is how far that gap "
                               "swung over five years.")
    chart.caveat_template = ("NEAR's left reading is a different kind of measure, staking yield minus supply "
                             "growth, and the United States sits at zero on the right by construction.")
    quarterly = [panel[a]["label"] for a, _ in PANEL if panel[a]["cpi_quarterly"]]
    chart.notes_templates = notes_for(reg, [
        "The gray bars show each gap's five-year range as context; the comparison in the title uses today's "
        "readings only.",
        "Argentina is excluded: BIS reports no Argentine policy rate after {ar_policy_last_month}.",
        f"{' and '.join(quarterly)} CPI is quarterly." if quarterly else "",
        ("NEAR's row has no gray bar: a range needs at least a year of monthly readings, and NEAR has fewer."
         if any(s is None or s["range"] is None for s in (d["near_sum_a"], d["near_sum_b"])) else ""),
    ])

    def wider(x_key: str, med_key: str, words: tuple[str, str, str]) -> str:
        x, med = abs(reg.value(x_key)), reg.value(med_key)
        return words[0] if x > med else words[1] if x < med else words[2]

    keys = ("near_reading_a", "panel_median_abs_a", "near_reading_b", "panel_median_abs_b", "n_panel")
    if ready(reg, *keys):
        # the title states today's two comparisons H5 is decided on, in words chosen by the data
        lvl = ("wider than", "narrower than", "level with")
        wa, wb = wider("near_reading_a", "panel_median_abs_a", lvl), wider("near_reading_b", "panel_median_abs_b", lvl)
        chart.title_template = (
            f"Against {{n_panel}} economies, NEAR's rate gap today is {wa} the median on rate minus own "
            f"inflation ({{near_reading_a:abs}} against {{panel_median_abs_a}}) and {wb.split()[0]} on rate minus US "
            f"rate ({{near_reading_b:abs}} against {{panel_median_abs_b}}).")
    else:
        chart.status = "partial"
        chart.title = "NEAR's holding-rate spread against other currencies"
        chart.status_banner = ("NEAR's readings are not computed (staking yield or supply growth history is "
                               "missing), so the comparison is not shown.")
    return chart.resolve(reg)
