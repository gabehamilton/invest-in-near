"""Group B, macro: US money and price measures against NEAR supply growth (chart c11; claim H1).

Formulas:
  F-yoy     X_t / X_{t-12} - 1 on FRED monthly levels; null if either month is blank. The BLS did not publish
            October 2025 CPI, so CPI and core CPI YoY are null for 2025-10 and 2026-10. Never filled.
  F-growth  NEAR trailing 365-day supply growth, formulas.trailing_growth on the merged full-day RPC samples
            (load.rpc_merged_series('total_supply_near'): history_samples and dense_samples)
            evaluated at month starts with max 7 days to a sample (the same call group A uses for g_365).
            The sample at 00:00 UTC on the 1st closes the previous month, so the value evaluated at
            YYYY-(MM+1)-01 is labelled month YYYY-MM ("month-end g_365").
  F-gap     USD measure YoY at its latest month - g_365 (NEAR's supply growth over the year to the latest full-day
            RPC sample), in pp: each side over its own latest year, so NEAR's figure is the one every page quotes.
            g_365_at_m2 (NEAR's month-end growth over the same twelve months as M2's latest reading) is registered
            for the data page's same-months check only. The monthly series (chart c11, h1_months_*) stay
            month-matched.
  F-cut-gap Chart c23 (claim H2): from M0, the first month after cut_date whose CPI base (M0-1) is published and
            which has a full-day supply sample on its 1st, to M1, the last month with a published CPI level and
            a sample on the 1st of M1+1. h2_cpi = (CPI_M1 / CPI_{M0-1})^(12/n) - 1 over the n months M0..M1;
            h2_near_g = F-growth between the samples on the 1st of M0 and of M1+1; h2_gap = h2_cpi - h2_near_g.
            The same per month gives the bars: (CPI_m / CPI_{m-1})^12 - 1 against F-growth over month m.

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
from pipeline.models import load, supply
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

# Fixed panel order: quantity first, then the CPI measure H2 reads, then PCE, then core.
MEASURES = (
    ("m2", "M2SL", "US money supply"),
    ("cpi", "CPIAUCSL", "US consumer prices (headline)"),
    ("pce", "PCEPI", "US PCE prices"),
    ("core", "CPILFESL", "US core consumer prices"),
)


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


def _gap(data: MacroData, mid: str, g365: float | None, why_g: str | None) -> tuple[float | None, date | None,
                                                                                 str | None]:
    """F-gap: the measure's latest year-over-year change minus g_365 (percent), each over its own latest year."""
    m = data.latest[mid]
    if m is None:
        return None, None, f"no {mid} year-over-year value in data/raw/fred (series missing or too short)"
    if g365 is None:
        return None, m, why_g
    return F.pct(data.yoy[mid][m]) - g365, m, None


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

    # the NEAR side of every gap is g_365, its supply growth over the year to the latest full-day sample: the one
    # figure for NEAR's latest year that every page quotes; the US side is each measure's own latest year
    g365, why_g = upstream(reg, "g_365")
    g365_files = list(reg.get("g_365").source_files) if "g_365" in reg else []
    g365_window = "; ".join(reg.get("g_365").inputs) if "g_365" in reg else ""
    for mid, sid, _ in MEASURES:
        gap, m, reason = _gap(data, mid, g365, why_g)
        inputs = ([f"{mid}_yoy {ym(m)}"] if m else []) + ([f"g_365 ({g365_window})"] if g365 is not None else [])
        reg.put(f"gap_{mid}", gap, "pp", "F-gap", source_files=sorted(set(data.files[mid] + g365_files)),
                inputs=inputs, null_reason=reason)
        # the direction in words, so prose prints {gap_x:abs} {gap_x_dir} and never a sign
        reg.put(f"gap_{mid}_dir", None if gap is None else ("slower" if gap >= 0 else "faster"), "text", "F-gap",
                source_files=sorted(set(data.files[mid] + g365_files)),
                inputs=[f"gap_{mid} = {mid}_yoy - g_365: positive -> NEAR grew slower"],
                null_reason=None if gap is not None else f"gap_{mid} is not computed: {reason}")

    # the data page's same-months check on H1: NEAR's month-end growth over the same twelve months as the dollar's
    # latest money reading (a month-matched window, which can take in weeks before the issuance ceiling cut)
    m = data.latest["m2"]
    p = data.g.get(m) if m else None
    ok = p is not None and p.value is not None
    reg.put("g_365_at_m2", F.pct(p.value) if ok else None, "%", "F-growth", source_files=g_files,
            inputs=[f"NEAR g_365 month-end {ym(m)} (samples {iso(p.start)} -> {iso(p.end)})"] if ok else [],
            null_reason=None if ok else (
                "no m2 year-over-year value in data/raw/fred" if m is None else
                f"NEAR month-end g_365 for {ym(m)} is not computed: needs RPC supply samples within 7 days of "
                f"{iso(F.add_months(m, 1) - timedelta(days=365))} and {iso(F.add_months(m, 1))}"))
    reg.put("g_365_at_m2_from", iso(p.start) if ok else None, "date", "F-growth", source_files=g_files,
            inputs=[f"first sample of NEAR's month-end window for {ym(m)}"] if ok else [],
            null_reason=None if ok else "g_365_at_m2 is not computed")

    # the latest month BLS did not publish (the October 2025 shutdown), shown on the CPI and core panels
    blank = max((d for mid in ("cpi", "core") for d in data.blanks[mid]), default=None)
    reg.put("cpi_blank_month", ym(blank), "month", "F-yoy", source_files=data.files["cpi"] + data.files["core"],
            inputs=["latest blank observation in CPIAUCSL or CPILFESL"] if blank else [],
            null_reason=None if blank else "CPIAUCSL and CPILFESL have no blank month")

    # H1 context: the months since the first valid window where NEAR g < M2 YoY.
    first = data.first_g_month
    m2_last = data.latest["m2"]
    if first is None or m2_last is None or m2_last < first:
        reason = ("NEAR g_365 has no valid month-end window (RPC supply samples a year apart are missing)"
                  if first is None else "no M2 year-over-year month at or after the first valid NEAR window")
        for key, unit in (("h1_months_with_data", "count"), ("h1_months_expected", "count"),
                          ("h1_months_below", "count"), ("h1_below_run", "text")):
            reg.put(key, None, unit, "F-share-months", source_files=data.files["m2"] + g_files, null_reason=reason)
    else:
        span = months(first, m2_last)
        both = [m for m in span if data.g_value(m) is not None and data.yoy["m2"].get(m) is not None]
        below = [m for m in both if data.g_value(m) < data.yoy["m2"][m]]
        inputs = [f"months {ym(first)}..{ym(m2_last)}", f"{len(below)} of {len(both)} months below M2 YoY"]
        reg.put("h1_months_with_data", len(both), "count", "F-share-months",
                source_files=data.files["m2"] + g_files, inputs=inputs)
        reg.put("h1_months_expected", len(span), "count", "F-share-months",
                source_files=data.files["m2"] + g_files, inputs=inputs)
        # how many months, and which runs of them, NEAR's growth sat below US money growth
        reg.put("h1_months_below", len(below), "count", "F-share-months",
                source_files=data.files["m2"] + g_files, inputs=inputs)
        runs: list[list[date]] = []
        for m in below:
            if runs and F.add_months(runs[-1][-1], 1) == m:
                runs[-1].append(m)
            else:
                runs.append([m])
        parts = [ym(r[0]) if len(r) == 1 else f"{ym(r[0])} to {ym(r[-1])}" for r in runs]
        text = (", ".join(parts[:-1]) + " and " + parts[-1]) if len(parts) > 1 else (parts[0] if parts else None)
        reg.put("h1_below_run", text, "text", "F-share-months", source_files=data.files["m2"] + g_files,
                inputs=inputs, null_reason=None if text else "no month with NEAR's growth below M2 YoY")

    _put_since_cut(reg, since_cut_window(reg))


# ---------------------------------------------------------------------------
# H2: the matched window since the issuance ceiling cut (chart c23)
# ---------------------------------------------------------------------------

NO_WINDOW = "no month after cut_date has both a published CPI base and full-day supply samples"
NO_CUT = "cut_date is not registered"


@dataclass
class CutMonth:
    month: date
    r0: dict | None            # full-day sample on the 1st of the month
    r1: dict | None            # full-day sample on the 1st of the next month
    near: float | None         # F-growth between r0 and r1, annualized (fraction)
    cpi: float | None          # (CPI_m / CPI_{m-1})^12 - 1 (fraction)
    why: list[str] = field(default_factory=list)   # why a month has no reading

    @property
    def gap_pp(self) -> float | None:
        return F.pct(self.cpi) - F.pct(self.near) if self.cpi is not None and self.near is not None else None


@dataclass
class CutWindow:
    cut: date | None
    reason: str | None                       # set when nothing can be computed (no cut, no samples, no CPI)
    cpi: dict[date, float | None] = field(default_factory=dict)
    cpi_files: list[str] = field(default_factory=list)
    smp_files: list[str] = field(default_factory=list)
    cut_files: list[str] = field(default_factory=list)
    candidates: list[CutMonth] = field(default_factory=list)
    m0: date | None = None
    m1: date | None = None
    r0: dict | None = None
    r1: dict | None = None
    near: float | None = None
    cpi_g: float | None = None
    alt: date | None = None
    cpi_alt: float | None = None
    since_r0: dict | None = None
    since_r1: dict | None = None
    since_g: float | None = None

    @property
    def n(self) -> int | None:
        return None if self.m0 is None or self.m1 is None else \
            (self.m1.year - self.m0.year) * 12 + self.m1.month - self.m0.month + 1

    @property
    def gap_pp(self) -> float | None:
        return F.pct(self.cpi_g) - F.pct(self.near) if self.cpi_g is not None and self.near is not None else None

    @property
    def gap_alt_pp(self) -> float | None:
        return F.pct(self.cpi_alt) - F.pct(self.near) if self.cpi_alt is not None and self.near is not None else None

    @property
    def files(self) -> list[str]:
        return self.cpi_files + self.smp_files + self.cut_files


def _cpi_missing(cpi: dict[date, float | None], m: date) -> str | None:
    if m not in cpi:
        return f"no CPI level for {ym(m)} in data/raw/fred/CPIAUCSL.csv"
    if cpi[m] is None:
        return f"CPI for {ym(m)} not published"
    return None


def since_cut_window(reg: Registry) -> CutWindow:
    """c23 / H2: the per-month readings and the matched window from the first month after cut_date (F-cut-gap)."""
    cut_s = reg.value("cut_date") if "cut_date" in reg else None
    cut = date.fromisoformat(cut_s) if cut_s else None
    s = load.fred("CPIAUCSL")
    smp = supply.load_samples()
    w = CutWindow(cut, None, dict(s.points), list(s.files), list(smp.files),
                  list(reg.get("cut_date").source_files) if "cut_date" in reg else [])
    latest = smp.latest_full()
    if cut is None:
        w.reason = NO_CUT
        return w
    if latest is None:
        w.reason = "no full-day supply samples in data/raw/rpc/history_samples.csv or dense_samples.csv"
        return w
    if not any(v is not None for v in w.cpi.values()):
        w.reason = "no CPIAUCSL levels in data/raw/fred/CPIAUCSL.csv"
        return w
    edges: dict[date, dict | None] = {}

    def edge0(d: date) -> dict | None:
        if d not in edges:
            edges[d] = smp.edge(d, "total_supply_near", max_days=0)
        return edges[d]

    m = F.add_months(F.month_start(cut), 1)          # the first month that starts after the cut
    while F.add_months(m, 1) <= latest["date"]:
        nxt = F.add_months(m, 1)
        r0, r1 = edge0(m), edge0(nxt)
        why = [x for x in (_cpi_missing(w.cpi, F.add_months(m, -1)), _cpi_missing(w.cpi, m)) if x]
        why += [f"no full-day supply sample on {iso(d)}" for d, r in ((m, r0), (nxt, r1)) if r is None]
        near = supply.growth(r0, r1)
        c0, c1 = w.cpi.get(F.add_months(m, -1)), w.cpi.get(m)
        c = (c1 / c0) ** 12 - 1 if c0 and c1 else None
        if near is None and r0 is not None and r1 is not None:
            why.append(f"supply growth not computable between {iso(m)} and {iso(nxt)}")
        w.candidates.append(CutMonth(m, r0, r1, near, c, why))
        m = nxt

    months_ = [c.month for c in w.candidates]
    w.m0 = next((mm for mm in months_ if w.cpi.get(F.add_months(mm, -1)) and edge0(mm)), None)
    w.m1 = next((mm for mm in reversed(months_) if w.cpi.get(mm) and edge0(F.add_months(mm, 1))), None)
    if w.m0 is not None and w.m1 is not None and w.m1 >= w.m0:
        w.r0, w.r1 = edge0(w.m0), edge0(F.add_months(w.m1, 1))
        w.near = supply.growth(w.r0, w.r1)
        w.cpi_g = (w.cpi[w.m1] / w.cpi[F.add_months(w.m0, -1)]) ** (12 / w.n) - 1
        # context: the CPI side re-based on the last level published before the cut's month
        alt = F.add_months(F.month_start(cut), -1)
        first_level = min((d for d, v in w.cpi.items() if v is not None), default=None)
        while first_level is not None and alt >= first_level and not w.cpi.get(alt):
            alt = F.add_months(alt, -1)
        if first_level is not None and alt >= first_level and w.cpi.get(alt):
            w.alt = alt
            n_alt = (w.m1.year - alt.year) * 12 + w.m1.month - alt.month
            w.cpi_alt = (w.cpi[w.m1] / w.cpi[alt]) ** (12 / n_alt) - 1
    else:
        w.m0 = w.m1 = None
    # context only, never in a rule: NEAR from the first full-day sample after the cut day to the latest
    w.since_r0, w.since_r1 = smp.edge(cut + timedelta(days=1), max_days=0), latest
    w.since_g = supply.growth(w.since_r0, w.since_r1)
    return w


def _put_since_cut(reg: Registry, w: CutWindow) -> None:
    files = w.files
    window_keys = (("h2_from", "date"), ("h2_to", "date"), ("h2_months", "count"), ("h2_near_g", "%"),
                   ("h2_cpi", "%"), ("h2_gap", "pp"), ("h2_gap_dir", "text"), ("h2_base", "month"))
    alt_keys = (("h2_alt_base", "month"), ("h2_cpi_alt", "%"), ("h2_gap_alt", "pp"))
    month_keys = (("h2_months_read", "count"), ("h2_months_slower", "count"), ("h2_months_blank", "text"))
    since_keys = (("g_since_cut", "%"), ("g_since_cut_from", "date"), ("g_since_cut_to", "date"))
    if w.reason:
        for key, unit in window_keys + alt_keys + month_keys + since_keys:
            reg.put(key, None, unit, "F-growth" if key.startswith("g_since") else "F-cut-gap",
                    source_files=files, null_reason=w.reason)
        return

    if w.m0 is None:
        for key, unit in window_keys + alt_keys:
            reg.put(key, None, unit, "F-cut-gap", source_files=files, null_reason=NO_WINDOW)
    else:
        base, to = F.add_months(w.m0, -1), F.add_months(w.m1, 1)
        inputs = [f"cut_date {iso(w.cut)}; months {ym(w.m0)}..{ym(w.m1)} (n = {w.n})",
                  f"CPIAUCSL {ym(w.m1)} / CPIAUCSL {ym(base)}, annualized ^(12/{w.n})",
                  f"supply samples {supply.sample_ref(w.r0)} -> {supply.sample_ref(w.r1)}"]
        gap = w.gap_pp
        reg.put("h2_from", iso(w.m0), "date", "F-cut-gap", source_files=files, inputs=inputs)
        reg.put("h2_to", iso(to), "date", "F-cut-gap", source_files=files, inputs=inputs)
        reg.put("h2_months", w.n, "count", "F-cut-gap", source_files=files, inputs=inputs)
        reg.put("h2_near_g", F.pct(w.near), "%", "F-cut-gap", source_files=w.smp_files + w.cut_files, inputs=inputs,
                null_reason=None if w.near is not None else "supply growth is not computable between "
                f"{iso(w.m0)} and {iso(to)}")
        reg.put("h2_cpi", F.pct(w.cpi_g), "%", "F-cut-gap", source_files=w.cpi_files + w.cut_files, inputs=inputs)
        reg.put("h2_gap", gap, "pp", "F-cut-gap", source_files=files, inputs=inputs,
                null_reason=None if gap is not None else "h2_near_g is not computed")
        reg.put("h2_gap_dir", None if gap is None else ("slower" if gap >= 0 else "faster"), "text", "F-cut-gap",
                source_files=files, inputs=["h2_gap = h2_cpi - h2_near_g: positive -> NEAR grew slower"],
                null_reason=None if gap is not None else "h2_gap is not computed")
        reg.put("h2_base", ym(base), "month", "F-cut-gap", source_files=w.cpi_files + w.cut_files, inputs=inputs)
        if w.alt is None:
            reason = f"no published CPI level before {ym(F.month_start(w.cut))} in data/raw/fred/CPIAUCSL.csv"
            for key, unit in alt_keys:
                reg.put(key, None, unit, "F-cut-gap", source_files=files, null_reason=reason)
        else:
            n_alt = (w.m1.year - w.alt.year) * 12 + w.m1.month - w.alt.month
            alt_inputs = [f"CPIAUCSL {ym(w.m1)} / CPIAUCSL {ym(w.alt)}, annualized ^(12/{n_alt})",
                          f"supply samples {supply.sample_ref(w.r0)} -> {supply.sample_ref(w.r1)}"]
            gap_alt = w.gap_alt_pp
            reg.put("h2_alt_base", ym(w.alt), "month", "F-cut-gap", source_files=w.cpi_files + w.cut_files,
                    inputs=[f"latest month before {ym(F.month_start(w.cut))} with a published CPIAUCSL level"])
            reg.put("h2_cpi_alt", F.pct(w.cpi_alt), "%", "F-cut-gap", source_files=w.cpi_files + w.cut_files,
                    inputs=alt_inputs)
            reg.put("h2_gap_alt", gap_alt, "pp", "F-cut-gap", source_files=files, inputs=alt_inputs,
                    null_reason=None if gap_alt is not None else "h2_near_g is not computed")

    read = [c for c in w.candidates if c.gap_pp is not None]
    blank = [c for c in w.candidates if c.gap_pp is None]
    m_inputs = [f"candidate months {ym(w.candidates[0].month)}..{ym(w.candidates[-1].month)}"
                if w.candidates else "no candidate month after cut_date yet"]
    reg.put("h2_months_read", len(read), "count", "F-cut-gap", source_files=files, inputs=m_inputs)
    reg.put("h2_months_slower", sum(1 for c in read if c.gap_pp > 0), "count", "F-cut-gap", source_files=files,
            inputs=m_inputs)
    text = ", ".join(f"{ym(c.month)} ({'; '.join(c.why) if c.why else 'not computed'})" for c in blank)
    reg.put("h2_months_blank", text or None, "text", "F-cut-gap", source_files=files, inputs=m_inputs,
            null_reason=None if text else ("every candidate month has a reading" if w.candidates
                                           else "no candidate month after cut_date yet"))

    a, b = w.since_r0, w.since_r1
    reason = (None if w.since_g is not None else
              f"no full-day supply sample on {iso(w.cut + timedelta(days=1))}" if a is None else
              "supply growth is not computable from the first sample after cut_date to the latest")
    s_inputs = [f"supply samples {supply.sample_ref(a)} -> {supply.sample_ref(b)}"] if a and b else []
    reg.put("g_since_cut", F.pct(w.since_g), "%", "F-growth", source_files=w.smp_files + w.cut_files,
            inputs=s_inputs, null_reason=reason)
    reg.put("g_since_cut_from", iso(a["date"]) if a else None, "date", "F-growth",
            source_files=w.smp_files + w.cut_files, inputs=s_inputs, null_reason=None if a else reason)
    reg.put("g_since_cut_to", iso(b["date"]) if b else None, "date", "F-growth", source_files=w.smp_files,
            inputs=s_inputs, null_reason=None if b else reason)


# ---------------------------------------------------------------------------
# Charts c11 and c23
# ---------------------------------------------------------------------------

def charts(reg: Registry) -> list[Chart]:
    return [inflation_measures(reg), since_cut(reg)]


def cpi_blank_annotations(blanks: Iterable[date], shown: Iterable[date], panel: str | None = None) -> list[dict]:
    """data_boundary annotations for blank CPI levels (BLS did not publish them): the blank month itself and the
    month a year later, whose year-over-year change has no base. Only months in `shown` are marked, once each."""
    shown = set(shown)
    out: list[dict] = []
    for d in sorted(blanks):
        for m, label in ((d, "Not published (shutdown)"), (F.add_months(d, 12), "No YoY: year-ago gap")):
            if m in shown and not any(a["x"] == iso(m) for a in out):
                out.append({"x": iso(m), "label": label, "kind": "data_boundary"} | ({"panel": panel} if panel else {}))
    return out


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
    annotations = [a for mid in ("cpi", "core") for a in cpi_blank_annotations(data.blanks[mid], span, mid)]
    blanks_shown = bool(annotations)

    queries = [fred_query(sid) for _, sid, _ in MEASURES] + [rpc_block_query()]
    chart = Chart(
        id="c11", section="A1", priority="lead", claims=["H1"], verdicts=["H1"],
        title="", sentence="", form="small-multiples",
        sources=["FRED (fredgraph.csv: M2SL, CPIAUCSL, PCEPI, CPILFESL)",
                 f"NEAR archival RPC ({RPC_ARCHIVAL}, block header total_supply)"],
        queries=queries,
        table={"columns": ["month", "near_g_365_pct", "near_window_start_sample", "near_window_end_sample",
                           "m2_yoy_pct", "cpi_yoy_pct", "pce_yoy_pct", "core_cpi_yoy_pct",
                           "gap_m2_pp", "gap_cpi_pp", "gap_pce_pp", "gap_core_pp"], "rows": rows,
               # the rows are month-matched; the title's NEAR figure is its latest year, which no row holds
               "footnote": ("Each row sets NEAR's supply growth over the twelve months to that month's end beside "
                            "the US figures for the same month. The title uses NEAR's latest year instead, to the "
                            "latest chain sample.")},
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
        "NEAR's supply grew slower than US money in {h1_months_below} of {h1_months_expected} months, in "
        "{h1_below_run}.",
        "US data run to {cpi_month} for consumer prices, {m2_month} for the money supply and {pce_month} for PCE.",
        "BLS published no consumer prices for {cpi_blank_month} (federal shutdown), so the CPI and core panels have "
        "no value for that month and no year-over-year change a year later." if blanks_shown else "",
        "The title sets NEAR's supply growth over the year to {latest_rpc_date}, {g_365}, against each US figure "
        "over the year to its latest month; month by month, the lines compare the same twelve months.",
    ])
    if ready(reg, "gap_m2", "gap_cpi", "gap_core", "cpi_month", "m2_month", "pce_month", "core_month",
             "latest_rpc_date"):
        # each side over its own latest year: NEAR's to the latest chain sample, each US figure's to its month (one
        # month, when all three agree)
        if reg.value("m2_month") == reg.value("cpi_month") == reg.value("core_month"):
            chart.title_template = ("Over the year to {latest_rpc_date}, NEAR's supply grew {gap_m2:abs} {gap_m2_dir} "
                                    "than the US money supply, {gap_cpi:abs} {gap_cpi_dir} than CPI and "
                                    "{gap_core:abs} {gap_core_dir} than core CPI, each over the year to {cpi_month}.")
        else:
            chart.title_template = ("Over the year to {latest_rpc_date}, NEAR's supply grew {gap_m2:abs} {gap_m2_dir} "
                                    "than the US money supply over the year to {m2_month}, {gap_cpi:abs} "
                                    "{gap_cpi_dir} than CPI over the year to {cpi_month} and {gap_core:abs} "
                                    "{gap_core_dir} than core CPI over the year to {core_month}.")
        chart.sentence_template = usd_sentence
    else:
        chart.status = "partial"
        chart.title = "NEAR supply growth against US money and prices"
        chart.sentence_template = usd_sentence
        chart.status_banner = ("NEAR's supply growth over the latest year or a latest US figure is missing, so the "
                               "gaps are not computed.")
    return chart.resolve(reg)


def since_cut(reg: Registry) -> Chart:
    """c23 since-the-cut (H2): one month's US consumer-price inflation minus NEAR's supply growth over the same
    month, both annualized, for every month since the issuance ceiling cut; the reference line is the matched
    window's gap (F-cut-gap)."""
    w = since_cut_window(reg)
    files = sorted(set(w.files))
    rows = []
    for c in w.candidates:
        rows.append([ym(c.month), iso(c.r0["date"]) if c.r0 else None, iso(c.r1["date"]) if c.r1 else None,
                     rnd(F.pct(c.near)), rnd(F.pct(c.cpi)), rnd(c.gap_pp)])
    if ready(reg, "h2_from", "h2_to", "h2_near_g", "h2_cpi", "h2_gap"):
        rows.append(["whole window", reg.value("h2_from"), reg.value("h2_to"), reg.value("h2_near_g"),
                     reg.value("h2_cpi"), reg.value("h2_gap")])
        if ready(reg, "h2_alt_base", "h2_cpi_alt", "h2_gap_alt"):
            rows.append(["whole window, prices from the last level before the cut", reg.value("h2_alt_base"),
                         reg.value("h2_to"), reg.value("h2_near_g"), reg.value("h2_cpi_alt"),
                         reg.value("h2_gap_alt")])
    annotations = []
    for c in w.candidates:
        if c.gap_pp is None:
            label = ("No CPI for the month before" if _cpi_missing(w.cpi, F.add_months(c.month, -1)) else
                     "No CPI for the month" if _cpi_missing(w.cpi, c.month) else "No supply sample")
            annotations.append({"x": ym(c.month), "label": label, "kind": "data_boundary"})
    refs = [{"y": 0, "label": "same pace"}]
    if ready(reg, "h2_gap"):
        refs.append({"y": reg.value("h2_gap"), "label": "whole window, a year"})
    chart = Chart(
        id="c23", section="A1", priority="lead", claims=["H2"], verdicts=["H2"], title="", sentence="",
        form="diverging-bar", eyebrow="A1 · tests H2",
        sources=["FRED (fredgraph.csv: CPIAUCSL)", f"NEAR archival RPC ({RPC_ARCHIVAL}, block header total_supply)",
                 "NEAR archival RPC: max_inflation_rate changes (rpc/protocol_changes.csv)"],
        queries=[fred_query("CPIAUCSL"), rpc_block_query(),
                 rpc_query("EXPERIMENTAL_protocol_config", {"block_id": "<height of each sample>"})],
        table={"columns": ["month", "from_sample", "to_sample", "near_growth_pct", "cpi_month_pct", "gap_pp"],
               "rows": rows},
        code_ref="pipeline/models/macro.py::since_cut", formula_ids=["F-cut-gap", "F-growth"],
        x={"type": "category"}, y={"unit": "pp", "diverging_center": 0},
        series=[Series("gap", "US prices minus NEAR supply growth, one month, annualized", "near",
                       [[ym(c.month), rnd(c.gap_pp)] for c in w.candidates], role="diverging", unit="pp")],
        reference_lines=refs, annotations=annotations,
        coverage=coverage([c.month for c in w.candidates], monthly=True, expected=len(w.candidates)) | {
            "gaps": [ym(c.month) for c in w.candidates if c.gap_pp is None]},
        as_of=files_as_of(files),
    )
    chart.title_template = supply.pick(reg, [
        "Since the issuance ceiling cut, NEAR's supply grew {h2_near_g} a year, {h2_gap:abs} {h2_gap_dir} than US "
        "consumer prices ({h2_cpi}).",
        "NEAR's supply growth against US consumer prices, month by month since the issuance ceiling cut.",
    ])
    chart.sentence_template = ("Each bar is one month's US consumer-price inflation minus NEAR's supply growth over "
                               "the same month, both annualized; above zero, NEAR's supply grew more slowly.")
    window_note = ("Single months swing widely once annualized, so the verdict reads the whole window from {h2_from} "
                   "to {h2_to}, not the bars.")
    chart.caveat_template = supply.pick(reg, [
        "The window's consumer-price base, {h2_base}, follows a month the shutdown left unpublished, so prices may "
        "include some catch-up: from {h2_alt_base}, the last level published before the cut, they ran {h2_cpi_alt} "
        "a year, a gap of {h2_gap_alt}.",
        window_note,
    ]) if ready(reg, "h2_from", "h2_to") else ""
    base_only = all(_cpi_missing(w.cpi, F.add_months(c.month, -1)) for c in w.candidates if c.gap_pp is None)
    chart.notes_templates = notes_for(reg, [
        window_note,
        "NEAR's supply grew more slowly in {h2_months_slower} of the {h2_months_read} months with both readings.",
        "Consumer prices are the seasonally adjusted CPI-U, a monthly average, so each month's reading is centred "
        "about half a month before NEAR's month-boundary samples.",
        "Months without a reading, because the month before has no published consumer-price level: "
        "{h2_months_blank}." if base_only else "Months without a reading, and why: {h2_months_blank}.",
        "From the first sample after {cut_date} to the latest, NEAR's supply grew {g_since_cut} a year.",
    ])
    if not w.candidates or not ready(reg, "h2_gap"):
        chart.status = "unavailable"
        chart.empty_state = empty_state(
            (w.reason or NO_WINDOW) + ".",
            "FRED CPIAUCSL and full-day RPC supply samples on the first of each month since cut_date.")
        if not rows:
            chart.table = {"columns": chart.table["columns"], "rows": []}
    return chart.resolve(reg)
