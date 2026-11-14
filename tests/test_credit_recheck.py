"""After a credit error (architect, 14 Nov): Claude off for 1 hour, then ONE tiny
Haiku test call (max_tokens 5) per hour; when it answers, Claude is back on.
Fake SDK and fake test calls only, 0 real calls."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import frontier
from frontier import HAIKU, OPUS_55
from frontier import credit_test_call as real_credit_test_call  # conftest blocks it in every other test
from gemini_budget import MemoryStore, current_question_key
from spend import SpendGuard
from tests.test_frontier_agent import _budget

CREDIT = "Your credit balance is too low to access the Anthropic API."


class Tests:
    """A fake test call with scripted answers; counts the calls."""

    def __init__(self, *answers: bool):
        self.answers, self.calls = list(answers), 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.answers.pop(0)


def _credit_error(claude, when=None):
    error = RuntimeError(CREDIT)
    error.status_code = 400  # type: ignore[attr-defined]
    token = current_question_key.set("1")
    try:
        claude.start(OPUS_55)
        claude.finish(OPUS_55, False, error=error)
    finally:
        current_question_key.reset(token)
    if when is not None:
        claude.credit_off = {"since": when.isoformat(), "checked": when.isoformat()}


def _age(claude, minutes):
    """Pretend the last error / test call was `minutes` ago."""
    claude.credit_off["checked"] = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _next_run(claude, test):
    """A later run on the saved ledger."""
    later = frontier.ClaudeBudget(MemoryStore(claude.snapshot()), 170.0, claude.reset, today=claude.today)
    planner = _pool(later)
    planner.credit_test = test
    line = planner.start_run(5.0, api_key=True)
    return planner, later, line


def _pool(claude):
    planner = frontier._frontier_pool(MemoryStore(), SpendGuard(MemoryStore({"days": {}})), claude)
    planner.seasonal_agents = 1
    return planner


def test_error_off_one_hour_test_fails_stays_off_test_ok_back_on(caplog):
    caplog.set_level("INFO", logger="fall26")
    claude = _budget()
    _credit_error(claude)
    assert claude.blocked and frontier.claude_mode(claude)[0] == "off"

    # Within the hour: no test call, Claude off.
    test = Tests()
    planner, claude, line = _next_run(claude, test)
    assert test.calls == 0 and planner.mode == "off" and "credit error, a test call every hour" in line

    # An hour later: ONE test call; it fails -> off one more hour.
    _age(claude, 61)
    test = Tests(False)
    planner, claude, _ = _next_run(claude, test)
    assert test.calls == 1 and planner.mode == "off"
    assert datetime.now(timezone.utc) - datetime.fromisoformat(claude.credit_off["checked"]) < timedelta(minutes=1)

    # The next run (minutes later): no test call yet.
    test = Tests()
    planner, claude, _ = _next_run(claude, test)
    assert test.calls == 0 and planner.mode == "off"

    # An hour after the failed test: ONE test call; it answers -> Claude back on.
    _age(claude, 60)
    test = Tests(True)
    planner, claude, line = _next_run(claude, test)
    assert test.calls == 1 and planner.mode == "full" and "credit" not in line
    assert "credit_off" not in claude.snapshot() and claude.credit_errors  # the error stays in the history
    assert "Claude back on" in caplog.text

    # Back on: no more test calls.
    test = Tests()
    planner, _, _ = _next_run(claude, test)
    assert test.calls == 0 and planner.mode == "full"


def test_one_test_call_per_run_and_never_a_forecast_retry(monkeypatch):
    claude = _budget()
    _credit_error(claude)
    _age(claude, 120)
    test = Tests(False)
    planner, claude, _ = _next_run(claude, test)
    assert test.calls == 1  # one per run even when hours have passed
    # The failed call itself was never retried: one call on the question, charged $0 (HTTP 400).
    calls = claude.questions["1"]["calls"]
    assert len(calls) == 1 and calls[0]["charged"] == 0.0


def test_a_second_credit_error_keeps_the_first_time():
    claude = _budget()
    start = datetime.now(timezone.utc) - timedelta(minutes=10)
    _credit_error(claude, when=start)
    claude.mark_credit_error()
    assert claude.credit_off["since"] == start.isoformat()


def test_no_key_or_unreadable_ledger_no_test_call():
    claude = _budget()
    _credit_error(claude)
    _age(claude, 90)
    test = Tests()
    later = frontier.ClaudeBudget(MemoryStore(claude.snapshot()), 170.0, claude.reset, today=claude.today)
    planner = _pool(later)
    planner.credit_test = test
    assert planner.start_run(5.0, api_key=False) and test.calls == 0


def test_public_log_words_only(caplog):
    caplog.set_level("INFO", logger="fall26")
    claude = _budget()
    _credit_error(claude)
    claude.credit_checked(False)
    claude.credit_checked(True)
    text = caplog.text
    assert "credit error" in text and "Claude back on" in text and "too low to access" not in text


@pytest.mark.parametrize("raises, answered", [(None, True), (RuntimeError(CREDIT), False), (TimeoutError(), False)])
def test_the_test_call_is_tiny_haiku_and_never_raises(monkeypatch, raises, answered):
    import anthropic

    seen: list[dict] = []

    class FakeClient:
        def __init__(self, **kwargs):
            seen.append({"client": kwargs})
            self.messages = SimpleNamespace(create=self.create)

        def create(self, **request):
            seen.append(request)
            if raises is not None:
                raise raises
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="OK")])

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    assert real_credit_test_call() is answered
    request = seen[1]
    assert request["model"] == HAIKU.removeprefix("anthropic/") == "claude-haiku-5-5"
    assert request["max_tokens"] == 5 and seen[0]["client"]["max_retries"] == 0
