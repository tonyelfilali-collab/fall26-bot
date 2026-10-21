"""The referee shadow (shadow.py + frontier.referee_llm + main._referee_shadow), fake SDK replies (0 real calls)."""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from types import SimpleNamespace

import pytest
from forecasting_tools import BinaryQuestion

import frontier
import main
import shadow
from frontier import OPUS_55, ClaudeBudget
from gemini_budget import MemoryStore, current_question_key
from spend import SpendGuard, question_seasonal, utc_day

RESET = date(2026, 10, 14)


class CreditError(Exception):
    status_code = 400


class Fake:
    """anthropic.AsyncAnthropic stand-in: every request gets `reply` (or raises it) after `delay` seconds."""

    def __init__(self, reply=None, delay=0.0):
        self.reply, self.delay, self.requests = reply, delay, []

    def __call__(self, **kwargs):
        fake = self

        class Messages:
            async def create(self, **request):
                fake.requests.append(request)
                if fake.delay:
                    await asyncio.sleep(fake.delay)
                if isinstance(fake.reply, BaseException):
                    raise fake.reply
                return fake.reply or SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="Weighed.\nProbability: 90%")], stop_reason="end_turn",
                    usage=SimpleNamespace(input_tokens=2000, output_tokens=800, cache_creation_input_tokens=0,
                                          cache_read_input_tokens=0))

        class Client:
            messages = Messages()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        return Client()


def _budget(day: float = 0.0) -> ClaudeBudget:
    claude = ClaudeBudget(MemoryStore({"days": {}}), 170.0, RESET, today=date(2026, 10, 21))
    if day:
        claude.days[utc_day()] = day
    return claude


def _planner(claude: ClaudeBudget | None):
    planner = frontier._frontier_pool(MemoryStore(), SpendGuard(MemoryStore({"days": {}})), claude)
    planner.claude_pacer = frontier.RequestPacer(60000)
    planner.start_run(5.0, api_key=True)
    return planner


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(main, "REFEREE_ENABLED", True)
    monkeypatch.setattr(shadow, "REFEREE_ENABLED", True)


def _bot(planner=None):
    bot = main.FallBot2026(
        llms={"default": "openrouter/x:free", "parser": "openrouter/x:free", "summarizer": "openrouter/x:free", "researcher": "no_research"},
        publish_reports_to_metaculus=False, enable_summarize_research=False, predictions_per_research_report=3,
    )
    bot.planner = planner
    return bot


def _record(*probs):
    return {"forecasts": [{"status": "ok", "parsed": p, "raw_output": f"Reason {i}.\nProbability: {p:.0%}"} for i, p in enumerate(probs)]}


Q = BinaryQuestion(question_text="Will X?", id_of_post=8, page_url="https://www.metaculus.com/questions/8")


def _direct(monkeypatch, fake, record, claude=None, seasonal=True, submitted=0.5, planner="make"):
    """main._referee_shadow on one question; returns (record, budget)."""
    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", fake)
    claude = _budget() if claude is None else claude
    bot = _bot(_planner(claude) if planner == "make" else planner)

    async def go():
        current_question_key.set("8")
        question_seasonal.set(seasonal)
        await bot._referee_shadow(Q, record, submitted)

    asyncio.run(go())
    return record, claude


# ---------------------------------------------------------------- the rule


def test_referee_needed():
    assert shadow.referee_needed([0.3, 0.5])
    assert not shadow.referee_needed([0.3, 0.45])  # exactly 15 points: not over
    assert not shadow.referee_needed([0.3, 0.4, 0.42])
    assert not shadow.referee_needed([0.3]) and not shadow.referee_needed([])


def test_candidate_is_the_clipped_mean_of_referee_and_submitted():
    # (90% + 50%) / 2 = 70%, kept under the highest forecast 60%.
    assert shadow.referee_candidate(0.9, 0.5, [0.3, 0.5, 0.6]) == pytest.approx(0.6)
    assert shadow.referee_candidate(0.4, 0.5, [0.3, 0.5, 0.6]) == pytest.approx(0.45)
    assert shadow.referee_candidate(0.0, 0.4, [0.3, 0.5, 0.6]) == pytest.approx(0.3)


def test_referee_is_built_off():
    assert shadow.REFEREE_ENABLED is False and main.REFEREE_ENABLED is False
    assert shadow.REFEREE_TIMEOUT_SECONDS == 240 and shadow.REFEREE_LATEST_SECONDS == 40 * 60


# ---------------------------------------------------------------- when it runs


def test_disagree_calls_opus_and_saves_the_shadow(monkeypatch, on, caplog):
    fake = Fake()
    with caplog.at_level(logging.INFO):
        record, claude = _direct(monkeypatch, fake, _record(0.3, 0.5, 0.7), submitted=0.5)
    [request] = fake.requests
    assert request["model"] == "claude-opus-5-5" and request["output_config"] == {"effort": "high"}
    assert "Forecaster 1: 30%" in request["messages"][0]["content"]
    # clip(mean(90%, 50%), 30%, 70%) = 70%
    assert record["shadow"]["referee"] == pytest.approx(0.7)
    [call] = claude.questions["8"]["calls"]  # in the Claude ledger like any Claude call
    assert call["model"] == OPUS_55 and call["ok"] and call["charged"] > 0
    assert claude.day_spent() == pytest.approx(call["charged"])
    report = _planner(claude)
    report.calls_before = {}  # this run's calls: the referee's
    assert "| 8 | claude-opus-5-5 |" in frontier.frontier_report(report)
    public = " ".join(r.getMessage() for r in caplog.records if r.name == "fall26" and "referee" in r.getMessage())
    assert "referee saved, $" in public and "70" not in public.replace("$", "")


@pytest.mark.parametrize("probs", [(0.3, 0.4, 0.42), (0.3,), ()])
def test_agree_or_too_few_forecasts_no_call(monkeypatch, on, probs):
    fake = Fake()
    record, claude = _direct(monkeypatch, fake, _record(*probs))
    assert fake.requests == [] and "shadow" not in record and not claude.questions


def test_failed_forecasts_do_not_count(monkeypatch, on):
    record = _record(0.3)
    record["forecasts"].append({"status": "failed"})
    fake = Fake()
    _direct(monkeypatch, fake, record)
    assert fake.requests == []


def test_claude_off_no_referee(monkeypatch, on):
    fake = Fake()
    record, _ = _direct(monkeypatch, fake, _record(0.3, 0.7), planner=_planner(None))
    assert fake.requests == [] and "shadow" not in record
    # No planner at all (today's setup): none either.
    record, _ = _direct(monkeypatch, fake, _record(0.3, 0.7), planner=None)
    assert fake.requests == [] and "shadow" not in record


def test_credit_error_earlier_no_referee(monkeypatch, on):
    claude = _budget()
    planner = _planner(claude)
    claude.mark_credit_error()
    fake = Fake()
    record, _ = _direct(monkeypatch, fake, _record(0.3, 0.7), claude=claude, planner=planner)
    assert fake.requests == [] and "shadow" not in record


def test_credit_error_on_the_referee_is_never_retried(monkeypatch, on):
    fake = Fake(CreditError("Your credit balance is too low to access the Anthropic API."))
    record, claude = _direct(monkeypatch, fake, _record(0.3, 0.7))
    assert len(fake.requests) == 1 and "shadow" not in record
    assert claude.blocked and claude.credit_error_this_cycle() and claude.day_spent() == 0.0


def test_timeout_cancels_saves_nothing_and_is_charged(monkeypatch, on, caplog):
    monkeypatch.setattr(shadow, "REFEREE_TIMEOUT_SECONDS", 0.05)
    with caplog.at_level(logging.INFO):
        record, claude = _direct(monkeypatch, Fake(delay=5), _record(0.3, 0.7))
    assert "shadow" not in record
    assert claude.day_spent() == pytest.approx(frontier.claude_reserve(OPUS_55))  # the worst case, as any Claude timeout
    assert claude.in_flight.get("8", 0.0) == pytest.approx(0.0)
    assert any("referee timed out" in r.getMessage() for r in caplog.records)


def test_past_minute_40_skipped(monkeypatch, on):
    monkeypatch.setattr(main, "REFEREE_LATEST_SECONDS", 0)  # any run time is past it
    fake = Fake()
    record, claude = _direct(monkeypatch, fake, _record(0.3, 0.7))
    assert fake.requests == [] and "shadow" not in record and not claude.questions


def test_minibench_past_9_dollars_refused_no_backup(monkeypatch, on):
    fake = Fake()
    claude = _budget(day=8.80)  # + the $0.36 reservation is over $9
    record, _ = _direct(monkeypatch, fake, _record(0.3, 0.7), claude=claude, seasonal=False)
    assert fake.requests == [] and "shadow" not in record and claude.day_spent() == pytest.approx(8.80)
    # The same day, a seasonal question still gets its referee.
    fake = Fake()
    record, _ = _direct(monkeypatch, fake, _record(0.3, 0.7), claude=claude, seasonal=True)
    assert len(fake.requests) == 1 and "referee" in record["shadow"]


def test_referee_chain_has_no_backups():
    llm = _planner(_budget()).referee_llm()
    assert llm.model == OPUS_55 and llm._backup is None and llm.allowed_tries == 1 and llm._spend is not None


def test_question_cap_refuses_the_referee(monkeypatch, on):
    claude = _budget()
    claude.questions["8"] = {"spent": 0.40}
    claude.set_cap("8", frontier.CLAUDE_MINIBENCH_QUESTION_CAP)  # $0.40 + $0.36 > $0.60
    fake = Fake()
    record, _ = _direct(monkeypatch, fake, _record(0.3, 0.7), claude=claude, seasonal=False)
    assert fake.requests == [] and "shadow" not in record


# ---------------------------------------------------------------- the submitted forecast


def _rehearsal_run(monkeypatch, enabled):
    """The frontier rehearsal's live-like run (real planner, recorded replies), 4 types, seasonal."""
    import frontier_rehearsal as fr
    from replay import _RecordedAnswer

    monkeypatch.setattr(main, "REFEREE_ENABLED", enabled)
    monkeypatch.setattr(shadow, "REFEREE_ENABLED", enabled)
    monkeypatch.setattr(_RecordedAnswer, "binary_percent", {OPUS_55: 10.0})  # Opus 10%, Flash its recorded reply
    questions = [fr._question(901, "binary"), fr._question(902, "multiple_choice"), fr._question(903, "numeric"),
                 fr._question(904, "discrete")]
    records, planner, calls = fr.run_phase(questions, [], fr._budget())
    return {r["question"]["id_of_post"]: r for r in records}, planner, calls


def test_submitted_forecasts_are_the_same_with_the_referee_on_or_off(monkeypatch):
    off, _, off_calls = _rehearsal_run(monkeypatch, False)
    on_, planner, on_calls = _rehearsal_run(monkeypatch, True)
    assert set(off) == set(on_) == {901, 902, 903, 904}
    for post in off:
        assert off[post]["final_forecast"] == on_[post]["final_forecast"]
    # One more Opus call, on the binary question only (non-binary: never a referee).
    assert len(on_calls) == len(off_calls) + 1 and on_calls.count(OPUS_55) == off_calls.count(OPUS_55) + 1
    assert "referee" not in off[901].get("shadow", {})
    finished = [float(f["parsed"]) for f in on_[901]["forecasts"] if f.get("status") == "ok"]
    assert max(finished) - min(finished) > 0.15
    expected = shadow.referee_candidate(0.10, float(on_[901]["final_forecast"]), finished)
    assert on_[901]["shadow"]["referee"] == pytest.approx(expected)
    for post in (902, 903, 904):
        assert "referee" not in (on_[post].get("shadow") or {})
    assert len(planner.claude.questions["901"]["calls"]) == 3  # 2 Opus slots + the referee, in the Claude ledger
