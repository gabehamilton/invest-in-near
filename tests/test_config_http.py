"""Offline tests for the shared HTTP helpers in pipeline.config: Retry-After parsing and its use on retries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

from pipeline import config


def test_retry_after_reads_seconds_and_dates_falls_back_and_caps():
    assert config.retry_after({"Retry-After": "7"}, 2.0) == 7.0
    assert config.retry_after({}, 2.0) == 2.0
    assert config.retry_after(None, 4.0) == 4.0
    assert config.retry_after({"Retry-After": "soon"}, 3.0) == 3.0
    assert config.retry_after({"Retry-After": "86400"}, 2.0) == config.RETRY_AFTER_CAP
    assert config.retry_after({"Retry-After": "-5"}, 2.0) == 0.0
    assert config.retry_after({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}, 2.0) == 0.0
    soon = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    assert 20.0 < config.retry_after({"Retry-After": soon}, 2.0) <= 30.0
    late = format_datetime(datetime.now(timezone.utc) + timedelta(days=1), usegmt=True)
    assert config.retry_after({"Retry-After": late}, 2.0) == config.RETRY_AFTER_CAP


def fake_response(status: int, headers: dict | None = None):
    return SimpleNamespace(status_code=status, headers=headers or {}, raise_for_status=lambda: None,
                           json=lambda: {"ok": True})


def test_get_and_post_wait_the_retry_after_header(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(config.time, "sleep", sleeps.append)
    answers = iter([fake_response(429, {"Retry-After": "5"}), fake_response(200)])
    monkeypatch.setattr(config._session, "get", lambda *a, **k: next(answers))
    assert config.http_get("https://example.test/").status_code == 200
    answers = iter([fake_response(503, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}),
                    fake_response(429), fake_response(200)])
    monkeypatch.setattr(config._session, "post", lambda *a, **k: next(answers))
    assert config.http_post_json("https://example.test/", {}, backoff=1.0) == {"ok": True}
    assert sleeps == [5.0, 0.0, 2.0]
