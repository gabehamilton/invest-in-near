"""NEAR's advantage over the dollar, month by month (chart c24; claim H4).

Formula:
  F-adv  For each month M, closed by the full-day sample on E = the 1st of M+1:
           A_M = infl_M + stake_M, in pp
           infl_M  = CPI_yoy(M) - g(M): CPIAUCSL year over year (F-yoy) minus NEAR's month-end trailing-year supply
                     growth (F-growth, the edges c11 uses)
           stake_M = y_M - ff_M: the liquid-staking yield (F-lst) minus DFF compounded ACT/360 (F-ffcomp) over the
                     same two LST samples, the trailing year ending at the LST sample nearest E
                     (staking.trailing_spread, the like-for-like spread_adj_365 convention)
         A month with either part missing has no A; nothing is filled. A month counts after the cut when E is
         after cut_date, and clean after when its whole trailing year starts on or after cut_date
         (E - 365 days >= cut_date). NEAR's dollar price is not part of A.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

from pipeline.chartspec import Chart, Series
from pipeline.models import formulas as F
from pipeline.models import load, macro, staking, supply
from pipeline.models.macro import coverage, cpi_blank_annotations, empty_state, files_as_of, iso, notes_for, rnd, ym
from pipeline.models.params import param
from pipeline.models.staking import Window
from pipeline.models.supply import WINDOW_DAYS as YEAR_DAYS   # each part reads one trailing year ending at the close
from pipeline.numbers import Registry

ADV_LE0_MAX_PCT = param("adv_le0_max_pct")   # H4: refuted when more than this share of months is at or below zero


@dataclass
class Month:
    month: date
    close: date                      # E, the 1st of the next month
    cpi: float | None                # CPIAUCSL year over year (fraction)
    g: float | None                  # NEAR month-end trailing-year supply growth (fraction)
    w: Window | None                 # the trailing LST and DFF window that ends at E
    side: str | None                 # "before" / "after" the cut (None when cut_date is missing)
    clean: bool                      # the whole trailing year starts on or after cut_date
    why: list[str] = field(default_factory=list)

    @property
    def infl_pp(self) -> float | None:
        return F.pct(self.cpi) - F.pct(self.g) if self.cpi is not None and self.g is not None else None

    @property
    def stake_pp(self) -> float | None:
        return self.w.spread_pp if self.w is not None else None

    @property
    def adv_pp(self) -> float | None:
        a, b = self.infl_pp, self.stake_pp
        return a + b if a is not None and b is not None else None


@dataclass
class AdvData:
    cut: date | None
    months: list[Month]
    files: list[str]
    reason: str | None                       # set when nothing can be counted (no samples, no CPI)

    @property
    def counted(self) -> list[Month]:
        return [m for m in self.months if m.adv_pp is not None]

    @property
    def span(self) -> list[Month]:
        """From the first counted month to the latest month whose closing sample exists (its staking part is
        computable), so a blank latest month shows."""
        have = self.counted
        if not have:
            return []
        last = max((m.month for m in self.months if m.stake_pp is not None), default=have[-1].month)
        last = max(last, have[-1].month)
        return [m for m in self.months if have[0].month <= m.month <= last]

    @property
    def blank(self) -> list[Month]:
        return [m for m in self.span if m.adv_pp is None]

    @property
    def clean_from(self) -> date | None:
        """The first month whose trailing year starts on or after cut_date, sampled or not."""
        if self.cut is None:
            return None
        m = F.month_start(self.cut)
        while F.add_months(m, 1) - timedelta(days=YEAR_DAYS) < self.cut:
            m = F.add_months(m, 1)
        return m


def _why(levels: dict[date, float | None], mo: Month) -> list[str]:
    out = []
    if mo.cpi is None:
        prior = F.add_months(mo.month, -12)
        if mo.month not in levels:
            out.append("no CPI yet")
        elif levels[mo.month] is None:
            out.append("CPI not published")
        elif levels.get(prior) is None:
            out.append("no year-ago CPI")
        else:
            out.append("no CPI year over year")
    if mo.g is None:
        out.append("no supply sample a year apart")
    if mo.stake_pp is None:
        out.append("no staking window" if mo.w is None else "no LST yield or fed funds over the window")
    return out


def analyze(reg: Registry) -> AdvData:
    data = macro.analyze()
    cpi = data.yoy.get("cpi", {})
    levels = load.fred("CPIAUCSL").as_dict()
    smp = supply.load_samples()
    dff_series = load.fred("DFF")
    dff = dff_series.as_dict()
    cut_s = reg.value("cut_date") if "cut_date" in reg else None
    cut = date.fromisoformat(cut_s) if cut_s else None
    cut_files = list(reg.get("cut_date").source_files) if "cut_date" in reg else []
    files = sorted(set(data.files.get("cpi", []) + data.g_files + smp.files + dff_series.files + cut_files))
    out = AdvData(cut, [], files, None)
    if not smp.rows:
        out.reason = "no RPC supply samples in data/raw/rpc/history_samples.csv or dense_samples.csv"
        return out
    if not any(v is not None for v in levels.values()):
        out.reason = "no CPIAUCSL levels in data/raw/fred/CPIAUCSL.csv"
        return out
    for m, gp in sorted(data.g.items()):
        close = F.add_months(m, 1)
        w = staking.trailing_spread(smp, close, YEAR_DAYS, dff)
        mo = Month(m, close, cpi.get(m), gp.value, w,
                   None if cut is None else ("after" if close > cut else "before"),
                   cut is not None and close - timedelta(days=YEAR_DAYS) >= cut)
        mo.why = _why(levels, mo)
        out.months.append(mo)
    return out


def _mean(xs: list[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def compute(reg: Registry) -> None:
    d = analyze(reg)
    files = d.files

    def put(key, value, unit, inputs=(), reason=None, fid="F-adv", src=None):
        reg.put(key, value, unit, fid, source_files=files if src is None else src, inputs=inputs,
                null_reason=None if value is not None else (reason or "not computed"))

    put("adv_le0_max_pct", ADV_LE0_MAX_PCT, "%", ["thesis/rules.toml [params] adv_le0_max_pct: H4's limit"],
        fid="P-adv_le0_max_pct", src=["thesis/rules.toml"])

    count_keys = (("adv_months", "count"), ("adv_months_before", "count"), ("adv_months_after", "count"),
                  ("adv_months_le0", "count"), ("adv_share_le0", "%"), ("adv_mean_before", "pp"),
                  ("adv_mean_after", "pp"), ("adv_infl_mean_before", "pp"), ("adv_infl_mean_after", "pp"),
                  ("adv_stake_mean_before", "pp"), ("adv_stake_mean_after", "pp"), ("adv_latest", "pp"),
                  ("adv_latest_month", "month"), ("adv_min", "pp"), ("adv_min_month", "month"),
                  ("adv_first_month", "month"), ("adv_last_month", "month"), ("adv_years", "count"),
                  ("adv_months_clean_after", "count"), ("adv_clean_from", "month"), ("adv_months_blank", "text"))
    if d.reason:
        for key, unit in count_keys:
            put(key, None, unit, reason=d.reason)
        return

    have = d.counted
    no_month = "no month has both a CPI year over year and NEAR's supply and staking windows"
    no_cut = "cut_date is not registered"
    span_inputs = ([f"months {ym(have[0].month)}..{ym(have[-1].month)}, closed by the samples on "
                    f"{iso(have[0].close)}..{iso(have[-1].close)}", f"cut_date {iso(d.cut)}"] if have else [])
    put("adv_months", len(have), "count", span_inputs)
    le0 = [m for m in have if m.adv_pp <= 0]
    put("adv_months_le0", len(le0), "count", span_inputs + ["A_M <= 0 (exactly zero counts)"])
    put("adv_share_le0", 100 * len(le0) / len(have) if have else None, "%", span_inputs, no_month)

    for side in ("before", "after"):
        rows = [m for m in have if m.side == side]
        reason = no_cut if d.cut is None else f"no counted month whose year ends {side} the cut"
        rule = (f"side: the month's closing sample is after cut_date {iso(d.cut)}" if side == "after" else
                f"side: the month's closing sample is on or before cut_date {iso(d.cut)}")
        inputs = span_inputs + [rule]
        put(f"adv_months_{side}", len(rows) if d.cut is not None else None, "count", inputs, no_cut)
        put(f"adv_mean_{side}", _mean([m.adv_pp for m in rows]), "pp", inputs, reason)
        put(f"adv_infl_mean_{side}", _mean([m.infl_pp for m in rows]), "pp", inputs, reason)
        put(f"adv_stake_mean_{side}", _mean([m.stake_pp for m in rows]), "pp", inputs, reason)

    last = have[-1] if have else None
    lo = min(have, key=lambda m: m.adv_pp) if have else None
    for key, value, unit, mo in (("adv_latest", last.adv_pp if last else None, "pp", last),
                                 ("adv_latest_month", ym(last.month) if last else None, "month", last),
                                 ("adv_min", lo.adv_pp if lo else None, "pp", lo),
                                 ("adv_min_month", ym(lo.month) if lo else None, "month", lo)):
        inputs = ([f"{ym(mo.month)}: CPI {rnd(F.pct(mo.cpi))}% - NEAR g {rnd(F.pct(mo.g))}%, LST "
                   f"{supply.sample_ref(mo.w.r0)} -> {supply.sample_ref(mo.w.r1)}"] if mo else [])
        put(key, value, unit, inputs, no_month)
    put("adv_first_month", ym(have[0].month) if have else None, "month", span_inputs, no_month)
    put("adv_last_month", ym(last.month) if last else None, "month", span_inputs, no_month)
    years = (math.floor((have[-1].close - (have[0].close - timedelta(days=YEAR_DAYS))).days / YEAR_DAYS)
             if have else None)
    put("adv_years", years, "count", span_inputs + ["floor((E_last - (E_first - 365 days)) / 365 days)"], no_month)

    clean = [m for m in have if m.side == "after" and m.clean]
    put("adv_months_clean_after", len(clean) if d.cut is not None else None, "count",
        span_inputs + ["clean after: E - 365 days >= cut_date"], no_cut)
    put("adv_clean_from", ym(d.clean_from), "month", [f"first month with E - 365 days >= cut_date {iso(d.cut)}"],
        no_cut)
    blank = d.blank
    text = ", ".join(f"{ym(m.month)} ({'; '.join(m.why)})" for m in blank)
    put("adv_months_blank", text or None, "text", span_inputs,
        "every month in the span has an advantage" if have else no_month)


# ---------------------------------------------------------------------------
# Chart c24
# ---------------------------------------------------------------------------

def charts(reg: Registry) -> list[Chart]:
    return [advantage_chart(reg)]


def advantage_chart(reg: Registry) -> Chart:
    """c24 advantage: per month, US prices minus NEAR supply growth, liquid staking minus compounded fed funds, and
    their sum, NEAR's advantage over the dollar (F-adv)."""
    d = analyze(reg)
    span = d.span
    # the staking part stops with the advantage, so every line ends on the same month and the end labels add up
    last_adv = max((m.month for m in span if m.adv_pp is not None), default=None)
    smp = supply.load_samples()
    chart = Chart(
        id="c24", section="A2", priority="lead", claims=["H4"], verdicts=["H4"], title="", sentence="",
        form="line", eyebrow="A2 · tests H4",
        sources=["FRED (fredgraph.csv: CPIAUCSL)",
                 f"NEAR archival RPC ({supply.ARCHIVAL}, block header total_supply)",
                 supply.RPC_SOURCE + ": meta-pool.near get_st_near_price, linear-protocol.near ft_price",
                 supply.fred_source("DFF"),
                 "NEAR archival RPC: max_inflation_rate changes (rpc/protocol_changes.csv)"],
        queries=[macro.fred_query("CPIAUCSL")] + supply.block_queries(smp) + supply.lst_queries(smp)
        + [supply.fred_query("DFF")],
        table={"columns": ["month", "close_sample", "cpi_yoy_pct", "near_growth_pct", "prices_part_pp",
                           "lst_yield_pct", "ffcomp_pct", "staking_part_pp", "advantage_pp", "side"],
               "rows": [[ym(m.month), iso(m.w.r1["date"]) if m.w else None, rnd(F.pct(m.cpi)), rnd(F.pct(m.g)),
                         rnd(m.infl_pp), rnd(F.pct(m.w.y)) if m.w else None, rnd(F.pct(m.w.ff)) if m.w else None,
                         rnd(m.stake_pp), rnd(m.adv_pp), m.side] for m in span],
               "footnote": ("Each row's figures cover the same year, ending at that month's close sample; the "
                            "latest-year figures quoted elsewhere end at the latest chain sample.")},
        code_ref="pipeline/models/advantage.py::advantage_chart",
        formula_ids=["F-adv", "F-yoy", "F-growth", "F-lst", "F-ffcomp", "F-spread-adj"],
        x={"type": "time", "unit": "month"}, y={"unit": "pp"},
        series=[Series("infl", "US prices minus NEAR supply growth", "usd",
                       [[iso(m.month), rnd(m.infl_pp)] for m in span], role="context", unit="pp"),
                Series("stake", "Liquid staking minus compounded fed funds", "third",
                       [[iso(m.month), rnd(m.stake_pp) if last_adv and m.month <= last_adv else None]
                        for m in span], role="context", unit="pp", dashed=True),
                Series("adv", "NEAR's advantage over the dollar", "near",
                       [[iso(m.month), rnd(m.adv_pp)] for m in span], role="emphasis", unit="pp")],
        reference_lines=[{"y": 0, "label": "no advantage"}],
        coverage=coverage([m.month for m in span], monthly=True, expected=len(span)) | {
            "gaps": [ym(m.month) for m in d.blank]},
        as_of=files_as_of(d.files),
    )
    blank = [m.month for m in d.blank]
    cpi_blanks = sorted(day for day, v in load.fred("CPIAUCSL").points if v is None)
    annotations = cpi_blank_annotations(cpi_blanks, blank)
    marked = {a["x"] for a in annotations}
    annotations += [{"x": iso(m), "label": "No sample at an edge", "kind": "data_boundary"}
                    for m in blank if iso(m) not in marked]
    if d.cut is not None:
        annotations.append({"x": iso(d.cut), "label": "issuance ceiling cut", "kind": "ceiling_change"})
    chart.annotations = sorted(annotations, key=lambda a: a["x"])

    le0 = reg.value("adv_months_le0") if "adv_months_le0" in reg else None
    chart.title_template = supply.pick(reg, [
        *(["NEAR's advantage over the dollar stayed above zero in all {adv_months} months, across {adv_years} "
           "separate years ending before and after the issuance ceiling cut."] if le0 == 0 else
          ["NEAR's advantage over the dollar was at or below zero in {adv_months_le0} of {adv_months} months, "
           "across {adv_years} separate years ending before and after the issuance ceiling cut."] if le0 else []),
        "NEAR's advantage over the dollar, month by month: prices minus supply growth, plus staking minus fed funds.",
    ])
    chart.sentence_template = (
        "The blue line is the advantage, the sum of the other two: US consumer-price inflation minus NEAR's supply "
        "growth (orange), and liquid-staking yield minus compounded fed funds (green, dashed), each over the year to "
        "the month's end.")
    chart.caveat_template = supply.pick(reg, [
        "NEAR's dollar price is not part of this advantage, and a month counts after the cut when its year ends "
        "after {cut_date}, so the first months after it still hold mostly earlier issuance.",
        "NEAR's dollar price is not part of this advantage.",
    ])
    clean = reg.value("adv_months_clean_after") if "adv_months_clean_after" in reg else None
    chart.notes_templates = notes_for(reg, [
        "Months whose year ends before the cut: {adv_months_before}, averaging {adv_mean_before}; after it: "
        "{adv_months_after}, averaging {adv_mean_after}.",
        "The prices part averaged {adv_infl_mean_before} before and {adv_infl_mean_after} after; the staking part "
        "{adv_stake_mean_before} and {adv_stake_mean_after}.",
        "Months with no published US consumer-price reading have no advantage and stay blank: {adv_months_blank}.",
        "Each month reads a trailing year, so neighbouring months overlap and the {adv_months} months span only "
        "{adv_years} separate years.",
        "No counted month's year yet lies wholly after the cut; the first such month is {adv_clean_from}."
        if clean == 0 else
        "{adv_months_clean_after} of the months after the cut have a year wholly after it, from {adv_clean_from}.",
    ])
    if not span:
        chart.status = "unavailable"
        chart.empty_state = empty_state(
            (d.reason or "No month has both a CPI year over year and NEAR's supply and staking windows") + ".",
            "FRED CPIAUCSL and DFF, and RPC supply and LST samples a year apart.")
    return chart.resolve(reg)
