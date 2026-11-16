"""Chart specs: the JSON contract between pipeline models and site/assets/charts.js.

A model builds one Chart per figure, fills its title and sentence from the number
registry, and the build writes site/data/charts/<id>.json plus csv/<id>.csv.
Validation here enforces the dataviz rules that are checkable in data: one y axis
(no y2), a table twin, a source, a query and a one-sentence reading on every chart,
and no unresolved placeholders.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any

FORMS = {"line", "small-multiples", "diverging-bar", "column", "bar",
         "dot-range", "table-heatmap", "table", "empty"}
STATUSES = {"ok", "fallback", "partial", "error", "unavailable"}
ENTITIES = {"near", "usd", "third", "context", "positive", "negative"}
ROLES = {"emphasis", "context", "diverging", "model"}
QUERY_KINDS = {"jsonrpc", "http", "sql", "derived"}
MAX_SENTENCE_WORDS = 60
# a sentence ends at . ! or ? followed by a capitalized word or the end of the text ("2.48%" and "0.06x" do not)
_SENTENCE_END = re.compile(r"[.!?][\"')\]]?(?=\s+[A-Z]|\s*$)")


def sentence_count(text: str) -> int:
    """How many sentences a reading holds (0 for empty text)."""
    text = text.strip()
    if not text:
        return 0
    n = len(_SENTENCE_END.findall(text))
    return n if re.search(r"[.!?][\"')\]]?$", text) else n + 1


@dataclass
class Series:
    id: str
    label: str
    entity: str
    points: list[list[Any]]  # [[x, y|None], ...] ; x is an ISO date or a category label
    role: str = "emphasis"
    unit: str = "%"
    panel: str | None = None
    dashed: bool = False
    underlay: bool = False  # drawn as a wide pale band under the other lines (a ceiling the data should hug)


@dataclass
class Query:
    kind: str
    endpoint: str
    body_or_sql: str
    file: str | None = None


@dataclass
class Chart:
    id: str
    section: str
    priority: str
    claims: list[str]
    title: str
    sentence: str
    form: str
    sources: list[str]
    queries: list[Query]
    table: dict  # {"columns": [...], "rows": [[...], ...]}
    code_ref: str
    formula_ids: list[str] = field(default_factory=list)
    status: str = "ok"
    eyebrow: str = ""
    x: dict = field(default_factory=lambda: {"type": "time"})
    y: dict = field(default_factory=lambda: {"unit": "%"})
    panels: list[dict] = field(default_factory=list)
    series: list[Series] = field(default_factory=list)
    reference_lines: list[dict] = field(default_factory=list)  # {y, label}
    bands: list[dict] = field(default_factory=list)            # {from, to, label}
    annotations: list[dict] = field(default_factory=list)      # {x, label, kind, panel?}; label <= 28 chars
    values: dict = field(default_factory=dict)                 # placeholder -> number key used
    verdicts: list[str] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)               # {first, last, n, gaps}
    as_of: str | None = None
    status_banner: str | None = None
    empty_state: dict | None = None                            # {reason, needs, fix_command}
    title_template: str = ""
    sentence_template: str = ""
    # one plain sentence says what the chart shows; the caveat (optional, one sentence) is the single thing a
    # reader must know to read it correctly; method notes sit in a collapsed list under the source line
    caveat_template: str = ""
    caveat: str = ""
    notes_templates: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Optional full-resolution rows for csv/<id>.csv when `table` holds the downsampled rows that are drawn
    # (chart JSON stays small; full resolution in the CSV). Never written to the chart JSON.
    csv_table: dict | None = None

    def resolve(self, registry) -> "Chart":
        """Fill title/sentence from title_template/sentence_template; every {placeholder} must be a registry key.

        Records values[placeholder] = {key, value, display} so the page can wrap each figure in a .num span.
        """
        from pipeline.numbers import fill
        import string
        for template in (self.title_template, self.sentence_template, self.caveat_template, *self.notes_templates):
            for _, name, spec, _ in string.Formatter().parse(template):
                if name is not None:
                    n = registry.get(name) if name in registry else None
                    if n is None:
                        fill("{" + name + "}", registry)  # raises MissingNumber
                    # the display as printed ({key:abs} prints the magnitude; the words carry the sign)
                    self.values[name] = {"key": name, "value": n.value, "display": registry.display(name, spec)}
        if self.title_template:
            self.title = fill(self.title_template, registry)
        if self.sentence_template:
            self.sentence = fill(self.sentence_template, registry)
        if self.caveat_template:
            self.caveat = fill(self.caveat_template, registry)
        if self.notes_templates:
            self.notes = [fill(t, registry) for t in self.notes_templates]
        return self

    def validate(self) -> None:
        problems = []
        if self.form not in FORMS:
            problems.append(f"unknown form {self.form}")
        if self.status not in STATUSES:
            problems.append(f"unknown status {self.status}")
        if "y2" in self.y or any("y2" in p for p in self.panels):
            problems.append("y2 is forbidden (one axis per plot)")
        for text in (self.title, self.sentence, self.eyebrow, self.status_banner or "", self.caveat, *self.notes):
            if "{" in text or "}" in text:
                problems.append(f"unresolved placeholder in {text!r}")
            for bad in ("NaN", "None", "null", "undefined", "Infinity"):
                if bad in text.split():
                    problems.append(f"{bad!r} in {text!r}")
        if not self.sentence.strip():
            problems.append("missing one-sentence reading")
        for name, text in (("sentence", self.sentence), ("caveat", self.caveat)):
            n_sent, n_words = sentence_count(text), len(text.split())
            if n_sent > 1 or n_words > MAX_SENTENCE_WORDS:
                problems.append(f"{name} must be one plain sentence (found {n_sent} sentences, {n_words} words; "
                                "move method detail into notes)")
        if not self.sources:
            problems.append("missing source")
        if not self.queries:
            problems.append("missing query")
        if self.status == "unavailable":
            if not self.empty_state or not all(self.empty_state.get(k) for k in ("reason", "needs", "fix_command")):
                problems.append("unavailable chart needs empty_state reason/needs/fix_command")
        elif self.form != "empty" and not self.table.get("rows"):
            problems.append("missing table twin rows")
        for s in self.series:
            if s.entity not in ENTITIES:
                problems.append(f"series {s.id}: unknown entity {s.entity}")
            if s.role not in ROLES:
                problems.append(f"series {s.id}: unknown role {s.role}")
            for point in s.points:
                y = point[1]
                if isinstance(y, float) and (math.isnan(y) or math.isinf(y)):
                    problems.append(f"series {s.id}: NaN/inf point at {point[0]} (use None)")
                    break
        for q in self.queries:
            if q.kind not in QUERY_KINDS:
                problems.append(f"query kind {q.kind}")
        if problems:
            raise ValueError(f"chart {self.id}: " + "; ".join(problems))

    def thin_page_table(self, footnote: str, col: int = 0) -> "Chart":
        """Keep the first row of each calendar month and the latest row in `table` (the rows baked into the page
        and the chart JSON); every row stays in csv_table for csv/<id>.csv. Column `col` holds ISO dates and
        rows are in date order. Nothing is dropped from the plotted series."""
        rows = self.table.get("rows") or []
        if self.csv_table is None:
            self.csv_table = {"columns": list(self.table.get("columns") or []), "rows": rows}
        kept, last_month = [], None
        for i, row in enumerate(rows):
            month = str(row[col])[:7]
            if month != last_month or i == len(rows) - 1:
                kept.append(row)
            last_month = month
        self.table = {**self.table, "rows": kept, "footnote": footnote}
        return self

    def to_json(self) -> dict:
        self.validate()
        out = asdict(self)
        out.pop("csv_table", None)
        return out

    def to_csv(self) -> str:
        table = self.csv_table if self.csv_table and self.csv_table.get("rows") else self.table
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(table.get("columns", []))
        for row in table.get("rows", []):
            writer.writerow(["" if v is None else v for v in row])
        return buf.getvalue()


def dump(obj: Any) -> str:
    """Deterministic JSON for site/data (sorted keys, no wall clock, compact floats)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
