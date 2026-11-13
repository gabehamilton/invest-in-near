"""A synthetic repository for tests/test_build.py and tests/test_lint.py (not collected by pytest).

make_repo(tmp_path, monkeypatch) writes data/raw (with sidecars), thesis/{rules,sources,constants,copy,
open_items}.toml, templates/ and an importable fake model package, then points pipeline.numbers at the new
root so source paths come out repo-relative. Nothing here touches the real data/raw or the network.
"""

from __future__ import annotations

import json
import sys
import textwrap
import uuid
from dataclasses import dataclass
from pathlib import Path

import pipeline.numbers as numbers_mod
from pipeline.build import Paths

AS_OF = "2026-03-10T12:00:00+00:00"


@dataclass
class Repo:
    root: Path
    package: str
    models: tuple[str, ...] = ("alpha", "beta")

    @property
    def paths(self) -> Paths:
        return Paths(root=self.root, out=self.root)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"))
    return path


def raw(root: Path, rel: str, text: str, **meta) -> Path:
    p = write(root / "data" / "raw" / rel, text)
    p.with_suffix(".meta.json").write_text(json.dumps(meta, sort_keys=True))
    return p


HYPOTHESIS = ("NEAR is deflationary compared to other currencies. In addition, NEAR is volatile. "
              "Rather than being a disadvantage this is an asset, volatility can be sold.")

RULES = f'''
[meta]
hypothesis = "{HYPOTHESIS}"
tally_note = "H claims only."

[H1]
kind = "stated"
title = "Deflationary"
claimed = "NEAR is deflationary"
test = "Is growth below zero?"
metrics = ["growth"]
supported = "growth < 0"
refuted = "growth > 0"
chart = "c01"
section = "O1"

[H8]
kind = "stated"
title = "Volatility can be sold"
claimed = "volatility can be sold"
test = "Is there an options market?"
metrics = ["n_venues"]
requires = ["n_venues"]
untestable_if = "n_venues == 0"
untestable_reason = "no venue lists options"
trigger = "a venue lists options"
supported = "vrp_mean > 0"
refuted = "vrp_mean <= 0"
chart = "c01"
section = "A5"

[H9]
kind = "hidden"
title = "Volatile"
claimed = "NEAR is volatile"
assumption = "Volatility is high enough to matter."
test = "Is volatility at least fifty?"
metrics = ["vol"]
supported = "vol >= 50"
partly = "vol >= 20"
refuted = "vol < 20"
chart = "c01"
section = "O5"

[H9_reframe]
kind = "reframe"
title = "Volatility as an asset"
claimed = "Rather than being a disadvantage this is an asset"
test = "Is volatility at least fifty?"
metrics = ["vol"]
supported = "vol >= 50"
refuted = "vol < 20"
chart = "c01"
section = "O5"

[H10]
kind = "hidden"
title = "Missing input"
claimed = "compared to other currencies"
assumption = "There is a comparison."
test = "Is the gap positive?"
metrics = ["gap"]
supported = "gap > 0"
refuted = "gap <= 0"
chart = "c01"
section = "A1"

[tripwires.T1]
metric = "growth"
metric_label = "Supply growth"
keys = ["growth"]
threshold_text = "growth above five"
expr = "growth > 5"
meaning = "supply grows too fast"
chart = "c01"

[crosschecks.X3]
title = "Binance against Coinbase"
tolerance_text = "median gap at most twenty basis points"
start = "2022-09-01"
max_median_bp = 20
blocks_verify = true
needs = "market files"
fix = "make data"

[crosschecks.X6]
title = "Explorer against DefiLlama"
tolerance_text = "ratio at most one point oh five on most days"
max_ratio = 1.05
min_share_pct = 95
blocks_verify = false
needs = "NEAR_INTENTS_JWT"
fix = "make data"
'''

SOURCES = '''
[sources.demo_ok]
name = "Demo series"
class = "approved-primary"
endpoint = "https://example.org/series"
auth = "none"
paths = ["data/raw/demo/series.csv"]
stale = { rule = "days", max = 3 }
stale_text = "more than three days old"
committed = "yes"
tos_note = "Demo terms."
fix = "make data"

[sources.demo_stale]
name = "Old cache"
class = "approved-primary"
endpoint = "https://example.org/old"
auth = "none"
paths = ["data/raw/demo/old.csv"]
stale = { rule = "days", max = 3 }
stale_text = "more than three days old"
committed = "yes"
tos_note = "Demo terms."
fix = "make data"

[sources.demo_lag]
name = "Lagging upstream"
class = "approved-primary"
endpoint = "https://example.org/lag"
auth = "none"
paths = ["data/raw/demo/lag.csv"]
stale = { rule = "days", max = 3 }
stale_text = "more than three days old"
committed = "yes"
tos_note = "Demo terms."
fix = "make data"

[sources.demo_failed]
name = "Failed pull"
class = "approved-primary"
endpoint = "https://example.org/broken"
auth = "none"
paths = ["data/raw/demo/broken.csv"]
stale = { rule = "days", max = 3 }
stale_text = "more than three days old"
committed = "yes"
tos_note = "Demo terms."
fix = "make data"

[sources.gated]
name = "Credentialed source"
class = "approved-crosscheck"
endpoint = "https://example.org/private"
auth = "token"
requires_env = ["DEMO_TOKEN"]
paths = ["data/raw/gated/*.csv"]
stale = { rule = "none" }
stale_text = "not judged"
committed = "aggregates only"
tos_note = "Demo terms."
fix = "set DEMO_TOKEN"

[sources.intents_explorer]
name = "Explorer aggregates"
class = "approved-crosscheck"
endpoint = "https://example.org/explorer"
auth = "token"
requires_env = ["NEAR_INTENTS_JWT"]
paths = ["data/raw/intents/explorer_daily.csv"]
publish = false
stale = { rule = "days", max = 3, last_obs = "final_day" }
stale_text = "more than three days old"
committed = "aggregates only"
tos_note = "Publication needs permission."
fix = "set NEAR_INTENTS_JWT"

[sources.constants]
name = "Declared constants"
class = "declared-constant"
endpoint = "thesis/constants.toml"
auth = "none"
paths = ["thesis/constants.toml"]
stale = { rule = "none" }
stale_text = "not judged"
committed = "yes"
tos_note = "Quoted."
fix = "edit thesis/constants.toml"
'''

CONSTANTS = '''
[demo_rate]
value = 2.5
unit = "%"
source = "https://example.org/rate"
quote = "the rate is two and a half percent"
retrieved = "2026-03-01"

[demo_unverified]
value = "2026-01-01"
unit = "date"
source = "https://example.org/date"
quote = "from the first of January"
quote_status = "UNVERIFIED: re-check"
retrieved = "2026-03-02"
'''

COPY = '''
[headline]
[[headline.variants]]
when = ["H1=Supported"]
text = "NEAR is deflationary after all."
[[headline.variants]]
when = ["H1=Refuted", "H9=Supported|Partly"]
text = "Supply grows by {growth}, slowly."
[[headline.variants]]
when = []
text = "The ledger."

[lede]
[[lede.variants]]
when = []
text = "Volatility was {vol}; {n_refuted} of {n_claims} testable claims fail."

[unused]
[[unused.variants]]
when = ["H9=Partly"]
text = "Not rendered by any template."
[[unused.variants]]
when = []
text = ""
'''

OPEN_ITEMS = '''
[[items]]
id = "demo-gap"
title = "A gap"
status = "pending"
text = "Explained without figures; growth is {growth}."
resolve = "Pull more data."
'''

INDEX = '''
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Fixture</title>
<style>.x { width: 10px }</style><script>var n = 3;</script></head>
<body>
<!-- a comment with 42 -->
<h1>{{ copy('headline') }}</h1>
<p class="lede">{{ copy('lede') }}</p>
<p>{{ num('growth') }} · {{ num('vol', 1) }} · <code>growth_365</code></p>
{% for cid in claim_order %}
<div id="claim-{{ cid }}">{{ ident(cid) }} {% if rules[cid].get('claimed') %}{{ claimed(cid) }}{% endif %} {{ chip(cid) }} {{ rule(cid, 'test') }} · {{ rule(cid) }}</div>
{% endfor %}
<p>{{ verdict('H1').verdict }}</p>
{% for tid, tw in tripwires.items() %}
<p>{{ ident(tid) }} {{ rule(tid, 'threshold_text') }} {{ num(tw.metric) }}{% if tw.tripped %} tripped{% endif %}</p>
{% endfor %}
{% for cid, ch in charts.items() %}
<h3>{{ text(ch.title_template) }}</h3>
<p>{{ text(ch.sentence_template) }}</p>
<table>{% for row in ch.table.rows %}<tr>{% for v in row %}<td>{{ datum(v) }}</td>{% endfor %}</tr>{% endfor %}</table>
{% endfor %}
<p>{{ cite('Demo source', 'https://example.org/a') }} · data as of {{ tm(build.as_of) }}</p>
<ul>{% for s in sources %}<li data-state="{{ s.state }}">{{ s.name }}</li>{% endfor %}</ul>
{% for it in open_items.get('items', []) %}<p>{{ text(it.text) }}</p>{% endfor %}
</body></html>
'''

DATA = '''
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Fixture data</title></head>
<body>
<table>{% for k, n in numbers|dictsort %}<tr><th><code>{{ k }}</code></th><td>{{ num(k) }}</td></tr>{% endfor %}</table>
{% for xid, x in crosschecks.items() %}<p>{{ ident(xid) }} <code>{{ x.status }}</code> <code>{{ x.observed or 'n/a' }}</code></p>{% endfor %}
</body></html>
'''

THESIS = '''
# Fixture thesis

**{{ copy('headline') }}**

{{ copy('lede') }}

{% for cid in claim_order %}
- {{ ident(cid) }}: {{ chip(cid) }}
{% endfor %}

```sh
make charts   # step 1 of 2
```

Inline `code 123` is fine.
'''

ALPHA = '''
"""Fake model: reads data/raw/demo/series.csv under the (monkeypatched) registry root."""
import csv
import socket

from pipeline import numbers
from pipeline.chartspec import Chart, Query, Series

CALLS = []


def _rows():
    path = numbers.ROOT / "data" / "raw" / "demo" / "series.csv"
    with path.open() as fh:
        return path, list(csv.DictReader(fh))


def compute(reg):
    CALLS.append("compute")
    if getattr(numbers, "FAKE_TRY_NETWORK", False):
        socket.create_connection(("example.org", 80), timeout=1)
    path, rows = _rows()
    first, last = float(rows[0]["value"]), float(rows[-1]["value"])
    reg.put("growth", (last / first - 1) * 100, "%", "F-growth", source_files=[path], inputs=["first", "last"])
    reg.put("vol", 30.0, "%", "F-rv", source_files=[path])
    reg.put("gap", None, "pp", "F-gap", source_files=[path], null_reason="no comparison series in this fixture")


def charts(reg):
    CALLS.append("charts")
    path, rows = _rows()
    chart = Chart(
        id="c01", section="O1", priority="supporting", claims=["H1"], title="", sentence="", form="line",
        sources=["Demo series (https://example.org/series)"],
        queries=[Query(kind="http", endpoint="https://example.org/series", body_or_sql="GET /series")],
        table={"columns": ["date", "value"], "rows": [[r["date"], float(r["value"])] for r in rows]},
        code_ref="fake::charts", formula_ids=["F-growth"],
        series=[Series(id="s", label="Supply", entity="near", points=[[r["date"], float(r["value"])] for r in rows])],
        title_template="Supply grew {growth}", sentence_template="Volatility was {vol} over the sample.",
    )
    return [chart.resolve(reg)]
'''

BETA = '''
"""Fake model: an options-venue count (zero, so H8 is Not testable)."""
from pipeline import numbers


def compute(reg):
    reg.put("n_venues", 0, "count", "F-venues", source_files=[numbers.ROOT / "data" / "raw" / "demo" / "series.csv"])


def charts(reg):
    return []
'''


def make_repo(tmp_path: Path, monkeypatch, *, name: str = "repo") -> Repo:
    root = tmp_path / name
    # raw files: sidecar fetched_at decides as_of; last_date decides lag
    raw(root, "demo/series.csv", """
        date,value
        2026-03-07,100
        2026-03-08,101
        2026-03-09,102
        """, fetched_at=AS_OF, rows=3, last_date="2026-03-09")
    raw(root, "demo/old.csv", "date,value\n2026-01-01,1\n",
        fetched_at="2026-01-01T00:00:00+00:00", rows=1, last_date="2026-01-01")
    raw(root, "demo/lag.csv", "date,value\n2026-02-01,1\n",
        fetched_at="2026-03-10T06:00:00+00:00", rows=1, last_date="2026-02-01")
    raw(root, "demo/broken.csv", "date,value\n2026-03-09,1\n",
        fetched_at="2026-03-10T07:00:00+00:00", rows=1, last_date="2026-03-09", complete=False,
        error="HTTP 500")
    raw(root, "market/NEARUSDT_binance_daily.csv", """
        date,close
        2023-01-01,1.000
        2023-01-02,1.010
        2023-01-03,1.020
        """, fetched_at="2026-03-10T08:00:00+00:00", rows=3, last_date="2023-01-03")
    raw(root, "market/near_usd_coinbase_daily.csv", """
        date,close
        2023-01-01,1.000
        2023-01-02,1.0101
        2023-01-03,1.0201
        """, fetched_at="2026-03-10T08:00:00+00:00", rows=3, last_date="2023-01-03")
    raw(root, "intents/explorer_daily.csv", """
        date,final,volume_in_usd_success
        2026-03-08,true,100
        2026-03-09,true,101
        """, fetched_at="2026-03-10T09:00:00+00:00", rows=2)
    raw(root, "intents/defillama_volume_daily.csv", """
        date,volume_usd
        2026-03-08,100
        2026-03-09,100
        """, fetched_at="2026-03-10T09:00:00+00:00", rows=2, last_date="2026-03-09")

    write(root / "thesis" / "rules.toml", RULES)
    write(root / "thesis" / "sources.toml", SOURCES)
    write(root / "thesis" / "constants.toml", CONSTANTS)
    write(root / "thesis" / "copy.toml", COPY)
    write(root / "thesis" / "open_items.toml", OPEN_ITEMS)
    write(root / "templates" / "index.html.j2", INDEX)
    write(root / "templates" / "data.html.j2", DATA)
    write(root / "templates" / "THESIS.md.j2", THESIS)
    write(root / "templates" / "llms.txt.j2", "# Fixture index\n\n> {{ copy('headline') }}\n")

    package = f"fakemodels_{uuid.uuid4().hex[:8]}"
    pkg_dir = tmp_path / "pkgs" / package
    write(pkg_dir / "__init__.py", "")
    write(pkg_dir / "alpha.py", ALPHA)
    write(pkg_dir / "beta.py", BETA)
    monkeypatch.syspath_prepend(str(tmp_path / "pkgs"))
    use_root(root, monkeypatch)
    return Repo(root=root, package=package)


def use_root(root: Path, monkeypatch) -> None:
    """Point the registry at a repo root (it makes source paths relative to it)."""
    monkeypatch.setattr(numbers_mod, "ROOT", root)
    monkeypatch.setattr(numbers_mod, "RAW_DIR", root / "data" / "raw")


def forget_package(package: str) -> None:
    for name in [m for m in sys.modules if m == package or m.startswith(package + ".")]:
        del sys.modules[name]
