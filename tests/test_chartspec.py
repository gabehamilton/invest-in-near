"""The chart contract (pipeline/chartspec.py): one plain sentence, an optional one-sentence caveat, method notes,
and page tables thinned to month starts with every row kept for the CSV. Offline."""

from __future__ import annotations

import pytest

from pipeline.chartspec import MAX_SENTENCE_WORDS, Chart, Query, Series, sentence_count


def chart(**kw) -> Chart:
    base = dict(id="c99", section="O1", priority="supporting", claims=[], verdicts=[], title="A title",
                sentence="Each point is one thing.", form="line", sources=["s"], queries=[Query("derived", "registry", "x")],
                table={"columns": ["date", "v"], "rows": [["2025-01-01", 1.0]]}, code_ref="x", formula_ids=[],
                x={"type": "time"}, y={"unit": "%"}, series=[Series("a", "A", "near", [["2025-01-01", 1.0]])])
    base.update(kw)
    return Chart(**base)


@pytest.mark.parametrize("text, n", [
    ("", 0),
    ("One sentence.", 1),
    ("One sentence with a figure of 2.48% inside.", 1),
    ("Left, what you earn; right, what inflation takes.", 1),
    ("A first one. A second one.", 2),
    ("A first one. a lower-case start does not end it.", 1),
    ("No end mark", 1),
    ("A. B", 2),
])
def test_sentence_count(text, n):
    assert sentence_count(text) == n


def test_validate_holds_sentence_and_caveat_to_one_sentence():
    chart().validate()
    with pytest.raises(ValueError, match="one plain sentence"):
        chart(sentence="It shows supply. It also shows the ceiling.").validate()
    with pytest.raises(ValueError, match="one plain sentence"):
        chart(caveat="Read it this way. And also this way.").validate()
    with pytest.raises(ValueError, match="one plain sentence"):
        chart(sentence=" ".join(["word"] * (MAX_SENTENCE_WORDS + 1)) + ".").validate()
    chart(caveat="One caveat.", notes=["A note. Notes may run to several sentences."]).validate()


def test_thin_page_table_keeps_month_starts_and_the_latest_row():
    rows = [["2025-01-06", 1], ["2025-01-13", 2], ["2025-02-03", 3], ["2025-02-10", 4], ["2025-02-17", 5]]
    c = chart(table={"columns": ["date", "v"], "rows": rows}).thin_page_table("Rows: month starts.")
    assert c.table["rows"] == [["2025-01-06", 1], ["2025-02-03", 3], ["2025-02-17", 5]]
    assert c.table["footnote"] == "Rows: month starts."
    assert c.csv_table == {"columns": ["date", "v"], "rows": rows}
    assert "csv_table" not in c.to_json() and c.to_csv().count("\n") == len(rows) + 1


def test_thin_page_table_leaves_an_existing_csv_table_alone():
    full = {"columns": ["date", "v"], "rows": [["2025-01-01", 0], ["2025-01-02", 1]]}
    c = chart(table={"columns": ["date", "v"], "rows": [["2025-01-01", 0]]}, csv_table=full)
    c.thin_page_table("Rows: month starts.")
    assert c.csv_table is full
