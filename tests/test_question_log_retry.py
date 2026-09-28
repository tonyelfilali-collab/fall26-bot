"""Question logs: a clashing write (409) is retried, a real error is not."""
from __future__ import annotations

from types import SimpleNamespace

import requests

import question_log


def fake_put(statuses, calls):
    def put(url, **kwargs):
        status = statuses[min(len(calls), len(statuses) - 1)]
        calls.append(status)

        def raise_for_status():
            if status >= 400:
                raise requests.HTTPError(f"HTTP {status}")

        return SimpleNamespace(status_code=status, raise_for_status=raise_for_status)

    return put


RECORD = {"question": {"id_of_post": 7}}


def test_clash_is_retried_then_saved(monkeypatch):
    calls = []
    monkeypatch.setattr(question_log.requests, "put", fake_put([409, 409, 201], calls))
    writer = question_log.QuestionLogWriter(token="t", pause=lambda s: None)
    assert writer.save("questions/x.json", RECORD) is True
    assert calls == [409, 409, 201] and writer.saved == ["questions/x.json"]


def test_gives_up_after_the_attempts(monkeypatch):
    calls = []
    monkeypatch.setattr(question_log.requests, "put", fake_put([409], calls))
    writer = question_log.QuestionLogWriter(token="t", pause=lambda s: None)
    assert writer.save("questions/x.json", RECORD) is False
    assert len(calls) == question_log.SAVE_ATTEMPTS


def test_other_errors_are_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(question_log.requests, "put", fake_put([401], calls))
    writer = question_log.QuestionLogWriter(token="t", pause=lambda s: None)
    assert writer.save("questions/x.json", RECORD) is False
    assert calls == [401]
