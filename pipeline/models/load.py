"""Typed loaders over data/raw/ shared by every model.

Each loader returns plain Python structures sorted by date and also reports the raw
file(s) it read, so models can pass them straight to Registry.put(source_files=...).
Blank raw values load as None; nothing here fills, interpolates or forward-fills.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path

from pipeline.config import RAW_DIR, ROOT


@dataclass
class Series:
    """A dated series: points sorted by date, values may be None."""
    name: str
    points: list[tuple[date, float | None]]
    files: list[str]

    def as_dict(self) -> dict[date, float | None]:
        return dict(self.points)

    def non_null(self) -> list[tuple[date, float]]:
        return [(d, v) for d, v in self.points if v is not None]

    @property
    def first(self) -> date | None:
        return self.points[0][0] if self.points else None

    @property
    def last(self) -> date | None:
        return self.points[-1][0] if self.points else None


def rel(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def _num(text: str | None) -> float | None:
    if text is None:
        return None
    text = text.strip()
    if text in ("", ".", "NaN", "nan"):
        return None
    return float(text)


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def _iso(text: str) -> date:
    return date.fromisoformat(text[:10])


def exists(source: str, name: str, ext: str = "csv") -> bool:
    return (RAW_DIR / source / f"{name}.{ext}").exists()


def meta(source: str, name: str) -> dict:
    sidecar = RAW_DIR / source / f"{name}.meta.json"
    return json.loads(sidecar.read_text()) if sidecar.exists() else {}


def csv_series(source: str, name: str, value_col: str, date_col: str = "date") -> Series:
    path = RAW_DIR / source / f"{name}.csv"
    points = [(_iso(r[date_col]), _num(r.get(value_col))) for r in _rows(path)]
    points.sort(key=lambda p: p[0])
    return Series(f"{source}/{name}:{value_col}", points, [rel(path)] if path.exists() else [])


def csv_rows(source: str, name: str) -> tuple[list[dict], list[str]]:
    path = RAW_DIR / source / f"{name}.csv"
    return _rows(path), ([rel(path)] if path.exists() else [])


def json_file(source: str, name: str) -> tuple[dict | list | None, list[str]]:
    path = RAW_DIR / source / f"{name}.json"
    if not path.exists():
        return None, []
    return json.loads(path.read_text()), [rel(path)]


# --- named sources ---------------------------------------------------------

def fred(series_id: str) -> Series:
    """FRED series: monthly series are dated the 1st of the month."""
    return csv_series("fred", series_id, "value")


def bis(kind: str, area: str) -> Series:
    """kind: 'policy_rates_monthly' or 'cpi_yoy_monthly'; dates are the 1st of the month."""
    path = RAW_DIR / "bis" / f"{kind}.csv"
    points = [(_iso(r["period"] + "-01"), _num(r["value"])) for r in _rows(path) if r["area"] == area]
    points.sort(key=lambda p: p[0])
    return Series(f"bis/{kind}:{area}", points, [rel(path)] if path.exists() else [])


def price_close(file_name: str) -> Series:
    """market/<file_name>.csv close, e.g. 'NEARUSDT_binance_daily' or 'near_usd_coinbase_daily'."""
    return csv_series("market", file_name, "close")


def rpc_samples() -> tuple[list[dict], list[str]]:
    """rpc/history_samples.csv rows with numeric fields parsed (None when blank)."""
    rows, files = csv_rows("rpc", "history_samples")
    numeric = ("height", "total_supply_near", "validator_stake_near", "n_validators",
               "stake_epoch_start_height", "staked_ratio", "stnear_price", "linear_price",
               "treasury_near", "protocol_version", "max_inflation_rate")
    parsed = []
    for r in rows:
        out = dict(r)
        out["date"] = _iso(r["date"])
        for key in numeric:
            if key in r:
                out[key] = _num(r[key])
        parsed.append(out)
    parsed.sort(key=lambda r: (r["date"], r.get("height") or 0))
    return parsed, files


def rpc_series(column: str) -> Series:
    rows, files = rpc_samples()
    return Series(f"rpc/history_samples:{column}", [(r["date"], r.get(column)) for r in rows], files)


def _rpc_parsed(name: str, numeric: tuple[str, ...], date_key: str) -> tuple[list[dict], list[str]]:
    """rpc/<name>.csv rows with `date_key` parsed to a date and numeric fields to float (None when blank)."""
    rows, files = csv_rows("rpc", name)
    parsed = []
    for r in rows:
        out = dict(r)
        if r.get(date_key):
            out[date_key] = _iso(r[date_key])
        for key in numeric:
            if key in r:
                out[key] = _num(r[key])
        parsed.append(out)
    return parsed, files


def rpc_dense() -> tuple[list[dict], list[str]]:
    """rpc/dense_samples.csv (weekly from 2022-04-04, daily for the trailing 400 days; 00:00 UTC blocks), by date."""
    rows, files = _rpc_parsed("dense_samples", ("height", "total_supply_near", "stnear_price", "linear_price"), "date")
    rows.sort(key=lambda r: (r["date"], r.get("height") or 0))
    return rows, files


def rpc_protocol_changes() -> tuple[list[dict], list[str]]:
    """rpc/protocol_changes.csv: first block of each tracked parameter change, by first_height."""
    rows, files = _rpc_parsed("protocol_changes", ("old_value", "new_value", "first_height", "protocol_version",
                                                   "prev_height"), "")
    rows.sort(key=lambda r: (r.get("first_height") or 0, r.get("param", "")))
    return rows, files


def rpc_epoch_history() -> tuple[list[dict], list[str]]:
    """rpc/epoch_history.csv: one complete epoch per history sample date (sample_date parsed), by sample_date."""
    rows, files = _rpc_parsed("epoch_history", (
        "start_height", "end_height", "next_start_height", "epoch_seconds", "supply_start_near", "supply_end_near",
        "minted_next_boundary_near", "burnt_in_epoch_near", "annualized_mint_rate", "annualized_burn_rate",
        "protocol_version", "max_inflation_rate"), "sample_date")
    rows.sort(key=lambda r: r["sample_date"])
    return rows, files


# --- merged RPC samples (history_samples ∪ dense_samples) -------------------------------------------------

# Columns both RPC sample files carry for the same 00:00 UTC block; on a shared date they must be identical.
MERGE_COLS = ("height", "block_time_utc", "total_supply_near", "stnear_price", "linear_price")
HISTORY_NUMERIC = ("height", "total_supply_near", "validator_stake_near", "n_validators", "stake_epoch_start_height",
                   "staked_ratio", "stnear_price", "linear_price", "treasury_near", "protocol_version",
                   "max_inflation_rate")


class SampleConflict(ValueError):
    """history_samples and dense_samples disagree on a date both sampled (same rule, so they must not)."""


def block_instant(row: dict) -> datetime | None:
    text = (row.get("block_time_utc") or "").strip()
    if not text:
        return None
    stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def is_day_boundary(row: dict) -> bool:
    """True for a full-day sample: the block is the last one at or before 00:00 UTC of row['date'] (both files'
    sampling rule). False for the latest-block row history_samples appends, whose block is after 00:00 UTC of its
    date. A row without a block time is taken at its target instant (00:00 UTC), so it counts as full-day."""
    stamp = block_instant(row)
    return stamp is None or stamp <= datetime.combine(row["date"], time(0), tzinfo=timezone.utc)


def _same(a, b) -> bool:
    if isinstance(a, str) or isinstance(b, str):
        return block_instant({"block_time_utc": a}) == block_instant({"block_time_utc": b})
    return a == b


def rpc_merged() -> tuple[list[dict], list[str]]:
    """rpc/history_samples.csv ∪ rpc/dense_samples.csv: every NEAR supply / LST-price sample, sorted by (date, height).

    Full-day samples (00:00 UTC blocks) are deduplicated by date. Where both files sampled a date, every column in
    MERGE_COLS must be identical (SampleConflict otherwise) and the history row is kept, since it carries the
    stake and protocol columns too. A dense-only row has those columns as None. The history file's latest-block row
    is kept as its own row with full_day False. Each row gains `full_day` (bool) and `sources` ("history", "dense"
    or "history+dense"). Nothing is interpolated: a date neither file sampled has no row.
    """
    hist, hfiles = rpc_samples()
    dense, dfiles = rpc_dense()
    by_date: dict[date, dict] = {}
    other: list[dict] = []
    for raw in hist:
        r = dict(raw)
        r["full_day"] = is_day_boundary(r)
        r["sources"] = "history"
        if not r["full_day"]:
            other.append(r)
        elif r["date"] in by_date:
            raise SampleConflict(f"rpc/history_samples.csv has two full-day rows dated {r['date']}")
        else:
            by_date[r["date"]] = r
    for raw in dense:
        if not is_day_boundary(raw):
            raise SampleConflict(f"rpc/dense_samples.csv row {raw['date']} is not a 00:00 UTC block "
                                 f"({raw.get('block_time_utc')})")
        prev = by_date.get(raw["date"])
        if prev is None:
            r = {col: None for col in HISTORY_NUMERIC}
            r.update(raw)
            r["full_day"], r["sources"] = True, "dense"
            by_date[raw["date"]] = r
            continue
        diff = [c for c in MERGE_COLS if not _same(prev.get(c), raw.get(c))]
        if diff:
            raise SampleConflict(
                f"rpc/history_samples.csv and rpc/dense_samples.csv disagree on {raw['date']}: "
                + "; ".join(f"{c} {prev.get(c)!r} vs {raw.get(c)!r}" for c in diff))
        prev["sources"] = "history+dense"
    rows = list(by_date.values()) + other
    rows.sort(key=lambda r: (r["date"], r.get("height") or 0))
    return rows, sorted(set(hfiles + dfiles))


def rpc_merged_series(column: str, full_day_only: bool = True) -> Series:
    """One column of rpc_merged() as a dated Series (full-day samples only by default, so every point is the
    state at 00:00 UTC of its date and date arithmetic measures elapsed time)."""
    rows, files = rpc_merged()
    pts = [(r["date"], r.get(column)) for r in rows if r["full_day"] or not full_day_only]
    return Series(f"rpc/history_samples+dense_samples:{column}", pts, files)


# --- fiat money panel (data/raw/money/, data/raw/bis_fx/) -------------------------------------------------
# Written by pipeline/sources/money.py and pipeline/sources/bis_fx.py. Values load exactly as published: the
# sidecar's `units` string (see unit_scale) and `seasonal_adjustment` say how to read them. Blank values load as None; a period the publisher does not list
# has no point.

MONEY_SOURCE = "money"
# scale words in sidecar unit strings ("AUD billion", "JPY 100 million", "CNY million")
UNIT_SCALES = {"million": 1e6, "billion": 1e9, "100 million": 1e8}


def period_start(period: str) -> date:
    """First day of a 'YYYY-MM' period (how money series are dated)."""
    text = period.strip()
    if len(text) == 7 and text[4] == "-":
        return date(int(text[:4]), int(text[5:]), 1)
    raise ValueError(f"not a YYYY-MM period: {period!r}")


def money_path(ccy: str, field: str) -> Path:
    return RAW_DIR / MONEY_SOURCE / ccy.lower() / f"{field}.csv"


def money_meta(ccy: str, field: str) -> dict:
    """Sidecar of data/raw/money/<ccy>/<field>.csv ({} when neither file nor sidecar exists)."""
    return meta(MONEY_SOURCE, f"{ccy.lower()}/{field}")


def money_series(ccy: str, field: str) -> Series:
    """data/raw/money/<ccy>/<field>.csv as published, dated on the first day of each month or quarter."""
    path = money_path(ccy, field)
    points = [(period_start(r["period"]), _num(r["value"])) for r in _rows(path)]
    points.sort(key=lambda p: p[0])
    return Series(f"{MONEY_SOURCE}/{ccy.lower()}/{field}", points, [rel(path)] if path.exists() else [])


def money_null_reason(ccy: str, field: str) -> str | None:
    """Why a money series has no data, or None when a raw file exists (a failed refresh that kept the previous
    vintage still loads; its sidecar carries `error` and `error_at`)."""
    if money_path(ccy, field).exists():
        return None
    m = money_meta(ccy, field)
    if m.get("null_reason"):
        return m["null_reason"]
    return f"no raw file data/raw/{MONEY_SOURCE}/{ccy.lower()}/{field}.csv (run make data)"


def unit_scale(units: str | None) -> float | None:
    """Multiplier from a sidecar unit string to single currency units: 'AUD billion' -> 1e9, 'JPY 100 million'
    -> 1e8, 'CHF million (end of month)' -> 1e6. None for an index, a percent, or an unknown string."""
    if not units:
        return None
    head = units.split("(", 1)[0].strip()
    parts = head.split(" ", 1)
    if len(parts) != 2 or len(parts[0]) != 3 or not parts[0].isalpha() or not parts[0].isupper():
        return None
    return UNIT_SCALES.get(parts[1].strip())


def unit_currency(units: str | None) -> str | None:
    """ISO code at the start of a sidecar unit string ('AUD billion' -> 'AUD'); None for an index or percent."""
    return units.split(" ", 1)[0] if unit_scale(units) is not None else None


def oecd_monagg(area: str, adjustment: str) -> Series:
    """money/oecd/monagg.csv: OECD broad money (MABM, national currency million) for one area and ADJUSTMENT
    code ('Y' or 'N', as labelled by OECD), dated on the first of the month."""
    path = RAW_DIR / MONEY_SOURCE / "oecd" / "monagg.csv"
    points = [(period_start(r["period"]), _num(r["value"])) for r in _rows(path)
              if r["area"] == area and r["adjustment"] == adjustment]
    points.sort(key=lambda p: p[0])
    return Series(f"{MONEY_SOURCE}/oecd/monagg:{area}.{adjustment}", points, [rel(path)] if path.exists() else [])


def bis_fx_turnover(year: str = "2025") -> tuple[dict, list[str]]:
    """bis_fx/fx_turnover_<year>.csv: {'total': all-currency turnover (TO1) or None, 'currencies': rows sorted by
    turnover, largest first, each {'currency', 'value' (USD million per day, None if not published), 'obs_status',
    'rank' (1-based among published values, None otherwise)}}. Shares of the total sum to two (two currencies per
    trade); computing them is left to the model."""
    path = RAW_DIR / "bis_fx" / f"fx_turnover_{year}.csv"
    rows = [r for r in _rows(path) if r["period"] == year]
    from pipeline.sources.bis_fx import NON_CURRENCY, TOTAL_CODE  # constants only; models never call the network
    total = next((_num(r["value"]) for r in rows if r["currency"] == TOTAL_CODE), None)
    ccys = [{"currency": r["currency"], "value": _num(r["value"]), "obs_status": r.get("obs_status")}
            for r in rows if r["currency"] != TOTAL_CODE and r["currency"] not in NON_CURRENCY]
    ccys.sort(key=lambda r: (r["value"] is None, -(r["value"] or 0.0), r["currency"]))
    for i, r in enumerate(ccys, 1):
        r["rank"] = i if r["value"] is not None else None
    return {"total": total, "currencies": ccys}, ([rel(path)] if path.exists() else [])
