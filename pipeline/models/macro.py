"""Group B, macro: US money and price measures against NEAR supply growth (chart c11; claims H1b, H2).

Formulas:
  F-yoy     X_t / X_{t-12} - 1 on FRED monthly levels; null if either month is blank. The BLS did not publish
            October 2025 CPI, so CPI and core CPI YoY are null for 2025-10 and 2026-10. Never filled.
  F-growth  NEAR trailing 365-day supply growth, formulas.trailing_growth on the merged full-day RPC samples
            (load.rpc_merged_series('total_supply_near'): history_samples and dense_samples)
            evaluated at month starts with max 7 days to a sample (the same call group A uses for g_365).
            The sample at 00:00 UTC on the 1st closes the previous month, so the value evaluated at
            YYYY-(MM+1)-01 is labelled month YYYY-MM ("month-end g_365").
  F-gap     USD measure YoY - NEAR g at the same month, in pp, at the measure's latest month. A stale common
            month is never reported as the latest gap: if NEAR g is missing at that month the gap is null.

This module also holds the small helpers the other group B modules (market, demand, derivatives, currencies,
sweep) import: upstream-key access, declared constants, coverage, weekly downsampling and query builders.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable

from pipeline import config
from pipeline.chartspec import Chart, Query, Series
from pipeline.models import formulas as F
from pipeline.models import load
from pipeline.models.params import param
from pipeline.numbers import Registry

# ---------------------------------------------------------------------------
# Shared helpers for group B modules
# ---------------------------------------------------------------------------

RPC_ARCHIVAL = "https://archival-rpc.mainnet.fastnear.com"
RPC_TIP = "https://rpc.mainnet.fastnear.com"
HISTORY_FILE = "data/raw/rpc/history_samples.csv"


def upstream(reg: Registry, key: str) -> tuple[Any, str | None]:
    """Value of a key registered by an earlier module, or (None, reason) when it is absent or null."""
    if key not in reg:
        return None, f"{key} was not registered (its module did not run)"
    number = reg.get(key)
    if number.value is None:
        return None, f"{key} is not computed: {number.null_reason}"
    return number.value, None


def ready(reg: Registry, *keys: str) -> bool:
    """True when every key is registered and non-null (safe to use in a title or sentence template)."""
    return all(k in reg and reg.get(k).value is not None for k in keys)


def notes_for(reg: Registry, entries: list) -> list[str]:
    """Chart method notes: each entry a template or a list of variants; the first whose placeholders are all
    registered and non-null wins, and an entry with none is dropped (never printed with a hole in it)."""
    import string
    out = []
    for entry in entries:
        for t in ([entry] if isinstance(entry, str) else entry):
            names = [n for _, n, _, _ in string.Formatter().parse(t or "") if n]
            if t and ready(reg, *names):
                out.append(t)
                break
    return out


def constants_path():
    return config.ROOT / "thesis" / "constants.toml"


def constants() -> dict:
    path = constants_path()
    return tomllib.loads(path.read_text()) if path.exists() else {}


def put_constant(reg: Registry, key: str, name: str, unit: str, fld: str = "value") -> Any:
    """Register a declared constant from thesis/constants.toml (formula K-<name>) once; return its value or None."""
    if key in reg:
        return reg.value(key)
    entry = constants().get(name) or {}
    path = constants_path()
    if fld not in entry:
        reg.put(key, None, unit, f"K-{name}",
                null_reason=f"constant {name}.{fld} is not declared in thesis/constants.toml")
        return None
    reg.put(key, entry[fld], unit, f"K-{name}", source_files=[path],
            inputs=[f"{name}.{fld} declared in thesis/constants.toml ({entry.get('source', 'no source')})"])
    return entry[fld]


def files_as_of(files: Iterable[str]) -> str | None:
    """Latest fetched_at among the sidecars of the raw files read (never the wall clock)."""
    stamps = []
    for rel in sorted(set(files)):
        sidecar = (config.ROOT / rel).with_suffix(".meta.json")
        if sidecar.exists():
            stamp = json.loads(sidecar.read_text()).get("fetched_at")
            if stamp:
                stamps.append(stamp)
    return max(stamps) if stamps else None


def iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def ym(d: date | None) -> str | None:
    return d.strftime("%Y-%m") if d else None


def rnd(x: float | None, dp: int = 4) -> float | None:
    return None if x is None else round(x, dp)


def months(start: date, end: date) -> list[date]:
    """Month starts from start's month through end's month, inclusive."""
    out, m = [], F.month_start(start)
    while m <= end:
        out.append(m)
        m = F.add_months(m, 1)
    return out


def coverage(dates: Iterable[date], *, monthly: bool = False, expected: int | None = None) -> dict:
    """{first, last, n, expected, gaps}; gaps are missing days (or months) between first and last."""
    ds = sorted(set(dates))
    if not ds:
        return {"first": None, "last": None, "n": 0, "expected": expected or 0, "gaps": []}
    label = ym if monthly else iso
    gaps = []
    for a, b in zip(ds, ds[1:]):
        nxt = F.add_months(a, 1) if monthly else a + timedelta(days=1)
        if b > nxt:
            last = F.add_months(b, -1) if monthly else b - timedelta(days=1)
            gaps.append(label(nxt) if nxt == last else f"{label(nxt)}..{label(last)}")
    return {"first": label(ds[0]), "last": label(ds[-1]), "n": len(ds),
            "expected": expected if expected is not None else len(ds), "gaps": gaps}


def weekly(points: Iterable[tuple[date, float | None]], dp: int = 4) -> list[list]:
    """Downsample a daily series to the last point of each ISO week (a null stays null; no filling)."""
    last: dict[tuple[int, int], tuple[date, float | None]] = {}
    for d, v in points:
        last[d.isocalendar()[:2]] = (d, v)
    return [[iso(d), rnd(v, dp)] for d, v in sorted(last.values())]


def fred_query(series_id: str) -> Query:
    meta = load.meta("fred", series_id)
    url = meta.get("source_url") or f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    return Query("http", url, f"GET {url}", f"data/raw/fred/{series_id}.csv")


def rpc_query(method: str, params: dict, endpoint: str = RPC_ARCHIVAL, file: str = HISTORY_FILE) -> Query:
    body = json.dumps({"jsonrpc": "2.0", "id": "near_rpc", "method": method, "params": params}, sort_keys=True)
    return Query("jsonrpc", endpoint, body, file)


def rpc_block_query() -> Query:
    return rpc_query("block", {"block_id": "<height of the last block before 00:00 UTC on each sample date>"})


def rpc_stnear_query() -> Query:
    return rpc_query("query", {"request_type": "call_function", "block_id": "<sample height>",
                               "account_id": "meta-pool.near", "method_name": "get_st_near_price",
                               "args_base64": "e30="})


def rpc_linear_query() -> Query:
    return rpc_query("query", {"request_type": "call_function", "block_id": "<sample height>",
                               "account_id": "linear-protocol.near", "method_name": "ft_price",
                               "args_base64": "e30="})


def derived_query(text: str) -> Query:
    return Query("derived", "registry", text)


def fed_turns(start: date | None = None, end: date | None = None) -> tuple[list[dict], list[str]]:
    """Fed turns: a target-range move (DFEDTARU) in the opposite direction to the previous move."""
    s = load.fred("DFEDTARU")
    turns, prev_v, prev_dir = [], None, 0
    for d, v in s.points:
        if v is None:
            continue
        if prev_v is not None and v != prev_v:
            direction = 1 if v > prev_v else -1
            if prev_dir and direction != prev_dir and (start is None or d >= start) and (end is None or d <= end):
                turns.append({"x": iso(d), "kind": "fed_turn",
                              "label": "Fed turns to hiking" if direction > 0 else "Fed turns to cutting"})
            prev_dir = direction
        prev_v = v
    return turns, s.files


def empty_state(reason: str, needs: str, fix: str = "make data && make charts") -> dict:
    return {"reason": reason, "needs": needs, "fix_command": fix}


# ---------------------------------------------------------------------------
# Macro analysis
# ---------------------------------------------------------------------------

# Fixed panel order: quantity first, then the committed H2 measure, then PCE, then core.
MEASURES = (
    ("m2", "M2SL", "US money supply"),
    ("cpi", "CPIAUCSL", "US consumer prices (headline)"),
    ("pce", "PCEPI", "US PCE prices"),
    ("core", "CPILFESL", "US core consumer prices"),
)
# H2's band and window: thesis/rules.toml [params], pinned to the H2 rule literals by tests/test_params.py
H2_TARGET_PP = param("h2_target_pp")
H2_TOL_PP = param("h2_tol_pp")
H2_WINDOW_MONTHS = int(param("h2_window_months"))


@dataclass
class GPoint:
    month: date               # the month the window closes (label)
    value: float | None       # trailing 365-day growth, fraction
    start: date | None        # sample used at the window start
    end: date | None          # sample used at the window end


@dataclass
class MacroData:
    yoy: dict[str, dict[date, float | None]] = field(default_factory=dict)
    latest: dict[str, date | None] = field(default_factory=dict)
    blanks: dict[str, list[date]] = field(default_factory=dict)
    files: dict[str, list[str]] = field(default_factory=dict)
    g: dict[date, GPoint] = field(default_factory=dict)
    g_files: list[str] = field(default_factory=list)

    def g_value(self, m: date | None) -> float | None:
        p = self.g.get(m) if m else None
        return p.value if p else None

    @property
    def first_g_month(self) -> date | None:
        valid = [m for m, p in sorted(self.g.items()) if p.value is not None]
        return valid[0] if valid else None


def near_g_monthly() -> tuple[dict[date, GPoint], list[str]]:
    """NEAR month-end g_365: trailing_growth at each month start, labelled with the month it closes."""
    s = load.rpc_merged_series("total_supply_near")
    pts = s.points
    nn = s.non_null()
    out: dict[date, GPoint] = {}
    if not nn:
        return out, s.files
    last = nn[-1][0]
    m = F.month_start(nn[0][0])
    while F.add_months(m, 1) <= last + timedelta(days=7):
        end = F.add_months(m, 1)
        g = F.trailing_growth(pts, end, 365, 7)
        a, b = F.nearest(pts, end - timedelta(days=365), 7), F.nearest(pts, end, 7)
        out[m] = GPoint(m, g, a[0] if g is not None else None, b[0] if g is not None else None)
        m = end
    return out, s.files


def analyze() -> MacroData:
    data = MacroData()
    for mid, sid, _ in MEASURES:
        s = load.fred(sid)
        levels = dict(s.points)
        data.files[mid] = s.files
        data.blanks[mid] = sorted(d for d, v in s.points if v is None)
        yo = {m: F.yoy(levels, m) for m in sorted(levels)}
        data.yoy[mid] = yo
        valid = [m for m, v in yo.items() if v is not None]
        data.latest[mid] = max(valid) if valid else None
    data.g, data.g_files = near_g_monthly()
    return data


def _gap(data: MacroData, mid: str) -> tuple[float | None, date | None, str | None]:
    m = data.latest[mid]
    if m is None:
        return None, None, f"no {mid} year-over-year value in data/raw/fred (series missing or too short)"
    g = data.g_value(m)
    if g is None:
        return None, m, (f"NEAR month-end g_365 for {ym(m)} is not computed: needs RPC supply samples within 7 days "
                         f"of {iso(F.add_months(m, 1) - timedelta(days=365))} and {iso(F.add_months(m, 1))}")
    return F.pct(data.yoy[mid][m]) - F.pct(g), m, None


def compute(reg: Registry) -> None:
    data = analyze()
    g_files = data.g_files

    for mid, sid, _ in MEASURES:
        m = data.latest[mid]
        val = F.pct(data.yoy[mid][m]) if m else None
        reason = None if m else f"no {sid} year-over-year value in data/raw/fred"
        key = f"{mid}_yoy"
        reg.put(key, val, "%", "F-yoy", source_files=data.files[mid],
                inputs=[f"{sid} {ym(m)} / {sid} {ym(F.add_months(m, -12))} - 1"] if m else [sid],
                null_reason=reason)
        reg.put(f"{mid}_month", ym(m), "month", "F-yoy", source_files=data.files[mid],
                inputs=[f"latest month with a non-null {sid} year-over-year value"], null_reason=reason)

    for mid, sid, _ in MEASURES:
        gap, m, reason = _gap(data, mid)
        p = data.g.get(m) if m else None
        inputs = [f"{mid}_yoy {ym(m)}"] if m else []
        if p and p.value is not None:
            inputs.append(f"NEAR g_365 month-end {ym(m)} (samples {iso(p.start)} -> {iso(p.end)})")
        reg.put(f"gap_{mid}", gap, "pp", "F-gap", source_files=data.files[mid] + g_files,
                inputs=inputs, null_reason=reason)
        # the NEAR side of the gap: month-end growth for the same month as the US figure (not the daily g_365,
        # whose window ends at the latest RPC sample), so a page can print both inputs and they subtract
        reg.put(f"g_365_at_{mid}", F.pct(p.value) if p and p.value is not None else None, "%", "F-growth",
                source_files=g_files,
                inputs=[f"NEAR g_365 month-end {ym(m)} (samples {iso(p.start)} -> {iso(p.end)})"]
                if p and p.value is not None else [],
                null_reason=None if (p and p.value is not None) else reason)
        # the direction in words, so prose prints {gap_x:abs} {gap_x_dir} and never a sign
        reg.put(f"gap_{mid}_dir", None if gap is None else ("slower" if gap >= 0 else "faster"), "text", "F-gap",
                source_files=data.files[mid] + g_files,
                inputs=[f"gap_{mid} = {mid}_yoy - NEAR g_365: positive -> NEAR grew slower"],
                null_reason=None if gap is not None else f"gap_{mid} is not computed: {reason}")

    # the latest month BLS did not publish (the October 2025 shutdown), shown on the CPI and core panels
    blank = max((d for mid in ("cpi", "core") for d in data.blanks[mid]), default=None)
    reg.put("cpi_blank_month", ym(blank), "month", "F-yoy", source_files=data.files["cpi"] + data.files["core"],
            inputs=["latest blank observation in CPIAUCSL or CPILFESL"] if blank else [],
            null_reason=None if blank else "CPIAUCSL and CPILFESL have no blank month")

    # H1b context: the months since the first valid window where NEAR g < M2 YoY.
    first = data.first_g_month
    m2_last = data.latest["m2"]
    if first is None or m2_last is None or m2_last < first:
        reason = ("NEAR g_365 has no valid month-end window (RPC supply samples a year apart are missing)"
                  if first is None else "no M2 year-over-year month at or after the first valid NEAR window")
        for key, unit in (("h1b_months_with_data", "count"), ("h1b_months_expected", "count"),
                          ("h1b_months_below", "count"), ("h1b_below_run", "text")):
            reg.put(key, None, unit, "F-share-months", source_files=data.files["m2"] + g_files, null_reason=reason)
    else:
        span = months(first, m2_last)
        both = [m for m in span if data.g_value(m) is not None and data.yoy["m2"].get(m) is not None]
        below = [m for m in both if data.g_value(m) < data.yoy["m2"][m]]
        inputs = [f"months {ym(first)}..{ym(m2_last)}", f"{len(below)} of {len(both)} months below M2 YoY"]
        reg.put("h1b_months_with_data", len(both), "count", "F-share-months",
                source_files=data.files["m2"] + g_files, inputs=inputs)
        reg.put("h1b_months_expected", len(span), "count", "F-share-months",
                source_files=data.files["m2"] + g_files, inputs=inputs)
        # how many months, and which runs of them, NEAR's growth sat below US money growth
        reg.put("h1b_months_below", len(below), "count", "F-share-months",
                source_files=data.files["m2"] + g_files, inputs=inputs)
        runs: list[list[date]] = []
        for m in below:
            if runs and F.add_months(runs[-1][-1], 1) == m:
                runs[-1].append(m)
            else:
                runs.append([m])
        parts = [ym(r[0]) if len(r) == 1 else f"{ym(r[0])} to {ym(r[-1])}" for r in runs]
        text = (", ".join(parts[:-1]) + " and " + parts[-1]) if len(parts) > 1 else (parts[0] if parts else None)
        reg.put("h1b_below_run", text, "text", "F-share-months", source_files=data.files["m2"] + g_files,
                inputs=inputs, null_reason=None if text else "no month with NEAR's growth below M2 YoY")

    # H2: months in the band |gap_cpi - 1.5| <= 0.5 over the last 12 months ending at the gap month.
    gap_cpi, cpi_m, reason = _gap(data, "cpi")
    files = data.files["cpi"] + g_files
    if gap_cpi is None:
        for key in ("h2_months_in_band_12", "h2_months_with_data_12"):
            reg.put(key, None, "count", "F-band-months", source_files=files,
                    null_reason=f"gap_cpi is not computed: {reason}")
    else:
        window = [F.add_months(cpi_m, -k) for k in range(H2_WINDOW_MONTHS - 1, -1, -1)]
        gaps = {}
        for m in window:
            y, g = data.yoy["cpi"].get(m), data.g_value(m)
            if y is not None and g is not None:
                gaps[m] = F.pct(y) - F.pct(g)
        in_band = [m for m, v in gaps.items() if abs(v - H2_TARGET_PP) <= H2_TOL_PP]
        no_data = [ym(m) for m in window if m not in gaps]
        inputs = [f"months {ym(window[0])}..{ym(window[-1])}",
                  f"band: |gap_cpi - {H2_TARGET_PP}| <= {H2_TOL_PP} pp",
                  "months without data: " + (", ".join(no_data) if no_data else "none")]
        reg.put("h2_months_in_band_12", len(in_band), "count", "F-band-months", source_files=files, inputs=inputs)
        reg.put("h2_months_with_data_12", len(gaps), "count", "F-band-months", source_files=files, inputs=inputs)


# ---------------------------------------------------------------------------
# Chart c11
# ---------------------------------------------------------------------------

def charts(reg: Registry) -> list[Chart]:
    return [inflation_measures(reg)]


def inflation_measures(reg: Registry) -> Chart:
    """c11 inflation-measures: NEAR month-end g_365 against four US measures, 2x2 small multiples."""
    data = analyze()
    all_files = sorted({f for fs in data.files.values() for f in fs} | set(data.g_files))
    usd_last = [m for m in data.latest.values() if m]
    g_valid = [m for m, p in data.g.items() if p.value is not None]

    if usd_last:
        end = max(usd_last + g_valid)
        start = data.first_g_month or F.add_months(max(usd_last), -59)
        span = months(start, end)
    else:
        span = []

    series, panels, rows = [], [], []
    for mid, sid, label in MEASURES:
        near_pts = [[iso(m), rnd(F.pct(data.g_value(m)))] for m in span]
        usd_pts = [[iso(m), rnd(F.pct(data.yoy[mid].get(m)))] for m in span]
        series.append(Series(f"near_{mid}", "NEAR supply growth, trailing year", "near", near_pts,
                             role="emphasis", unit="%", panel=mid))
        series.append(Series(f"usd_{mid}", f"{label}, year over year", "usd", usd_pts,
                             role="emphasis", unit="%", panel=mid))
        panels.append({"id": mid, "label": label, "series_ids": [f"near_{mid}", f"usd_{mid}"], "fred_id": sid})

    for m in span:
        g = data.g_value(m)
        p = data.g.get(m)
        row = [ym(m), rnd(F.pct(g)), iso(p.start) if p else None, iso(p.end) if p else None]
        for mid, _, _ in MEASURES:
            row.append(rnd(F.pct(data.yoy[mid].get(m))))
        for mid, _, _ in MEASURES:
            y = data.yoy[mid].get(m)
            row.append(rnd(F.pct(y) - F.pct(g)) if (y is not None and g is not None) else None)
        rows.append(row)

    # BLS blanks, on the panels they affect only; short labels here, the full statement is in the sentence
    annotations = []
    shown = set(span)
    for mid in ("cpi", "core"):
        for d in data.blanks[mid]:
            for m in (d, F.add_months(d, 12)):
                if m in shown and not any(a["x"] == iso(m) and a["panel"] == mid for a in annotations):
                    label = "Not published (shutdown)" if m == d else "No YoY: year-ago gap"
                    annotations.append({"x": iso(m), "label": label, "kind": "data_boundary", "panel": mid})
    blanks_shown = bool(annotations)

    queries = [fred_query(sid) for _, sid, _ in MEASURES] + [rpc_block_query()]
    chart = Chart(
        id="c11", section="A1", priority="lead", claims=["H1b", "H2"], verdicts=["H1b", "H2"],
        title="", sentence="", form="small-multiples",
        sources=["FRED (fredgraph.csv: M2SL, CPIAUCSL, PCEPI, CPILFESL)",
                 f"NEAR archival RPC ({RPC_ARCHIVAL}, block header total_supply)"],
        queries=queries,
        table={"columns": ["month", "near_g_365_pct", "near_window_start_sample", "near_window_end_sample",
                           "m2_yoy_pct", "cpi_yoy_pct", "pce_yoy_pct", "core_cpi_yoy_pct",
                           "gap_m2_pp", "gap_cpi_pp", "gap_pce_pp", "gap_core_pp"], "rows": rows},
        code_ref="pipeline/models/macro.py::inflation_measures",
        formula_ids=["F-yoy", "F-growth", "F-gap"],
        x={"type": "time", "unit": "month"}, y={"unit": "%"}, panels=panels, series=series,
        reference_lines=[{"y": 0, "label": "zero"}], annotations=annotations,
        coverage=coverage(span, monthly=True, expected=len(span)) | {
            "gaps": [ym(m) for m in span if data.g_value(m) is None]},
        as_of=files_as_of(all_files),
    )

    caveat = ("NEAR supply is a quantity of tokens while consumer prices are a price index, so only the money-supply "
              "panel compares like with like.")
    usd_sentence = ("In each panel blue is NEAR supply growth over the trailing year and orange is one US measure's "
                    "year-over-year change.")
    chart.caveat_template = caveat
    if not span:
        chart.status = "unavailable"
        chart.title = "NEAR supply growth against US money and prices"
        chart.sentence = "Not computed: no FRED money or price series was pulled."
        chart.empty_state = empty_state("No FRED money or price series is in data/raw.",
                                        "FRED M2SL, CPIAUCSL, PCEPI and CPILFESL.",
                                        "uv run python -m pipeline.pull --only fred")
        chart.table = {"columns": chart.table["columns"], "rows": []}
        return chart
    chart.notes_templates = notes_for(reg, [
        "NEAR's supply grew slower than US money in {h1b_months_below} of {h1b_months_expected} months, in "
        "{h1b_below_run}.",
        "US data run to {cpi_month} for consumer prices, {m2_month} for the money supply and {pce_month} for PCE.",
        "BLS published no consumer prices for {cpi_blank_month} (federal shutdown), so the CPI and core panels have "
        "no value for that month and no year-over-year change a year later." if blanks_shown else "",
        "Each gap compares NEAR's growth over the year to the end of a US month with the US figure for that month: "
        "over the year to {cpi_month} NEAR's supply grew {g_365_at_cpi} against CPI's {cpi_yoy}.",
        "The headline {g_365} runs to the latest chain sample ({latest_rpc_date}), so it differs from the month-end "
        "figure.",
    ])
    if ready(reg, "gap_m2", "gap_cpi", "gap_core", "cpi_month", "m2_month", "pce_month", "core_month"):
        # gaps are trailing-year figures as of a month: the title names the month (one, when all three agree)
        if reg.value("m2_month") == reg.value("cpi_month") == reg.value("core_month"):
            chart.title_template = ("Over the year to {cpi_month}, NEAR's supply grew {gap_m2:abs} {gap_m2_dir} than "
                                    "the US money supply, {gap_cpi:abs} {gap_cpi_dir} than CPI and {gap_core:abs} "
                                    "{gap_core_dir} than core CPI.")
        else:
            chart.title_template = ("NEAR's supply grew {gap_m2:abs} {gap_m2_dir} than the US money supply over the "
                                    "year to {m2_month}, {gap_cpi:abs} {gap_cpi_dir} than CPI over the year to "
                                    "{cpi_month} and {gap_core:abs} {gap_core_dir} than core CPI over the year to "
                                    "{core_month}.")
        chart.sentence_template = usd_sentence
    else:
        chart.status = "partial"
        chart.title = "NEAR supply growth against US money and prices"
        chart.sentence_template = usd_sentence
        chart.status_banner = ("NEAR supply growth is missing for the latest US month: the RPC supply samples a year "
                               "apart are not both present, so the gaps are not computed.")
    return chart.resolve(reg)
