"""fall26-data file saves (quota ledger, retry state) try again when a
concurrent commit moved the branch (409) or GitHub is briefly down (5xx)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import gemini_budget
from gemini_budget import GitHubFileStore


def _store(monkeypatch, statuses):
    calls = []

    def put(url, headers=None, json=None, timeout=None):
        status = statuses[len(calls)]
        calls.append(json)
        return SimpleNamespace(
            status_code=status,
            json=lambda: {"content": {"sha": "new"}},
            raise_for_status=lambda: (_ for _ in ()).throw(RuntimeError(status)) if status >= 400 else None,
        )

    monkeypatch.setattr(gemini_budget.requests, "put", put)
    store = GitHubFileStore("o/r", "quota/x.json", "t")
    store.pause = lambda seconds: None
    store._sha = "old"
    return store, calls


def test_409_from_a_concurrent_commit_is_retried(monkeypatch):
    store, calls = _store(monkeypatch, [409, 409, 200])
    store.save({"day": "d"})
    assert len(calls) == 3
    assert all(c["sha"] == "old" for c in calls)  # the same save, same file version
    assert store._sha == "new"


def test_gives_up_after_4_attempts(monkeypatch):
    store, calls = _store(monkeypatch, [503, 503, 503, 503])
    with pytest.raises(RuntimeError):
        store.save({"day": "d"})
    assert len(calls) == 4


def test_422_file_changed_is_not_retried(monkeypatch):
    store, calls = _store(monkeypatch, [422])
    with pytest.raises(RuntimeError):
        store.save({"day": "d"})
    assert len(calls) == 1


def test_ledger_save_failure_still_never_raises(monkeypatch):
    store, _ = _store(monkeypatch, [503, 503, 503, 503])
    ledger = gemini_budget.QuotaLedger(gemini_budget.MemoryStore(), {"m": 20}, 0.2)
    ledger._store = store
    ledger.save()  # a warning only


def test_test_bot_groups():
    text = open(".github/workflows/test_bot.yaml").read()
    assert "inputs.lineup == 'gemini-free' && 'forecast-bot' || 'test-bot-no-gemini'" in text
    assert "inputs.lineup == 'gemini-free' && secrets.GEMINI_API_KEY || ''" in text
    for workflow in ("run_bot_on_tournament.yaml", "probe.yaml"):
        assert "group: forecast-bot" in open(f".github/workflows/{workflow}").read()
