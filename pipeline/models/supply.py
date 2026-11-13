"""NEAR supply: growth, the issuance ceiling, staked share, burn, and the BigQuery reconciliation.

Supply S(d), mint and burn, staked share, protocol parameters and epoch duration. Charts: c02 supply-growth, c05 burn-vs-issuance, c16 supply-reconciliation. The NEAR sample helpers
used by staking.py and holders.py live here too (Samples, growth, lst_yield, query builders).

Conventions
- Samples: load.rpc_merged(), i.e. rpc/history_samples.csv (monthly, with stake and protocol columns) merged with
  rpc/dense_samples.csv (weekly Mondays, then daily), deduplicated by date with equality asserted on overlap.
  Every trailing window ends at the latest full-day sample (the 00:00 UTC block); the history file's latest-block
  row is used only for "now" snapshots (supply_now, s_now, the rate in force).
- Rates are computed as fractions and registered in percent; spreads in pp.
- Window edges use formulas.nearest(max_days=7) on each sample's target date; with daily samples the edge lands on
  the exact date. Elapsed time between two samples comes from the sampled blocks' timestamps (block_time_utc), so
  the Δd in F-growth is exact to the second.
- Nothing is interpolated or carried forward. A window whose edge has no sample within 7 days is null.
- The issuance ceiling integrates max_inflation_rate over time, switching at the exact first block of each change
  in rpc/protocol_changes.csv. Without that file a change is only bracketed by two samples; the ceiling inside a
  bracket uses the old (higher) rate, tables flag those rows "upper bound", and ceiling_gap_dir is null.
- Burn (D2): exact (BigQuery daily_supply_flows) > exact from every epoch boundary (rpc/epoch_mints.csv, F-burn-epochs,
  only when the chained boundary rows cover the whole window) > sampled (rpc/epoch_history.csv: one complete epoch per
  month, F-burn-sampled) > upper bound (F-burn-ub). The upper bound is always shown next to the method used, and the
  sampled share is kept beside an exact one for comparison.
"""

from __future__ import annotations

import json
import math
import statistics
import string
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from pipeline import config
from pipeline.chartspec import Chart, Query, Series
from pipeline.models import load
from pipeline.models.formulas import DAYS_PER_YEAR, annualize, nearest, pct
from pipeline.models.params import param
from pipeline.numbers import Registry

# --- parameters (sampling rules; not output figures) -------------------------------------------------------
EDGE_DAYS = 7                     # nearest-sample tolerance at a window edge
BURN_MAJORITY_PCT = param("burn_majority_pct")   # c05 title switch = H10's supported cut-off (rules.toml [params])
T3_BURN_PCT = param("t3_burn_pct")               # tripwire T3's burn share; burn_needed_t3 is that share of mint
GAP_DAYS = 35                     # consecutive samples further apart than this are a coverage gap
WINDOW_DAYS = 365                 # trailing window for g_365, burn_share_365
EPOCH_WINDOW_DAYS = 30            # epoch_hours_median_30d
NEARCORE_YEAR_SECONDS = 365 * 24 * 3600   # nearcore NUM_SECONDS_IN_A_YEAR: mint = i * S * dt / this
BQ_STALE_DAYS = 7                 # BigQuery more than this behind the RPC tip is partial

ARCHIVAL = "https://archival-rpc.mainnet.fastnear.com"
BQ_DATASET = "bigquery-public-data.crypto_near_mainnet_us"
RPC_SOURCE = f"NEAR mainnet archival JSON-RPC ({ARCHIVAL})"
FIX = "make data"
# BigQuery charts print one reason, one need and one fix command
BQ_NOT_CONFIGURED = "BigQuery not configured"
BQ_NEEDS = "GCP_PROJECT in .env + gcloud auth application-default login"
BQ_FIX = "make check-bq && make data && make charts"


# =====================================================================================================
# Shared helpers (also used by staking.py and holders.py)
# =====================================================================================================


def _block_time(row: dict) -> datetime:
    text = (row.get("block_time_utc") or "").strip()
    if text:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    # No block time recorded: the sample's target instant (00:00 UTC of its date).
    return datetime.combine(row["date"], time(0), tzinfo=timezone.utc)


def _float(text) -> float | None:
    if text is None:
        return None
    text = str(text).strip()
    if text in ("", ".", "NaN", "nan", "None"):
        return None
    return float(text)


@dataclass
class Samples:
    """Merged RPC samples (load.rpc_merged) sorted by (date, height), with block time, staked share s and an LST flag.

    `full_day` rows are 00:00 UTC blocks; the one row that is not is the history file's latest block (the tip).
    Window edges and trailing-window ends use full-day rows only.
    """
    rows: list[dict]
    files: list[str]

    def with_(self, col: str, full_only: bool = False) -> list[dict]:
        return [r for r in self.rows if r.get(col) is not None and (r["full_day"] or not full_only)]

    def latest(self, col: str = "total_supply_near") -> dict | None:
        """The latest sample with `col`, the tip row included (for "now" snapshots)."""
        rows = self.with_(col)
        return rows[-1] if rows else None

    def latest_full(self, col: str = "total_supply_near") -> dict | None:
        """The latest full-day (00:00 UTC) sample with `col`: where every trailing window ends."""
        rows = self.with_(col, full_only=True)
        return rows[-1] if rows else None

    def edge(self, target: date, col: str = "total_supply_near", max_days: int = EDGE_DAYS) -> dict | None:
        hit = nearest([(r["date"], r) for r in self.with_(col, full_only=True)], target, max_days)
        return hit[1] if hit else None


def load_samples() -> Samples:
    rows, files = load.rpc_merged()
    out = []
    for raw in rows:
        r = dict(raw)
        r.setdefault("full_day", True)
        r["time"] = _block_time(r)
        r["height"] = int(r["height"]) if r.get("height") is not None else None
        supply, stake = r.get("total_supply_near"), r.get("validator_stake_near")
        r["s"] = stake / supply if (supply and stake is not None) else None
        has_lst = r.get("stnear_price") is not None or r.get("linear_price") is not None
        r["lst"] = 1.0 if has_lst else None
        out.append(r)
    out.sort(key=lambda r: (r["date"], r["height"] or 0))
    return Samples(out, files)


def cadence(smp: Samples) -> dict:
    """Where the merged supply samples turn weekly (first dense date) and daily (start of the trailing run of
    consecutive days), read from the rows themselves."""
    dense = sorted({r["date"] for r in smp.rows if "dense" in (r.get("sources") or "")})
    if not dense:
        return {"weekly_from": None, "daily_from": None}
    daily_from = dense[-1]
    for a, b in zip(reversed(dense[:-1]), reversed(dense)):
        if (b - a).days != 1:
            break
        daily_from = a
    return {"weekly_from": dense[0], "daily_from": daily_from if daily_from != dense[-1] else None}


def days_between(r0: dict, r1: dict) -> float:
    return (r1["time"] - r0["time"]).total_seconds() / 86400


def growth(r0: dict | None, r1: dict | None, col: str = "total_supply_near") -> float | None:
    """F-growth / F-lst between two sample rows: (v1/v0)^(365.25/Δd) - 1, Δd from block times."""
    if r0 is None or r1 is None:
        return None
    a, b = r0.get(col), r1.get(col)
    span = days_between(r0, r1)
    if a is None or b is None or a <= 0 or span <= 0:
        return None
    return annualize(b / a, span)


def lst_yield(r0: dict | None, r1: dict | None) -> float | None:
    """F-lst: mean of the stNEAR and LiNEAR annualized yields where both exist, else the one that does."""
    ys = [growth(r0, r1, c) for c in ("stnear_price", "linear_price")]
    ys = [y for y in ys if y is not None]
    return statistics.fmean(ys) if ys else None


def sample_ref(r: dict) -> str:
    return f"{r['date'].isoformat()} (block {r['height']})"


def gaps(dates: list[date], limit: int = GAP_DAYS) -> list[list[str]]:
    """Intervals between consecutive dates longer than `limit` days."""
    out = []
    for a, b in zip(dates, dates[1:]):
        if (b - a).days > limit:
            out.append([a.isoformat(), b.isoformat()])
    return out


def with_breaks(points: list[list], limit: int = GAP_DAYS) -> list[list]:
    """Insert a null point after any gap longer than `limit` days so a line breaks instead of bridging it."""
    out: list[list] = []
    for p in points:
        if out and out[-1][1] is not None:
            prev = date.fromisoformat(out[-1][0])
            if (date.fromisoformat(p[0]) - prev).days > limit:
                out.append([(prev + timedelta(days=1)).isoformat(), None])
        out.append(p)
    return out


def weekly(points: list[list]) -> list[list]:
    """Downsample a dated series to the last point of each ISO week (nulls kept so lines still break)."""
    out: dict = {}
    for p in points:
        d = date.fromisoformat(p[0])
        key = d.isocalendar()[:2]
        if p[1] is None:
            key = (key, "null")
        out[key] = p
    return sorted(out.values(), key=lambda p: p[0])


def r4(x: float | None, dp: int = 4) -> float | None:
    return None if x is None else round(x, dp)


def val(reg: Registry, key: str):
    return reg.value(key) if key in reg else None


def put(reg: Registry, key: str, value, unit: str, fid: str, files=(), inputs=(), reason: str | None = None,
        decimals: int | None = None) -> None:
    if value is None and not reason:
        reason = "not computed"
    reg.put(key, value, unit, fid, source_files=files, inputs=inputs,
            null_reason=None if value is not None else reason, decimals=decimals)


def pick(reg: Registry, templates: list[str]) -> str:
    """First template whose placeholders are all registered and non-null; the last one is the fallback."""
    for template in templates:
        names = [n for _, n, _, _ in string.Formatter().parse(template) if n]
        if all(n in reg and reg.value(n) is not None for n in names):
            return template
    return templates[-1]


def resolvable(reg: Registry, template: str) -> bool:
    """Every placeholder in the template is registered and non-null."""
    names = [n for _, n, _, _ in string.Formatter().parse(template) if n]
    return all(n in reg and reg.value(n) is not None for n in names)


def pick_notes(reg: Registry, notes: list) -> list[str]:
    """Method notes for a chart: each entry is a template or a list of variants (first resolvable wins);
    an entry with no resolvable variant is dropped rather than printed with a hole in it."""
    out = []
    for note in notes:
        for template in ([note] if isinstance(note, str) else note):
            if template and resolvable(reg, template):
                out.append(template)
                break
    return out


def as_of(files) -> str | None:
    stamps = []
    for f in files:
        sidecar = (config.ROOT / f).with_suffix(".meta.json")
        if sidecar.exists():
            stamp = json.loads(sidecar.read_text()).get("fetched_at")
            if stamp:
                stamps.append(stamp)
    return max(stamps) if stamps else None


def constants() -> tuple[dict, list[str]]:
    path = config.ROOT / "thesis" / "constants.toml"
    if not path.exists():
        return {}, []
    return tomllib.loads(path.read_text()), [str(path)]


HSP027_PARAM = "burnt_gas_reward"   # HSP-027 set the developer gas rebate to zero


def change_time(c: dict) -> datetime | None:
    text = (c.get("block_time_utc") or "").strip()
    if not text:
        return None
    stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def hsp027_change() -> tuple[dict | None, list[str]]:
    """The rpc/protocol_changes.csv row where burnt_gas_reward became 0 (the latest such row), and the files read."""
    rows, files = load.csv_rows("rpc", "protocol_changes")
    hits = [c for c in rows if (c.get("param") or "").strip() == HSP027_PARAM and _float(c.get("new_value")) == 0
            and _float(c.get("first_height")) is not None and change_time(c) is not None]
    hits.sort(key=lambda c: _float(c["first_height"]))
    return (hits[-1] if hits else None), files


def put_hsp027(reg: Registry) -> date | None:
    """Register the HSP-027 activation (date, first block, protocol version) from the bisected burnt_gas_reward
    change in rpc/protocol_changes.csv; return the activation date. Prose prints these keys, never typed figures."""
    c, files = hsp027_change()
    keys = (("hsp027_from", "date"), ("hsp027_height", "count"), ("hsp027_protocol", "count"))
    if c is None:
        reason = ("rpc/protocol_changes.csv has no burnt_gas_reward change to 0 (file absent, or the change is not "
                  "on chain in the sampled configs)")
        for key, unit in keys:
            if key not in reg:
                reg.put(key, None, unit, "D5", source_files=files, null_reason=reason)
        return None
    stamp = change_time(c)
    height = int(_float(c["first_height"]))
    proto = _float(c.get("protocol_version"))
    _, _, cfg_files = config_bracket(HSP027_PARAM, height)
    files = sorted(set(files + cfg_files))
    inputs = [f"rpc/protocol_changes.csv: burnt_gas_reward {c.get('old_value')} -> {c.get('new_value')} first at "
              f"block {height} ({stamp.isoformat()}, protocol {c.get('protocol_version')}; block "
              f"{c.get('prev_height')} still had the old value)", bracket_note(HSP027_PARAM, c)]
    values = {"hsp027_from": stamp.date().isoformat(), "hsp027_height": height,
              "hsp027_protocol": int(proto) if proto is not None else None}
    for key, unit in keys:
        if key in reg:
            continue
        reg.put(key, values[key], unit, "D5", source_files=files, inputs=inputs,
                null_reason=None if values[key] is not None else "protocol_version is blank in the change row",
                decimals=0 if unit == "count" else None)
    return stamp.date()


def rectify_protocol() -> tuple[int | None, list[str]]:
    """The first protocol version whose epoch reward is time-based (nearcore RectifyInflation), from
    thesis/constants.toml; from it an epoch's annualized mint is comparable with max_inflation_rate."""
    consts, files = constants()
    v = consts.get("rectify_inflation_protocol", {}).get("value")
    return (None if v is None else int(v)), files


def config_bracket(param: str, height: int | None) -> tuple[dict | None, dict | None, list[str]]:
    """The rpc/protocol_config_samples.csv reads just before and at-or-after `height` (the bisection's bracket)."""
    rows, files = load.csv_rows("rpc", "protocol_config_samples")
    rows = [r for r in rows if _float(r.get("height")) is not None and (r.get(param) or "").strip()]
    if height is None or not rows:
        return None, None, files
    before = [r for r in rows if _float(r["height"]) < height]
    after = [r for r in rows if _float(r["height"]) >= height]
    pick_before = max(before, key=lambda r: _float(r["height"])) if before else None
    pick_after = min(after, key=lambda r: _float(r["height"])) if after else None
    return pick_before, pick_after, files


def bracket_note(param: str, c: dict) -> str:
    """Input line: the sampled config reads either side of a protocol_changes.csv row agree with it."""
    height = _float(c.get("first_height"))
    b, a, _ = config_bracket(param, None if height is None else int(height))
    if b is None or a is None:
        return f"rpc/protocol_config_samples.csv has no read on both sides of block {c.get('first_height')}"
    ok = _float(b[param]) == _float(c.get("old_value")) and _float(a[param]) == _float(c.get("new_value"))
    return (f"rpc/protocol_config_samples.csv reads {param} {b[param]} at block {b['height']} ({b['date']}) and "
            f"{a[param]} at block {a['height']} ({a['date']})" + ("" if ok else ": DISAGREES with the change row"))


def staker_fraction() -> tuple[float | None, list[str]]:
    """1 - treasury_share from thesis/constants.toml (stakers receive 0.9 of each mint)."""
    consts, files = constants()
    share = consts.get("treasury_share", {}).get("value")
    return (None, files) if share is None else (1 - float(share), files)


def rpc_query(method: str, params, endpoint: str = ARCHIVAL, file: str = "data/raw/rpc/history_samples.csv") -> Query:
    body = json.dumps({"jsonrpc": "2.0", "id": "near_rpc", "method": method, "params": params},
                      indent=2, sort_keys=True)
    return Query("jsonrpc", endpoint, body, file)


def example_height(smp: Samples) -> int | str:
    latest = smp.latest()
    return latest["height"] if latest and latest.get("height") else "<sampled block height>"


def block_queries(smp: Samples) -> list[Query]:
    h = example_height(smp)
    return [rpc_query("block", {"block_id": h}),
            rpc_query("EXPERIMENTAL_protocol_config", {"block_id": h})]


def validators_query(smp: Samples) -> Query:
    latest = smp.latest()
    epoch = latest.get("epoch_id") if latest else None
    return rpc_query("validators", {"block_id": epoch or "<header.epoch_id of the sampled block>"})


def lst_queries(smp: Samples) -> list[Query]:
    h = example_height(smp)
    out = []
    for account, method in (("meta-pool.near", "get_st_near_price"), ("linear-protocol.near", "ft_price")):
        out.append(rpc_query("query", {"request_type": "call_function", "block_id": h, "account_id": account,
                                       "method_name": method, "args_base64": "e30="}))
    return out


def fred_query(series_id: str) -> Query:
    url = load.meta("fred", series_id).get("source_url") or \
        f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    return Query("http", url, f"GET {url}", f"data/raw/fred/{series_id}.csv")


def sql_query(name: str) -> Query:
    path = config.SQL_DIR / f"{name}.sql"
    text = path.read_text() if path.exists() else f"-- sql/{name}.sql is not in this checkout"
    return Query("sql", BQ_DATASET, text, f"sql/{name}.sql")


def fred_source(*ids: str) -> str:
    return "FRED, Federal Reserve Bank of St. Louis (https://fred.stlouisfed.org/graph/fredgraph.csv): " + \
        ", ".join(ids)


# =====================================================================================================
# Issuance ceiling (D5)
# =====================================================================================================

@dataclass
class Obs:
    time: datetime
    date: date
    rate: float
    height: int | None
    kind: str  # "sample" | "change"


@dataclass
class Segment:
    t0: datetime
    t1: datetime
    lo: float
    hi: float
    exact: bool


@dataclass
class Change:
    before: Obs
    after: Obs

    @property
    def exact(self) -> bool:
        return self.after.kind == "change"


def rate_observations(smp: Samples, change_rows: list[dict]) -> list[Obs]:
    obs = [Obs(r["time"], r["date"], r["max_inflation_rate"], r["height"], "sample")
           for r in smp.with_("max_inflation_rate")]
    # rpc/protocol_changes.csv (pipeline/sources/near_rpc.py): one row per changed parameter,
    # param,old_value,new_value,first_height,block_time_utc,protocol_version,prev_height. first_height is the first
    # block whose config has new_value; block_time_utc is that block's header time.
    for c in change_rows:
        if (c.get("param") or "").strip() != "max_inflation_rate":
            continue
        rate = _float(c.get("new_value"))
        text = (c.get("block_time_utc") or "").strip()
        if rate is None or not text:
            continue
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
        height = _float(c.get("first_height"))
        obs.append(Obs(stamp, stamp.date(), rate, int(height) if height is not None else None, "change"))
    obs.sort(key=lambda o: (o.time, o.kind != "change"))
    return obs


def segments(obs: list[Obs]) -> list[Segment]:
    out = []
    for a, b in zip(obs, obs[1:]):
        if b.time <= a.time:
            continue
        if a.rate == b.rate or b.kind == "change":
            out.append(Segment(a.time, b.time, a.rate, a.rate, True))
        else:
            out.append(Segment(a.time, b.time, min(a.rate, b.rate), max(a.rate, b.rate), False))
    return out


def changes(obs: list[Obs]) -> list[Change]:
    return [Change(a, b) for a, b in zip(obs, obs[1:]) if a.rate != b.rate]


def ceiling_log(segs: list[Segment], t0: datetime, t1: datetime, bound: str = "hi") -> tuple[float | None, bool]:
    """Σ i_k Δt_k in nearcore years over [t0, t1]; (None, False) if the schedule does not cover the window."""
    if not segs or t1 <= t0 or t0 < segs[0].t0 or t1 > segs[-1].t1:
        return None, False
    total, exact = 0.0, True
    for s in segs:
        a, b = max(s.t0, t0), min(s.t1, t1)
        if b > a:
            total += (b - a).total_seconds() * (s.hi if bound == "hi" else s.lo)
            exact = exact and s.exact
    return total / NEARCORE_YEAR_SECONDS, exact


def ceiling_equiv(segs: list[Segment], t0: datetime, t1: datetime, bound: str = "hi") -> tuple[float | None, bool]:
    """F-ceiling annualized like F-growth: exp(Σ i_k Δt_k)^(365.25/Δd) - 1."""
    log, exact = ceiling_log(segs, t0, t1, bound)
    if log is None:
        return None, False
    span_days = (t1 - t0).total_seconds() / 86400
    return math.exp(log * DAYS_PER_YEAR / span_days) - 1, exact


# =====================================================================================================
# Burn (D2): exact (BigQuery) > exact from every epoch boundary (RPC) > sampled (one RPC epoch a month) >
# upper bound (F-burn-ub)
# =====================================================================================================

# what each burn method is called wherever it prints (burn_method, the c05 title, eyebrow and table)
METHOD_LABEL = {"exact": "exact", "epochs": "exact: every epoch boundary", "sampled": "sampled: one epoch per month",
                "upper bound": "upper bound"}
# the short word for running prose (burn_method_short); copy tells the two exact methods apart by burn_method
METHOD_SHORT = {"exact": "exact", "epochs": "exact", "sampled": "sampled", "upper bound": "upper bound"}


@dataclass
class BurnCol:
    label: str
    start: date
    end: date
    method: str | None = None        # "exact" | "epochs" | "sampled" | "upper bound" | None
    days: float | None = None
    minted: float | None = None
    burnt: float | None = None
    share: float | None = None       # fraction
    ub_share: float | None = None    # F-burn-ub share over the same edges, shown next to the method used
    ub_exact_ceiling: bool = True
    epochs: list[dict] = field(default_factory=list)   # the sampled epochs in the period (one per month)
    sampled_share: float | None = None                 # F-burn-sampled over `epochs`, whatever method won
    sampled_reason: str = ""
    epochs_share: float | None = None                  # F-burn-epochs over every boundary, whatever method won
    epochs_reason: str = ""
    boundaries: list[dict] = field(default_factory=list)   # the epoch_mints rows F-burn-epochs summed
    supply_growth: float | None = None                 # S1 - S0 over the column's window (F-burn-epochs)
    note: str = ""
    formula: str = ""
    files: list[str] = field(default_factory=list)

    @property
    def largest_epoch(self) -> dict | None:
        return max(self.epochs, key=lambda e: e["share"]) if self.epochs else None


def load_bq_flows() -> tuple[dict[date, dict], list[str]]:
    rows, files = load.csv_rows("bigquery", "daily_supply_flows")
    out = {}
    for r in rows:
        try:
            d = date.fromisoformat((r.get("block_date") or "")[:10])
        except ValueError:
            continue
        out[d] = {"eod": _float(r.get("eod_supply_near")), "minted": _float(r.get("minted_near")),
                  "burnt": _float(r.get("burnt_near"))}
    return out, files


def load_gross_burn() -> tuple[dict[date, float], list[str]]:
    """BigQuery gross fee burn (sql/fee_burn.sql) on sampled days; a cross-check only, never the burn method."""
    rows, files = load.csv_rows("bigquery", "fee_burn_sampled")
    out = {}
    for r in rows:
        try:
            d = date.fromisoformat((r.get("block_date") or "")[:10])
        except ValueError:
            continue
        v = _float(r.get("gross_fee_burn_near"))
        if v is not None:
            out[d] = v
    return out, files


def bq_window(flows: dict[date, dict], d0: date, d1: date) -> tuple[float, float, int] | None:
    """Σ minted, Σ burnt over block_dates [d0, d1) if every day has both, else None."""
    n = (d1 - d0).days
    if n <= 0 or not flows:
        return None
    minted = burnt = 0.0
    for k in range(n):
        day = flows.get(d0 + timedelta(days=k))
        if not day or day["minted"] is None or day["burnt"] is None:
            return None
        minted += day["minted"]
        burnt += day["burnt"]
    return minted, burnt, n


def load_mints() -> tuple[list[dict], list[str]]:
    """rpc/epoch_mints.csv: the mint at each epoch boundary, linked to the boundary before it (prev_boundary_height;
    None in rows written before the link existed)."""
    rows, files = load.csv_rows("rpc", "epoch_mints")
    out = []
    for r in rows:
        h, minted = _float(r.get("boundary_height")), _float(r.get("minted_near"))
        if h is None:
            continue
        stamp = (r.get("block_time_utc") or "").strip()
        prev = _float(r.get("prev_boundary_height"))
        out.append({"height": int(h), "minted": minted, "epoch_seconds": _float(r.get("epoch_seconds")),
                    "time": datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else None,
                    "prev": None if prev is None else int(prev), "mint_rate": _float(r.get("annualized_mint_rate"))})
    out.sort(key=lambda r: r["height"])
    return out, files


def _boundary_ref(b: dict) -> str:
    return f"boundary {b['height']}" + (f" ({b['time'].isoformat()})" if b.get("time") else "")


def epoch_burn(mints: list[dict], r0: dict | None, r1: dict | None) -> tuple[dict | None, str]:
    """F-burn-epochs between two full-day samples: minted = Σ minted_near over every epoch boundary with
    r0.height < boundary_height <= r1.height (rpc/epoch_mints.csv), burnt = minted - (S1 - S0), share = burnt / minted.

    Exact, not sampled: total supply moves only by the mint at each epoch's first block and the burn in every block,
    so S1 - S0 = Σ mint - Σ burn over the blocks after r0 through r1. That needs every boundary in the window, so the
    rows must chain: the first boundary inside links (prev_boundary_height) to one at or below r0, each later one links
    to the row below it, and a row past r1 links back to the last one inside. Otherwise (None, why)."""
    if r0 is None or r1 is None or r0.get("height") is None or r1.get("height") is None:
        return None, "no full-day supply sample with a block height at a window edge"
    if not mints:
        return None, "rpc/epoch_mints.csv is missing or empty"
    lo, hi = r0["height"], r1["height"]
    inside = [b for b in mints if lo < b["height"] <= hi]
    after = [b for b in mints if b["height"] > hi]
    if not inside:
        return None, f"no rpc/epoch_mints.csv boundary between blocks {lo} and {hi}"
    first = inside[0]
    if first["prev"] is None:
        return None, ("rpc/epoch_mints.csv rows carry no prev_boundary_height, so the boundaries cannot be shown to "
                      "cover the window")
    if first["prev"] > lo:
        return None, (f"rpc/epoch_mints.csv starts at {_boundary_ref(first)}, after the window start (block {lo}, "
                      f"{r0['time'].isoformat()}); the boundary before it, {first['prev']}, is missing")
    for below, row in zip(inside, inside[1:]):
        if row["prev"] != below["height"]:
            return None, (f"rpc/epoch_mints.csv has a gap: {_boundary_ref(row)} links to boundary {row['prev']}, "
                          f"but the row below it is boundary {below['height']}")
    if not after:
        return None, (f"rpc/epoch_mints.csv ends at {_boundary_ref(inside[-1])}, with no later boundary to show that "
                      f"none is missing before the window end (block {hi})")
    if after[0]["prev"] != inside[-1]["height"]:
        return None, (f"rpc/epoch_mints.csv has a gap: {_boundary_ref(after[0])} links to boundary {after[0]['prev']}, "
                      f"but the last boundary inside the window is {inside[-1]['height']}")
    if any(b["minted"] is None for b in inside):
        return None, "an rpc/epoch_mints.csv boundary inside the window has no minted_near"
    minted = sum(b["minted"] for b in inside)
    growth_near = r1["total_supply_near"] - r0["total_supply_near"]
    burnt = minted - growth_near
    return {"minted": minted, "burnt": burnt, "share": burnt / minted if minted > 0 else None, "growth": growth_near,
            "days": days_between(r0, r1), "n": len(inside), "rows": inside}, ""


def load_epochs() -> tuple[list[dict], list[str]]:
    """rpc/epoch_history.csv: one complete epoch per history sample date, with its exact mint and burn (RPC)."""
    rows, files = load.rpc_epoch_history()
    out = []
    for r in rows:
        minted, burnt, secs = r.get("minted_next_boundary_near"), r.get("burnt_in_epoch_near"), r.get("epoch_seconds")
        if r.get("sample_date") is None or minted is None or burnt is None or not secs or secs <= 0:
            continue
        out.append({"date": r["sample_date"], "start": (r.get("start_time_utc") or "")[:19],
                    "end": (r.get("end_time_utc") or "")[:19], "start_height": r.get("start_height"),
                    "end_height": r.get("end_height"), "minted": minted, "burnt": burnt, "seconds": secs,
                    "share": burnt / minted if minted > 0 else None, "mint_rate": r.get("annualized_mint_rate"),
                    "burn_rate": r.get("annualized_burn_rate"), "protocol": r.get("protocol_version"),
                    "max_rate": r.get("max_inflation_rate")})
    out.sort(key=lambda e: e["date"])
    return out, files


def monthly_epochs(epochs: list[dict], d0: date, d1: date) -> list[dict]:
    """Sampled epochs whose sample date is in [d0, d1), at most one per calendar month (the month's earliest)."""
    seen, out = set(), []
    for e in epochs:
        month = (e["date"].year, e["date"].month)
        if d0 <= e["date"] < d1 and month not in seen and e["minted"] > 0:
            seen.add(month)
            out.append(e)
    return out


def sampled_burn(epochs: list[dict], d0: date, d1: date) -> tuple[dict | None, list[dict], str]:
    """F-burn-sampled over [d0, d1): Σ burnt / Σ minted over one complete epoch per calendar month.

    Each epoch's mint (paid at the next epoch's first block) and burn (supply fall inside the epoch) are exact RPC
    reads; the sample is one epoch a month, so a single unusual epoch weighs about a twelfth of a year. Returns
    (result, epochs, reason); result is None when a month in the period has no sampled epoch (no two consecutive
    sampled epochs, nor a period edge and its nearest epoch, more than GAP_DAYS apart).
    """
    picked = monthly_epochs(epochs, d0, d1)
    if not picked:
        return None, [], "no sampled epoch (rpc/epoch_history.csv) in the period"
    edges = [d0] + [e["date"] for e in picked] + [d1]
    worst = max((b - a).days for a, b in zip(edges, edges[1:]))
    if worst > GAP_DAYS:
        return None, picked, (f"sampled epochs leave a {worst}-day gap in the period (more than {GAP_DAYS}), so "
                              "not every month is sampled")
    minted = sum(e["minted"] for e in picked)
    burnt = sum(e["burnt"] for e in picked)
    days = sum(e["seconds"] for e in picked) / 86400
    return {"minted": minted, "burnt": burnt, "share": burnt / minted, "days": days, "n": len(picked)}, picked, ""


def upper_bound(segs: list[Segment], r0: dict | None, r1: dict | None) -> dict | None:
    """F-burn-ub: S0(e^{Σ i Δy} - 1) - ΔS with Δy in nearcore years and the higher rate inside a bracket."""
    if r0 is None or r1 is None or r1["time"] <= r0["time"]:
        return None
    log, exact = ceiling_log(segs, r0["time"], r1["time"], "hi")
    if log is None:
        return None
    s0, s1 = r0["total_supply_near"], r1["total_supply_near"]
    minted = s0 * math.expm1(log)
    if minted <= 0:
        return None
    burnt = minted - (s1 - s0)
    return {"minted": minted, "burnt": burnt, "share": burnt / minted, "days": days_between(r0, r1),
            "exact_ceiling": exact}


# =====================================================================================================
# The supply model (computed once for compute() and once for charts(); pure function of data/raw)
# =====================================================================================================

@dataclass
class SupplyModel:
    smp: Samples
    latest: dict | None                 # latest full-day supply sample: where every trailing window ends
    tip: dict | None                    # latest supply sample of any kind (the latest block), for "now" figures
    series: list[dict]                  # trailing windows: {r0, r1, g, ceil, ceil_exact}
    g365: dict | None                   # {r0, r1, g}
    g365_reason: str | None
    years: list[dict]                   # calendar years: {year, r0, r1, g, ceil, ceil_exact, gap_pp}
    missing_years: list[int]
    obs: list[Obs]
    segs: list[Segment]
    chg: list[Change]
    change_rows: list[dict]
    change_files: list[str]
    burn: list[BurnCol]
    burn_trailing: BurnCol | None
    flows: dict
    flow_files: list[str]
    mints: list[dict]
    mint_files: list[str]
    epochs: list[dict]                  # rpc/epoch_history.csv rows (one complete epoch per history sample date)
    epoch_files: list[str]
    matches: list[tuple[date, float, float, float]]   # (day, rpc supply, bq eod, diff)
    bq_max: date | None
    fresh_files: list[str]
    files: list[str]


def _calendar_years(smp: Samples, latest: dict) -> list[int]:
    first = smp.with_("total_supply_near", full_only=True)[0]["date"]
    years = []
    for year in range(first.year, latest["date"].year):
        if date(year, 1, 1) >= first - timedelta(days=EDGE_DAYS) and date(year + 1, 1, 1) <= latest["date"]:
            years.append(year)
    return years


def build_model() -> SupplyModel:
    smp = load_samples()
    change_rows, change_files = load.csv_rows("rpc", "protocol_changes")
    obs = rate_observations(smp, change_rows)
    segs = segments(obs)
    chg = changes(obs)
    flows, flow_files = load_bq_flows()
    mints, mint_files = load_mints()
    epochs, epoch_files = load_epochs()
    _, fresh_files = load.csv_rows("bigquery", "freshness")
    latest, tip = smp.latest_full(), smp.latest()

    # trailing 365-day growth at every full-day sample date (monthly, then weekly, then daily)
    series = []
    for r in smp.with_("total_supply_near", full_only=True):
        r0 = smp.edge(r["date"] - timedelta(days=WINDOW_DAYS))
        g = growth(r0, r) if r0 is not None and r0["time"] < r["time"] else None
        if g is None:
            continue
        ceil, exact = ceiling_equiv(segs, r0["time"], r["time"])
        series.append({"r0": r0, "r1": r, "g": g, "ceil": ceil, "ceil_exact": exact})

    g365, g365_reason = None, None
    if latest is None:
        g365_reason = "no NEAR supply samples in data/raw/rpc (history_samples.csv, dense_samples.csv)"
    else:
        target = latest["date"] - timedelta(days=WINDOW_DAYS)
        r0 = smp.edge(target)
        g = growth(r0, latest) if r0 is not None and r0["time"] < latest["time"] else None
        if g is None:
            g365_reason = (f"no supply sample within {EDGE_DAYS} days of {target.isoformat()} "
                           "(the trailing window start); the RPC backfill has not reached it")
        else:
            g365 = {"r0": r0, "r1": latest, "g": g}

    years, missing = [], []
    if latest is not None:
        for year in _calendar_years(smp, latest):
            r0, r1 = smp.edge(date(year, 1, 1)), smp.edge(date(year + 1, 1, 1))
            g = growth(r0, r1)
            if g is None:
                missing.append(year)
                continue
            ceil, exact = ceiling_equiv(segs, r0["time"], r1["time"])
            years.append({"year": year, "r0": r0, "r1": r1, "g": g, "ceil": ceil, "ceil_exact": exact,
                          "gap_pp": None if ceil is None else (ceil - g) * 100})

    burn_cols: list[BurnCol] = []
    trailing = None
    if latest is not None:
        for y in sorted({y["year"] for y in years} | set(missing)):
            burn_cols.append(_burn_column(str(y), date(y, 1, 1), date(y + 1, 1, 1), smp, segs, flows, epochs,
                                          mints, None))
        trailing = _burn_column("Trailing year", latest["date"] - timedelta(days=WINDOW_DAYS), latest["date"],
                                smp, segs, flows, epochs, mints, latest)
        burn_cols.append(trailing)
    for col in burn_cols:
        col.files = sorted(set(smp.files + (flow_files if col.method == "exact" else []) +
                               (epoch_files if col.epochs else []) +
                               (mint_files if col.epochs_share is not None else []) + change_files))

    matches = []
    for r in smp.with_("total_supply_near", full_only=True):
        stamp = r["time"]
        if (stamp.hour, stamp.minute) < (23, 59):
            continue  # not the last block of its UTC day
        day = flows.get(stamp.date())
        if day and day["eod"] is not None:
            matches.append((stamp.date(), r["total_supply_near"], day["eod"], day["eod"] - r["total_supply_near"]))
    bq_dates = [d for d, v in flows.items() if v["eod"] is not None]
    bq_max = max(bq_dates) if bq_dates else None
    if bq_max is None and fresh_files:
        fresh_rows, _ = load.csv_rows("bigquery", "freshness")
        if fresh_rows and fresh_rows[0].get("max_block_date"):
            bq_max = date.fromisoformat(fresh_rows[0]["max_block_date"][:10])

    files = sorted(set(smp.files + change_files))
    return SupplyModel(smp, latest, tip, series, g365, g365_reason, years, missing, obs, segs, chg, change_rows,
                       change_files,
                       burn_cols, trailing, flows, flow_files, mints, mint_files, epochs, epoch_files, matches,
                       bq_max, fresh_files, files)


def _burn_column(label: str, d0: date, d1: date, smp: Samples, segs: list[Segment], flows: dict,
                 epochs: list[dict], mints: list[dict], end_row: dict | None) -> BurnCol:
    col = BurnCol(label, d0, d1)
    r0 = smp.edge(d0)
    r1 = end_row if end_row is not None else smp.edge(d1)
    ub = upper_bound(segs, r0, r1)
    if ub:
        col.ub_share = ub["share"]
        col.ub_exact_ceiling = ub["exact_ceiling"]
    sampled, col.epochs, col.sampled_reason = sampled_burn(epochs, d0, d1)
    if sampled:
        col.sampled_share = sampled["share"]
    every, col.epochs_reason = epoch_burn(mints, r0, r1)
    if every and every["share"] is not None:
        col.epochs_share, col.boundaries, col.supply_growth = every["share"], every["rows"], every["growth"]
    elif every:
        every, col.epochs_reason = None, "the boundaries inside the window minted nothing"
    exact = bq_window(flows, d0, d1)
    if exact and exact[0] > 0:
        col.method, col.formula = "exact", "F-burnshare"
        col.minted, col.burnt, col.days = exact[0], exact[1], float(exact[2])
        col.share = col.burnt / col.minted
        col.note = "BigQuery daily_supply_flows, every day covered"
        return col
    if every:
        col.method, col.formula = "epochs", "F-burn-epochs"
        col.minted, col.burnt, col.days, col.share = every["minted"], every["burnt"], every["days"], every["share"]
        # no computed counts in the note: the epoch_boundaries column carries the count
        col.note = ("exact: every epoch boundary between the two 00:00 UTC samples (rpc/epoch_mints.csv, each row "
                    "linked to the one before); minted sums their mints, burnt is minted minus supply growth")
        return col
    if sampled and sampled["minted"] > 0:
        col.method, col.formula = "sampled", "F-burn-sampled"
        col.minted, col.burnt, col.days = sampled["minted"], sampled["burnt"], sampled["days"]
        col.share = sampled["share"]
        # no computed counts in the note: the sampled_epochs column carries the epoch count
        col.note = ("sampled: one complete epoch per month (RPC epoch_history), exact mint and burn per epoch; "
                    "minted, burnt and days sum the sampled epochs only")
        return col
    if ub:
        col.method, col.formula = "upper bound", "F-burn-ub"
        col.minted, col.burnt, col.days, col.share = ub["minted"], ub["burnt"], ub["days"], ub["share"]
        col.note = ("upper bound: counts unminted rewards (validators below the online threshold) as burn"
                    + ("" if ub["exact_ceiling"] else "; the ceiling change is bracketed, old rate used inside"))
        return col
    col.note = f"no supply sample within {EDGE_DAYS} days of a window edge and no exact or sampled source"
    return col


# =====================================================================================================
# compute()
# =====================================================================================================

def compute(reg: Registry) -> None:
    m = build_model()
    smp, latest, tip, files = m.smp, m.latest, m.tip, m.files
    no_rpc = "no NEAR supply samples in data/raw/rpc (history_samples.csv, dense_samples.csv)"

    # --- growth -----------------------------------------------------------------------------------
    if m.g365:
        r0, r1 = m.g365["r0"], m.g365["r1"]
        put(reg, "g_365", pct(m.g365["g"]), "%", "F-growth", files,
            [f"total_supply_near {sample_ref(r0)}", f"total_supply_near {sample_ref(r1)}"])
    else:
        put(reg, "g_365", None, "%", "F-growth", files, reason=m.g365_reason)
    # the ceiling over the same trailing window (the gray line's last point): the nominal rate compounded as it
    # mints, so it sits a little above i_max_now
    last_w = next((w for w in reversed(m.series) if m.g365 and w["r1"]["date"] == m.g365["r1"]["date"]), None)
    if last_w and last_w["ceil"] is not None:
        put(reg, "ceiling_365", pct(last_w["ceil"]), "%", "F-ceiling", files,
            [f"max_inflation_rate over {last_w['r0']['date'].isoformat()} to {last_w['r1']['date'].isoformat()}"
             + ("" if last_w["ceil_exact"] else " (upper bound: a ceiling change lies between samples)")])
    else:
        put(reg, "ceiling_365", None, "%", "F-ceiling", files,
            reason=m.g365_reason or "no ceiling schedule over the trailing window")

    if m.g365:
        if m.g365["g"] >= 0:
            run = 0
        else:
            run_start = m.series[-1]["r1"]["date"]
            for prev, cur in zip(reversed(m.series[:-1]), reversed(m.series)):
                if prev["g"] >= 0 or (cur["r1"]["date"] - prev["r1"]["date"]).days > GAP_DAYS:
                    break
                run_start = prev["r1"]["date"]
            run = (m.g365["r1"]["date"] - run_start).days
        put(reg, "g_365_neg_run_days", run, "days", "F-growth", files,
            ["g_365 at each sample date, consecutive negative windows ending at the latest sample"])
    else:
        put(reg, "g_365_neg_run_days", None, "days", "F-growth", files, reason=m.g365_reason or no_rpc)

    # --- ceiling ------------------------------------------------------------------------------------
    year_gaps = [y for y in m.years if y["gap_pp"] is not None]
    bracketed = [y["year"] for y in m.years if y["ceil"] is not None and not y["ceil_exact"]]
    if m.years and not m.missing_years and len(year_gaps) == len(m.years) and not bracketed:
        # "below" when growth sat under the ceiling in every calendar year
        gaps = [y["gap_pp"] for y in year_gaps]
        put(reg, "ceiling_gap_dir", "below" if min(gaps) >= 0 else "above" if max(gaps) < 0 else "mixed", "text",
            "F-ceiling", files, ["sign of ceiling minus growth in every calendar year"])
    else:
        why = (no_rpc if latest is None else
               "no complete calendar year has samples at both edges" if not m.years and not m.missing_years else
               "calendar-year growth missing for " + ", ".join(map(str, m.missing_years)) if m.missing_years else
               "a max_inflation_rate change inside " + ", ".join(map(str, bracketed)) + " is only bracketed by two "
               "samples (no rpc/protocol_changes.csv row), so that year's ceiling is an upper bound, not exact"
               if bracketed else
               "the issuance-ceiling schedule does not cover every calendar year")
        put(reg, "ceiling_gap_dir", None, "text", "F-ceiling", files, reason=why)

    _, _, cfg_files = config_bracket("max_inflation_rate", None)
    rate_files = sorted(set(smp.files + m.change_files + cfg_files))
    now_obs = m.obs[-1] if m.obs else None
    put(reg, "i_max_now", pct(now_obs.rate) if now_obs else None, "%", "D5", rate_files,
        [f"max_inflation_rate at {now_obs.date.isoformat()} (block {now_obs.height})"] if now_obs else [],
        "no max_inflation_rate in the RPC samples")
    last_change = m.chg[-1] if m.chg else None
    no_change = ("no max_inflation_rate change observed in the samples" if m.obs else
                 "no max_inflation_rate in the RPC samples")

    if last_change:
        b, a = last_change.before, last_change.after
        bracket = (f"max_inflation_rate changed between block {b.height} ({b.time.isoformat()}) and block "
                   f"{a.height} ({a.time.isoformat()})")
        if last_change.exact:
            row = next((c for c in m.change_rows if (c.get("param") or "").strip() == "max_inflation_rate"
                        and _float(c.get("first_height")) == a.height), None)
            inputs = [f"rpc/protocol_changes.csv: max_inflation_rate {b.rate} -> {a.rate} first at block {a.height} "
                      f"({a.time.isoformat()})"] + ([bracket_note("max_inflation_rate", row)] if row else [])
            put(reg, "cut_date", a.date.isoformat(), "date", "D5", rate_files, inputs)
            put(reg, "cut_height", a.height, "blocks", "D5", rate_files, inputs, decimals=0)
        else:
            inputs = [bracket, "cut_date = first sample date with the new rate"]
            put(reg, "cut_date", a.date.isoformat(), "date", "D5", rate_files, inputs)
            put(reg, "cut_height", None, "blocks", "D5", rate_files,
                reason=bracket + "; the exact height is not bisected (rpc/protocol_changes.csv has no "
                       "max_inflation_rate row for it)")
    else:
        for key, unit in (("cut_date", "date"), ("cut_height", "blocks")):
            put(reg, key, None, unit, "D5", rate_files, reason=no_change)

    # --- staked share -------------------------------------------------------------------------------
    if latest is not None:
        put(reg, "supply_now", tip["total_supply_near"], "NEAR", "D1", files,
            [f"total_supply_near {sample_ref(tip)}, the latest sampled block"])
        put(reg, "latest_rpc_date", latest["date"].isoformat(), "date", "D1", files,
            [f"{sample_ref(latest)}: the latest full-day (00:00 UTC) sample, where every trailing window ends"])
        put(reg, "rpc_tip_date", tip["time"].date().isoformat(), "date", "D1", files,
            [f"{sample_ref(tip)} at {tip['time'].isoformat()}: the latest sampled block"])
    else:
        for key, unit in (("supply_now", "NEAR"), ("latest_rpc_date", "date"), ("rpc_tip_date", "date")):
            put(reg, key, None, unit, "D1", files, reason=no_rpc)
    cad = cadence(smp)
    cad_inputs = ["first rpc/dense_samples.csv date (weekly Mondays from then)",
                  "start of the trailing run of consecutive daily samples"]
    put(reg, "rpc_weekly_from", cad["weekly_from"].isoformat() if cad["weekly_from"] else None, "date", "D1",
        files, cad_inputs[:1], "no rpc/dense_samples.csv rows")
    put(reg, "rpc_daily_from", cad["daily_from"].isoformat() if cad["daily_from"] else None, "date", "D1",
        files, cad_inputs[1:], "no run of consecutive daily samples in rpc/dense_samples.csv")
    staked = smp.latest("s")
    lag = "validator stake lags the sampled block by about two epochs"
    if staked is not None:
        put(reg, "s_now", pct(staked["s"]), "%", "F-staked-share", files,
            [f"validator_stake_near / total_supply_near {sample_ref(staked)}", lag])
        put(reg, "unstaked_now", pct(1 - staked["s"]), "%", "F-staked-share", files,
            [f"1 - s at {sample_ref(staked)}", lag])
        top = max(smp.with_("s"), key=lambda r: r["s"])
        put(reg, "s_max", pct(top["s"]), "%", "F-staked-share", files,
            [f"max over {len(smp.with_('s'))} samples of validator_stake_near / total_supply_near, at "
             f"{sample_ref(top)}", lag])
    else:
        reason = no_rpc if latest is None else "no sample has validator stake"
        for key in ("s_now", "unstaked_now", "s_max"):
            put(reg, key, None, "%", "F-staked-share", files, reason=reason)

    # --- burn ---------------------------------------------------------------------------------------
    put_hsp027(reg)
    _put_burn(reg, m, no_rpc)

    # --- BigQuery reconciliation (c16) --------------------------------------------------------------
    bq_files = sorted(set(m.flow_files + m.fresh_files))
    not_bq = f"{BQ_NOT_CONFIGURED} (needs {BQ_NEEDS}; fix: {BQ_FIX})"
    put(reg, "bq_max_date", m.bq_max.isoformat() if m.bq_max else None, "date", "D2", bq_files,
        ["MAX(block_date) with an end-of-day supply"] if m.bq_max else [], not_bq)
    if m.matches:
        xfiles = sorted(set(bq_files + smp.files))
        worst = max(m.matches, key=lambda t: abs(t[3]))
        put(reg, "max_abs_diff_near", abs(worst[3]), "NEAR", "X1", xfiles,
            [f"{len(m.matches)} days where an RPC sample is the last block of its UTC day",
             f"largest at {worst[0].isoformat()}"], decimals=2)
        put(reg, "n_overlap", len(m.matches), "count", "X1", xfiles,
            [f"{len(m.matches)} matched days"])
    else:
        why = not_bq if not m.flow_files else "no RPC sample falls on a day BigQuery covers"
        put(reg, "max_abs_diff_near", None, "NEAR", "X1", bq_files, reason=why)
        put(reg, "n_overlap", None, "count", "X1", bq_files, reason=why)

    # --- epoch duration (D6) ------------------------------------------------------------------------
    timed = [b for b in m.mints if b["time"] is not None and b["epoch_seconds"] is not None]
    if timed:
        last = max(b["time"] for b in timed)
        recent = [b for b in timed if b["time"] >= last - timedelta(days=EPOCH_WINDOW_DAYS)]
        put(reg, "epoch_hours_median_30d", statistics.median(b["epoch_seconds"] for b in recent) / 3600,
            "hours", "D6", m.mint_files,
            [f"median epoch_seconds over the {len(recent)} rpc/epoch_mints.csv boundaries from "
             f"{min(b['time'] for b in recent).date().isoformat()} to {last.date().isoformat()} (all within "
             f"{EPOCH_WINDOW_DAYS} days of the last)"], decimals=2)
    else:
        put(reg, "epoch_hours_median_30d", None, "hours", "D6", m.mint_files,
            reason="no epoch boundaries in data/raw/rpc/epoch_mints.csv")

    _put_mint_check(reg, m)
    _put_mint_check_365(reg, m)


def mint_check_epochs(m: SupplyModel) -> tuple[list[dict], int | None, list[str]]:
    """Sampled epochs (rpc/epoch_history.csv) with a time-based reward (protocol >= RectifyInflation), each with
    gap_pp = (annualized mint - max_inflation_rate) x 100. The mint includes the treasury's tenth, so a full-online
    epoch sits at the ceiling; validators below the online threshold leave rewards unminted (gap < 0)."""
    rect, rect_files = rectify_protocol()
    out = []
    if rect is None:
        return out, None, rect_files
    for e in m.epochs:
        if e["protocol"] is None or e["protocol"] < rect or e["mint_rate"] is None or e["max_rate"] is None \
                or e["minted"] <= 0:
            continue
        out.append(dict(e, gap_pp=(e["mint_rate"] - e["max_rate"]) * 100))
    return out, rect, rect_files


def _put_mint_check(reg: Registry, m: SupplyModel) -> None:
    """Realized-mint check (F-mint-check): the first sampled epoch under the time-based reward rule, from which an
    epoch's annualized mint is comparable with the ceiling in force."""
    rows, rect, rect_files = mint_check_epochs(m)
    files = sorted(set(m.epoch_files + rect_files))
    if not rows:
        why = ("thesis/constants.toml has no rectify_inflation_protocol" if rect is None else
               "no rpc/epoch_history.csv epoch with a time-based reward (protocol at or above "
               f"{rect}) and a mint")
        put(reg, "mint_check_from", None, "date", "F-mint-check", files, reason=why)
        return
    inputs = [f"{len(rows)} rpc/epoch_history.csv epochs from {rows[0]['date'].isoformat()} to "
              f"{rows[-1]['date'].isoformat()} with protocol_version >= {rect} (RectifyInflation: reward = "
              "max_inflation_rate x supply x epoch seconds / seconds per year)",
              "annualized_mint_rate - max_inflation_rate per epoch, in pp"]
    put(reg, "mint_check_from", rows[0]["date"].isoformat(), "date", "F-mint-check", files, inputs)


def ceiling_at(segs: list[Segment], t: datetime) -> float | None:
    """The issuance ceiling in force at instant t: the rate of the exact segment holding t, else None."""
    for seg in segs:
        if seg.t0 <= t < seg.t1:
            return seg.hi if seg.exact else None
    return None


def mint_check_boundaries(m: SupplyModel) -> tuple[list[dict], str]:
    """F-mint-check over every epoch boundary of the trailing year (rpc/epoch_mints.csv, rows chained across the
    window as for F-burn-epochs): each boundary's annualized mint against the ceiling in force during the epoch it pays
    for (the exact ceiling segment holding the epoch's midpoint; the first boundary on a new ceiling still pays the
    epoch before it at the old one). ([], why) when the chain does not cover the window or a ceiling is unknown."""
    if not m.g365:
        return [], m.g365_reason or "no trailing-year window"
    r0, r1 = m.g365["r0"], m.g365["r1"]
    rect, _ = rectify_protocol()
    # protocol versions only rise, so the latest sample at or before the window start bounds every epoch inside it
    known = [r for r in m.smp.rows if r.get("protocol_version") is not None and r["date"] <= r0["date"]]
    proto = known[-1]["protocol_version"] if known else None
    if rect is None or proto is None or proto < rect:
        return [], (f"the window start's protocol ({proto}) is not known to be at or above RectifyInflation ({rect}), "
                    "so an epoch's annualized mint is not comparable with the ceiling")
    res, why = epoch_burn(m.mints, r0, r1)
    if res is None:
        return [], why
    by_height = {b["height"]: b for b in m.mints}
    out = []
    for b in res["rows"]:
        prev = by_height.get(b["prev"])
        if prev is None or prev.get("time") is None or b.get("time") is None or b.get("mint_rate") is None:
            return [], f"boundary {b['height']} lacks its previous boundary's time or a mint rate"
        mid = prev["time"] + (b["time"] - prev["time"]) / 2
        ceil = ceiling_at(m.segs, mid)
        if ceil is None:
            return [], (f"no exact ceiling is known at {mid.isoformat()} (the epoch paid at boundary {b['height']}); "
                        "rpc/protocol_changes.csv and the samples do not pin it")
        out.append(dict(b, ceiling=ceil, gap_pp=(b["mint_rate"] - ceil) * 100))
    return out, ""


def _put_mint_check_365(reg: Registry, m: SupplyModel) -> None:
    """Realized mint over the trailing year: every epoch boundary when rpc/epoch_mints.csv covers the
    window, else the sampled epochs (rpc/epoch_history.csv) inside it; mint_check_365_method says which."""
    rows, why = mint_check_boundaries(m)
    keys = (("mint_vs_ceiling_median_365_pp", "pp", 3), ("mint_vs_ceiling_min_365_pp", "pp", 3),
            ("mint_vs_ceiling_max_365_pp", "pp", 3), ("n_mint_epochs_365", "count", None),
            ("mint_check_365_method", "text", None))
    if rows:
        files = sorted(set(m.mint_files + m.change_files + m.smp.files))
        method = "every epoch boundary"
        inputs = [f"{len(rows)} rpc/epoch_mints.csv boundaries after block {m.g365['r0']['height']} through block "
                  f"{m.g365['r1']['height']} (the g_365 window), each annualized_mint_rate against the ceiling in force "
                  "during the epoch it pays for", "annualized mint - ceiling per boundary, in pp"]
    else:
        sampled, _, rect_files = mint_check_epochs(m)
        d0 = m.g365["r0"]["date"] if m.g365 else None
        d1 = m.g365["r1"]["date"] if m.g365 else None
        rows = [e for e in sampled if d0 is not None and d0 <= e["date"] <= d1]
        files = sorted(set(m.epoch_files + rect_files))
        method = "sampled: one epoch per month"
        inputs = [f"every epoch boundary not available: {why}",
                  f"{len(rows)} rpc/epoch_history.csv epochs sampled inside the g_365 window"]
        if not rows:
            for key, unit, dp in keys:
                put(reg, key, None, unit, "F-mint-check", files, inputs,
                    reason=f"no epoch boundaries or sampled epochs cover the trailing year ({why})", decimals=dp)
            return
    gaps_pp = [r["gap_pp"] for r in rows]
    values = {"mint_vs_ceiling_median_365_pp": statistics.median(gaps_pp), "mint_vs_ceiling_min_365_pp": min(gaps_pp),
              "mint_vs_ceiling_max_365_pp": max(gaps_pp), "n_mint_epochs_365": len(rows),
              "mint_check_365_method": method}
    for key, unit, dp in keys:
        put(reg, key, values[key], unit, "F-mint-check", files, inputs, decimals=dp)


def _put_burn(reg: Registry, m: SupplyModel, no_rpc: str) -> None:
    col = m.burn_trailing
    if col is not None and col.method:
        label = METHOD_LABEL[col.method]
        inputs = [f"{label} over {col.start.isoformat()} to {col.end.isoformat()}: {col.note}"]
        if col.method == "epochs":
            first, last = col.boundaries[0], col.boundaries[-1]
            inputs.append(f"{len(col.boundaries)} epoch boundaries, {_boundary_ref(first)} to {_boundary_ref(last)}: "
                          f"minted {col.minted:.2f} NEAR, supply growth {col.supply_growth:.2f} NEAR")
        if col.method == "sampled":
            inputs.append(f"{len(col.epochs)} sampled epochs: " + ", ".join(e["date"].isoformat() for e in col.epochs))
        put(reg, "burn_share_365", pct(col.share), "%", col.formula, col.files, inputs)
        put(reg, "burn_method", label, "text", col.formula, col.files, inputs)
        put(reg, "mint_per_day_365", col.minted / col.days, "NEAR", col.formula, col.files,
            inputs + [f"{col.days:.2f} days"])
        put(reg, "burn_per_day_365", col.burnt / col.days, "NEAR", col.formula, col.files,
            inputs + [f"{col.days:.2f} days"])
    else:
        reason = no_rpc if m.latest is None else (
            "no exact source (BigQuery daily_supply_flows absent or incomplete, rpc/epoch_mints.csv not covering the "
            "year), no month-by-month epoch sample (rpc/epoch_history.csv) and no supply sample within "
            f"{EDGE_DAYS} days of {col.start.isoformat()} for the upper bound")
        for key, unit in (("burn_share_365", "%"), ("burn_method", "text"), ("mint_per_day_365", "NEAR"),
                          ("burn_per_day_365", "NEAR")):
            put(reg, key, None, unit, "F-burnshare", files_of(m), reason=reason)

    no_burn = reason if not (col is not None and col.method) else "the trailing burn window has no length"
    # a short method word for running prose (the full label stays in the c05 title and the num() tooltip)
    if col is not None and col.method:
        put(reg, "burn_method_short", METHOD_SHORT[col.method], "text", col.formula, col.files,
            [f"method of burn_share_365: {METHOD_LABEL[col.method]}"])
    else:
        put(reg, "burn_method_short", None, "text", "F-burnshare", files_of(m), reason=no_burn)
    # T3's gap: the burn a day the tripwire needs, and how many times today's burn that is
    t3 = T3_BURN_PCT
    if col is not None and col.method and col.days:
        mint_day, burn_day = col.minted / col.days, col.burnt / col.days
        need = t3 / 100 * mint_day
        put(reg, "burn_needed_t3", need, "NEAR", "F-t3-gap", col.files,
            [f"T3 threshold {t3:g}% x mint_per_day_365 {mint_day:.2f} NEAR"])
        put(reg, "burn_multiple_t3", need / burn_day if burn_day > 0 else None, "x", "F-t3-gap", col.files,
            [f"burn_needed_t3 {need:.2f} NEAR / burn_per_day_365 {burn_day:.2f} NEAR"],
            "no fee burn in the trailing year, so no multiple of it")
    else:
        for key, unit in (("burn_needed_t3", "NEAR"), ("burn_multiple_t3", "x")):
            put(reg, key, None, unit, "F-t3-gap", files_of(m), reason=no_burn)

    # the upper bound over the same window, shown next to whichever method won
    ub_files = sorted(set(m.files))
    put(reg, "burn_share_365_ub", pct(col.ub_share) if col else None, "%", "F-burn-ub", ub_files,
        [f"F-burn-ub over {col.start.isoformat()} to {col.end.isoformat()}"] if col and col.ub_share is not None
        else [], no_rpc if col is None else "no supply sample near a window edge, or the ceiling schedule does not "
        "cover the window")
    # the sampled share and the every-epoch share over the same year, whichever method won (comparison, X7)
    window = f"{col.start.isoformat()} to {col.end.isoformat()}" if col else ""
    put(reg, "burn_share_365_sampled", pct(col.sampled_share) if col else None, "%", "F-burn-sampled",
        m.epoch_files, [f"{len(col.epochs)} epochs sampled from {window}: "
                        + ", ".join(e["date"].isoformat() for e in col.epochs)]
        if col and col.sampled_share is not None else [], (col.sampled_reason if col else no_rpc) or "no sampled epochs")
    put(reg, "n_burn_epochs_365_sampled", len(col.epochs) if col and col.sampled_share is not None else None,
        "count", "F-burn-sampled", m.epoch_files, [f"epochs sampled from {window}"] if col else [],
        (col.sampled_reason if col else no_rpc) or "no sampled epochs")
    epoch_files = sorted(set(m.mint_files + m.smp.files))
    put(reg, "burn_share_365_epochs", pct(col.epochs_share) if col else None, "%", "F-burn-epochs", epoch_files,
        [f"{len(col.boundaries)} epoch boundaries from {_boundary_ref(col.boundaries[0])} to "
         f"{_boundary_ref(col.boundaries[-1])}, between the samples of {window}"]
        if col and col.epochs_share is not None else [], (col.epochs_reason if col else no_rpc) or "no boundaries")
    # epochs behind burn_share_365: every boundary (exact from epochs) or the monthly sample (sampled)
    if col is not None and col.method == "epochs":
        put(reg, "n_burn_epochs_365", len(col.boundaries), "count", "F-burn-epochs", epoch_files,
            [f"epoch boundaries summed from {window}"])
    elif col is not None and col.method == "sampled":
        put(reg, "n_burn_epochs_365", len(col.epochs), "count", "F-burn-sampled", m.epoch_files,
            [f"epochs sampled from {window}"])
    else:
        put(reg, "n_burn_epochs_365", None, "count", col.formula if col and col.formula else "F-burnshare",
            files_of(m), reason=(f"the {METHOD_LABEL[col.method]} method does not count epochs"
                                 if col is not None and col.method else no_burn))


def files_of(m: SupplyModel) -> list[str]:
    return sorted(set(m.files))


# =====================================================================================================
# charts()
# =====================================================================================================

def charts(reg: Registry) -> list[Chart]:
    m = build_model()
    return [rolling_growth(reg, m), mint_burn(reg, m), supply_reconciliation(reg, m)]


def _coverage(dates: list[date]) -> dict:
    if not dates:
        return {"first": None, "last": None, "n": 0, "gaps": []}
    dates = sorted(dates)
    return {"first": dates[0].isoformat(), "last": dates[-1].isoformat(), "n": len(dates), "gaps": gaps(dates)}


def _empty_rpc(chart: Chart) -> Chart:
    chart.status = "unavailable"
    chart.empty_state = {"reason": "No NEAR RPC supply samples",
                         "needs": "rpc/history_samples.csv from the NEAR archival JSON-RPC",
                         "fix_command": FIX}
    return chart


def rolling_growth(reg: Registry, m: SupplyModel) -> Chart:
    """c02: trailing one-year supply growth at every full-day sample date vs the exact ceiling-equivalent over the
    same window, plus the realized-mint check (each sampled epoch's annualized mint vs the rate in force)."""
    queries = block_queries(m.smp)
    if m.epochs:
        queries.append(rpc_query("block", {"block_id": "<epoch boundary height>"},
                                 file="data/raw/rpc/epoch_history.csv"))
    if m.flow_files:
        queries.append(sql_query("daily_supply_flows"))
    chart = Chart(
        id="c02", section="O1", priority="lead", claims=["H1"], title="", sentence="", form="line",
        sources=[RPC_SOURCE + ": block header total_supply (rpc/history_samples.csv, rpc/dense_samples.csv), "
                 "EXPERIMENTAL_protocol_config max_inflation_rate (rpc/protocol_changes.csv for the exact change "
                 "block)"] + (["NEAR RPC epoch mints and burns, one complete epoch per month "
                               "(data/raw/rpc/epoch_history.csv)"] if m.epochs else []),
        queries=queries, table={"columns": [], "rows": []},
        code_ref="pipeline/models/supply.py::rolling_growth",
        formula_ids=["F-growth", "F-ceiling"] + (["F-mint-check"] if m.epochs else []),
        eyebrow="O1 · tests H1", x={"type": "time"}, y={"unit": "%"},
        verdicts=["H1"], as_of=as_of(m.files))
    # the sign becomes the verb; a shrinking supply prints its figure unsigned
    g = val(reg, "g_365")
    moved = "shrank {g_365:abs}" if g is not None and g < 0 else "grew {g_365}"
    chart.title_template = pick(reg, [
        f"NEAR's supply {moved} over the last year, against an issuance ceiling of {{i_max_now}}.",
        f"NEAR's supply {moved} over the last year.",
        "NEAR's total-supply growth over trailing one-year windows, against the protocol's issuance ceiling.",
    ])
    # one plain sentence says what is drawn; the caveat reconciles the gray line with the nominal ceiling; the
    # method detail (cadence, calendar-year gap, mint check) sits in the notes
    chart.sentence_template = ("Blue is how much NEAR's supply grew over the trailing year at each sample date; gray "
                               "is the most the issuance ceiling allowed over the same window, and a point below "
                               "zero would mean supply shrank.")
    chart.caveat_template = pick(reg, [
        "The gray line compounds the nominal ceiling as it mints, so a full year at today's {i_max_now} allows "
        "{ceiling_365}; growth of {g_365} sits below both.",
        "",
    ])
    chart.notes_templates = pick_notes(reg, [
        "Samples are monthly until {rpc_weekly_from}, weekly until {rpc_daily_from} and daily since, so the line "
        "grows denser to the right.",
        "The gray line switches rate at the exact block of each ceiling change.",
        "Over the last year the {n_mint_epochs_365} epochs minted at the ceiling with a median gap of "
        "{mint_vs_ceiling_median_365_pp}, from {mint_vs_ceiling_min_365_pp} to {mint_vs_ceiling_max_365_pp}.",
    ])
    if not m.series:
        chart.resolve(reg)
        return _empty_rpc(chart)

    pts = [[w["r1"]["date"].isoformat(), r4(pct(w["g"]))] for w in m.series]
    cpts = [[w["r1"]["date"].isoformat(), r4(pct(w["ceil"]))] for w in m.series]
    # every window is plotted (one per full-day sample date) and the CSV lists each one
    chart.series = [
        Series("g_365", "NEAR supply growth, trailing year", "near", with_breaks(pts)),
        Series("ceiling", "Issuance ceiling over the same window", "context", with_breaks(cpts),
               role="context", underlay=True),
    ]
    # the zero line is unlabeled on the plot (a legend swatch for the axis baseline was clutter); the sentence
    # says what falling below it means
    chart.reference_lines = [{"y": 0, "label": "zero"}]
    columns = ["kind", "period", "start_sample", "end_sample", "days", "growth_pct", "ceiling_pct", "ceiling_basis",
               "ceiling_minus_growth_pp"]
    windows = []
    for w in m.series:
        windows.append((w, ["trailing year", w["r1"]["date"].isoformat(), w["r0"]["date"].isoformat(),
                            w["r1"]["date"].isoformat(), round(days_between(w["r0"], w["r1"]), 3),
                            r4(pct(w["g"])), r4(pct(w["ceil"])), "exact" if w["ceil_exact"] else "upper bound",
                            r4(None if w["ceil"] is None else (w["ceil"] - w["g"]) * 100)]))
    rest = []
    for y in m.years:
        rest.append(["calendar year", str(y["year"]), y["r0"]["date"].isoformat(), y["r1"]["date"].isoformat(),
                     round(days_between(y["r0"], y["r1"]), 3), r4(pct(y["g"])), r4(pct(y["ceil"])),
                     "exact" if y["ceil_exact"] else "upper bound", r4(y["gap_pp"])])
    for year in m.missing_years:
        rest.append(["calendar year", str(year), None, None, None, None, None, "missing", None])
    checked, rect, _ = mint_check_epochs(m)
    compared = {e["date"] for e in checked}
    for e in m.epochs:
        if e["minted"] <= 0 or e["mint_rate"] is None:
            continue
        timed = e["date"] in compared
        rest.append(["epoch mint", e["date"].isoformat(), e["start"], e["end"], round(e["seconds"] / 86400, 4),
                     r4(pct(e["mint_rate"])), r4(pct(e["max_rate"])),
                     "time-based reward: compared" if timed else "block-count reward: not compared",
                     r4((e["max_rate"] - e["mint_rate"]) * 100) if timed else None])
    full = [row for _, row in windows] + rest
    chart.csv_table = {"columns": columns, "rows": full}
    # the page table keeps the windows ending on the first of a month and the latest one (the chart JSON stays
    # under its size budget); the CSV download lists every window
    shown = [row for w, row in windows if w["r1"]["date"].day == 1 or w is m.series[-1]]
    chart.table = {"columns": columns, "rows": shown + rest,
                   "footnote": ("Trailing-year rows: windows ending on the first of each month, and the latest. "
                                "The CSV download lists the window ending at every sample date. Epoch-mint rows: "
                                "one complete epoch per month, annualized mint against max_inflation_rate.")}
    chart.annotations = _cut_annotations(m)
    chart.bands = _cut_bands(m)
    dates = [r["date"] for r in m.smp.with_("total_supply_near", full_only=True)]
    chart.coverage = _coverage(dates)
    trailing_gaps = [g for g in chart.coverage["gaps"]
                     if m.latest and date.fromisoformat(g[1]) > m.latest["date"] - timedelta(days=WINDOW_DAYS)]
    if chart.coverage["gaps"]:
        chart.status = "partial"
        chart.status_banner = ("RPC samples have gaps (listed under coverage); the line breaks there and no "
                               "window is computed without a sample near both edges.")
    if m.g365 is None or trailing_gaps:
        chart.status = "partial"
    chart.resolve(reg)
    return chart


def _cut_annotations(m: SupplyModel) -> list[dict]:
    out = []
    for c in m.chg:
        label = "issuance ceiling cut" if c.after.rate < c.before.rate else "issuance ceiling raised"
        if not c.exact:
            label += " (first sample with the new rate)"
        out.append({"x": c.after.date.isoformat(), "label": label, "kind": "ceiling_change"})
    return out


def _cut_bands(m: SupplyModel) -> list[dict]:
    return [{"axis": "x", "from": c.before.date.isoformat(), "to": c.after.date.isoformat(),
             "label": "ceiling changed somewhere in this interval"} for c in m.chg if not c.exact]


def mint_burn(reg: Registry, m: SupplyModel) -> Chart:
    """c05: fee burn as a share of issuance, per calendar year and the trailing year (exact > every epoch > sampled >
    upper bound), with the upper bound alongside and every sampled epoch listed."""
    kind = m.burn_trailing.method if m.burn_trailing else None   # "exact" | "epochs" | "sampled" | "upper bound"
    label = METHOD_LABEL.get(kind or "", None)
    queries = []
    if m.flow_files:
        queries.append(sql_query("daily_supply_flows"))
    gross, gross_files = load_gross_burn()
    if gross_files:
        queries.append(sql_query("fee_burn"))
    if any(c.method == "epochs" for c in m.burn):
        queries.append(rpc_query("block", {"block_id": "<epoch boundary height, then its prev_hash and next_epoch_id>"},
                                 file="data/raw/rpc/epoch_mints.csv"))
    if any(c.epochs for c in m.burn):
        queries.append(rpc_query("block", {"block_id": "<epoch first and last height>"},
                                 file="data/raw/rpc/epoch_history.csv"))
    queries += block_queries(m.smp)
    files = sorted(set(sum((c.files for c in m.burn), [])) | set(m.files) | set(gross_files))
    chart = Chart(
        id="c05", section="O3", priority="lead", claims=["H10"], title="", sentence="", form="column",
        sources=[RPC_SOURCE + ": block header total_supply, EXPERIMENTAL_protocol_config max_inflation_rate"] +
                (["NEAR RPC mint at every epoch boundary of the trailing year (data/raw/rpc/epoch_mints.csv)"]
                 if any(c.method == "epochs" for c in m.burn) else []) +
                (["NEAR RPC epoch mint and burn, one complete epoch per month (data/raw/rpc/epoch_history.csv)"]
                 if m.epochs else []) +
                ([f"BigQuery {BQ_DATASET}.blocks via sql/daily_supply_flows.sql"] if m.flow_files else []),
        queries=queries, table={"columns": [], "rows": []}, code_ref="pipeline/models/supply.py::mint_burn",
        formula_ids=sorted({c.formula for c in m.burn if c.formula} | {"F-burn-ub"}) or ["F-burnshare", "F-burn-ub"],
        eyebrow="O3 · tests H10 · trailing year, " + (label or "not computed"),
        # the axis fits the columns: a fixed full-issuance scale left them as slivers; the sentence states the
        # full-issuance line instead of drawing it off in empty space. pack: a column draws only the methods it
        # has (the trailing year's exact column and the years' sampled ones share a slot)
        x={"type": "category", "pack": True}, y={"unit": "%"}, verdicts=["H10"], as_of=as_of(files))
    share = val(reg, "burn_share_365")
    if share is not None and share >= BURN_MAJORITY_PCT:
        lead_title = ["Fee burn now offsets {burn_share_365} ({burn_method}) of issuance."]
    elif kind == "epochs":
        lead_title = ["Fees burned {burn_share_365} of the NEAR minted in the last year ({burn_method}, "
                      "{n_burn_epochs_365} epochs): usage does not offset issuance.",
                      "Fees burned {burn_share_365} of the NEAR minted in the last year ({burn_method}): usage does "
                      "not offset issuance."]
    elif kind == "sampled":
        # the sampled share rests on a handful of epochs; the hard upper bound travels with it
        lead_title = ["Fees burned about {burn_share_365} of the NEAR minted in the last year ({burn_method}, "
                      "{n_burn_epochs_365} epochs) and at most {burn_share_365_ub}: usage does not offset issuance.",
                      "Fees burned about {burn_share_365} of the NEAR minted in the last year ({burn_method}): usage "
                      "does not offset issuance."]
    elif kind == "upper bound":
        lead_title = ["Fees burned at most {burn_share_365} of the NEAR minted in the last year ({burn_method}): "
                      "usage does not offset issuance."]
    else:
        lead_title = ["Fees burned {burn_share_365} of the NEAR minted in the last year: usage does not offset "
                      "issuance."]
    chart.title_template = pick(reg, lead_title + ["NEAR fee burn as a share of issuance, by year."])
    hsp027 = put_hsp027(reg)
    paired = kind == "sampled" or any(c.method == "sampled" for c in m.burn)
    chart.sentence_template = (
        "Each solid column is NEAR burned as a share of NEAR minted, exact over every epoch for the trailing year "
        "and sampled from one epoch per month for each calendar year, beside the most supply alone allows "
        "(hatched); supply stops growing only when burn reaches all of issuance."
        if paired and kind == "epochs" else
        "Each pair of columns is NEAR burned as a share of NEAR minted, from one sampled epoch per month (solid) "
        "and the most supply alone allows (hatched); supply stops growing only when burn reaches all of issuance."
        if paired else
        "Each column is the most fees can have burned in that period, as a share of NEAR minted, from supply alone; "
        "supply stops growing only when burn reaches all of issuance."
        if kind == "upper bound" and not any(c.method == "exact" for c in m.burn) else
        "Each column is NEAR burned as a share of NEAR minted in that period; supply stops growing only when burn "
        "reaches all of issuance.")
    verb = {"upper bound": "at most ", "sampled": "about "}.get(kind or "", "")
    chart.notes_templates = pick_notes(reg, [
        f"Over the trailing year the protocol minted {verb}{{mint_per_day_365}} a day and burned {verb}"
        "{burn_per_day_365}.",
        "The trailing-year share sums the mint at all {n_burn_epochs_365} epoch boundaries between the two midnight "
        "UTC samples a year apart and counts as burn whatever of it supply did not keep."
        if kind == "epochs" else "",
        "The one-epoch-a-month sample read {burn_share_365_sampled} over the same year, from "
        "{n_burn_epochs_365_sampled} epochs; the exact figure replaces it." if kind in ("epochs", "exact") else "",
        "The trailing-year share rests on {n_burn_epochs_365} sampled epochs, each with its exact mint and burn."
        if kind == "sampled" else "",
        "The upper bound comes from supply alone and counts rewards left unminted by offline validators as burn, "
        "so the true share cannot exceed it; over the trailing year it is {burn_share_365_ub}.",
        "The developer gas rebate ended at block {hsp027_height} on {hsp027_from}, so from then the whole gas fee "
        "burns; before, part of it went to the called contract.",
        "The axis is scaled to the columns, which sit far below the full-issuance line.",
    ])
    if not m.burn or not any(c.method for c in m.burn):
        chart.resolve(reg)
        if m.latest is None:
            return _empty_rpc(chart)
        chart.status = "unavailable"
        chart.empty_state = {"reason": "No period has an exact or sampled burn source or supply samples at both "
                                       "edges",
                             "needs": "rpc/epoch_history.csv epochs in every month, rpc/history_samples.csv samples "
                                      "near each period edge, or BigQuery daily_supply_flows", "fix_command": FIX}
        return chart
    # the measured share per column (exact where BigQuery covers it, else the epoch sample), and beside it the
    # hard upper bound from supply alone for every column that has one (a sample above it is visible)
    if any(c.method == "exact" for c in m.burn):
        chart.series.append(Series("burn_exact", "Exact (BigQuery)", "near",
                                   [[c.label, r4(pct(c.share)) if c.method == "exact" else None] for c in m.burn]))
    if any(c.method == "epochs" for c in m.burn):
        chart.series.append(Series("burn_epochs", "Exact: every epoch boundary (RPC)", "near",
                                   [[c.label, r4(pct(c.share)) if c.method == "epochs" else None] for c in m.burn]))
    measured = ("exact", "epochs")
    if any(c.sampled_share is not None and c.method not in measured for c in m.burn):
        chart.series.append(Series("burn_sampled", "Sampled: one epoch per month (RPC)", "near",
                                   [[c.label, r4(pct(c.sampled_share)) if c.method not in measured else None]
                                    for c in m.burn]))
    if any(c.ub_share is not None for c in m.burn):
        chart.series.append(Series("burn_ub", "Upper bound from supply (at most)", "near",
                                   [[c.label, r4(pct(c.ub_share))] for c in m.burn], dashed=True))
    for c in m.burn:
        if c.sampled_share is not None and c.ub_share is not None and c.sampled_share > c.ub_share \
                and c.method not in measured:
            chart.annotations.append({"x": c.label, "label": "Sample above the bound", "kind": "data_boundary"})

    def gross_days(c: BurnCol) -> list[float]:
        return [v for d, v in gross.items() if c.start <= d < c.end]

    def per_day(v: float | None, days: float | None) -> float | None:
        return None if not days or v is None else round(v / days, 2)

    def note(c: BurnCol) -> str:
        out = c.note
        if c.sampled_share is None and c.sampled_reason:
            out += f"; sampled: {c.sampled_reason}"
        if c.epochs_share is None and c.epochs_reason and c.method != "exact":
            out += f"; every epoch: {c.epochs_reason}"
        return out

    columns = ["period", "from", "to", "method", "sampled_epochs", "epoch_boundaries", "days", "minted_near",
               "burnt_near", "burn_share_pct", "sampled_share_pct", "upper_bound_share_pct", "minted_per_day_near",
               "burnt_per_day_near", "note", "bq_gross_fee_burn_per_day_near", "bq_gross_fee_burn_days_sampled"]
    rows = [[c.label, c.start.isoformat(), c.end.isoformat(), METHOD_LABEL.get(c.method or "", "missing"),
             len(c.epochs) if c.epochs else None, len(c.boundaries) if c.boundaries else None,
             None if c.days is None else round(c.days, 3),
             None if c.minted is None else round(c.minted, 2), None if c.burnt is None else round(c.burnt, 2),
             r4(pct(c.share)), r4(pct(c.sampled_share)), r4(pct(c.ub_share)), per_day(c.minted, c.days),
             per_day(c.burnt, c.days), note(c),
             round(statistics.mean(gross_days(c)), 2) if gross_days(c) else None, len(gross_days(c))]
            for c in m.burn]
    # every sampled epoch, outliers included (none is dropped: each weighs about a month in its period)
    seen = set()
    for c in m.burn:
        for e in c.epochs:
            if e["date"] in seen:
                continue
            seen.add(e["date"])
            days = e["seconds"] / 86400
            rows.append([f"epoch {e['date'].isoformat()}", e["start"], e["end"], "one sampled epoch", None, None,
                         round(days, 4), round(e["minted"], 2), round(e["burnt"], 2), r4(pct(e["share"])), None,
                         None, per_day(e["minted"], days), per_day(e["burnt"], days),
                         f"blocks {int(e['start_height'])} to {int(e['end_height'])}" if e.get("start_height")
                         and e.get("end_height") else "", None, None])
    chart.table = {
        "columns": columns,
        "footnote": ("method names how burn_share_pct was measured. exact: every epoch boundary sums the mint at "
                     "each boundary between the two 00:00 UTC samples (rpc/epoch_mints.csv, each row linked to the "
                     "one before) and takes burnt as minted minus supply growth; epoch_boundaries counts them. "
                     "sampled_share_pct: burnt over minted summed over one complete epoch per calendar month "
                     "(rpc/epoch_history.csv); sampled_epochs counts them. upper_bound_share_pct: the same edges "
                     "from supply alone, counting rewards left unminted as burn. Epoch rows list each sampled epoch. "
                     "bq_gross_fee_burn_per_day_near: mean BigQuery gross tokens_burnt (sql/fee_burn.sql) over the "
                     "sampled days in the period. Before the developer gas rebate ended "
                     + (f"(HSP-027, {hsp027.isoformat()}) " if hsp027 else "(HSP-027) ")
                     + "gross overstates the supply burn, because nearcore paid part of function-call gas to "
                       "the called contract; from then gross equals net. Cross-check only."),
        "rows": rows}
    if hsp027:
        # the column whose period contains the activation (the trailing year wins over a calendar year)
        hit = [c for c in m.burn if c.start <= hsp027 < c.end or (c is m.burn_trailing and c.start <= hsp027 <= c.end)]
        if hit:
            chart.annotations.append({"x": hit[-1].label, "label": "Rebate ends in this period",
                                      "kind": "protocol_change", "date": hsp027.isoformat()})
    chart.coverage = _coverage([r["date"] for r in m.smp.with_("total_supply_near", full_only=True)])
    chart.coverage["missing_periods"] = [c.label for c in m.burn if not c.method]
    if kind == "epochs":
        # the headline is exact; the calendar years still fall back to the monthly sample
        if any(c.method not in measured for c in m.burn if c is not m.burn_trailing):
            chart.status = "fallback"
            chart.status_banner = ("The trailing year is exact from every epoch boundary; the calendar years are "
                                   "sampled, because exact years need BigQuery daily_supply_flows, which "
                                   + ("does not cover them." if m.flow_files else "is not configured."))
    elif kind != "exact":
        chart.status = "fallback"
        # digit-free (no heights or dates): the table note and burn_share_365_epochs' null reason say why
        why = bool(m.burn_trailing and m.burn_trailing.epochs_reason)
        chart.status_banner = (f"Built from the {kind or 'available'} method; exact burn needs BigQuery "
                               "daily_supply_flows, which " + ("does not cover the trailing year"
                                                               if m.flow_files else "is not configured")
                               + (", or every epoch boundary of the year in rpc/epoch_mints.csv, which does not "
                                  "cover it (the table note says why)." if why else "."))
    if any(not c.method for c in m.burn):
        chart.status = "partial"
    chart.resolve(reg)
    return chart


def supply_reconciliation(reg: Registry, m: SupplyModel) -> Chart:
    """c16 (data.html): BigQuery end-of-day supply minus the RPC sample on matched days."""
    queries = [sql_query("freshness"), sql_query("daily_supply_flows")] + block_queries(m.smp)[:1]
    files = sorted(set(m.flow_files + m.fresh_files + m.smp.files))
    chart = Chart(
        id="c16", section="data", priority="supporting", claims=[], title="", sentence="", form="line",
        sources=[f"BigQuery {BQ_DATASET}.blocks via sql/daily_supply_flows.sql", RPC_SOURCE + ": block"],
        queries=queries, table={"columns": [], "rows": []},
        code_ref="pipeline/models/supply.py::supply_reconciliation", formula_ids=["X1"],
        eyebrow="Diagnostic · cross-check X1", x={"type": "time"}, y={"unit": "NEAR"}, as_of=as_of(files))
    chart.title_template = pick(reg, [
        "BigQuery and NEAR RPC agree on total supply to within {max_abs_diff_near} on {n_overlap} matched "
        "days; BigQuery has blocks through {bq_max_date}.",
        "BigQuery and NEAR RPC total supply, reconciled on matched days.",
    ])
    chart.sentence_template = pick(reg, [
        "The line is BigQuery's end-of-day total supply minus the RPC sample taken at the last block of the same "
        "UTC day; the gray band marks days after {bq_max_date}, where BigQuery has no data.",
        "The line is BigQuery's end-of-day total supply minus the RPC sample taken at the last block of the same "
        "UTC day.",
    ])
    if not m.matches:
        chart.resolve(reg)
        chart.status = "unavailable"
        chart.form = "empty"
        if not m.flow_files:
            chart.empty_state = {"reason": BQ_NOT_CONFIGURED, "needs": BQ_NEEDS, "fix_command": BQ_FIX}
        else:
            chart.empty_state = {"reason": "No RPC sample falls on a day BigQuery covers",
                                 "needs": "RPC samples inside the BigQuery window", "fix_command": BQ_FIX}
        return chart
    chart.series = [Series("diff", "BigQuery minus RPC total supply", "near",
                           [[d.isoformat(), round(diff, 6)] for d, _, _, diff in m.matches], unit="NEAR")]
    chart.reference_lines = [{"y": 0, "label": "sources agree"}]
    if m.bq_max and m.tip and m.tip["time"].date() > m.bq_max:
        chart.bands = [{"axis": "x", "from": m.bq_max.isoformat(), "to": m.tip["time"].date().isoformat(),
                        "label": "no data in source"}]
    chart.table = {"columns": ["block_date", "rpc_supply_near", "bigquery_eod_supply_near", "diff_near"],
                   "rows": [[d.isoformat(), round(s, 6), round(e, 6), round(diff, 6)]
                            for d, s, e, diff in m.matches]}
    chart.coverage = _coverage([d for d, *_ in m.matches])
    if m.bq_max and m.tip and (m.tip["time"].date() - m.bq_max).days > BQ_STALE_DAYS:
        chart.status = "partial"
        chart.status_banner = "BigQuery ends before the RPC tip; the band marks days with no data in source."
    chart.resolve(reg)
    return chart
