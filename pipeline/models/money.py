"""Money growth of the ten most-traded currencies against NEAR's supply growth (claim H1b; chart c21; cross-check X9).

Runs after currencies and before sweep (build.MODEL_ORDER). Reads data/raw only, no network:
  bis_fx/fx_turnover_<year>.csv     BIS Triennial Survey turnover by currency; the panel is the ten largest
  money/<ccy>/money.csv             each currency's broad money, monthly (EUR: eur/money_growth_published.csv for
                                    the ECB's own annual growth rate)
  money/<ccy>/money_alt.csv         the national series the OECD mirrors, where it is not money.csv (cross-check X9)
  money/oecd/monagg.csv             the OECD broad-money mirror (cross-check X9)
and NEAR's month-end supply growth from the RPC samples (macro.near_g_monthly). A missing value nulls every quantity
that needs it; nothing is interpolated or back-cast.

Formulas (fractions in, fractions out; percent registered with pct):
  F-bis-rank             sort the survey's turnover by currency, take the ten largest
  F-money-growth-latest  g_M,latest = M_t / M_{t-12} - 1 at the latest month with both (EUR: the ECB's published rate)
  F-median               median over the currencies with a value; the count is registered beside it
  F-h1b-gap              gap_c = g_M,latest,c - g_365_at_c (NEAR's month-end supply growth over the same twelve months)
  X9                     national money against the OECD mirror of the same aggregate, latest common month
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date

from pipeline.chartspec import Chart, Query, Series
from pipeline.models import formulas as F
from pipeline.models import load
from pipeline.models.macro import derived_query, empty_state, files_as_of, iso, near_g_monthly, notes_for, ready, rnd, ym
from pipeline.numbers import Registry

BIS_YEAR = "2025"           # the survey the panel is ranked from (the next survey is April 2028)
PANEL_SIZE = 10             # the ten most-traded currencies

NAMES = {"USD": "US dollar", "EUR": "euro", "JPY": "Japanese yen", "GBP": "pound sterling", "CNY": "renminbi",
         "CHF": "Swiss franc", "AUD": "Australian dollar", "CAD": "Canadian dollar", "HKD": "Hong Kong dollar",
         "SGD": "Singapore dollar", "INR": "Indian rupee", "KRW": "Korean won", "MXN": "Mexican peso",
         "NZD": "New Zealand dollar", "SEK": "Swedish krona", "NOK": "Norwegian krone"}
# exchange-rate regime flags; every other currency floats
REGIMES = {"HKD": "pegged to the US dollar (currency board)", "CNY": "managed, with capital controls",
           "SGD": "managed: the central bank steers the exchange rate, with no policy rate"}
FLOATING = "floating"
# X9: (field, OECD area, OECD adjustment flag) for the national series that should equal OECD's broad money. The
# euro's money file is the ECB's index of notional stocks, not a level, so it has no level to compare; OECD's UK
# broad money is a wider aggregate than the Bank of England's M4; HKD and SGD have no OECD mirror.
MIRROR = {"USD": ("money", "USA", "Y"), "JPY": ("money", "JPN", "Y"), "CNY": ("money", "CHN", "N"),
          "CHF": ("money", "CHE", "N"), "AUD": ("money_alt", "AUS", "Y"), "CAD": ("money_alt", "CAN", "Y")}

# per-currency registry keys: suffix -> (unit, formula id, signed)
FIAT_KEYS = {
    "regime": ("text", "F-bis-rank", True), "aggregate": ("text", "F-money-growth-latest", True),
    "gm_latest": ("%", "F-money-growth-latest", True), "gm_month": ("month", "F-money-growth-latest", True),
}
PCT_KEYS = {"gm_latest"}


@dataclass
class R:
    """One reading: a value (a fraction for growth rates) or None with the reason, plus the inputs that made it."""
    value: float | str | None
    reason: str | None = None
    inputs: list[str] = field(default_factory=list)


def _ok(value, *inputs: str) -> R:
    return R(value, None, [i for i in inputs if i])


def _no(reason: str) -> R:
    return R(None, reason)


def _last(series: dict[date, float | None]) -> date | None:
    return max((d for d, v in series.items() if v is not None), default=None)


# --------------------------------------------------------------------------- the panel

def panel() -> tuple[list[dict], dict, list[str]]:
    """The ten most-traded currencies, largest first (F-bis-rank), with the survey sidecar and files."""
    data, files = load.bis_fx_turnover(BIS_YEAR)
    rows = [r for r in data["currencies"] if r["rank"] is not None][:PANEL_SIZE]
    return rows, load.meta("bis_fx", f"fx_turnover_{BIS_YEAR}"), files


@dataclass
class Series_:
    points: dict[date, float | None]
    meta: dict
    files: list[str]
    reason: str | None


def _series(ccy: str, fld: str) -> Series_:
    s = load.money_series(ccy, fld)
    return Series_(dict(s.points), load.money_meta(ccy, fld), s.files, load.money_null_reason(ccy, fld))


@dataclass
class Fiat:
    code: str
    rank: int | None
    turnover: float | None
    r: dict[str, R] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)

    @property
    def c(self) -> str:
        return self.code.lower()

    def val(self, key: str):
        r = self.r.get(key)
        return r.value if r else None


def _scale(meta: dict) -> float | None:
    return load.unit_scale(meta.get("units"))


def _latest_yoy(series: dict[date, float | None]) -> tuple[date | None, float | None]:
    """The latest month t with both M_t and M_{t-12} (a blank month is skipped, never filled)."""
    for t in sorted((d for d, v in series.items() if v is not None), reverse=True):
        prev = series.get(F.add_months(t, -12))
        if prev:
            return t, series[t] / prev - 1
    return None, None


def analyze_currency(code: str, rank: int | None, turnover: float | None) -> Fiat:
    f = Fiat(code, rank, turnover)
    c = f.c
    money, published = _series(c, "money"), _series(c, "money_growth_published")
    f.files = sorted(set(money.files + published.files))
    agg = money.meta.get("aggregate")
    f.r["regime"] = _ok(REGIMES.get(code, FLOATING), "exchange-rate regime")
    f.r["aggregate"] = _ok(agg, money.meta.get("series_id") or "") if agg else _no(
        money.reason or f"the {code} money sidecar names no aggregate")
    history = money.meta.get("history_note")

    # the latest twelve months (EUR: the ECB's published annual growth rate)
    if code == "EUR" and published.reason is None:
        t = _last(published.points)
        f.r["gm_latest"] = _ok(published.points[t] / 100, f"ECB published annual growth rate of M3, {ym(t)}") if t \
            else _no("the ECB published growth rate file has no value")
        f.r["gm_month"] = _ok(ym(t)) if t else _no("the ECB published growth rate file has no value")
    elif money.reason is None:
        t, g = _latest_yoy(money.points)
        why = f"no {code} month with money a year earlier" + (f" ({history})" if history else "")
        f.r["gm_latest"] = _ok(g, f"{agg} {ym(t)} / {agg} {ym(F.add_months(t, -12))} - 1") if t else _no(why)
        f.r["gm_month"] = _ok(ym(t)) if t else _no(why)
    else:
        f.r["gm_latest"] = f.r["gm_month"] = _no(money.reason)
    return f


# --------------------------------------------------------------------------- X9 mirror

def mirror(fiats: dict[str, Fiat]) -> tuple[list[dict], list[str]]:
    rows, files = [], []
    for code, (fld, area, adj) in MIRROR.items():
        if code not in fiats:
            continue
        nat = _series(code.lower(), fld)
        oecd = load.oecd_monagg(area, adj)
        files += nat.files + oecd.files
        o = dict(oecd.points)
        common = [d for d, v in nat.points.items() if v is not None and o.get(d) is not None]
        scale = _scale(nat.meta)
        if not common or scale is None:
            continue
        t = max(common)
        ref = o[t] * load.UNIT_SCALES["million"]
        rows.append({"code": code, "field": fld, "month": t, "rel_pct": abs(nat.points[t] * scale - ref) / ref * 100,
                     "aggregate": nat.meta.get("aggregate")})
    return rows, sorted(set(files))


# --------------------------------------------------------------------------- compute

def _med(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def analyze(reg: Registry) -> dict:
    rows, bis_meta, bis_files = panel()
    fiats = {r["currency"]: analyze_currency(r["currency"], r["rank"], r["value"]) for r in rows}
    return {"rows": rows, "bis_meta": bis_meta, "bis_files": bis_files, "fiats": fiats,
            "panel": [r["currency"] for r in rows]}


def compute(reg: Registry) -> None:
    d = analyze(reg)
    fiats, bis_files, bis_meta = d["fiats"], d["bis_files"], d["bis_meta"]
    in_panel = [fiats[c] for c in d["panel"]]
    turnover_scale = load.UNIT_SCALES["million"] if str(bis_meta.get("units", "")).startswith("USD million") else None
    survey = (bis_meta.get("first_date") or "")[:7] or None
    reg.put("bis_survey_month", survey, "month", "F-bis-rank", source_files=bis_files,
            inputs=[bis_meta.get("survey", "")], null_reason=None if survey else
            f"no BIS turnover file data/raw/bis_fx/fx_turnover_{BIS_YEAR}.csv (uv run python -m pipeline.pull --only bis_fx)")
    reg.put("n_fiat_panel", len(in_panel) or None, "count", "F-bis-rank", source_files=bis_files,
            inputs=[f"the {PANEL_SIZE} largest by turnover"] + [f.code for f in in_panel],
            null_reason=None if in_panel else "no BIS turnover file in data/raw/bis_fx")

    g_near, g_files = near_g_monthly()
    for f in fiats.values():
        c = f.c
        files = f.files
        reg.put(f"fx_{c}_rank", f.rank, "count", "F-bis-rank", source_files=bis_files,
                inputs=[f"rank by OTC FX turnover, {bis_meta.get('reference_period', '')}"],
                null_reason=None if f.rank else f"{f.code} has no rank in the BIS turnover file")
        tv = f.turnover * turnover_scale if (f.turnover is not None and turnover_scale) else None
        reg.put(f"fx_{c}_turnover", tv, "USD", "F-bis-rank", source_files=bis_files,
                inputs=[f"{bis_meta.get('units', '')}, net-net, daily average"],
                null_reason=None if tv is not None else f"{f.code} has no turnover in the BIS file")
        for suffix, (unit, fid, signed) in FIAT_KEYS.items():
            r = f.r.get(suffix) or _no("not computed")
            v = F.pct(r.value) if (suffix in PCT_KEYS and r.value is not None) else r.value
            reg.put(f"mon_{c}_{suffix}", v, unit, fid, source_files=files, inputs=r.inputs,
                    null_reason=None if v is not None else (r.reason or "not computed"), signed=signed)
        # H1b: NEAR's month-end supply growth over the same twelve months as the latest money reading
        m = f.r["gm_month"].value
        p = g_near.get(date.fromisoformat(m + "-01")) if m else None
        g = p.value if p else None
        why = (f"{f.code} has no latest money growth: {f.r['gm_month'].reason}" if not m else
               f"NEAR month-end g_365 for {m} is not computed (RPC supply samples a year apart are missing)")
        reg.put(f"g_365_at_{c}", F.pct(g) if g is not None else None, "%", "F-growth", source_files=g_files,
                inputs=[f"NEAR g_365 month-end {m} (samples {iso(p.start)} -> {iso(p.end)})"] if g is not None else [],
                null_reason=None if g is not None else why)
        gl = f.r["gm_latest"].value
        gap = F.pct(gl) - F.pct(g) if (gl is not None and g is not None) else None
        reg.put(f"h1b_gap_{c}", gap, "pp", "F-h1b-gap", source_files=files + g_files,
                inputs=[f"mon_{c}_gm_latest - g_365_at_{c}"],
                null_reason=None if gap is not None else (f.r["gm_latest"].reason or why))

    # the median and count over the panel
    latest = [reg.value(f"mon_{f.c}_gm_latest") for f in in_panel if reg.value(f"mon_{f.c}_gm_latest") is not None]
    all_files = sorted({p for f in in_panel for p in f.files})
    reg.put("fiat_gm_latest_med", _med(latest), "%", "F-median", source_files=all_files,
            inputs=[f"median of mon_<c>_gm_latest over {len(latest)} of {len(in_panel)} currencies"],
            null_reason=None if latest else "no currency in the panel has mon_<c>_gm_latest")
    reg.put("n_fiat_money", len(latest), "count", "F-median", source_files=all_files,
            inputs=[f.code for f in in_panel if reg.value(f"mon_{f.c}_gm_latest") is not None])

    gaps = [(f.code, reg.value(f"h1b_gap_{f.c}")) for f in in_panel if reg.value(f"h1b_gap_{f.c}") is not None]
    reg.put("h1b_gap_med", _med([g for _, g in gaps]), "pp", "F-h1b-gap", source_files=all_files + g_files,
            inputs=[f"median of h1b_gap_<c> over {len(gaps)} currencies"],
            null_reason=None if gaps else "no currency has both a latest money growth and NEAR's growth to match")
    reg.put("h1b_n_faster", sum(1 for _, g in gaps if g > 0) if gaps else None, "count", "F-h1b-gap",
            source_files=all_files + g_files, inputs=[c for c, g in gaps if g > 0],
            null_reason=None if gaps else "no h1b_gap_<c> is computed")
    _mirror(reg, fiats)


def _mirror(reg: Registry, fiats: dict[str, Fiat]) -> None:
    rows, files = mirror(fiats)
    worst = max(rows, key=lambda r: r["rel_pct"]) if rows else None
    reg.put("money_mirror_n", len(rows) if rows else None, "count", "X9", source_files=files,
            inputs=[f"{r['code']} {r['aggregate']} {ym(r['month'])}: {r['rel_pct']:.4f}%" for r in rows],
            null_reason=None if rows else "no national money series has an OECD mirror month in data/raw/money")
    reg.put("money_mirror_max_rel_pct", worst["rel_pct"] if worst else None, "%", "X9", source_files=files,
            decimals=3, signed=False, inputs=[f"{worst['code']} {ym(worst['month'])}"] if worst else [],
            null_reason=None if worst else "no national money series has an OECD mirror month")


# --------------------------------------------------------------------------- chart c21

def charts(reg: Registry) -> list[Chart]:
    return [chart_c21(reg)]


def _queries(d: dict) -> list[Query]:
    out, seen = [], set()
    for f in d["fiats"].values():
        for rel in f.files:
            meta = load.money_meta(f.c, rel.rsplit("/", 1)[-1].removesuffix(".csv"))
            url = meta.get("source_url")
            if url and (url, rel) not in seen:
                seen.add((url, rel))
                out.append(Query("http", url, f"GET {url}", rel))
    url = d["bis_meta"].get("source_url")
    if url:
        out.append(Query("http", url, f"GET {url}", (d["bis_files"] or [None])[0]))
    out += [derived_query(t) for t in (
        "F-bis-rank: sort OTC FX turnover by currency, take the ten largest",
        "F-money-growth-latest: M_t / M_{t-12} - 1 at the latest month with both (EUR: the ECB's published rate)",
        "F-h1b-gap: h1b_gap_<c> = mon_<c>_gm_latest - g_365_at_<c>",
        "F-median over the currencies with a value")]
    return out


def _domain(values: list[float]) -> list[float]:
    """Fiat values plus NEAR's supply growth, padded by one gridline step either side (zero always inside)."""
    lo, hi = min(values + [0.0]), max(values + [0.0])
    span = max(hi - lo, 1.0)
    step = next(s for s in (0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200, 500) if span / s <= 8)
    return [(lo // step) * step - step, -((-hi) // step) * step + step]


def chart_c21(reg: Registry) -> Chart:
    """c21: NEAR's supply growth against each major currency's broad-money growth over its latest twelve months
    (H1b's lead chart)."""
    d = analyze(reg)
    fiats = d["fiats"]
    in_panel = [fiats[c] for c in d["panel"]]
    usd, other, rows, x_vals = [], [], [], []
    for f in in_panel:
        c = f.c
        label = f"{f.code} {NAMES.get(f.code, f.code)}" + (
            " (pegged)" if REGIMES.get(f.code, "").startswith("pegged") else "")
        gl = reg.value(f"mon_{c}_gm_latest") if f"mon_{c}_gm_latest" in reg else None
        if gl is not None:
            x_vals.append(gl)
        (usd if f.code == "USD" else other).append([label, rnd(gl)])
        rows.append([f.code, NAMES.get(f.code, f.code), f.r["aggregate"].value, rnd(gl),
                     reg.value(f"mon_{c}_gm_month") if f"mon_{c}_gm_month" in reg else None,
                     rnd(reg.value(f"g_365_at_{c}")) if f"g_365_at_{c}" in reg else None,
                     rnd(reg.value(f"h1b_gap_{c}")) if f"h1b_gap_{c}" in reg else None])
    g365 = reg.value("g_365") if "g_365" in reg else None
    if g365 is not None:
        x_vals.append(g365)
    series = [Series("usd_gm_latest", "US dollar money growth, latest twelve months", "usd", usd, role="emphasis",
                     unit="%"),
              Series("fiat_gm_latest", "Major currency money growth, latest twelve months", "third", other,
                     role="emphasis", unit="%")]
    refs = ([{"x": rnd(g365), "label": f"NEAR supply, past year ({reg.display('g_365')})", "entity": "near"}]
            if g365 is not None else [])
    files = sorted({p for f in in_panel for p in f.files} | set(d["bis_files"]))
    chart = Chart(
        id="c21", section="A1", priority="lead", claims=["H1b"], verdicts=["H1b"], title="", sentence="",
        form="dot-range", eyebrow="Supply against money, latest year",
        sources=sorted({f"{(load.money_meta(f.c, 'money').get('publisher') or f.code)}" for f in fiats.values()
                        if load.money_meta(f.c, "money")})
        + [f"{d['bis_meta'].get('survey', 'BIS Triennial Central Bank Survey')} (stats.bis.org)",
           "NEAR archival RPC (total supply samples)"],
        queries=_queries(d) + [derived_query("g_365 = supply now / supply a year earlier - 1 (RPC samples)")],
        table={"columns": ["code", "currency", "money_aggregate", "money_growth_latest_pct", "latest_month",
                           "near_supply_growth_same_12m_pct", "gap_pp"], "rows": rows},
        code_ref="pipeline/models/money.py::chart_c21",
        formula_ids=["F-bis-rank", "F-money-growth-latest", "F-growth", "F-h1b-gap", "F-median"],
        x={"type": "linear", "unit": "%", "domain": _domain(x_vals) if x_vals else [-5, 10], "clamp": True},
        y={"type": "category", "unit": "%"}, series=series, reference_lines=refs,
        coverage={"first": None, "last": None, "n": len([r for r in rows if r[3] is not None]),
                  "expected": len(in_panel), "gaps": []},
        as_of=files_as_of(files),
    )
    if not ready(reg, "g_365", "fiat_gm_latest_med"):
        chart.status = "unavailable"
        chart.title = "NEAR's supply growth against the major currencies' money"
        chart.sentence = "Not computed: NEAR's supply growth or the major currencies' money growth is missing."
        chart.series, chart.reference_lines = [], []
        chart.empty_state = empty_state("No year of NEAR supply growth or no broad-money series in data/raw.",
                                        "RPC supply samples and the central-bank money series", "make data && make charts")
        return chart
    gap = reg.value("h1b_gap_med") if "h1b_gap_med" in reg else None
    if gap is not None and gap > 0:
        chart.title = "NEAR's supply grew more slowly than the major currencies' money over the past year"
    elif gap is not None:
        chart.title = "NEAR's supply grew at least as fast as the median major currency's money over the past year"
    else:
        chart.title = "NEAR's supply growth against the major currencies' money, latest year"
    # the line is NEAR to the latest chain sample; the gap uses NEAR over each currency's own twelve months, so the
    # sentence also quotes NEAR over the US money month, the window most of the panel shares
    if ready(reg, "g_365_at_m2", "m2_month"):
        chart.sentence_template = ("NEAR's supply grew {g_365} over the year to the latest chain sample (the line) and "
                                   "{g_365_at_m2} over the year to {m2_month}, against a median {fiat_gm_latest_med} for "
                                   "the major currencies' money, each over its latest year; this is supply only, not "
                                   "demand.")
    else:
        chart.sentence_template = ("NEAR's supply grew {g_365} over the year to the latest chain sample (the line), "
                                   "against a median {fiat_gm_latest_med} for the major currencies' money, each over its "
                                   "latest year; this is supply only, not demand.")
    chart.notes_templates = notes_for(reg, [
        "Each currency is compared with NEAR's supply growth over the same twelve months; the median of those gaps "
        "is {h1b_gap_med}, the figure the supply claim tests.",
        "Broad money includes deposits created by bank lending; NEAR's supply is issued by protocol only, so it is "
        "nearer to base money.",
        "The dollar's money grew {m2_yoy} on FRED's broad US money series.",
    ])
    missing = [f.code for f in in_panel if f"mon_{f.c}_gm_latest" not in reg or reg.value(f"mon_{f.c}_gm_latest") is None]
    if missing:
        chart.status = "partial"
        chart.status_banner = "Not computed for " + ", ".join(missing) + ": the table gives each reason."
    return chart.resolve(reg)
