"""The number registry: the only way a number reaches the site or THESIS.md.

Every model registers each figure it computes with its unit, the formula that
produced it and the raw files it read. Templates print numbers only by key, so a
figure without provenance cannot be rendered, and a lint (pipeline.lint) fails the
build if a digit appears anywhere else.

Units and value conventions (models must follow these):
    "%"     a rate, stored in percent: 2.48 means 2.48 %
    "pp"    a difference of two rates, in percentage points, rendered signed unless registered
            with signed=False (a dispersion or a distance, which has no direction)
    "NEAR", "USD", "count", "days", "hours", "blocks", "seconds"   plain magnitudes ("1 day" singular,
            seconds as " s"); USD of a million or more
            renders compactly ("$3.73 billion") in prose, exactly in tables (fmt(..., compact=False))
    "x"     a multiple (2.3 means 2.3x)
    "ratio" a unitless fraction 0..1 (rendered as-is, e.g. correlations)
    "date"  ISO date string YYYY-MM-DD;  "month" YYYY-MM;  "text" a non-numeric label
A value of None means "not computed"; null_reason must say why.

Placeholder format specs ({key:spec} in templates, read by fill() and the build's text()):
    {key:2}      the display with 2 decimals
    {key:abs}    the magnitude, unsigned ("1.04 pp" for -1.04): pair it with a direction word chosen in the
                 model (e.g. a text key gap_cpi_dir = "slower"/"faster"), so prose never prints a sign
    {key:abs,2}  both
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from pipeline.config import RAW_DIR, ROOT

NUMERIC_UNITS = {"%", "pp", "NEAR", "USD", "count", "days", "hours", "blocks", "seconds", "x", "ratio"}
TEXT_UNITS = {"date", "month", "text"}


@dataclass
class Number:
    key: str
    value: float | int | str | None
    unit: str
    formula_id: str
    display: str
    inputs: list[str] = field(default_factory=list)
    source_files: list[str] = field(default_factory=list)
    as_of: str | None = None
    status: str = "ok"
    null_reason: str | None = None
    signed: bool = True
    decimals: int | None = None  # the registered display's decimals (reused by {key:abs})


def _round_sig(value: float, unit: str) -> float:
    if unit in ("%", "pp", "x"):
        return round(value, 4)
    if unit == "ratio":
        return round(value, 4)
    if unit in ("NEAR", "USD"):
        return round(value, 2)
    return value


COMPACT_USD = ((1e12, "trillion"), (1e9, "billion"), (1e6, "million"))
SPEC_RE = re.compile(r"(?:(abs)(?:,(\d+))?|(\d+))?")


def parse_spec(spec: str | None) -> tuple[int | None, bool]:
    """(decimals, absolute) of a placeholder format spec: "", "2", "abs" or "abs,2"."""
    m = SPEC_RE.fullmatch(spec or "")
    if m is None:
        raise ValueError(f"unknown placeholder format spec {spec!r} (use N, abs or abs,N)")
    digits = m.group(2) or m.group(3)
    return (int(digits) if digits else None), bool(m.group(1))


def _usd(v: float, decimals: int | None, compact: bool) -> str:
    sign = "-" if v < 0 else ""
    a = abs(v)
    if compact:
        for scale, word in COMPACT_USD:
            if a >= scale:
                m = a / scale
                dp = decimals if decimals is not None else (2 if m < 10 else 1 if m < 100 else 0)
                return f"{sign}${m:,.{dp}f} {word}"
    dp = decimals if decimals is not None else 0
    return f"{sign}${a:,.{dp}f}"


def fmt(value: Any, unit: str, decimals: int | None = None, *, signed: bool = True,
        absolute: bool = False, compact: bool = True) -> str:
    """Human display of a value. Rates: 2 dp below 10, 1 dp at or above 10; pp signed unless signed=False.

    absolute=True prints the magnitude (no sign: the sentence carries the direction in words).
    compact=False keeps USD exact ("$3,727,751,510") for tables; prose shows "$3.73 billion".
    """
    if value is None:
        return "n/a"
    if unit in TEXT_UNITS:
        return str(value)
    v = float(value)
    if absolute:
        v, signed = abs(v), False
    if unit in ("%", "pp", "x"):
        dp = decimals if decimals is not None else (2 if abs(v) < 10 else 1)
        text = f"{v:,.{dp}f}"
        if text.lstrip("-").strip("0.,") == "":
            text = text.lstrip("-")  # no "-0.00"
        if unit == "pp" and signed and v > 0 and text.strip("0.,"):
            text = "+" + text
        return text + ("%" if unit == "%" else " pp" if unit == "pp" else "×")
    if unit == "ratio":
        dp = decimals if decimals is not None else 2
        return f"{v:.{dp}f}"
    if unit == "USD":
        return _usd(v, decimals, compact)
    if unit == "NEAR":
        dp = decimals if decimals is not None else 0
        return f"{v:,.{dp}f} NEAR"
    if unit in ("count", "days", "hours", "blocks", "seconds"):
        dp = decimals if decimals is not None else (0 if float(v).is_integer() else 1)
        text = f"{v:,.{dp}f}"
        if unit == "days" and text == "1":
            return "1 day"
        suffix = {"count": "", "days": " days", "hours": " h", "blocks": " blocks", "seconds": " s"}[unit]
        return text + suffix
    raise ValueError(f"unknown unit {unit!r}")


def _relative(path: str | Path) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = RAW_DIR / p if not str(p).startswith("data/") else ROOT / p
    return p.resolve().relative_to(ROOT).as_posix()


def _as_of(source_files: Iterable[str]) -> str | None:
    """Latest fetched_at among the raw files' sidecars (never the wall clock)."""
    stamps = []
    for rel in source_files:
        sidecar = (ROOT / rel).with_suffix(".meta.json")
        if sidecar.exists():
            stamp = json.loads(sidecar.read_text()).get("fetched_at")
            if stamp:
                stamps.append(stamp)
    return max(stamps) if stamps else None


class Registry:
    """Collects Numbers; serialized to site/data/numbers.json by the build."""

    def __init__(self) -> None:
        self._numbers: dict[str, Number] = {}

    def put(self, key: str, value: Any, unit: str, formula_id: str, *,
            source_files: Iterable[str] = (), inputs: Iterable[str] = (),
            null_reason: str | None = None, decimals: int | None = None, signed: bool = True) -> Number:
        if key in self._numbers:
            raise KeyError(f"number {key!r} registered twice")
        if unit not in NUMERIC_UNITS | TEXT_UNITS:
            raise ValueError(f"{key}: unknown unit {unit!r}")
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            value = None
            null_reason = null_reason or "computation produced NaN/inf"
        if value is None and not null_reason:
            raise ValueError(f"{key}: a null number needs null_reason")
        if value is not None and unit in NUMERIC_UNITS:
            value = _round_sig(float(value), unit)
        files = sorted({_relative(f) for f in source_files})
        number = Number(
            key=key, value=value, unit=unit, formula_id=formula_id,
            display=fmt(value, unit, decimals, signed=signed), inputs=sorted(set(inputs)),
            source_files=files, as_of=_as_of(files),
            status="ok" if value is not None else "null", null_reason=null_reason, signed=signed,
            decimals=decimals,
        )
        self._numbers[key] = number
        return number

    def __contains__(self, key: str) -> bool:
        return key in self._numbers

    def get(self, key: str) -> Number:
        return self._numbers[key]

    def value(self, key: str) -> Any:
        return self._numbers[key].value

    def display(self, key: str, spec: str | None = None) -> str:
        """The registered display, or a re-format for a placeholder spec (decimals / abs)."""
        n = self._numbers[key]
        decimals, absolute = parse_spec(spec)
        if decimals is None and not absolute:
            return n.display
        if n.value is None:
            return "n/a"
        return fmt(n.value, n.unit, decimals if decimals is not None else n.decimals,
                   signed=n.signed, absolute=absolute)

    def items(self):
        return sorted(self._numbers.items())

    def to_json(self) -> dict:
        """Every number as a dict."""
        return {k: asdict(n) for k, n in self.items()}


class MissingNumber(KeyError):
    pass


def fill(template: str, registry: Registry, extra: dict[str, str] | None = None) -> str:
    """Fill {key} / {key:spec} placeholders with registry displays (or `extra` literal strings).

    Raises MissingNumber for an unknown key so a template can never render a gap.
    """
    import string

    out = []
    for literal, name, spec, conv in string.Formatter().parse(template):
        out.append(literal)
        if name is None:
            continue
        if extra and name in extra:
            out.append(extra[name])
        elif name in registry:
            out.append(registry.display(name, spec))
        else:
            raise MissingNumber(f"template placeholder {{{name}}} has no registered number")
    return "".join(out)
