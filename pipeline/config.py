"""Shared paths, environment loading and raw-data I/O for every pipeline stage.

Contract: every source module writes its output under data/raw/<source>/ as
<name>.csv (or .json) plus a sidecar <name>.meta.json describing exactly how the
file was produced (URL or SQL, fetch time, row count). The build step reads only
data/raw/ and writes site/data/, so charts can always be rebuilt offline.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "sql"
RAW_DIR = ROOT / "data" / "raw"
SITE_DIR = ROOT / "site"
SITE_DATA_DIR = SITE_DIR / "data"

load_dotenv(ROOT / ".env")

REPO_URL = "https://github.com/gabehamilton/invest-in-near"
# the published static site (GitHub Pages, .github/workflows/pages.yml deploys site/ on every push to main)
SITE_URL = "https://gabehamilton.github.io/invest-in-near/"
USER_AGENT = "invest-in-near-thesis/0.1 (+https://github.com/gabehamilton/invest-in-near)"


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value if value else default


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def raw_path(source: str, name: str, ext: str = "csv") -> Path:
    path = RAW_DIR / source / f"{name}.{ext}"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_meta(data_path: Path, **meta: Any) -> None:
    """Write the provenance sidecar for a raw file. Never put secrets in meta."""
    meta.setdefault("fetched_at", utc_now_iso())
    meta.setdefault("file", data_path.relative_to(ROOT).as_posix())
    sidecar = data_path.with_suffix(".meta.json")
    tmp = sidecar.with_name(sidecar.name + ".tmp")
    tmp.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, sidecar)


def write_csv(source: str, name: str, rows: Iterable[dict], fieldnames: list[str], **meta: Any) -> Path:
    """Write the CSV to a temporary file and move it into place, so an interrupted run never leaves half a file."""
    path = raw_path(source, name, "csv")
    rows = list(rows)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)
    write_meta(path, rows=len(rows), columns=fieldnames, **meta)
    return path


def write_json(source: str, name: str, payload: Any, **meta: Any) -> Path:
    path = raw_path(source, name, "json")
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    write_meta(path, **meta)
    return path


def read_csv(source: str, name: str) -> list[dict]:
    path = RAW_DIR / source / f"{name}.csv"
    if not path.exists():
        return []
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def read_meta(source: str, name: str, ext: str = "csv") -> dict:
    path = (RAW_DIR / source / f"{name}.{ext}").with_suffix(".meta.json")
    return json.loads(path.read_text()) if path.exists() else {}


_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT

RETRY_AFTER_CAP = 120.0   # seconds; a longer Retry-After is waited out only this long before the next attempt


def retry_after(headers, fallback: float, cap: float = RETRY_AFTER_CAP) -> float:
    """Seconds to wait before retrying: the Retry-After header in either form (delta-seconds or an HTTP-date), else
    `fallback`; never negative and never more than `cap`."""
    raw = (headers or {}).get("Retry-After")
    wait = fallback
    if raw is not None:
        text = str(raw).strip()
        try:
            wait = float(text)
        except ValueError:
            try:
                when = parsedate_to_datetime(text)
            except (TypeError, ValueError, IndexError):
                when = None
            if when is not None:
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                wait = (when - datetime.now(timezone.utc)).total_seconds()
    return min(max(wait, 0.0), cap)


def http_get(url: str, *, params: dict | None = None, headers: dict | None = None,
             retries: int = 4, backoff: float = 2.0, timeout: float = 60.0) -> requests.Response:
    """GET with retry on 429/5xx. Raises for other HTTP errors."""
    for attempt in range(retries + 1):
        resp = _session.get(url, params=params, headers=headers, timeout=timeout)
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            time.sleep(retry_after(resp.headers, backoff * (2 ** attempt)))
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError(f"unreachable: {url}")


def http_post_json(url: str, payload: dict, *, retries: int = 4, backoff: float = 2.0,
                   timeout: float = 60.0) -> dict:
    for attempt in range(retries + 1):
        resp = _session.post(url, json=payload, timeout=timeout)
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            time.sleep(retry_after(resp.headers, backoff * (2 ** attempt)))
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"unreachable: {url}")
