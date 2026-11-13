"""The pull command line: a misspelled source name stops the run before any request goes out."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import pull  # noqa: E402


@pytest.mark.parametrize("argv", [["--only", "fred,nope"], ["--skip", "bigqeury"]])
def test_unknown_source_names_are_rejected(argv, monkeypatch, capsys):
    monkeypatch.setattr(pull.importlib, "import_module", lambda name: pytest.fail(f"imported {name}"))
    with pytest.raises(SystemExit) as exit_info:
        pull.main(argv)
    assert exit_info.value.code == 2
    assert "unknown source" in capsys.readouterr().err
