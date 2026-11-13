"""Build the site from data/raw/ only: numbers, charts, verdicts, cross-checks, source status, pages.

    uv run python -m pipeline.build                         # `make charts`: strict, every model must exist
    uv run python -m pipeline.build --allow-missing-models  # skip absent model modules (partial builds, tests)
    uv run python -m pipeline.build --out-dir DIR           # write DIR/site/ and DIR/THESIS.md instead
    uv run python -m pipeline.build --verify                # `make verify`: offline rebuild in a temp dir + diff

Order: every model's compute(reg) in MODEL_ORDER, then every model's charts(reg), then verdicts
(thesis/rules.toml), then the rules.toml [crosschecks] and the source status board. The registry, charts and
verdicts are serialized to site/data/ with chartspec.dump (sorted keys, no wall clock), then read back
and rendered through the Jinja templates, so every page shows exactly what the JSON says.

Outputs (paths relative to the output root):
    site/data/{numbers,verdicts,sources,crosschecks,tripwires,manifest}.json
    site/data/charts/<id>.json, site/data/csv/<id>.csv
    site/data/sim.json (a model's site_files(): the desk simulation's payload, when its inputs exist)
    site/index.html, site/data.html, site/THESIS.md, THESIS.md, site/llms.txt
site/assets/ is written by hand (frontend) and is not touched.

Determinism: as_of is the latest fetched_at among the raw sidecars, never the wall clock; outputs carry
content hashes (rules_sha256, raw input sha256s), never a git sha, so a rebuild is byte-identical.
The network is blocked while models run: they must read data/raw/ only.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import difflib
import hashlib
import importlib
import json
import math
import os
import re
import shutil
import socket
import statistics
import string
import subprocess
import sys
import tempfile
import tomllib
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

import jinja2
from markupsafe import Markup, escape

from pipeline import config
from pipeline import numbers as numbers_mod
from pipeline.chartspec import Chart, dump
from pipeline.models import verdicts as verdict_model
from pipeline.numbers import NUMERIC_UNITS, TEXT_UNITS, Registry, fmt, parse_spec

MODEL_ORDER = ("supply", "staking", "holders", "macro", "market", "demand", "derivatives", "currencies", "money",
               "sweep", "portfolio", "pay")
MODEL_PACKAGE = "pipeline.models"

# template, mode, outputs (relative to the output root)
TEMPLATES = (
    ("index.html.j2", "html", ("site/index.html",)),
    ("data.html.j2", "html", ("site/data.html",)),
    ("THESIS.md.j2", "md", ("THESIS.md", "site/THESIS.md")),
    ("llms.txt.j2", "md", ("site/llms.txt",)),
)
DATA_FILES = ("numbers", "verdicts", "sources", "crosschecks", "tripwires", "manifest")
# copied into the temp dir by --verify (data/raw only; never .env)
VERIFY_COPY = ("pipeline", "templates", "thesis", "sql", "data/raw", "site/assets", "pyproject.toml")
VERIFY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".DS_Store")

# nearcore genesis num_seconds_per_year (365 days); used by cross-check X2 exactly as the protocol does
SECONDS_PER_YEAR = 365 * 24 * 60 * 60

STATE_ORDER = ("failed", "stale", "lagging", "partial", "ok")  # worst first
STALE_STATES = {"failed", "stale", "lagging", "partial"}
MISSING_STATES = {"not_configured", "withheld"}


class BuildError(RuntimeError):
    """The build refuses to produce output (fail closed). The message says what to fix."""


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


# =========================================================================== paths and small helpers

@dataclass(frozen=True)
class Paths:
    root: Path   # inputs: templates/, thesis/, data/raw/
    out: Path    # outputs: site/, THESIS.md

    @property
    def raw(self) -> Path:
        return self.root / "data" / "raw"

    @property
    def thesis(self) -> Path:
        return self.root / "thesis"

    @property
    def templates(self) -> Path:
        return self.root / "templates"

    @property
    def site(self) -> Path:
        return self.out / "site"

    @property
    def data(self) -> Path:
        return self.site / "data"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def load_toml(path: Path, required: bool = True) -> dict | None:
    if not path.exists():
        if required:
            raise BuildError(f"{path.name} is missing ({path})")
        return None
    with path.open("rb") as fh:
        return tomllib.load(fh)


def parse_ts(text: Any) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def to_date(text: Any) -> date | None:
    """ISO date, datetime or YYYY-MM (first of the month)."""
    if text is None or text == "":
        return None
    s = str(text).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}", s):
            return date.fromisoformat(s + "-01")
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def business_days_after(start: date, end: date) -> int:
    """Weekdays d with start < d <= end."""
    if end <= start:
        return 0
    n, d = 0, start + timedelta(days=1)
    while d <= end:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


def read_csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def num_or_none(text: Any) -> float | None:
    if text is None:
        return None
    s = str(text).strip()
    if s in ("", ".", "NaN", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def write_json(path: Path, obj: Any) -> None:
    write_text(path, dump(obj) + "\n")


def roundtrip(obj: Any) -> Any:
    """What the JSON file will hold (tuples -> lists, etc.)."""
    return json.loads(dump(obj))


# =========================================================================== no network while building

@contextmanager
def no_network():
    """Block sockets: models and the build read data/raw/ only."""
    def deny(*_args, **_kwargs):
        raise BuildError("network access during the build is forbidden: models must read data/raw/ only")

    targets = [(socket.socket, "connect"), (socket.socket, "connect_ex"),
               (socket, "create_connection"), (socket, "getaddrinfo")]
    saved = [(obj, name, getattr(obj, name)) for obj, name in targets]
    for obj, name, _ in saved:
        setattr(obj, name, deny)
    try:
        yield
    finally:
        for obj, name, original in saved:
            setattr(obj, name, original)


# =========================================================================== raw inventory

def raw_files(paths: Paths) -> list[Path]:
    """Data files under data/raw/ (sidecars, hidden files and _chunks caches excluded), sorted."""
    if not paths.raw.exists():
        return []
    out = []
    for p in sorted(paths.raw.rglob("*")):
        rel_parts = p.relative_to(paths.raw).parts
        if not p.is_file() or p.name.endswith(".meta.json"):
            continue
        if any(part.startswith(("_", ".")) for part in rel_parts):
            continue
        out.append(p)
    return out


def sidecar(path: Path) -> dict:
    meta = path.with_suffix(".meta.json")
    if not meta.exists():
        return {}
    try:
        return json.loads(meta.read_text())
    except json.JSONDecodeError:
        return {}


def build_as_of(files: Iterable[Path]) -> str | None:
    """Latest fetched_at among the sidecars of the raw files (never the wall clock)."""
    best: tuple[datetime, str] | None = None
    for p in files:
        stamp = sidecar(p).get("fetched_at")
        ts = parse_ts(stamp)
        if ts is not None and (best is None or ts > best[0]):
            best = (ts, stamp)
    return best[1] if best else None


# =========================================================================== models

def import_models(names: Iterable[str], package: str, allow_missing: bool) -> list[tuple[str, Any]]:
    modules, missing = [], []
    for name in names:
        full = f"{package}.{name}"
        try:
            module = importlib.import_module(full)
        except ModuleNotFoundError as exc:
            if exc.name in (full, package):
                missing.append(name)
                continue
            raise BuildError(f"{full} failed to import: {exc}") from exc
        for fn in ("compute", "charts"):
            if not callable(getattr(module, fn, None)):
                raise BuildError(f"{full} must define {fn}(reg)")
        modules.append((name, module))
    if missing:
        where = ", ".join(f"{package.replace('.', '/')}/{m}.py" for m in missing)
        if not allow_missing:
            raise BuildError(
                f"missing model module(s): {where}. Every model in the contract order must exist for "
                f"`make charts`; pass --allow-missing-models for a partial build without them.")
        warn(f"building without model module(s) {where} (--allow-missing-models)")
    return modules


def register_constants(reg: Registry, constants: dict, path: Path) -> None:
    """Declared constants as const_<name>[_lo|_hi], formula K-<name>, unless a model already registered them."""
    for name, c in constants.items():
        if not isinstance(c, dict):
            continue
        unit = c.get("unit")
        if unit not in NUMERIC_UNITS | TEXT_UNITS:
            continue
        for suffix, field_name in (("", "value"), ("_lo", "lo"), ("_hi", "hi")):
            key = f"const_{name}{suffix}"
            if field_name in c and key not in reg:
                reg.put(key, c[field_name], unit, f"K-{name}", source_files=[path], inputs=[name])


# =========================================================================== cross-checks

def _put(reg: Registry, key: str, value: Any, unit: str, formula: str, files: Iterable[Path | str],
         reason: str | None, decimals: int | None = None) -> None:
    # a cross-check gap (|a - b|) is a distance: printed without a sign
    reg.put(key, value, unit, formula, source_files=list(files),
            null_reason=None if value is not None else (reason or "not computed"), decimals=decimals,
            signed="abs" not in key)


def _reg_value(reg: Registry, key: str) -> tuple[Any, str | None]:
    if key not in reg:
        return None, f"{key} is not registered by any model"
    n = reg.get(key)
    return n.value, (None if n.value is not None else f"{key}: {n.null_reason}")


def _x1(reg: Registry, c: dict, paths: Paths) -> dict:
    keys = {"max_abs_diff_near": "max_abs_diff_near", "n_overlap": "n_overlap"}
    diff, why_d = _reg_value(reg, "max_abs_diff_near")
    n, why_n = _reg_value(reg, "n_overlap")
    if diff is None or n is None:
        return {"status": "not run", "reason": why_d or why_n, "keys": keys}
    if not n:
        return {"status": "not run", "reason": "no RPC sample height matched a BigQuery block", "keys": keys}
    return {"status": "pass" if abs(diff) <= c["max_abs_near"] else "fail", "reason": None, "keys": keys}


def _x2(reg: Registry, c: dict, paths: Paths) -> dict:
    keys = {"median_ratio": "xchk_x2_median_ratio", "n": "xchk_x2_n_boundaries"}
    mints_path = paths.raw / "rpc" / "epoch_mints.csv"
    hist_path = paths.raw / "rpc" / "history_samples.csv"
    snap_path = paths.raw / "rpc" / "snapshot_now.json"
    files = [p for p in (mints_path, hist_path, snap_path) if p.exists()]
    samples: list[tuple[int, float]] = []
    for row in read_csv_rows(hist_path):
        h, rate = num_or_none(row.get("height")), num_or_none(row.get("max_inflation_rate"))
        if h is not None and rate is not None:
            samples.append((int(h), rate))
    if snap_path.exists():
        snap = json.loads(snap_path.read_text())
        h, rate = num_or_none(snap.get("height")), num_or_none(snap.get("max_inflation_rate"))
        if h is not None and rate is not None:
            samples.append((int(h), rate))
    samples.sort()
    rows = [r for r in read_csv_rows(mints_path) if num_or_none(r.get("boundary_height")) is not None]
    rows.sort(key=lambda r: int(float(r["boundary_height"])))
    rows = rows[-int(c.get("last_n", 30)):]
    ratios, skipped = [], 0
    for r in rows:
        h = int(float(r["boundary_height"]))
        below = [s for s in samples if s[0] <= h]
        above = [s for s in samples if s[0] >= h]
        minted, supply, secs = (num_or_none(r.get(k)) for k in ("minted_near", "supply_before_near", "epoch_seconds"))
        if not below or not above or below[-1][1] != above[0][1] or None in (minted, supply, secs):
            skipped += 1
            continue
        expected = below[-1][1] * supply * secs / SECONDS_PER_YEAR
        if expected > 0:
            ratios.append(minted / expected)
    if not ratios:
        reason = ("no epoch boundary is bracketed by two protocol samples with the same max_inflation_rate"
                  if rows else "rpc/epoch_mints.csv is missing or empty")
        _put(reg, keys["median_ratio"], None, "ratio", "X2", files, reason)
        _put(reg, keys["n"], None, "count", "X2", files, reason)
        return {"status": "not run", "reason": reason, "keys": keys}
    median = statistics.median(ratios)
    _put(reg, keys["median_ratio"], median, "ratio", "X2", files, None, decimals=4)
    _put(reg, keys["n"], len(ratios), "count", "X2", files, None)
    ok = c["lo"] <= median <= c["hi"]
    # no computed count in prose: the registered xchk_x2_n_boundaries is the number of boundaries the median uses
    note = ("some epoch boundaries were skipped (no bracketing protocol samples); the median uses the "
            "xchk_x2_n_boundaries that were bracketed") if skipped else None
    return {"status": "pass" if ok else "fail", "reason": note, "keys": keys}


def _closes(path: Path) -> dict[date, float]:
    out = {}
    for row in read_csv_rows(path):
        d, v = to_date(row.get("date")), num_or_none(row.get("close"))
        if d is not None and v is not None and v > 0:
            out[d] = v
    return out


def _x3(reg: Registry, c: dict, paths: Paths) -> dict:
    keys = {"median_abs_pct": "xchk_x3_median_abs_pct", "n": "xchk_x3_n_days"}
    b_path = paths.raw / "market" / "NEARUSDT_binance_daily.csv"
    c_path = paths.raw / "market" / "near_usd_coinbase_daily.csv"
    files = [p for p in (b_path, c_path) if p.exists()]
    b, cb = _closes(b_path), _closes(c_path)
    start = to_date(c["start"])
    common = sorted(d for d in b if d in cb and d >= start)
    if not common:
        reason = "no common Binance and Coinbase close since " + c["start"]
        _put(reg, keys["median_abs_pct"], None, "%", "X3", files, reason)
        _put(reg, keys["n"], None, "count", "X3", files, reason)
        return {"status": "not run", "reason": reason, "keys": keys}
    diffs_bp = [abs(b[d] / cb[d] - 1) * 10000 for d in common]
    median_bp = statistics.median(diffs_bp)
    _put(reg, keys["median_abs_pct"], median_bp / 100, "%", "X3", files, None, decimals=3)
    _put(reg, keys["n"], len(common), "count", "X3", files, None)
    return {"status": "pass" if median_bp <= c["max_median_bp"] else "fail", "reason": None, "keys": keys}


def _x4(reg: Registry, c: dict, paths: Paths) -> dict:
    keys = {"max_abs_pp": "xchk_x4_max_abs_pp", "n": "xchk_x4_n_months"}
    # BIS publishes unadjusted CPI, so the like-for-like FRED series is CPI-U not seasonally adjusted
    f_path = paths.raw / "fred" / "CPIAUCNS.csv"
    b_path = paths.raw / "bis" / "cpi_yoy_monthly.csv"
    files = [p for p in (f_path, b_path) if p.exists()]
    cpi = {to_date(r.get("date")): num_or_none(r.get("value")) for r in read_csv_rows(f_path)}
    bis = {to_date(r["period"]): num_or_none(r.get("value"))
           for r in read_csv_rows(b_path) if r.get("area") == "US"}
    diffs = []
    for m in sorted(bis):
        if m is None or bis[m] is None:
            continue
        prev = date(m.year - 1, m.month, 1)
        now, then = cpi.get(m), cpi.get(prev)
        if now is None or then is None or then == 0:
            continue
        diffs.append((m, abs((now / then - 1) * 100 - bis[m])))
    diffs = diffs[-int(c.get("months", 12)):]
    if not diffs:
        reason = "no month with both FRED CPI-U (not seasonally adjusted) year-over-year and BIS US CPI"
        _put(reg, keys["max_abs_pp"], None, "pp", "X4", files, reason)
        _put(reg, keys["n"], None, "count", "X4", files, reason)
        return {"status": "not run", "reason": reason, "keys": keys}
    worst = max(d for _, d in diffs)
    _put(reg, keys["max_abs_pp"], worst, "pp", "X4", files, None, decimals=3)
    _put(reg, keys["n"], len(diffs), "count", "X4", files, None)
    return {"status": "pass" if worst <= c["max_abs_pp"] else "fail", "reason": None, "keys": keys}


def _x5(reg: Registry, c: dict, paths: Paths) -> dict:
    keys = {"abs_pp": "xchk_x5_abs_pp", "y_st_365": "y_st_365", "y_li_365": "y_li_365"}
    st, why_s = _reg_value(reg, "y_st_365")
    li, why_l = _reg_value(reg, "y_li_365")
    files = []
    for k in ("y_st_365", "y_li_365"):
        if k in reg:
            files += [numbers_mod.ROOT / f for f in reg.get(k).source_files]
    if st is None or li is None:
        reason = why_s or why_l
        _put(reg, keys["abs_pp"], None, "pp", "X5", files, reason)
        return {"status": "not run", "reason": reason, "keys": keys}
    diff = abs(st - li)
    _put(reg, keys["abs_pp"], diff, "pp", "X5", files, None)
    return {"status": "pass" if diff <= c["max_abs_pp"] else "fail", "reason": None, "keys": keys}


def _x6(reg: Registry, c: dict, paths: Paths) -> dict:
    keys = {"share_pct": "xchk_x6_share_pct", "n": "xchk_x6_n_days"}
    e_path = paths.raw / "intents" / "explorer_daily.csv"
    l_path = paths.raw / "intents" / "defillama_volume_daily.csv"
    if not e_path.exists():
        reason = ("The NEAR Intents Explorer comparison is not included: it needs partner access and written "
                  "permission to publish, so Intents volume rests on DefiLlama alone.")
        for k, unit in ((keys["share_pct"], "%"), (keys["n"], "count")):
            _put(reg, k, None, unit, "X6", [], reason)
        return {"status": "not run", "reason": reason, "keys": keys}
    llama = {to_date(r.get("date")): num_or_none(r.get("volume_usd")) for r in read_csv_rows(l_path)}
    ratios = []
    for r in read_csv_rows(e_path):
        if str(r.get("final", "")).strip().lower() not in ("true", "1", "yes"):
            continue
        d, vol = to_date(r.get("date")), num_or_none(r.get("volume_in_usd_success"))
        ref = llama.get(d)
        if vol is not None and ref:
            ratios.append(vol / ref)
    share = 100 * sum(1 for x in ratios if x <= c["max_ratio"]) / len(ratios) if ratios else None
    status = "not run" if share is None else ("pass" if share >= c["min_share_pct"] else "fail")
    explorer = (load_toml(paths.thesis / "sources.toml", required=False) or {}).get("sources", {}).get(
        "intents_explorer", {})
    if withheld(explorer):
        # computed locally only; Explorer-derived figures are not published without written permission
        local = "no covered final day" if share is None else f"{status}, {share:.1f}% of {len(ratios)} days"
        print(f"X6 (local only, withheld from the site): {local}", file=sys.stderr)
        reason = ("withheld: awaiting written permission from NEAR Intents to publish Explorer-derived "
                  "aggregates (ToS 4.1(e))")
        for k, unit in ((keys["share_pct"], "%"), (keys["n"], "count")):
            _put(reg, k, None, unit, "X6", [], reason)
        return {"status": "withheld", "reason": reason, "keys": keys}
    files = [p for p in (e_path, l_path) if p.exists()]
    reason = None if share is not None else "no final Explorer day overlaps DefiLlama"
    _put(reg, keys["share_pct"], share, "%", "X6", files, reason)
    _put(reg, keys["n"], len(ratios) if ratios else None, "count", "X6", files, reason)
    return {"status": status, "reason": reason, "keys": keys}


def _x7(reg: Registry, c: dict, paths: Paths) -> dict:
    """The exact trailing-year burn share from every epoch boundary (F-burn-epochs) against the hard upper bound from
    supply alone (F-burn-ub, which counts unminted rewards as burn), and how far the monthly sample sat from it."""
    keys = {"ub_margin_pp": "xchk_x7_ub_margin_pp", "vs_sampled_pp": "xchk_x7_vs_sampled_pp"}
    exact, why_e = _reg_value(reg, "burn_share_365_epochs")
    ub, why_u = _reg_value(reg, "burn_share_365_ub")
    sampled, why_s = _reg_value(reg, "burn_share_365_sampled")
    files = []
    for k in ("burn_share_365_epochs", "burn_share_365_ub", "burn_share_365_sampled"):
        if k in reg:
            files += [numbers_mod.ROOT / f for f in reg.get(k).source_files]
    files = sorted(set(files))
    if exact is None or ub is None:
        reason = why_e or why_u
        _put(reg, keys["ub_margin_pp"], None, "pp", "X7", files, reason)
        _put(reg, keys["vs_sampled_pp"], None, "pp", "X7", files, reason)
        return {"status": "not run", "reason": reason, "keys": keys}
    margin = ub - exact
    _put(reg, keys["ub_margin_pp"], margin, "pp", "X7", files, None, decimals=3)
    _put(reg, keys["vs_sampled_pp"], None if sampled is None else exact - sampled, "pp", "X7", files, why_s,
         decimals=3)
    ok = margin >= -float(c["max_excess_pp"])
    note = None if sampled is not None else "no sampled share over the same year to compare"
    return {"status": "pass" if ok else "fail", "reason": note, "keys": keys}


def _x9(reg: Registry, c: dict, paths: Paths) -> dict:
    """National money series against the OECD mirror of the same aggregate, latest common month (models/money.py)."""
    keys = {"n": "money_mirror_n", "max_rel_pct": "money_mirror_max_rel_pct"}
    worst, why = _reg_value(reg, keys["max_rel_pct"])
    n, why_n = _reg_value(reg, keys["n"])
    if worst is None or not n:
        return {"status": "not run", "reason": why or why_n or "no currency has both series", "keys": keys}
    return {"status": "pass" if worst <= float(c["max_rel_pct"]) else "fail", "reason": None, "keys": keys}


CROSSCHECKS: dict[str, Callable[[Registry, dict, Paths], dict]] = {
    "X1": _x1, "X2": _x2, "X3": _x3, "X4": _x4, "X5": _x5, "X6": _x6, "X7": _x7, "X9": _x9,
}


def run_crosschecks(reg: Registry, rules: dict, paths: Paths) -> dict:
    out = {}
    for xid, c in rules.get("crosschecks", {}).items():
        if xid not in CROSSCHECKS:
            raise BuildError(f"rules.toml crosschecks.{xid}: no cross-check function for this id")
        result = CROSSCHECKS[xid](reg, c, paths)
        shown = [reg.get(k) for k in result["keys"].values() if k in reg and reg.get(k).value is not None]
        out[xid] = {"id": xid, "title": c["title"], "tolerance_text": c["tolerance_text"],
                    "tolerance": c["tolerance_text"],
                    # registry displays only (status "withheld" registers nulls, so nothing leaks)
                    "observed": " · ".join(f"{n.key} {n.display}" for n in shown) or None,
                    "detail": result.get("reason"),
                    "blocks_verify": bool(c.get("blocks_verify", False)), "needs": c.get("needs"),
                    "fix": c.get("fix"), **result}
    counts = {"pass": 0, "fail": 0, "not run": 0, "withheld": 0}
    for x in out.values():
        counts[x["status"]] += 1
    for key, n in (("n_xchk", len(out)), ("n_xchk_pass", counts["pass"]), ("n_xchk_fail", counts["fail"]),
                   ("n_xchk_not_run", counts["not run"] + counts["withheld"])):
        reg.put(key, n, "count", "F-crosscheck", inputs=sorted(out))
    return out


# =========================================================================== source status

def _glob(root: Path, patterns: Iterable[str]) -> list[Path]:
    found: set[Path] = set()
    for pattern in patterns:
        for p in root.glob(pattern):
            if p.is_file() and not p.name.endswith(".meta.json") and "_chunks" not in p.parts:
                found.add(p)
    return sorted(found)


def _rpc_tip(paths: Paths) -> date | None:
    snap = paths.raw / "rpc" / "snapshot_now.json"
    if snap.exists():
        d = to_date(json.loads(snap.read_text()).get("block_time_utc"))
        if d:
            return d
    meta = sidecar(paths.raw / "rpc" / "history_samples.csv")
    return to_date((meta.get("latest_row") or {}).get("date"))


def _csv_extreme(path: Path, pick: Callable[[list[date]], date]) -> date | None:
    rows = read_csv_rows(path)
    if not rows:
        return None
    for col in ("date", "block_date", "period", "time", "block_time_utc"):
        if col in rows[0]:
            ds = [d for d in (to_date(r.get(col)) for r in rows) if d]
            return pick(ds) if ds else None
    return None


def _last_obs(path: Path, meta: dict, mode: str | None) -> date | None:
    if mode == "final_day":
        ds = [to_date(r.get("date")) for r in read_csv_rows(path)
              if str(r.get("final", "")).strip().lower() in ("true", "1", "yes")]
        ds = [d for d in ds if d]
        return max(ds) if ds else None
    if mode == "max_block_date":
        ds = [to_date(r.get("max_block_date")) for r in read_csv_rows(path)]
        ds = [d for d in ds if d]
        return max(ds) if ds else None
    for field_name in ("last_observed_date", "last_date", "last_time"):
        if meta.get(field_name):
            return to_date(meta[field_name])
    if isinstance(meta.get("latest_row"), dict) and meta["latest_row"].get("date"):
        return to_date(meta["latest_row"]["date"])
    if isinstance(meta.get("coverage"), dict):
        ds = [to_date(v.get("last_period")) for v in meta["coverage"].values() if isinstance(v, dict)]
        ds = [d for d in ds if d]
        if ds:
            return max(ds)
    if path.suffix == ".json":
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict):
            for k in ("block_time_utc", "checked_at", "window_end"):
                if payload.get(k):
                    return to_date(payload[k])
        return None
    return _csv_extreme(path, max)


def _first_obs(path: Path, meta: dict) -> date | None:
    for field_name in ("first_date", "first_time"):
        if meta.get(field_name):
            return to_date(meta[field_name])
    if isinstance(meta.get("coverage"), dict):
        ds = [to_date(v.get("first_period")) for v in meta["coverage"].values() if isinstance(v, dict)]
        ds = [d for d in ds if d]
        if ds:
            return min(ds)
    if path.suffix == ".csv":
        return _csv_extreme(path, min)
    return None


def _judge(rule: dict, last: date | None, as_of: date | None, tip: date | None) -> tuple[int | None, bool | None]:
    """(lag_days, over_threshold); over is None when it cannot be judged."""
    kind = rule.get("rule", "none")
    if last is None or as_of is None:
        return None, None if kind != "none" else False
    if kind == "behind_rpc_tip":
        if tip is None:
            return None, None
        lag = (tip - last).days
        return lag, lag > rule["max"]
    lag = (as_of - last).days
    if kind == "none":
        return lag, False
    if kind == "days":
        return lag, lag > rule["max"]
    if kind == "business_days":
        return lag, business_days_after(last, as_of) > rule["max"]
    if kind == "days_after_month_end":
        return (as_of - month_end(last)).days, (as_of - month_end(last)).days > rule["max"]
    raise BuildError(f"sources.toml: unknown stale rule {kind!r}")


def _worst(states: Iterable[str]) -> str:
    states = list(states)
    for s in STATE_ORDER:
        if s in states:
            return s
    return "ok"


def _source_constants(entry: dict, files: list[Path]) -> dict:
    constants = load_toml(files[0]) if files else {}
    tables = {k: v for k, v in (constants or {}).items() if isinstance(v, dict)}
    unverified = sorted(k for k, v in tables.items() if v.get("quote_status"))
    retrieved = sorted(d for d in (to_date(v.get("retrieved")) for v in tables.values()) if d)
    entry.update(state="partial" if unverified else "ok", rows=len(tables),
                 last_obs=retrieved[-1].isoformat() if retrieved else None,
                 first_obs=retrieved[0].isoformat() if retrieved else None,
                 note=("unverified quotes: " + ", ".join(unverified)) if unverified else None)
    return entry


def withheld(s: dict) -> bool:
    """A source whose files stay local until its publisher allows publication (sources.toml publish = false)."""
    return s.get("publish", True) is False


def withheld_files(cfg: dict, paths: Paths) -> set[Path]:
    """Raw files of withheld sources: left out of sources.json and the manifest's raw_inputs."""
    return {p for s in cfg.get("sources", {}).values() if withheld(s) for p in _glob(paths.root, s["paths"])}


def source_status(reg: Registry, cfg: dict, paths: Paths, as_of: str | None) -> list[dict]:
    as_of_dt = parse_ts(as_of)
    as_of_d = as_of_dt.date() if as_of_dt else None
    tip = _rpc_tip(paths)
    out = []
    for sid, s in cfg.get("sources", {}).items():
        files = _glob(paths.root, s["paths"])
        entry: dict[str, Any] = {
            "id": sid, "name": s["name"], "class": s["class"], "endpoint": s["endpoint"], "auth": s["auth"],
            "state": "ok", "first_obs": None, "last_obs": None, "lag_days": None, "fetched_at": None,
            "rows": None, "path": ", ".join(s["paths"]), "sha256": None, "committed": s["committed"],
            "tos_note": s["tos_note"], "fix": s["fix"], "stale_text": s.get("stale_text"),
            "note": None, "files": [],
        }
        if s["class"] == "declared-constant":
            _source_constants(entry, files)
        elif not files:
            gated = bool(s.get("requires_env"))
            entry["state"] = "not_configured" if gated else "failed"
            entry["note"] = (f"not configured: needs {', '.join(s['requires_env'])}" if gated
                             else "no cached file in data/raw")
        elif withheld(s):
            # publish the state only: no file list, hashes, fetch times or row counts until permission exists
            entry["state"] = "withheld"
            entry["note"] = "aggregates exist but publication waits for written permission"
        else:
            file_stale = s.get("file_stale", {})
            details = []
            for p in files:
                meta = sidecar(p)
                rule = dict(file_stale.get(p.stem, s.get("stale", {"rule": "none"})))
                last = _last_obs(p, meta, rule.get("last_obs"))
                lag, over = _judge(rule, last, as_of_d, tip)
                fetched = parse_ts(meta.get("fetched_at"))
                if meta.get("complete") is False or meta.get("error"):
                    state = "failed"
                elif over:
                    recent = fetched is not None and as_of_dt is not None and as_of_dt - fetched <= timedelta(days=1)
                    state = "lagging" if recent else "stale"
                elif over is None:
                    state = "partial"
                else:
                    state = "ok"
                rows = meta.get("rows") if isinstance(meta.get("rows"), int) else None
                first = _first_obs(p, meta)
                details.append({
                    "path": p.relative_to(paths.root).as_posix(), "sha256": sha256_file(p),
                    "fetched_at": meta.get("fetched_at"), "rows": rows,
                    "first_obs": first.isoformat() if first else None,
                    "last_obs": last.isoformat() if last else None, "lag_days": lag,
                    "rule": rule.get("rule", "none"), "state": state,
                })
            entry["files"] = details
            judged = [d for d in details if d["rule"] != "none" and d["last_obs"]]
            entry["state"] = _worst(d["state"] for d in details)
            if judged:
                newest = max(judged, key=lambda d: d["last_obs"])
                entry["last_obs"], entry["lag_days"] = newest["last_obs"], newest["lag_days"]
            firsts = [d["first_obs"] for d in details if d["first_obs"]]
            entry["first_obs"] = min(firsts) if firsts else None
            # the oldest fetch among the source's files: the board shows how old the oldest cache is
            stamps = [(parse_ts(d["fetched_at"]), d["fetched_at"]) for d in details if parse_ts(d["fetched_at"])]
            entry["fetched_at"] = min(stamps)[1] if stamps else None
            row_counts = [d["rows"] for d in details if d["rows"] is not None]
            entry["rows"] = sum(row_counts) if row_counts else None
            if s.get("planned_monthly_from") and as_of_d:
                entry.update(_planned(s, paths, as_of_d))
                if entry["state"] == "ok" and entry["planned_pct"] < s.get("min_planned_pct", 100):
                    entry["state"] = "partial"
            bad = [d["path"] for d in details if d["state"] != "ok"]
            if bad:
                entry["note"] = "not ok: " + ", ".join(bad)
        if files and entry["state"] != "withheld":
            entry["sha256"] = sha256_bytes("".join(
                f"{p.relative_to(paths.root).as_posix()} {sha256_file(p)}\n" for p in files).encode())
        out.append(entry)
    states = [e["state"] for e in out]
    all_files = sorted({f["path"] for e in out for f in e["files"]})
    n_not_configured = states.count("not_configured")
    # the status pill: ok, needs attention (anything else except an unconfigured optional source), not configured
    for key, n in (("n_sources", len(out)), ("n_sources_ok", states.count("ok")),
                   ("n_sources_stale", sum(s in STALE_STATES for s in states)),
                   ("n_sources_missing", sum(s in MISSING_STATES for s in states)),
                   ("n_sources_not_configured", n_not_configured),
                   ("n_sources_attention", len(out) - states.count("ok") - n_not_configured)):
        reg.put(key, n, "count", "F-source-state", inputs=[e["id"] for e in out],
                source_files=[paths.root / f for f in all_files])
    return out


def _planned(s: dict, paths: Paths, as_of: date) -> dict:
    start = to_date(s["planned_monthly_from"])
    planned = []
    d = start
    while d <= as_of:
        planned.append(d)
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    have = {to_date(r.get("date")) for r in read_csv_rows(paths.raw / "rpc" / f"{s['planned_file']}.csv")}
    present = sum(1 for p in planned if p in have)
    pct = 100.0 * present / len(planned) if planned else 0.0
    missing = [p.isoformat()[:7] for p in planned if p not in have]
    return {"planned_samples": len(planned), "present_samples": present, "planned_pct": round(pct, 1),
            "missing_samples": missing}


# =========================================================================== build

@dataclass
class BuildResult:
    paths: Paths
    as_of: str | None
    numbers: dict
    verdicts: dict
    charts: dict
    sources: list
    crosschecks: dict
    tripwires: dict
    manifest: dict
    outputs: list[str] = field(default_factory=list)


def build(paths: Paths, *, models: Iterable[str] = MODEL_ORDER, package: str = MODEL_PACKAGE,
          allow_missing: bool = False) -> BuildResult:
    rules_path = paths.thesis / "rules.toml"
    rules = load_toml(rules_path)
    verdict_model.validate_rules(rules)
    sources_cfg = load_toml(paths.thesis / "sources.toml")
    constants = load_toml(paths.thesis / "constants.toml")

    reg = Registry()
    modules = import_models(models, package, allow_missing)
    charts: list[tuple[str, Chart]] = []
    with no_network():
        for name, module in modules:
            try:
                module.compute(reg)
            except Exception as exc:
                raise BuildError(f"{package}.{name}.compute() raised {type(exc).__name__}: {exc}") from exc
        for name, module in modules:
            try:
                produced = module.charts(reg) or []
            except Exception as exc:
                raise BuildError(f"{package}.{name}.charts() raised {type(exc).__name__}: {exc}") from exc
            charts += [(name, c) for c in produced]
        register_constants(reg, constants, paths.thesis / "constants.toml")

        # strict mode: every key the rules read must be registered (null is fine; absent is a wiring bug)
        unregistered = [k for k in verdict_model.contract_keys(rules) if k not in reg]
        if unregistered and not allow_missing:
            raise BuildError("thesis/rules.toml reads keys no model registered: " + ", ".join(unregistered))
        if unregistered:
            warn("keys read by rules but not registered (verdicts will say Not computed): " + ", ".join(unregistered))

        verdicts, tripwires = verdict_model.run(reg, rules)
        crosschecks = run_crosschecks(reg, rules, paths)
        hidden = withheld_files(sources_cfg, paths)
        inputs = [p for p in raw_files(paths) if p not in hidden]
        as_of = build_as_of(inputs)
        sources = source_status(reg, sources_cfg, paths, as_of)

    # charts -> JSON (validated by chartspec)
    chart_json: dict[str, dict] = {}
    chart_csv: dict[str, str] = {}
    for module_name, chart in charts:
        if chart.id in chart_json:
            raise BuildError(f"chart id {chart.id!r} produced twice (second by {module_name})")
        try:
            chart_json[chart.id] = roundtrip(chart.to_json())
        except ValueError as exc:
            raise BuildError(f"{package}.{module_name}: {exc}") from exc
        chart_csv[chart.id] = chart.to_csv()
    for cid, rule in verdict_model.claims(rules).items():
        for chart_id in rule.get("charts", [rule.get("chart")]):
            if chart_id and chart_id not in chart_json and not allow_missing:
                warn(f"rules.toml {cid} points at chart {chart_id}, which no model produced")

    numbers = reg.to_json()
    rules_sha = sha256_file(rules_path)
    raw_inputs = []
    for p in inputs:
        meta = sidecar(p)
        raw_inputs.append({"path": p.relative_to(paths.root).as_posix(), "sha256": sha256_file(p),
                           "fetched_at": meta.get("fetched_at"),
                           "rows": meta.get("rows") if isinstance(meta.get("rows"), int) else None})
    inputs_sha = sha256_bytes("".join(f"{r['path']} {r['sha256']}\n" for r in raw_inputs).encode()
                              + rules_sha.encode())
    build_info = {
        "as_of": as_of,
        "git_sha": "",  # deliberately empty: a git sha would change every rebuild and break `make verify`
        "build_id": inputs_sha[:12],
        "rules_sha256": rules_sha,
        "inputs_sha256": inputs_sha,
        "constants_sha256": sha256_file(paths.thesis / "constants.toml"),
        "site_url": config.SITE_URL,   # where the static site is published (GitHub Pages); THESIS.md links there
        "repo_url": config.REPO_URL,
    }

    # Everything is written to a staging directory first and copied into place only after every page
    # rendered, so a failed build never leaves site/data out of step with the pages.
    written: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="near-build-") as staging:
        stage = Paths(root=paths.root, out=Path(staging))

        def emit(rel: str, text: str) -> None:
            write_text(stage.out / rel, text)
            written[rel] = sha256_bytes(text.encode("utf-8"))

        payloads = {"numbers": numbers, "verdicts": verdicts, "sources": sources,
                    "crosschecks": crosschecks, "tripwires": tripwires}
        for name, payload in payloads.items():
            emit(f"site/data/{name}.json", dump(payload) + "\n")
        for cid in sorted(chart_json):
            emit(f"site/data/charts/{cid}.json", dump(chart_json[cid]) + "\n")
            emit(f"site/data/csv/{cid}.csv", chart_csv[cid])
        # extra data files a model writes (the desk simulation's site/data/sim.json); none when its inputs are missing
        for name, module in modules:
            extra = getattr(module, "site_files", None)
            try:
                files = extra(reg) if extra else {}
            except Exception as exc:
                raise BuildError(f"{package}.{name}.site_files() raised {type(exc).__name__}: {exc}") from exc
            for rel in sorted(files or {}):
                if not rel.startswith("site/data/") or rel in written:
                    raise BuildError(f"{package}.{name}.site_files(): {rel!r} is outside site/data or written twice")
                emit(rel, files[rel])

        def manifest_now() -> dict:
            return {
                "build": build_info,
                "charts": [{"id": cid, "file": f"site/data/charts/{cid}.json", "csv": f"site/data/csv/{cid}.csv",
                            "status": c.get("status"), "section": c.get("section"), "priority": c.get("priority"),
                            "claims": c.get("claims", [])} for cid, c in sorted(chart_json.items())],
                "raw_inputs": raw_inputs,
                "outputs": [{"path": p, "sha256": h} for p, h in sorted(written.items())],
            }

        # the pages see the manifest of the data files; the final manifest adds the pages' hashes
        write_json(stage.data / "manifest.json", manifest_now())
        data = load_site_data(stage)
        for _template, _mode, outs, text in render_all(data, stage, strict=not allow_missing):
            for out in outs:
                emit(out, text)
        manifest = manifest_now()
        write_json(stage.data / "manifest.json", manifest)

        # commit: site/data is entirely build output (start clean); pages and THESIS.md are overwritten
        if paths.data.exists():
            shutil.rmtree(paths.data)
        for rel in sorted(written) + ["site/data/manifest.json"]:
            (paths.out / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(stage.out / rel, paths.out / rel)

    return BuildResult(paths=paths, as_of=as_of, numbers=numbers, verdicts=verdicts, charts=chart_json,
                       sources=sources, crosschecks=crosschecks, tripwires=tripwires, manifest=manifest,
                       outputs=sorted(written) + ["site/data/manifest.json"])


# =========================================================================== rendering

def load_site_data(paths: Paths) -> dict:
    """Everything the templates see, read back from site/data/ and thesis/ (lint uses this too)."""
    def read(name: str) -> Any:
        p = paths.data / f"{name}.json"
        if not p.exists():
            raise BuildError(f"{p} is missing: run `make charts` first")
        return json.loads(p.read_text())

    charts_dir = paths.data / "charts"
    charts = {p.stem: json.loads(p.read_text()) for p in sorted(charts_dir.glob("*.json"))} if charts_dir.exists() else {}
    rules = load_toml(paths.thesis / "rules.toml")
    manifest = read("manifest")
    numbers = read("numbers")
    return {
        "numbers": numbers,
        "verdicts": read("verdicts"),
        "charts": charts,
        "sources": read("sources"),
        "manifest": manifest,
        "crosschecks": read("crosschecks"),
        "tripwires": read("tripwires"),
        "rules": rules,
        "constants": load_toml(paths.thesis / "constants.toml"),
        "copy": load_toml(paths.thesis / "copy.toml", required=False),
        "open_items": resolve_open_items(load_toml(paths.thesis / "open_items.toml", required=False) or {}, numbers),
        "glossary": (load_toml(paths.thesis / "glossary.toml", required=False) or {}).get("terms", []),
        "build": manifest["build"],
        "film": film_context(numbers, charts),
    }


def resolve_open_items(open_items: dict, numbers: dict) -> dict:
    """An open item with `resolved_if = "<registry key>"` takes `resolved_status` (default "partly resolved") once
    that key is non-null, so its status follows the data rather than a hand edit."""
    items = []
    for item in open_items.get("items", []):
        key = item.get("resolved_if")
        if key:
            if key not in numbers:
                raise BuildError(f"open item {item.get('id')!r}: resolved_if names {key!r}, which is not in the "
                                 f"registry")
            if numbers[key].get("value") is not None:
                item = {**item, "status": item.get("resolved_status", "partly resolved")}
        items.append(item)
    return {**open_items, "items": items}


# =========================================================================== the film's baked geometry
#
# The index page opens with a short animated film. Its pictures are drawn in the HTML at build time, so the page is
# whole without JavaScript and the script only moves what is already there. Everything here is a pure function of
# the registry and the chart files: no clock, no randomness beyond a fixed seed, no fetch.

ORBIT = {"w": 640, "h": 640, "cx": 320.0, "cy": 320.0, "R": 280.0}
ORBIT_STEP = 2          # ring spacing, in percentage points of growth
LABEL_CW, LABEL_LH, LABEL_GAP = 10.0, 20.0, 12.0   # label metrics in viewBox units, sized for the narrowest stage
BODY_R, CORE_R = 8.0, 10.0
SKY = {"w": 1440, "h": 900, "n": 150, "seed": 7}
DESK_KEY = "sim_mean_usd"   # the simulation's headline key: without it the desk scene is not rendered


def _pct(v: float, whole: float) -> str:
    return f"{100.0 * v / whole:.2f}%"


def _r1(v: float) -> str:
    return f"{v:.1f}"


def _box(ax: float, ay: float, w: float, h: float, anchor: str, valign: str) -> tuple[float, float, float, float]:
    x0 = ax if anchor == "start" else ax - w if anchor == "end" else ax - w / 2
    y0 = ay - h / 2 if valign == "middle" else ay - h if valign == "above" else ay
    return (x0, y0, x0 + w, y0 + h)


def _overlap(a: tuple, b: tuple) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _near_far(box: tuple, cx: float, cy: float) -> tuple[float, float]:
    nx, ny = min(max(cx, box[0]), box[2]), min(max(cy, box[1]), box[3])
    far = max(((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 for x in (box[0], box[2]) for y in (box[1], box[3]))
    return ((nx - cx) ** 2 + (ny - cy) ** 2) ** 0.5, far


def _anchor_for(ux: float, uy: float) -> tuple[str, str]:
    return ("start" if ux > 0.3 else "end" if ux < -0.3 else "middle",
            "above" if uy < -0.7 else "below" if uy > 0.7 else "middle")


def _place(cands: list[tuple], n_chars: int, obstacles: dict, lines: int = 1) -> tuple:
    """The first candidate (ax, ay, anchor, valign, leader) whose label box hits nothing; else the one that hits
    least. Obstacles: boxes, discs (x, y, r), rings (r, half width) about the centre, and the frame."""
    w, h = n_chars * LABEL_CW, lines * LABEL_LH
    cx, cy = ORBIT["cx"], ORBIT["cy"]
    best, best_hits = None, None
    for cand in cands:
        box = _box(cand[0], cand[1], w, h, cand[2], cand[3])
        hits = sum(1 for b in obstacles["boxes"] if _overlap(box, b))
        hits += sum(1 for (x, y, r) in obstacles["discs"] if _near_far(box, x, y)[0] < r)
        for r, band in obstacles["rings"]:
            near, far = _near_far(box, cx, cy)
            hits += 1 if near <= r + band and far >= r - band else 0
        if box[0] < 0 or box[1] < 0 or box[2] > ORBIT["w"] or box[3] > ORBIT["h"]:
            hits += 1
        if best_hits is None or hits < best_hits:
            best, best_hits = (cand, box), hits
        if hits == 0:
            break
    cand, box = best
    obstacles["boxes"].append(box)
    return cand


def _label(cand: tuple) -> dict:
    ax, ay, anchor, valign, leader = cand
    shift_x = {"start": "0", "end": "-100%", "middle": "-50%"}[anchor]
    shift_y = {"middle": "-50%", "above": "-100%", "below": "0"}[valign]
    return {"left": _pct(ax, ORBIT["w"]), "top": _pct(ay, ORBIT["h"]), "anchor": anchor, "valign": valign,
            "style": f"left:{_pct(ax, ORBIT['w'])};top:{_pct(ay, ORBIT['h'])};translate:{shift_x} {shift_y}",
            "leader": leader}


def film_sky() -> list[dict]:
    """About a hundred and fifty specks for the stage's sky, from a fixed Park-Miller seed: drawn once, never moved."""
    s = SKY["seed"]
    out = []

    def rnd() -> float:
        nonlocal s
        s = (s * 16807) % 2147483647
        return s / 2147483647

    for _ in range(SKY["n"]):
        x, y, r, o = rnd() * SKY["w"], rnd() * SKY["h"], 0.3 + rnd() * 0.8, 0.1 + rnd() * 0.4
        out.append({"cx": f"{x:.1f}", "cy": f"{y:.1f}", "r": f"{r:.2f}", "o": f"{o:.2f}"})
    return out


def _value(numbers: dict, key: str) -> Any:
    n = numbers.get(key)
    return n.get("value") if isinstance(n, dict) else None


def film_orbit(numbers: dict, c21: dict | None) -> dict | None:
    """The title's picture. One radial axis: radius is growth over twelve months. NEAR's supply growth is a blue ring,
    the US dollar's money growth an orange ring, and each of the most-traded currencies a body on its own spoke, in
    order of its trading rank, clockwise from just past twelve o'clock. A body is hollow when its money grew more
    slowly than NEAR's supply over the same twelve months (the sign of c21's gap column decides, not the pixels).
    None when an input is missing, so the page falls back to the article."""
    near_g, usd_g = _value(numbers, "g_365_at_m2"), _value(numbers, "m2_yoy")
    table = (c21 or {}).get("table") or {}
    cols, rows = table.get("columns") or [], table.get("rows") or []
    need = ("code", "currency", "money_growth_latest_pct", "gap_pp")
    if near_g is None or usd_g is None or not rows or any(c not in cols for c in need):
        return None
    ix = {c: cols.index(c) for c in cols}
    bodies = []
    for row in rows:
        code, g = str(row[ix["code"]]), row[ix["money_growth_latest_pct"]]
        rank = _value(numbers, f"fx_{code.lower()}_rank")
        if not isinstance(g, (int, float)) or rank is None:
            return None
        gap = row[ix["gap_pp"]]
        bodies.append({"code": code, "name": str(row[ix["currency"]]), "g": float(g), "rank": rank,
                       "inside": isinstance(gap, (int, float)) and gap < 0,
                       "key": f"mon_{code.lower()}_gm_latest" if f"mon_{code.lower()}_gm_latest" in numbers else None})
    bodies.sort(key=lambda b: (b["rank"], b["code"]))
    # the second beat (what holding pays) reuses the same radial scale, so the scale covers both sets of values
    y_pay = _value(numbers, "y_lst_365")
    for b in bodies:
        pol = _value(numbers, f"pol_{b['code'].lower()}_latest")
        b["pol"] = float(pol) if isinstance(pol, (int, float)) else None
    pay_vals = [b["pol"] for b in bodies if b["pol"] is not None] + ([y_pay] if isinstance(y_pay, (int, float)) else [])
    top = max(ORBIT_STEP, ORBIT_STEP * -(-max(max(b["g"] for b in bodies), near_g, usd_g, *pay_vals) // ORBIT_STEP))
    cx, cy, R = ORBIT["cx"], ORBIT["cy"], ORBIT["R"]
    k = R / top
    r_near, r_usd = max(near_g, 0.0) * k, max(usd_g, 0.0) * k
    step = 360.0 / len(bodies)
    for i, b in enumerate(bodies):
        deg = -90.0 + step / 2 + i * step
        ux, uy = math.cos(math.radians(deg)), math.sin(math.radians(deg))
        r = (max(usd_g, 0.0) if b["code"] == "USD" else max(b["g"], 0.0)) * k
        b.update(i=i, deg=deg, ux=ux, uy=uy, r=r, x=cx + ux * r, y=cy + uy * r, negative=b["g"] < 0,
                 entity="usd" if b["code"] == "USD" else "eur" if b["code"] == "EUR" else "other")
        b["valued"] = b["code"] == "USD" or b["inside"] or b["negative"]

    grid = [v * k for v in range(ORBIT_STEP, int(top) + ORBIT_STEP, ORBIT_STEP)]
    ticks = [(cx + 4, cy - r - 18, cx + 4 + 3.5 * LABEL_CW, cy - r - 2) for r in grid]
    obstacles = {"boxes": list(ticks), "discs": [(b["x"], b["y"], BODY_R) for b in bodies] + [(cx, cy, CORE_R)],
                 "rings": [(r_near, 3.0), (r_usd, 2.0)]}
    G = LABEL_GAP
    # inside the ring only when it fits there; else just outside it, below, above, then on the diagonals
    near_cands = [(cx, cy + 16, "middle", "below", None), (cx, cy + r_near + G, "middle", "below", None),
                  (cx, cy - r_near - G, "middle", "above", None)]
    for deg in (135.0, 45.0, 225.0, 315.0):
        ux, uy = math.cos(math.radians(deg)), math.sin(math.radians(deg))
        near_cands.append((cx + ux * (r_near + G), cy + uy * (r_near + G), *_anchor_for(ux, uy), None))
    disp = str((numbers["g_365_at_m2"].get("display") or ""))
    fits = lambda n, lines: math.hypot(n * LABEL_CW / 2, 16 + lines * LABEL_LH) < r_near - 4   # noqa: E731
    near_lines = 2 if not fits(len("NEAR supply ") + len(disp), 1) and fits(max(len("NEAR supply"), len(disp)), 2) else 1
    if near_lines == 2:
        # too wide for the ring on one line, narrow enough on two: it stays inside, under the core
        near_label = _place(near_cands[:1], max(len("NEAR supply"), len(disp)), obstacles, lines=2)
    else:
        near_label = _place(near_cands, len("NEAR supply ") + len(disp), obstacles)
    order = sorted(bodies, key=lambda b: (b["code"] != "USD", not b["inside"], b["code"] != "EUR", b["i"]))
    for b in order:
        x, y, ux, uy = b["x"], b["y"], b["ux"], b["uy"]
        disp = (numbers.get(b["key"]) or {}).get("display") or "" if b["key"] else ""
        n_chars = len(b["code"]) + (1 + len(disp) if b["valued"] else 0)
        out_anchor = _anchor_for(ux, uy)
        cands = [(x + ux * G, y + uy * G, *out_anchor, None), (x + G, y, "start", "middle", None),
                 (x - G, y, "end", "middle", None), (x, y - G, "middle", "above", None),
                 (x, y + G, "middle", "below", None)]
        # a body inside a ring hangs its label just outside that ring, on its own spoke, with a hairline leader
        for rr in sorted(r for r in (r_near, r_usd) if r > b["r"]):
            ax, ay = cx + ux * (rr + G), cy + uy * (rr + G)
            lead = {"x1": _r1(x + ux * BODY_R), "y1": _r1(y + uy * BODY_R),
                    "x2": _r1(ax - ux * 4), "y2": _r1(ay - uy * 4)}
            cands.append((ax, ay, *out_anchor, lead))
        if b["inside"] or b["negative"]:
            cands = cands[5:] + cands[:5]
        b["label"] = _label(_place(cands, n_chars, obstacles))

    gap_y = cy - (r_near + r_usd) / 2
    bracket = {"x": _r1(cx - 8), "y1": _r1(cy - r_near), "y2": _r1(cy - r_usd),
               "label": _label((cx - 16, gap_y, "end", "middle", None))}
    ring = lambda r: (f"M{cx - r:.1f} {cy:.1f}a{r:.1f} {r:.1f} 0 1 0 {2 * r:.1f} 0"   # noqa: E731
                      f"a{r:.1f} {r:.1f} 0 1 0 {-2 * r:.1f} 0Z")
    pay = _orbit_pay(numbers, bodies, y_pay, k, grid)
    rank = _orbit_rank(numbers, bodies) if pay else None
    if rank is None:
        for b in bodies:
            b["rank"] = None
    for b in bodies:
        for f in ("x", "y", "r"):
            b[f] = _r1(b[f])
        b["ux"], b["uy"], b["deg"] = f"{b['ux']:.4f}", f"{b['uy']:.4f}", f"{b['deg']:.1f}"
        del b["g"], b["pol"]
    return {
        "w": ORBIT["w"], "h": ORBIT["h"], "cx": _r1(cx), "cy": _r1(cy), "R": _r1(R),
        "top": int(top), "step": ORBIT_STEP, "k": f"{k:.4f}",
        "grid": [_r1(r) for r in grid],
        "r_near": _r1(r_near), "r_usd": _r1(r_usd),
        "wash": ring(r_usd) + ring(r_near),
        "near_label": _label(near_label),
        "near_lines": near_lines,
        "bracket": bracket,
        "bodies": bodies,
        "inside": [b for b in bodies if b["inside"]],
        "faster": sum(1 for b in bodies if not b["inside"]),
        "pay": pay,
        "rank": rank,
    }


def _orbit_pay(numbers: dict, bodies: list[dict], y: Any, k: float, grid: list[float]) -> dict | None:
    """The title's second beat on the same picture and scale: radius becomes what holding pays. The blue ring moves
    to NEAR's liquid-staking yield (y_lst_365), each body to its currency's latest policy rate (pol_<c>_latest; the
    dollar's is fed funds). The reading flips: inside the ring a currency's rate pays less than staking NEAR. Hollow
    keeps one meaning in both beats, against the thesis: film.js draws hollow the bodies outside the ring here (a rate
    that pays more), as it draws hollow the money that grew more slowly in the first beat (the values decide, not the
    pixels). A currency with no policy rate has no radius here and is listed by name, never placed. None when the
    yield or every rate is missing, so the beat is not rendered. Fills b["pay"] on each body (None without a rate);
    "months" names the earliest and latest month keys of the rates drawn."""
    cx, cy = ORBIT["cx"], ORBIT["cy"]
    if not isinstance(y, (int, float)) or not any(b["pol"] is not None for b in bodies):
        for b in bodies:
            b["pay"] = None
        return None
    r_ring = max(y, 0.0) * k
    usd = next((b for b in bodies if b["code"] == "USD" and b["pol"] is not None), None)
    r_usd = max(usd["pol"], 0.0) * k if usd else None
    for b in bodies:
        if b["pol"] is None:
            b["pay"] = None
            continue
        r = max(b["pol"], 0.0) * k
        b["pay"] = {"r": r, "x": cx + b["ux"] * r, "y": cy + b["uy"] * r, "inside": b["pol"] < y,
                    "core": b["pol"] <= 0, "key": f"pol_{b['code'].lower()}_latest"}
    placed = [b for b in bodies if b["pay"]]
    ticks = [(cx + 4, cy - r - 18, cx + 4 + 3.5 * LABEL_CW, cy - r - 2) for r in grid]
    rings = [(r_ring, 3.0)] + ([(r_usd, 2.0)] if r_usd is not None else [])
    obstacles = {"boxes": list(ticks), "discs": [(b["pay"]["x"], b["pay"]["y"], BODY_R) for b in placed]
                 + [(cx, cy, CORE_R)], "rings": rings}
    G = LABEL_GAP
    near_disp = (numbers.get("y_lst_365") or {}).get("display") or ""
    near_label = _place([(cx, cy - r_ring - G, "middle", "above", None), (cx, cy + r_ring + G, "middle", "below", None),
                         (cx, cy + 16, "middle", "below", None)], len("NEAR staking ") + len(near_disp), obstacles)
    # the exceptions to the reading (a rate that pays more than staking) carry their figure and are placed first
    order = sorted(placed, key=lambda b: (b["code"] != "USD", b["pay"]["inside"], b["code"] != "EUR", b["i"]))
    for b in order:
        p = b["pay"]
        x, y0, ux, uy = p["x"], p["y"], b["ux"], b["uy"]
        p["valued"] = b["code"] == "USD" or not p["inside"] or p["core"]
        disp = (numbers.get(p["key"]) or {}).get("display") or ""
        # the dollar's label also says its rate is today's (the copy word film_tp_now), not the year's compounded one
        n_chars = len(b["code"]) + (1 + len(disp) if p["valued"] else 0) + (len(" now") if b["code"] == "USD" else 0)
        out_anchor = _anchor_for(ux, uy)
        cands = [(x + ux * G, y0 + uy * G, *out_anchor, None), (x + G, y0, "start", "middle", None),
                 (x - G, y0, "end", "middle", None), (x, y0 - G, "middle", "above", None),
                 (x, y0 + G, "middle", "below", None)]
        for rr in sorted(r for r, _band in rings if r > p["r"]):
            ax, ay = cx + ux * (rr + G), cy + uy * (rr + G)
            lead = {"x1": _r1(x + ux * BODY_R), "y1": _r1(y0 + uy * BODY_R),
                    "x2": _r1(ax - ux * 4), "y2": _r1(ay - uy * 4)}
            cands.append((ax, ay, *out_anchor, lead))
        if p["r"] < CORE_R + BODY_R:
            # a rate at or near zero sits under NEAR's core: its label stands off along its own spoke, inside the
            # ring, with a hairline back to the centre, so it never reads as the core's own label
            core = []
            for f in (0.5, 0.62, 0.38, 0.75):
                ax, ay = cx + ux * r_ring * f, cy + uy * r_ring * f
                core.append((ax, ay, *out_anchor, {"x1": _r1(cx + ux * (CORE_R + 3)), "y1": _r1(cy + uy * (CORE_R + 3)),
                                                   "x2": _r1(ax - ux * 4), "y2": _r1(ay - uy * 4)}))
            cands = core + cands
        p["label"] = _label(_place(cands, n_chars, obstacles))
    for b in placed:
        p = b["pay"]
        p["r"], p["x"], p["y"] = _r1(p["r"]), _r1(p["x"]), _r1(p["y"])
    return {
        "r_near": _r1(r_ring), "r_usd": _r1(r_usd) if r_usd is not None else None,
        "near_label": _label(near_label),
        "bodies": placed,
        "norate": [b for b in bodies if not b["pay"]],
        "below": [b for b in placed if b["pay"]["inside"]],
        "above": [b for b in placed if not b["pay"]["inside"]],
        "months": _month_span(numbers, [f"pol_{b['code'].lower()}_month" for b in placed]),
    }


def _month_span(numbers: dict, keys: list[str]) -> list[str] | None:
    """The registry keys of the earliest and latest month among keys (one key twice when they agree), so the page
    can say which months a set of figures is from; None when no key has a month."""
    have = sorted((str(v), k) for k in keys if isinstance(v := _value(numbers, k), str) and v)
    if not have:
        return None
    return [have[0][1], have[-1][1] if have[-1][0] != have[0][0] else have[0][1]]


# the axis sits on the orbit's centre line, leaving the zero line's two words room before the tick figures even
# when the stage is scaled down at 720 px high
LADDER = {"ax": 320.0, "y0": 66.0, "y1": 574.0, "row": 26.0, "swarm": 16.0, "lab": 34.0, "zero": 8.0}
RANK_KEYS = ("hold_near_net", "hold_near_rank", "hold_n_ranked")


def _dodge(want: list[float], row: float, lo: float, hi: float) -> list[float]:
    """Label rows for wanted positions in order (top first): each cluster of labels closer than a row sits centred on
    its members' wanted positions, a row apart, inside [lo, hi]. Order is kept, so the column reads as the ranking."""
    clusters = [[i] for i in range(len(want))]
    while True:
        tops = [sum(want[i] for i in c) / len(c) - (len(c) - 1) * row / 2 for c in clusters]
        tops = [min(max(t, lo), hi - (len(c) - 1) * row) for t, c in zip(tops, clusters)]
        merged = next((j for j in range(1, len(clusters)) if tops[j - 1] + len(clusters[j - 1]) * row > tops[j] + 1e-9),
                      None)
        if merged is None:
            return [t + n * row for t, c in zip(tops, clusters) for n in range(len(c))]
        clusters[merged - 1:merged + 1] = [clusters[merged - 1] + clusters[merged]]


def _orbit_rank(numbers: dict, bodies: list[dict]) -> dict | None:
    """The title's third beat: the orbit resolves into a ranked ladder. One value axis, up the page: one year's
    change in a holder's share of the total (F-hold-net), staked NEAR against cash earning each currency's policy
    rate. Zero is a labelled line (above it the share grows, below it shrinks). Each body has its dot at its exact
    value (nudged sideways only where two dots would touch) and its label in one column, rows kept apart and in
    rank order, with a hairline from dot to label. A currency without both figures is not placed and is listed.
    None when NEAR's figure, its rank or every currency's figure is missing, so the beat is not rendered. Fills
    b["rank"] on each body (None when it is not ranked)."""
    L = LADDER
    near = _value(numbers, "hold_near_net")
    if any(_value(numbers, k) is None for k in RANK_KEYS):
        return None
    rows = [(None, float(near))] + [(b, float(v)) for b in bodies
                                    if isinstance(v := _value(numbers, f"hold_{b['code'].lower()}_net"), (int, float))]
    if len(rows) < 2:
        return None
    rows.sort(key=lambda r: (-r[1], r[0] is not None, r[0]["i"] if r[0] else -1))
    vals = [v for _b, v in rows]
    span = max(max(vals), 0.0) - min(min(vals), 0.0)
    step = 1 if span <= 8 else 2 if span <= 16 else 5
    top = max(step, step * math.ceil(max(max(vals), 0.0) / step))
    bot = min(-step, step * math.floor(min(min(vals), 0.0) / step))
    y = lambda v: L["y0"] + (top - v) / (top - bot) * (L["y1"] - L["y0"])   # noqa: E731
    ax, row = L["ax"], L["row"]
    dots: list[tuple[float, float]] = []
    for _b, v in rows:
        yy = y(v)
        dx = next(d for d in (0.0, L["swarm"], -L["swarm"], 2 * L["swarm"], 3 * L["swarm"])
                  if all(abs(px - (ax + d)) >= 2 * BODY_R - 2 or abs(py - yy) >= 2 * BODY_R - 2 for px, py in dots))
        dots.append((ax + dx, yy))
    lab_x = max(px for px, _py in dots) + L["lab"]
    labs = _dodge([py for _px, py in dots], row, L["y0"] - row / 2, L["y1"] + row / 2)
    out, zero = [], y(0.0)
    for (b, v), (px, py), ly in zip(rows, dots, labs):
        place = {"x": _r1(px), "y": _r1(py), "label": _label((lab_x, ly, "start", "middle", None)),
                 "leader": {"x1": _r1(px + BODY_R), "y1": _r1(py), "x2": _r1(lab_x - 5), "y2": _r1(ly)},
                 "key": f"hold_{b['code'].lower()}_net" if b else "hold_near_net"}
        if b:
            b["rank"] = place
        out.append(place)
    ranked = {id(b) for b, _v in rows if b}
    for b in bodies:
        if id(b) not in ranked:
            b["rank"] = None
    ticks = [{"v": v, "y": _r1(y(v))} for v in range(bot, top + step, step)]
    # the zero line is the reading rule: it runs from the left margin, under its two words, across the dot column,
    # and stops short of the label column so it never reads as a leader; the words sit on the margin, away from the
    # dots and from the tick figures by the axis
    rk = [b for b, _v in rows if b]
    return {
        "ax": _r1(ax), "y_top": _r1(y(top)), "y_bot": _r1(y(bot)), "ticks": ticks,
        "zero": {"x1": _r1(L["zero"]), "x2": _r1(lab_x - 8), "y": _r1(zero)},
        "up": _label((L["zero"], zero - 5, "start", "above", None)),
        "down": _label((L["zero"], zero + 5, "start", "below", None)),
        "near": next(p for p, (b, _v) in zip(out, rows) if b is None),
        "bodies": rk,
        "unranked": [b for b in bodies if id(b) not in ranked],
        # the months behind the cash rows: each currency's policy rate, and the year its money growth ends
        "months": _month_span(numbers, [f"pol_{b['code'].lower()}_month" for b in rk]),
        "money_months": _month_span(numbers, [f"mon_{b['code'].lower()}_gm_month" for b in rk]),
    }


# every figure the desk frame prints (d1 to d5, the plot and the tiles); a missing one means no scene at all
DESK_NEEDS = (DESK_KEY, "sim_desk_usd", "sim_horizon_days", "sim_share", "sim_near_usd", "sim_cash_interest_usd",
              "sim_split_usd", "sim_split_eur", "sim_carry_usd", "sim_median_usd", "sim_p_beat_n", "sim_p_beat_r",
              "sim_p_behind_n", "sim_es95_usd", "sim_exit_days", "sim_dots", "sim_tail_n", "sim_paths",
              "sim_carry_euro_usd", "sim_mean_dollar_only_usd", "sim_median_dir", "sim_p05_usd", "sim_p95_usd",
              "sim_tail_hi_n")
PLOT_MARKS = ("p05", "median", "zero", "mean", "p95")


def _share(numbers: dict, key: str) -> float | None:
    """A registry share as a fraction: percent units divide by a hundred, fractions pass through."""
    v = _value(numbers, key)
    if not isinstance(v, (int, float)):
        return None
    return float(v) / 100.0 if (numbers[key].get("unit") or "") in ("%", "pct", "percent") else float(v)


def _desk_plot(c30: dict | None) -> dict | None:
    """The hundred simulated years as baked dots (c30's own geometry, x.layout, which sim.js mirrors): stacks by
    column, left to right, so they can drop in one stack at a time, and the markers (the bad year, the typical year,
    zero, the average, the good year) as lines down to the axis. Their labels sit in rows just under the axis, one
    marker to a row, in an order no marker line crosses: right-hand markers (labels ending at their line) left to
    right, then the rest right to left (sim.js orders them the same way). The rows and the counts above the stacks
    are laid out by sim.css in pixels, so their type never scales with the plot. None without the geometry."""
    lay = (((c30 or {}).get("x") or {}).get("layout")) or {}
    dots, marks = lay.get("dots") or [], dict(lay.get("marks") or {})
    if not dots or not all(k in lay for k in ("w", "h", "base", "zero")):
        return None
    w, base = float(lay["w"]), float(lay["base"])
    marks["zero"] = lay["zero"]
    order = sorted((k for k in PLOT_MARKS if isinstance(marks.get(k), (int, float))), key=lambda k: (marks[k], k))
    flip = w * float(lay.get("flip", 0.55))      # c30's own label flip (portfolio.PLOT), which sim.js reads too
    right = lambda k: marks[k] > flip   # noqa: E731
    rows = sorted(order, key=lambda k: (not right(k), marks[k] if right(k) else -marks[k], k))
    seq = {k: i for i, k in enumerate(k for k in order if k != "zero")}   # the markers arrive left to right
    vh = base + 1.0                                  # down to the axis; the label rows are HTML under it
    cols: dict[float, list] = {}
    for x, y, filled, _k in dots:
        cols.setdefault(float(x), []).append({"cy": _r1(float(y)), "filled": bool(filled)})
    columns = [{"cx": _r1(x), "dots": cols[x]} for x in sorted(cols)]
    return {
        "viewbox": f"0 0 {w:.0f} {vh:.1f}", "w": _r1(w), "base": _r1(base), "zero": _r1(float(lay["zero"])),
        "columns": columns, "ratio": f"{w / vh:.4f}",
        "marks": [{"mark": k, "x": _r1(float(marks[k])), "left": _pct(float(marks[k]), w),
                   "anchor": "end" if right(k) else "start", "seq": seq.get(k, 0)} for k in rows],
    }


def film_desk(numbers: dict, charts: dict | None = None) -> dict | None:
    """The desk scene's frame (shots d1 to d5): the segmented bar of the desk (dollars, euros and the NEAR part, in
    proportion), the two bars of the yield against the swing on one dollar axis, and the hundred simulated years
    baked from c30. None when any figure the frame prints is missing, so the scene and its skip link are not
    rendered at all."""
    if any(_value(numbers, k) is None for k in DESK_NEEDS):
        return None
    other = "sim_day_swing_usd" if _value(numbers, "sim_day_swing_usd") is not None else "sim_es95_usd"
    w = _share(numbers, "sim_share")
    usd, eur = _share(numbers, "sim_split_usd"), _share(numbers, "sim_split_eur")   # the simulation's own split
    carry, vs = _value(numbers, "sim_carry_usd"), _value(numbers, other)
    plot = _desk_plot((charts or {}).get("c30"))
    if w is None or not 0.0 <= w <= 1.0 or not usd or not eur or not isinstance(vs, (int, float)) or plot is None:
        return None
    s = usd / (usd + eur)            # the dollar share of the cash (spec AD-3: trading volume, not holdings)
    top = max(abs(carry), abs(vs)) or 1.0
    # the bar on a 0-100 axis, the NEAR part between the two currencies it is taken from
    u, n = 100.0 * (1 - w) * s, 100.0 * w
    f2 = lambda v: f"{v:.2f}"   # noqa: E731
    return {
        "bar": {"usd": f2(u), "near_x": f2(u), "near": f2(n), "eur_x": f2(u + n), "eur": f2(100.0 - u - n)},
        "edge": {"carry": f2(100.0 * abs(carry) / top), "other": f2(100.0 * abs(vs) / top), "other_key": other},
        "plot": plot,
    }


def film_context(numbers: dict, charts: dict) -> dict | None:
    """Everything the film's markup needs that is geometry rather than a figure: the orbit, the sky, and the desk
    frame when the simulation's keys exist. None (the page renders as the plain article) when the orbit cannot be
    drawn."""
    orbit = film_orbit(numbers, charts.get("c21"))
    if orbit is None:
        return None
    return {"orbit": orbit, "sky": film_sky(), "desk": film_desk(numbers, charts)}


VERDICT_ICONS = {
    verdict_model.SUPPORTED: "✓",
    verdict_model.PARTLY: "◐",
    verdict_model.REFUTED: "✕",
    verdict_model.NOT_TESTABLE: "?",
    verdict_model.NOT_COMPUTED: "–",
}
# the same shapes as the film's glyph() macro in templates/_macros.html.j2, in a 16-unit box
VERDICT_GLYPHS = {
    verdict_model.SUPPORTED: '<path d="M3 8.5l3.2 3.2L13 4.5" fill="none" stroke="currentColor" stroke-width="2"/>',
    verdict_model.PARTLY: '<circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" stroke-width="1.6"/>'
                          '<path d="M8 2.5a5.5 5.5 0 0 0 0 11z" fill="currentColor"/>',
    verdict_model.REFUTED: '<path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="2"/>',
    verdict_model.NOT_TESTABLE: '<circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" stroke-width="1.6" '
                                'stroke-dasharray="1.5 2.2"/>',
    None: '<path d="M3.5 8h9" fill="none" stroke="currentColor" stroke-width="2"/>',
}
FILM_NUM_RE = re.compile(r'(<span class="num(?: null)?" data-key="[^"]*") tabindex="0" title="[^"]*"')
IDENT_RE = re.compile(r"(?=.*[A-Za-z])[A-Za-z0-9_.\-]{1,64}")
MINUS_RE = re.compile(r"^-(?=[0-9$.])")   # a leading sign on a figure, printed as U+2212 in HTML
COND_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(!=|=)\s*(.+?)\s*$")
LINT_MARK = "«{}»"

# lint mode: field names whose values are identifiers (kept, so template logic and lookups still work)
STRUCTURAL = {"id", "key", "keys", "metric", "metrics", "requires", "metric_keys", "margin_key", "chart",
              "charts", "chart_ids", "claims", "verdicts", "section", "status", "state", "verdict", "kind",
              "unit", "counted", "separate", "ids", "class", "committed", "form", "entity", "role", "panel",
              "priority", "rule", "blocks_verify", "unregistered", "type", "scale", "refs",
              "affects", "tripped", "formula_id", "formula_ids"}


def _norm_state(text: str) -> str:
    return re.sub(r"[\s_\-]", "", str(text)).lower()


PLACEHOLDER_RE = re.compile(r"\{[a-z_][a-z0-9_]*(?::(?:abs(?:,\d+)?|\d+))?\}")


def _strip_digits(s: str) -> str:
    """Remove digits, except inside {registry_key} placeholders (model-written templates go through text())."""
    out, last = [], 0
    for m in PLACEHOLDER_RE.finditer(s):
        out += [re.sub(r"\d", "", s[last:m.start()]), m.group(0)]
        last = m.end()
    out.append(re.sub(r"\d", "", s[last:]))
    return "".join(out)


def _mask(obj: Any, keep: Callable[[tuple], bool], path: tuple = (), numbers_too: Callable[[tuple], bool] = lambda p: False) -> Any:
    if keep(path):
        return obj
    if isinstance(obj, dict):
        return {k: (v if k in STRUCTURAL else _mask(v, keep, path + (k,), numbers_too)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask(v, keep, path + ("*",), numbers_too) for v in obj]
    if isinstance(obj, str):
        return _strip_digits(obj)
    if isinstance(obj, (int, float)) and not isinstance(obj, bool) and numbers_too(path):
        return LINT_MARK.format("datum")
    return obj


def lint_context(data: dict) -> dict:
    """The data the templates see in lint mode: digits that come from data files are removed, except the
    registry figures (value/display) and resolved chart titles, which must be printed through num()/text()."""
    def keep_numbers(p):
        return len(p) == 2 and p[1] in ("value", "display")

    def keep_verdicts(p):
        return (len(p) == 4 and p[1] == "values" and p[3] in ("value", "display")) or \
               (len(p) == 2 and p[1] == "margin") or \
               (len(p) == 3 and p[1] == "margin_detail" and p[2] in ("value", "display", "diff"))

    def keep_charts(p):
        return (len(p) == 2 and p[1] in ("title", "sentence", "eyebrow", "status_banner",
                                          "title_template", "sentence_template", "caveat", "caveat_template",
                                          "notes", "notes_templates")) or \
               (len(p) == 4 and p[1] == "values" and p[3] in ("value", "display"))

    def chart_cells(p):
        return (len(p) >= 3 and p[1] == "table" and p[2] == "rows") or (len(p) >= 3 and p[1] == "series")

    def keep_values(p):
        return len(p) == 4 and p[1] == "values" and p[3] in ("value", "display")

    out = dict(data)
    out["numbers"] = _mask(data["numbers"], keep_numbers)
    out["verdicts"] = _mask(data["verdicts"], keep_verdicts)
    out["charts"] = _mask(data["charts"], keep_charts, numbers_too=chart_cells)
    out["tripwires"] = _mask(data["tripwires"], keep_values)
    # open_items is prose source like copy.toml (its text goes through text(), so it is linted as typed)
    for name in ("sources", "manifest", "crosschecks", "rules", "constants", "build"):
        out[name] = _mask(data[name], lambda p: False)
    return out


class Helpers:
    """The template globals of the rendering contract. html: Markup; md: plain text; lint: digit-free markers."""

    def __init__(self, data: dict, mode: str, lint: bool = False, masked: dict | None = None):
        self.data, self.mode, self.lint = data, mode, lint
        self.html = mode == "html"
        self.claims = verdict_model.claims(data["rules"])
        self.trip_cfg = data["rules"].get("tripwires", {})
        self.x_cfg = data["rules"].get("crosschecks", {})
        self.masked = (masked if masked is not None else lint_context(data)) if lint else data

    # -- output plumbing
    def _out(self, html: str, md: str) -> Any:
        return Markup(html) if self.html else md

    def _mark(self, name: str, tag: str = "span", cls: str = "") -> Any:
        mark = LINT_MARK.format(name)
        attr = f' class="{cls}"' if cls else ""
        return self._out(f"<{tag}{attr}>{mark}</{tag}>", mark)

    # -- numbers
    def num(self, key: str, decimals: int | None = None, absolute: bool = False) -> Any:
        """A registry figure in a .num span. absolute=True prints the magnitude (the words carry the sign)."""
        n = self.data["numbers"].get(key)
        if n is None:
            raise BuildError(f"num({key!r}): no such number in site/data/numbers.json")
        if self.lint:
            return self._out(f'<span class="num" data-key="{escape(key)}">{LINT_MARK.format("num")}</span>',
                             LINT_MARK.format("num"))
        if n["value"] is None:
            title = n.get("null_reason") or "not computed"
            return self._out(f'<span class="num null" data-key="{escape(key)}" tabindex="0" '
                             f'title="{escape(title)}">n/a</span>', "n/a")
        if decimals is None and not absolute:
            display = n["display"]
        else:
            display = fmt(n["value"], n["unit"], decimals if decimals is not None else n.get("decimals"),
                          signed=n.get("signed", True), absolute=absolute)
        title = f"{n['formula_id']}: {', '.join(n.get('inputs') or []) or 'no model inputs'}"
        # the page prints a true minus (U+2212), as the charts do; Markdown keeps the ASCII hyphen
        shown = MINUS_RE.sub("\u2212", display)
        return self._out(f'<span class="num" data-key="{escape(key)}" tabindex="0" '
                         f'title="{escape(title)}">{escape(shown)}</span>', display)

    def num_text(self, key: str) -> Any:
        """A registry figure as plain escaped text, for places that take text only (an <option>, a <button> label):
        no span, no tabindex, no title."""
        n = self.data["numbers"].get(key)
        if n is None:
            raise BuildError(f"num_text({key!r}): no such number in site/data/numbers.json")
        if self.lint:
            return self._out(str(escape(LINT_MARK.format("num"))), LINT_MARK.format("num"))
        display = "n/a" if n["value"] is None else n["display"]
        shown = MINUS_RE.sub("\u2212", display)
        return self._out(str(escape(shown)), display)

    def text(self, template: str) -> Any:
        if template is None:
            raise BuildError("text(None): nothing to render")
        try:
            parts = list(string.Formatter().parse(str(template)))
        except ValueError as exc:
            raise BuildError(f"text(): malformed template {template!r}: {exc}") from exc
        html_parts, md_parts = [], []
        for literal, name, spec, _conv in parts:
            html_parts.append(str(escape(literal)))
            md_parts.append(literal)
            if name is None:
                continue
            if name not in self.data["numbers"]:
                raise BuildError(f"text(): placeholder {{{name}}} in {template!r} is not a registered number")
            try:
                decimals, absolute = parse_spec(spec)
            except ValueError as exc:
                raise BuildError(f"text(): {exc} in {template!r}") from exc
            rendered = self.num(name, decimals, absolute)
            html_parts.append(str(rendered))
            md_parts.append(str(rendered))
        return self._out("".join(html_parts), "".join(md_parts))

    def datum(self, value: Any, unit: str | None = None, decimals: int | None = None) -> Any:
        """A data value that is not a registry number (chart table cells): <data value>. USD stays exact."""
        if self.lint or (isinstance(value, str) and value == LINT_MARK.format("datum")):
            return self._out(f"<data>{LINT_MARK.format('datum')}</data>", LINT_MARK.format("datum"))
        if value is None or value == "":
            return self._out('<data class="null">n/a</data>', "n/a")
        if unit and isinstance(value, (int, float)) and not isinstance(value, bool):
            display = fmt(value, unit, decimals, compact=False)
        else:
            display = str(value)
        shown = MINUS_RE.sub("\u2212", display) if unit else display   # a formatted figure gets a true minus
        return self._out(f'<data value="{escape(str(value))}">{escape(shown)}</data>', display)

    # -- verdicts and rules
    def verdict(self, claim_id: str) -> dict:
        source = self.masked["verdicts"]
        if claim_id not in self.claims or claim_id not in source:
            raise BuildError(f"verdict({claim_id!r}): no such claim in thesis/rules.toml")
        return source[claim_id]

    def chip(self, claim_id: str) -> Any:
        v = self.verdict(claim_id)["verdict"]
        if self.lint:
            return self._mark("chip", cls="chip")
        slug = _norm_state(v).replace("not", "not-") if v.startswith("Not") else _norm_state(v)
        icon = VERDICT_ICONS.get(v, "")
        # the page draws the glyph as inline SVG (the page fonts have no check, cross or half disc); Markdown keeps
        # the character
        glyph = (f'<svg viewBox="0 0 16 16" width="1em" height="1em" focusable="false">'
                 f'{VERDICT_GLYPHS.get(v, VERDICT_GLYPHS[None])}</svg>')
        return self._out(
            f'<span class="chip chip-{slug}" data-verdict="{slug}"><span class="chip-icon" aria-hidden="true">'
            f'{glyph}</span> <span class="chip-word">{escape(v)}</span></span>', f"{icon} {v}")

    def film_nums(self, markup: Any) -> Any:
        """The film's markup with its figures out of the tab order: on the stage a figure reads as itself (no key
        name or formula in a tooltip, no focus stop that does nothing); the evidence keeps both."""
        return self._out(FILM_NUM_RE.sub(r"\1", str(markup)), str(markup))

    def rule_text_of(self, ident: str, field: str = "rule") -> str:
        if ident in self.claims:
            r = self.claims[ident]
            if field == "rule":
                return verdict_model.rule_text(r)
            if field in verdict_model.EXPR_FIELDS:
                if field in r:
                    return verdict_model.pretty(r[field])
                if field == "partly":
                    return "otherwise"
                raise BuildError(f"rule({ident!r}, {field!r}): {ident} defines no {field}")
            if field == "binding":
                return self.data["verdicts"][ident].get("binding_condition") or ""
            if isinstance(r.get(field), str):
                return r[field]
            raise BuildError(f"rule({ident!r}, {field!r}): no such text field")
        if ident in self.trip_cfg:
            t = self.trip_cfg[ident]
            if field in ("rule", "expr"):
                return verdict_model.pretty(t["expr"])
            if isinstance(t.get(field), str):
                return t[field]
            raise BuildError(f"rule({ident!r}, {field!r}): no such tripwire field")
        if ident in self.x_cfg:
            x = self.x_cfg[ident]
            if field in ("rule", "tolerance"):
                return x["tolerance_text"]
            if isinstance(x.get(field), str):
                return x[field]
            raise BuildError(f"rule({ident!r}, {field!r}): no such cross-check field")
        constants = self.data["constants"] or {}
        if ident in constants and isinstance(constants[ident], dict):
            c = constants[ident]
            if field in ("rule", "value"):
                unit = c.get("unit", "text")
                if "value" in c:
                    return fmt(c["value"], unit) if unit in NUMERIC_UNITS | TEXT_UNITS else str(c["value"])
                if "lo" in c and "hi" in c:
                    return f"{fmt(c['lo'], unit)}–{fmt(c['hi'], unit)}"
                return str(c.get("quote", ""))
            if isinstance(c.get(field), str):
                return c[field]
            raise BuildError(f"rule({ident!r}, {field!r}): no such constant field")
        if ident == "meta":
            meta = self.data["rules"].get("meta", {})
            if isinstance(meta.get(field), str):
                return meta[field]
        raise BuildError(f"rule({ident!r}): not a claim, tripwire, cross-check or constant")

    def rule(self, ident: str, field: str = "rule") -> Any:
        text = self.rule_text_of(ident, field)  # validates even in lint mode
        if self.lint:
            return self._mark("rule", cls="rule")
        return self._out(f'<span class="rule">{escape(text)}</span>', text)

    def claimed(self, claim_id: str) -> Any:
        if claim_id in ("hypothesis", "thesis"):
            text = self.data["rules"].get("meta", {}).get(claim_id, "")
        else:
            text = self.claims.get(claim_id, {}).get("claimed", "")
        if not text:
            raise BuildError(f"claimed({claim_id!r}): no verbatim hypothesis fragment for this id")
        if self.lint:
            return self._mark("claimed", tag="q", cls="claimed")
        return self._out(f'<q class="claimed">{escape(text)}</q>', f"“{text}”")

    def cite(self, text: Any, href: str | None = None) -> Any:
        if self.lint:
            return self._mark("cite", tag="cite")
        t = str(text)
        if href:
            # only web links: a javascript: or data: URL in a data file must fail the build, not render
            if urlsplit(str(href)).scheme.lower() not in ("http", "https") or any(c.isspace() for c in str(href)):
                raise BuildError(f"cite(): href {href!r} is not a plain http(s) URL")
            return self._out(f'<cite><a href="{escape(href)}" rel="noopener">{escape(t)}</a></cite>',
                             f"[{t}]({href})")
        return self._out(f"<cite>{escape(t)}</cite>", t)

    def tm(self, iso: Any) -> Any:
        if self.lint:
            return self._mark("time", tag="time")
        if not iso:
            return self._out('<span class="time null">n/a</span>', "n/a")
        s = str(iso)
        display = s
        ts = parse_ts(s) if "T" in s else None
        if ts is not None:
            display = ts.strftime("%Y-%m-%d %H:%M UTC") if ts.utcoffset() in (None, timedelta(0)) else s
        return self._out(f'<time datetime="{escape(s)}">{escape(display)}</time>', display)

    def ident(self, text: Any, link: str | None = None) -> Any:
        """An identifier; with `link`, a link to link + id (THESIS.md links ids to the published page)."""
        t = str(text)
        if self.lint:  # validated in the real render; lint-mode data has its digits removed
            return self._mark("id", cls="id")
        if not IDENT_RE.fullmatch(t):
            raise BuildError(f"ident({t!r}): takes an identifier such as H1, O3, c05 or a registry key")
        if link:
            href = f"{link}{t}"
            if urlsplit(href).scheme.lower() not in ("http", "https", "") or any(c.isspace() for c in href):
                raise BuildError(f"ident(): link {href!r} is not a plain http(s) or relative URL")
            return self._out(f'<a class="id" href="{escape(href)}">{escape(t)}</a>', f"[{t}]({href})")
        return self._out(f'<span class="id">{escape(t)}</span>', t)

    # -- verdict-keyed copy
    def _state_of(self, subject: str) -> str:
        if subject in self.claims:
            return self.data["verdicts"][subject]["verdict"]
        if subject in self.data["tripwires"]:
            return self.data["tripwires"][subject]["state"]
        if subject in self.data["crosschecks"]:
            return self.data["crosschecks"][subject]["status"]
        for s in self.data["sources"]:
            if s["id"] == subject:
                return s["state"]
        if subject in self.data["charts"]:
            return self.data["charts"][subject].get("status", "")
        # a registry text key (a method word, a claim id chosen by the data): its value, or "n/a" when null
        n = self.data["numbers"].get(subject)
        if isinstance(n, dict) and n.get("unit") == "text":
            return "n/a" if n.get("value") is None else str(n["value"])
        raise BuildError(f"copy condition names {subject!r}, which is not a claim, tripwire, cross-check, "
                         f"source, chart id or registry text key")

    def condition(self, cond: str) -> bool:
        m = COND_RE.match(str(cond))
        if not m:
            raise BuildError(f"copy condition {cond!r} must look like 'H1=Refuted' or 'H3!=Supported'")
        subject, op, wanted = m.groups()
        state = _norm_state(self._state_of(subject))
        hit = state in {_norm_state(w) for w in wanted.split("|")}
        return hit if op == "=" else not hit

    def select_copy(self, name: str) -> dict:
        cfg = self.data.get("copy")
        if cfg is None:
            raise BuildError("thesis/copy.toml is missing; copy() cannot choose a variant")
        entry = cfg.get(name)
        if not isinstance(entry, dict) or not isinstance(entry.get("variants"), list):
            raise BuildError(f"copy({name!r}): thesis/copy.toml has no [{name}] table with variants")
        for variant in entry["variants"]:
            if all(self.condition(c) for c in variant.get("when", [])):
                return variant
        states = {cid: self.data["verdicts"][cid]["verdict"] for cid in self.claims}
        raise BuildError(f"copy({name!r}): no variant matches the verdicts {states} and there is no "
                         f"fallback (when = []); the build fails closed")

    def copy(self, name: str) -> Any:
        return self.text(self.select_copy(name)["text"])

    def copy_chart(self, name: str, default: str) -> str:
        """The chart the chosen copy variant points at (its optional `chart` field), else `default`; the
        strongest and weakest callouts link the chart their sentence is about."""
        chart = self.select_copy(name).get("chart") or default
        if chart not in self.data["charts"]:
            raise BuildError(f"copy({name!r}) points at chart {chart!r}, which is not among the charts")
        return chart

    def trip_now(self, tid: str) -> Any:
        """A tripwire's 'Now' cell: its now_text template (every input it reads) when the rule has one, else the
        metric's figure."""
        t = self.trip_cfg.get(tid)
        if t is None:
            raise BuildError(f"trip_now({tid!r}): no such tripwire in thesis/rules.toml")
        if t.get("now_text"):
            return self.text(t["now_text"])
        return self.num(t["metric"])

    def globals(self) -> dict:
        return {"num": self.num, "text": self.text, "datum": self.datum, "chip": self.chip,
                "verdict": self.verdict, "rule": self.rule, "claimed": self.claimed, "cite": self.cite,
                "tm": self.tm, "ident": self.ident, "copy": self.copy, "trip_now": self.trip_now,
                "copy_chart": self.copy_chart, "num_text": self.num_text, "film_nums": self.film_nums}


def template_context(data: dict, ctx: dict) -> dict:
    """The template variables of the rendering contract. ctx is data itself, or its lint-mode mask."""
    return {
        "numbers": ctx["numbers"], "verdicts": ctx["verdicts"], "charts": ctx["charts"],
        "sources": ctx["sources"], "manifest": ctx["manifest"], "rules": ctx["rules"],
        "constants": ctx["constants"], "open_items": ctx["open_items"], "tripwires": ctx["tripwires"],
        "glossary": ctx.get("glossary", []),
        "crosschecks": ctx["crosschecks"], "build": ctx["build"],
        "claim_order": list(verdict_model.claims(data["rules"])),
        # the animated page's baked geometry (film_context); None when its inputs are absent, and then the
        # index renders as the plain article
        "film": data.get("film"),
    }


def _finalize(value: Any) -> Any:
    """Every {{ expression }} passes here: a None, NaN or infinity must never be printed as text."""
    if value is None:
        raise BuildError("a template expression printed None (guard it, or use raw()/num(), which show n/a)")
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise BuildError(f"a template expression printed {value!r}")
    return value


def make_env(paths: Paths, mode: str) -> jinja2.Environment:
    return jinja2.Environment(
        finalize=_finalize,
        loader=jinja2.FileSystemLoader(str(paths.templates)),
        autoescape=(mode == "html"),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )


def _template_location(exc: BaseException, template: str) -> str:
    """templates/<file>:<line> of the innermost template frame (Jinja rewrites tracebacks to template lines), and
    the page template's own line when the innermost frame is a shared partial (a macro)."""
    where, outer = None, None
    if getattr(exc, "lineno", None):
        where = f"{Path(getattr(exc, 'filename', None) or template).name}:{exc.lineno}"
    tb = exc.__traceback__
    while tb is not None:
        name = tb.tb_frame.f_code.co_filename
        if name.endswith(".j2"):
            where = f"{Path(name).name}:{tb.tb_lineno}"
            if Path(name).name == template:
                outer = where
        tb = tb.tb_next
    if outer and where and outer != where:
        return f"templates/{where}, called from templates/{outer}"
    return f"templates/{where or template}"


def render_all(data: dict, paths: Paths, *, lint: bool = False, strict: bool = True) -> list[tuple[str, str, tuple, str]]:
    """Render every page template: [(template, mode, outputs, text)]. A missing template fails in strict mode."""
    rendered = []
    ctx = lint_context(data) if lint else data
    for template, mode, outs in TEMPLATES:
        if not (paths.templates / template).exists():
            if strict:
                raise BuildError(f"templates/{template} is missing (the frontend templates write it)")
            warn(f"templates/{template} is missing; {', '.join(outs)} not rendered")
            continue
        env = make_env(paths, mode)
        helpers = Helpers(data, mode, lint=lint, masked=ctx)
        env.globals.update(helpers.globals())
        try:
            text = env.get_template(template).render(**template_context(data, ctx))
        except BuildError as exc:
            raise BuildError(f"{exc} [{_template_location(exc, template)}]") from None
        except jinja2.TemplateError as exc:
            raise BuildError(f"rendering {_template_location(exc, template)}: {type(exc).__name__}: {exc}") from exc
        rendered.append((template, mode, outs, text))
    return rendered


# =========================================================================== verify

def _diff_trees(expected_root: Path, actual_root: Path, rel_paths: Iterable[str]) -> list[str]:
    problems = []
    for rel in rel_paths:
        e, a = expected_root / rel, actual_root / rel
        if e.is_dir() or a.is_dir():
            # hidden files (.DS_Store and the like) are never build output: they neither pass nor fail verify
            def listing(d: Path) -> set[str]:
                return {p.relative_to(d).as_posix() for p in d.rglob("*")
                        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(d).parts)}
            e_files = listing(e) if e.exists() else set()
            a_files = listing(a) if a.exists() else set()
            for f in sorted(e_files - a_files):
                problems.append(f"{rel}/{f}: committed but not produced by the rebuild")
            for f in sorted(a_files - e_files):
                problems.append(f"{rel}/{f}: produced by the rebuild but not committed (run make charts)")
            problems += _diff_trees(expected_root, actual_root, [f"{rel}/{f}" for f in sorted(e_files & a_files)])
            continue
        if not e.exists() or not a.exists():
            problems.append(f"{rel}: {'missing from the repo' if not e.exists() else 'not produced by the rebuild'}")
            continue
        eb, ab = e.read_bytes(), a.read_bytes()
        if eb != ab:
            diff = list(difflib.unified_diff(eb.decode("utf-8", "replace").splitlines(),
                                             ab.decode("utf-8", "replace").splitlines(),
                                             f"committed/{rel}", f"rebuild/{rel}", lineterm="", n=1))
            snippet = "\n".join(line[:200] for line in diff[:12])
            problems.append(f"{rel}: differs from the offline rebuild\n{snippet}")
    return problems


def blocking_failures(crosschecks: dict) -> list[str]:
    """Cross-checks marked blocks_verify (X1, X3) that failed. "not run" does not block: it is shown as such."""
    return [f"cross-check {xid} failed ({x.get('title')}; {x.get('tolerance_text')})"
            for xid, x in sorted(crosschecks.items()) if x.get("blocks_verify") and x.get("status") == "fail"]


def blocking_not_run(crosschecks: dict) -> list[str]:
    """Blocking cross-checks that did not run: verify passes but says so, so a pass is never read as X1 passing."""
    return [f"cross-check {xid} not run ({x.get('title')}): verify cannot vouch for it"
            for xid, x in sorted(crosschecks.items()) if x.get("blocks_verify") and x.get("status") != "pass"
            and x.get("status") != "fail"]


def _input_drift(committed_root: Path, rebuilt_root: Path) -> list[str]:
    """Raw files whose sha differs between the committed manifest and the rebuild's: a byte diff in that case
    means data/raw changed after the last `make charts` (or is changing now), not that the build is unstable."""
    def shas(root: Path) -> dict[str, str]:
        path = root / "site" / "data" / "manifest.json"
        try:
            return {r["path"]: r["sha256"] for r in json.loads(path.read_text()).get("raw_inputs", [])}
        except (OSError, ValueError, KeyError, TypeError):
            return {}
    old, new = shas(committed_root), shas(rebuilt_root)
    return sorted(p for p in set(old) | set(new) if old.get(p) != new.get(p))


def verify(root: Path) -> int:
    """Rebuild from data/raw only in a temp copy, diff against the committed site/ and THESIS.md, run lint,
    and fail if a blocking cross-check (X1, X3) fails. `make verify` then runs pytest."""
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="near-verify-") as tmpdir:
        tmp = Path(tmpdir)
        for item in VERIFY_COPY:
            src = root / item
            if src.is_dir():
                shutil.copytree(src, tmp / item, ignore=VERIFY_IGNORE)
            elif src.is_file():
                (tmp / item).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, tmp / item)
        env = {k: v for k, v in os.environ.items()
               if k not in ("GCP_PROJECT", "NEAR_INTENTS_JWT", "GOOGLE_APPLICATION_CREDENTIALS")}
        env["PYTHONPATH"] = str(tmp)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        print(f"verify: rebuilding offline in {tmp} ...")
        # data/raw was copied once above, so both rebuilds read one snapshot even if a pull is writing now;
        # the second rebuild uses another hash seed, so set or dict ordering cannot hide in the output
        env["PYTHONHASHSEED"] = "1"
        proc = subprocess.run([sys.executable, "-m", "pipeline.build"], cwd=tmp, env=env,
                              capture_output=True, text=True)
        second = tmp / ".seed-check"
        proc2 = None
        if proc.returncode == 0:
            proc2 = subprocess.run([sys.executable, "-m", "pipeline.build", "--out-dir", str(second)], cwd=tmp,
                                   env={**env, "PYTHONHASHSEED": "2"}, capture_output=True, text=True)
        if proc.returncode != 0:
            problems.append("offline rebuild failed:\n" + (proc.stderr or proc.stdout)[-4000:])
        elif proc2 is not None and proc2.returncode != 0:
            problems.append("second offline rebuild (PYTHONHASHSEED=2) failed:\n" + (proc2.stderr or proc2.stdout)[-4000:])
        else:
            produced = [p.relative_to(tmp).as_posix() for p in (tmp / "site").iterdir()] if (tmp / "site").exists() else []
            committed = [p.relative_to(root).as_posix() for p in (root / "site").iterdir()] if (root / "site").exists() else []
            drift = _input_drift(root, tmp)
            if drift:
                problems.append("data/raw differs from the inputs of the committed build (run make charts, or wait "
                                "for a running pull to finish): " + ", ".join(drift[:20])
                                + (f" and {len(drift) - 20} more" if len(drift) > 20 else ""))
            problems += _diff_trees(root, tmp, sorted(set(produced) | set(committed) | {"THESIS.md"}))
            # site/assets is hand-written and copied, not built: compare what the second build wrote
            built = sorted(f"site/{p.name}" for p in (second / "site").iterdir()) if (second / "site").exists() else []
            seed_diff = _diff_trees(tmp, second, built + ["THESIS.md"])
            problems += [f"hash-seed instability: {p}" for p in seed_diff]
            lint = subprocess.run([sys.executable, "-m", "pipeline.lint", "--root", str(tmp)], cwd=tmp, env=env,
                                  capture_output=True, text=True)
            if lint.returncode != 0:
                problems.append("lint failed:\n" + (lint.stdout + lint.stderr)[-6000:])
            xpath = tmp / "site" / "data" / "crosschecks.json"
            if xpath.exists():
                xchecks = json.loads(xpath.read_text())
                problems += blocking_failures(xchecks)
                for w in blocking_not_run(xchecks):
                    print(f"verify: warning: {w}")
    if problems:
        print(f"verify: FAILED ({len(problems)} problem(s))")
        for p in problems:
            print(" - " + p.replace("\n", "\n   "))
        return 1
    print("verify: OK (byte-identical offline rebuild, lint clean, blocking cross-checks pass)")
    return 0


# =========================================================================== change report

REPORTED = (("verdict", "verdicts", "verdict"), ("tripwire", "tripwires", "state"),
            ("cross-check", "crosschecks", "status"))


def previous_site_data(paths: Paths) -> dict | None:
    """The verdicts, tripwires and cross-checks of the site/data a build is about to replace, or None when there is
    no previous site/data (read before the build overwrites it)."""
    names = [name for _, name, _ in REPORTED]
    found = {n: paths.data / f"{n}.json" for n in names}
    if not any(f.exists() for f in found.values()):
        return None
    out = {}
    for n, f in found.items():
        try:
            out[n] = json.loads(f.read_text()) if f.exists() else {}
        except (OSError, ValueError):
            out[n] = {}
    return out


def change_report(prev: dict | None, result: BuildResult) -> list[str]:
    """Every claim verdict, tripwire state and cross-check status that changed against the previous site/data (id:
    old → new) and every new or removed id; ["none"] or ["no previous build"]."""
    if prev is None:
        return ["no previous build"]

    def states(data: object, fld: str) -> dict[str, str]:
        return {k: str(v.get(fld)) for k, v in (data or {}).items() if isinstance(v, dict) and fld in v} \
            if isinstance(data, dict) else {}
    lines = []
    for label, name, fld in REPORTED:
        a, b = states(prev.get(name), fld), states(getattr(result, name), fld)
        for k in sorted(set(a) | set(b)):
            if k not in a:
                lines.append(f"{label} {k}: new ({b[k]})")
            elif k not in b:
                lines.append(f"{label} {k}: removed (was {a[k]})")
            elif a[k] != b[k]:
                lines.append(f"{label} {k}: {a[k]} → {b[k]}")
    return lines or ["none"]


# =========================================================================== CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--allow-missing-models", action="store_true",
                        help="partial build: skip model modules that are absent")
    parser.add_argument("--out-dir", type=Path, help="write site/ and THESIS.md under this directory")
    parser.add_argument("--verify", action="store_true", help="offline rebuild in a temp dir and diff (make verify)")
    args = parser.parse_args(argv)
    root = config.ROOT
    if args.verify:
        return verify(root)
    paths = Paths(root=root, out=(args.out_dir.resolve() if args.out_dir else root))
    prev = previous_site_data(paths)
    try:
        result = build(paths, allow_missing=args.allow_missing_models)
    except (BuildError, verdict_model.RuleError) as exc:
        if exc.__cause__ is not None and not isinstance(exc.__cause__, (BuildError, jinja2.TemplateError)):
            traceback.print_exception(exc.__cause__, file=sys.stderr)
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    tally = result.verdicts["tally"]
    states = {}
    for s in result.sources:
        states[s["state"]] = states.get(s["state"], 0) + 1
    print(f"build: as_of {result.as_of} · {len(result.numbers)} numbers · {len(result.charts)} charts · "
          f"claims: {tally['supported']} supported, {tally['partly']} partly, {tally['refuted']} refuted, "
          f"{tally['not_testable']} not testable, {tally['not_computed']} not computed")
    print("cross-checks: " + ", ".join(f"{k} {v['status']}" for k, v in result.crosschecks.items()))
    print("sources: " + ", ".join(f"{n} {s}" for s, n in sorted(states.items())))
    print(f"wrote {len(result.outputs)} files under {paths.out}")
    print("Changes against the previous site/data:")
    for line in change_report(prev, result):
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
