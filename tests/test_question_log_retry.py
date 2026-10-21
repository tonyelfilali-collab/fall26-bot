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


# ---------------------------------------------------------------- end-of-run retry pass


def test_retry_pass_saves_what_failed_during_the_run(monkeypatch, capsys):
    calls, pauses = [], []
    # 4 attempts fail during the run (GitHub down), then it is back.
    monkeypatch.setattr(question_log.requests, "put", fake_put([503] * question_log.SAVE_ATTEMPTS + [201], calls))
    writer = question_log.QuestionLogWriter(token="t", pause=pauses.append)
    assert writer.save("questions/x.json", RECORD) is False and writer.failed
    assert writer.retry_failed() == 0
    assert writer.saved == ["questions/x.json"] and not writer.failed
    assert question_log.RETRY_PASS_WAIT_SECONDS not in pauses  # saved on the first retry: no wait
    assert "::warning" not in capsys.readouterr().out


def test_retry_pass_tries_3_times_about_2_minutes_then_warns(monkeypatch, capsys):
    calls, pauses = [], []
    monkeypatch.setattr(question_log.requests, "put", fake_put([500], calls))
    writer = question_log.QuestionLogWriter(token="t", pause=pauses.append)
    writer.save("questions/a.json", RECORD)
    writer.save("questions/b.json", {"question": {"id_of_post": 8}})
    calls.clear()
    assert writer.retry_failed() == 2
    assert len(calls) == 2 * question_log.RETRY_PASS_TRIES * question_log.SAVE_ATTEMPTS
    waits = [p for p in pauses if p == question_log.RETRY_PASS_WAIT_SECONDS]
    assert len(waits) == question_log.RETRY_PASS_TRIES - 1 and 90 <= sum(waits) <= 150
    out = capsys.readouterr().out
    assert out.count("::warning title=question-log-failed::") == 2


def test_retry_pass_does_nothing_when_all_saved(monkeypatch):
    calls = []
    monkeypatch.setattr(question_log.requests, "put", fake_put([201], calls))
    writer = question_log.QuestionLogWriter(token="t", pause=lambda s: None)
    writer.save("questions/x.json", RECORD)
    assert writer.retry_failed() == 0 and len(calls) == 1


def test_main_runs_the_retry_pass_before_the_end_of_run_summary():
    source = open("main.py").read()
    assert source.index("question_log.retry_failed()") < source.index("Question JSON logs saved to fall26-data")
