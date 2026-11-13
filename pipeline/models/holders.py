"""Who pays NEAR's inflation: a staker's and a non-staker's share of all NEAR, by window (claim H12; chart c03).

- F-share: staker share-yield = (1 + y_lst) / (1 + g) - 1; non-staker = 1 / (1 + g) - 1, both annualized over the
  same two samples (y_lst from the LST prices, g from total supply at those samples).
- Windows are fixed here: [lst_start_date, 2023-01-01), 2023, 2024, [2025-01-01, cut_date), [cut_date, latest], plus
  the trailing year behind staker_share_365 and nonstaker_365. "Latest" is the latest full-day (00:00 UTC) sample of
  the merged RPC set (supply.load_samples), never the history file's tip block.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from pipeline.chartspec import Chart, Series
from pipeline.models import supply
from pipeline.models.formulas import pct
from pipeline.models.supply import (EDGE_DAYS, FIX, GAP_DAYS, RPC_SOURCE, WINDOW_DAYS, Samples, as_of,
                                    block_queries, days_between, growth, load_samples, lst_queries, lst_yield,
                                    pick, put, r4, sample_ref, val)
from pipeline.numbers import Registry

YEAR_EDGES = (date(2023, 1, 1), date(2024, 1, 1), date(2025, 1, 1))


@dataclass
class ShareWindow:
    label: str
    start: date | None
    end: date | None
    r0: dict | None = None
    r1: dict | None = None
    g: float | None = None
    y: float | None = None
    reason: str = ""

    @property
    def staker(self) -> float | None:
        return None if self.y is None or self.g is None else (1 + self.y) / (1 + self.g) - 1

    @property
    def nonstaker(self) -> float | None:
        return None if self.g is None else 1 / (1 + self.g) - 1


@dataclass
class HoldersModel:
    smp: Samples
    windows: list[ShareWindow]
    trailing: ShareWindow | None
    files: list[str]


def _window_specs(lst_start: date | None, cut: date | None, latest: dict | None) -> list[tuple]:
    """(label, start, end, end_is_latest) for the five fixed windows; None edges are reported as missing."""
    end = latest["date"] if latest else None
    first_label = f"LST launch to end-{YEAR_EDGES[0].year - 1}"
    return [
        (first_label, lst_start, YEAR_EDGES[0], False),
        (str(YEAR_EDGES[0].year), YEAR_EDGES[0], YEAR_EDGES[1], False),
        (str(YEAR_EDGES[1].year), YEAR_EDGES[1], YEAR_EDGES[2], False),
        (f"{YEAR_EDGES[2].year} to the ceiling cut", YEAR_EDGES[2], cut, False),
        ("Since the ceiling cut", cut, end, True),
    ]


def build_model(lst_start: date | None, cut: date | None, cut_reason: str = "") -> HoldersModel:
    smp = load_samples()
    latest = smp.latest_full("lst")   # the latest full-day (00:00 UTC) LST sample: where trailing windows end

    def fill(w: ShareWindow, end_is_latest: bool) -> ShareWindow:
        if w.start is None or w.end is None:
            uses_cut = "cut" in w.label.lower()
            w.reason = (cut_reason if uses_cut and cut_reason else
                        "an edge date is unknown (" + ("cut_date" if uses_cut else
                                                       "lst_start_date or the latest sample") + " is null)")
            return w
        if w.end <= w.start:
            w.reason = "the window is empty"
            return w
        w.r0 = smp.edge(w.start, "lst")
        w.r1 = latest if end_is_latest else smp.edge(w.end, "lst")
        if w.r0 is None or w.r1 is None or w.r1["time"] <= w.r0["time"]:
            missing = w.start if w.r0 is None else w.end
            w.reason = f"no sample with an LST price within {EDGE_DAYS} days of {missing}"
            return w
        w.g = growth(w.r0, w.r1)
        w.y = lst_yield(w.r0, w.r1)
        return w

    windows = [fill(ShareWindow(label, s, e), last) for label, s, e, last in _window_specs(lst_start, cut, latest)]
    trailing = None
    if latest is not None:
        trailing = fill(ShareWindow("Trailing year", latest["date"] - timedelta(days=WINDOW_DAYS), latest["date"]),
                        True)
    return HoldersModel(smp, windows, trailing, smp.files)


def _model_from(reg: Registry) -> HoldersModel:
    """Windows from the registered lst_start_date and cut_date.

    When the ceiling change is only bracketed (cut_height null) and the bracket is wider than GAP_DAYS, cut_date
    (the first sample with the new rate) can sit far from the real change, so the two windows that start or end
    at the cut are left out with that reason instead of straddling the cut.
    """
    lst = val(reg, "lst_start_date")
    cut = val(reg, "cut_date")
    cut_d = date.fromisoformat(cut) if cut else None
    reason = ""
    if cut_d is not None and val(reg, "cut_height") is None:
        changes = supply.build_model().chg
        last = changes[-1] if changes else None
        if last is not None and not last.exact:
            width = (last.after.date - last.before.date).days
            if width > GAP_DAYS:
                reason = (f"the ceiling change is only bracketed between samples {last.before.date} and "
                          f"{last.after.date} ({width} days), so a window edge at the cut is not placed")
                cut_d = None
    return build_model(date.fromisoformat(lst) if lst else None, cut_d, reason)


def compute(reg: Registry) -> None:
    m = _model_from(reg)
    files = m.files
    t = m.trailing
    if t is not None and t.staker is not None:
        inputs = [f"samples {sample_ref(t.r0)} and {sample_ref(t.r1)}", f"y_lst {pct(t.y):.4f}%",
                  f"g {pct(t.g):.4f}%"]
        put(reg, "staker_share_365", pct(t.staker), "%", "F-share", files, inputs)
        put(reg, "nonstaker_365", pct(t.nonstaker), "%", "F-share", files, inputs)
    else:
        reason = "no sample with an LST price" if t is None else (
            t.reason or "the trailing year has no LST yield at both edges")
        put(reg, "staker_share_365", None, "%", "F-share", files, reason=reason)
        put(reg, "nonstaker_365", None, "%", "F-share", files, reason=reason)


def charts(reg: Registry) -> list[Chart]:
    m = _model_from(reg)
    return [share_yield(reg, m)]


def _coverage(windows: list[ShareWindow]) -> dict:
    done = [w for w in windows if w.staker is not None]
    return {"first": done[0].r0["date"].isoformat() if done else None,
            "last": done[-1].r1["date"].isoformat() if done else None, "n": len(done),
            "gaps": [w.label for w in windows if w.staker is None]}


def share_yield(reg: Registry, m: HoldersModel) -> Chart:
    """c03: staker vs non-staker change in share of all NEAR, per fixed window, annualized."""
    chart = Chart(
        id="c03", section="O2", priority="lead", claims=["H12"], title="", sentence="", form="diverging-bar",
        sources=[RPC_SOURCE + ": block header total_supply, meta-pool.near get_st_near_price, "
                 "linear-protocol.near ft_price"],
        queries=lst_queries(m.smp) + block_queries(m.smp)[:1], table={"columns": [], "rows": []},
        code_ref="pipeline/models/holders.py::share_yield", formula_ids=["F-share", "F-lst", "F-growth"],
        eyebrow="O2 · tests H12", x={"type": "category"}, y={"unit": "%"}, verdicts=["H12"],
        as_of=as_of(m.files))
    staker, non = val(reg, "staker_share_365"), val(reg, "nonstaker_365")
    # signs become verbs; the figures print unsigned
    non_verb = "grew" if non is not None and non >= 0 else "shrank"
    chart.title_template = pick(reg, [
        *(["Even stakers lost share of supply over the last year: their share shrank {staker_share_365:abs}."]
          if staker is not None and staker <= 0 else
          ["Inflation is a transfer: over the last year stakers' share of all NEAR grew {staker_share_365:abs} "
           f"and non-stakers' {non_verb} {{nonstaker_365:abs}}."]),
        "Who pays NEAR's inflation: a staker's and a non-staker's share of all NEAR, by period.",
    ])
    base = ("For each period, the upward bar is how much a liquid staker's fraction of all NEAR grew per year, "
            "and the downward bar is how much an unstaked holder's shrank")
    chart.sentence_template = pick(reg, [base + "; {unstaked_now} of supply is unstaked today.", base + "."])
    chart.series = [
        Series("staker", "Liquid staker", "near", [[w.label, r4(pct(w.staker))] for w in m.windows],
               role="diverging"),
        Series("nonstaker", "Unstaked holder", "negative", [[w.label, r4(pct(w.nonstaker))] for w in m.windows],
               role="diverging"),
    ]
    chart.reference_lines = [{"y": 0, "label": "share unchanged"}]
    rows = []
    for w in m.windows + ([m.trailing] if m.trailing else []):
        rows.append([w.label, w.r0["date"].isoformat() if w.r0 else None, w.r1["date"].isoformat() if w.r1 else None,
                     round(days_between(w.r0, w.r1), 3) if w.r0 and w.r1 else None, r4(pct(w.y)), r4(pct(w.g)),
                     r4(pct(w.staker)), r4(pct(w.nonstaker)), w.reason or ("trailing, not a window"
                                                                          if w is m.trailing else "")])
    chart.table = {"columns": ["window", "from_sample", "to_sample", "days", "lst_yield_pct", "supply_growth_pct",
                               "staker_share_change_pct", "nonstaker_share_change_pct", "note"], "rows": rows}
    chart.coverage = _coverage(m.windows)
    chart.resolve(reg)
    if not any(w.staker is not None for w in m.windows) and (m.trailing is None or m.trailing.staker is None):
        chart.status = "unavailable"
        chart.empty_state = {"reason": "No window has liquid-staking prices and supply at both edges",
                             "needs": "rpc/history_samples.csv samples near each window edge", "fix_command": FIX}
    elif chart.coverage["gaps"]:
        chart.status = "partial"
        chart.status_banner = "Some windows lack a sample near an edge; they are listed in the table, not drawn."
    return chart
