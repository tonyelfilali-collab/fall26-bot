"""The referee shadow (shadow.py + frontier.referee_llm + main._referee_later /
_referee_at_end): at the end of the run, saved in '<log>_shadows.json'; fake
SDK replies (0 real calls)."""
from __future__ import annotations

import asyncio
import copy
import logging
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from forecasting_tools import BinaryQuestion

import frontier
import main
import run_timing
import shadow
from frontier import OPUS_55, ClaudeBudget
from gemini_budget import MemoryStore, current_question_key
from question_log import record_path, shadows_path
from spend import SpendGuard, question_seasonal, utc_day

RESET = date(2026, 10, 14)
STARTED = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


class CreditError(Exception):
    status_code = 400


class Fake:
    """anthropic.AsyncAnthropic stand-in: every request gets `reply` (or raises it) after `delay` seconds."""

    def __init__(self, reply=None, delay=0.0, events=None):
        self.reply, self.delay, self.requests, self.events = reply, delay, [], events

    def __call__(self, **kwargs):
        fake = self

        class Messages:
            async def create(self, **request):
                fake.requests.append(request)
                if fake.events is not None:
                    fake.events.append("referee call")
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


class Recorder:
    """A question-log writer that keeps a copy of each save (and its order in `events`)."""

    def __init__(self, events=None):
        self.files: dict[str, dict] = {}
        self.events = events if events is not None else []

    def save(self, path, record):
        self.files[path] = copy.deepcopy(record)
        self.events.append(f"saved {path}")
        return True


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
    bot.question_log = Recorder()
    return bot


def _record(*probs):
    return {"question": {"id_of_post": 8, "question_type": "binary"},
            "forecasts": [{"status": "ok", "parsed": p, "raw_output": f"Reason {i}.\nProbability: {p:.0%}"} for i, p in enumerate(probs)]}


Q = BinaryQuestion(question_text="Will X?", id_of_post=8, page_url="https://www.metaculus.com/questions/8")
LOG = record_path("tournament", Q, STARTED)


def _direct(monkeypatch, fake, record, claude=None, seasonal=True, submitted=0.5, planner="make", elapsed=0.0):
    """One question queued in its own task (its contextvars), then the end of
    the run outside it, with the OTHER tournament's contextvars; returns
    (record, budget, the '<log>_shadows.json' payload or None)."""
    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", fake)
    claude = _budget() if claude is None else claude
    bot = _bot(_planner(claude) if planner == "make" else planner)

    async def question_task():
        current_question_key.set("8")
        question_seasonal.set(seasonal)
        main._question_seasonal.set(seasonal)
        bot._referee_later(Q, record, submitted, STARTED)

    async def go():
        bot._run_elapsed()
        bot.run_started -= elapsed
        await asyncio.create_task(question_task())
        current_question_key.set("other")
        question_seasonal.set(not seasonal)
        main._question_seasonal.set(not seasonal)
        await bot._finish_run()

    asyncio.run(go())
    return record, claude, bot.question_log.files.get(shadows_path(LOG))


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
    assert shadow.REFEREE_TIMEOUT_SECONDS == 240 and not hasattr(shadow, "REFEREE_LATEST_SECONDS")
    assert run_timing.SHADOW_START_BEFORE_SECONDS == 45 * 60 and run_timing.SHADOW_PHASE_END_SECONDS == 47 * 60


# ---------------------------------------------------------------- when it runs


def test_disagree_calls_opus_and_saves_in_the_shadows_file(monkeypatch, on, caplog):
    fake = Fake()
    with caplog.at_level(logging.INFO):
        record, claude, saved = _direct(monkeypatch, fake, _record(0.3, 0.5, 0.7), submitted=0.5)
    [request] = fake.requests
    assert request["model"] == "claude-opus-5-5" and request["output_config"] == {"effort": "high"}
    assert "Forecaster 1: 30%" in request["messages"][0]["content"]
    # clip(mean(90%, 50%), 30%, 70%) = 70%, in '<log>_shadows.json', not in the question's log
    assert saved["log"] == LOG and saved["shadow"] == {"referee": pytest.approx(0.7)}
    assert saved["referee"]["status"] == "saved" and saved["referee"]["cost"] > 0 and "shadow_model" not in saved
    assert "shadow" not in record and "referee" not in record
    [call] = claude.questions["8"]["calls"]  # in the Claude ledger like any Claude call, under the question's key
    assert call["model"] == OPUS_55 and call["ok"] and call["charged"] > 0
    assert claude.day_spent() == pytest.approx(call["charged"])
    assert "other" not in claude.questions
    report = _planner(claude)
    report.calls_before = {}  # this run's calls: the referee's
    assert "| 8 | claude-opus-5-5 |" in frontier.frontier_report(report)
    public = " ".join(r.getMessage() for r in caplog.records if r.name == "fall26" and "referee" in r.getMessage())
    assert "Question 8: referee saved, $" in public and "70" not in public.replace("$", "")


@pytest.mark.parametrize("probs", [(0.3, 0.4, 0.42), (0.3,), ()])
def test_agree_or_too_few_forecasts_no_call(monkeypatch, on, probs):
    fake = Fake()
    record, claude, saved = _direct(monkeypatch, fake, _record(*probs))
    assert fake.requests == [] and saved is None and not claude.questions


def test_failed_forecasts_do_not_count(monkeypatch, on):
    record = _record(0.3)
    record["forecasts"].append({"status": "failed"})
    fake = Fake()
    _, _, saved = _direct(monkeypatch, fake, record)
    assert fake.requests == [] and saved is None


def test_claude_off_no_referee(monkeypatch, on):
    fake = Fake()
    _, _, saved = _direct(monkeypatch, fake, _record(0.3, 0.7), planner=_planner(None))
    assert fake.requests == [] and saved is None
    # No planner at all (today's setup): none either.
    _, _, saved = _direct(monkeypatch, fake, _record(0.3, 0.7), planner=None)
    assert fake.requests == [] and saved is None


def test_credit_error_earlier_no_referee(monkeypatch, on):
    claude = _budget()
    planner = _planner(claude)
    claude.mark_credit_error()
    fake = Fake()
    _, _, saved = _direct(monkeypatch, fake, _record(0.3, 0.7), claude=claude, planner=planner)
    assert fake.requests == [] and saved is None


def test_credit_error_on_the_referee_is_never_retried(monkeypatch, on):
    fake = Fake(CreditError("Your credit balance is too low to access the Anthropic API."))
    _, claude, saved = _direct(monkeypatch, fake, _record(0.3, 0.7))
    assert len(fake.requests) == 1 and "referee" not in saved["shadow"]
    assert claude.blocked and claude.credit_error_this_cycle() and claude.day_spent() == 0.0


def test_timeout_cancels_saves_no_value_and_is_charged(monkeypatch, on, caplog):
    monkeypatch.setattr(main, "REFEREE_TIMEOUT_SECONDS", 0.05)
    with caplog.at_level(logging.INFO):
        _, claude, saved = _direct(monkeypatch, Fake(delay=5), _record(0.3, 0.7))
    assert saved["referee"]["status"] == "timed out" and "referee" not in saved["shadow"]
    assert claude.day_spent() == pytest.approx(frontier.claude_reserve(OPUS_55))  # the worst case, as any Claude timeout
    assert claude.in_flight.get("8", 0.0) == pytest.approx(0.0)
    assert any("referee timed out" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("minute", [45, 46, 50])
def test_at_or_after_minute_45_skipped(monkeypatch, on, minute, caplog):
    fake = Fake()
    with caplog.at_level(logging.INFO):
        _, claude, saved = _direct(monkeypatch, fake, _record(0.3, 0.7), elapsed=minute * 60)
    assert fake.requests == [] and saved is None and not claude.questions
    assert "Question 8: referee skipped (run at minute" in caplog.text


@pytest.mark.parametrize("minute, limit", [(0, 240), (40, 240), (43, 240), (44, 180), (44.5, 150)])
def test_each_call_within_240_s_or_the_time_left_until_minute_47(monkeypatch, on, minute, limit):
    seen = []
    real = main.referee_shadow

    async def spy(*args, timeout):
        seen.append(timeout)
        return await real(*args, timeout=timeout)

    monkeypatch.setattr(main, "referee_shadow", spy)
    _, _, saved = _direct(monkeypatch, Fake(), _record(0.3, 0.7), elapsed=minute * 60)
    [timeout] = seen
    assert timeout == pytest.approx(limit, abs=1.0) and saved["referee"]["status"] == "saved"


def test_minibench_follows_the_9_dollar_rule_at_the_end_of_the_run(monkeypatch, on):
    # The end of the run happens in the seasonal context: the MiniBench question's own flag still applies.
    fake = Fake()
    claude = _budget(day=8.80)  # + the $0.36 reservation is over $9
    _, _, saved = _direct(monkeypatch, fake, _record(0.3, 0.7), claude=claude, seasonal=False)
    assert fake.requests == [] and saved["referee"]["status"] == "refused (cap)" and "referee" not in saved["shadow"]
    assert claude.day_spent() == pytest.approx(8.80)
    # The same day, a seasonal question (ended in the MiniBench context) still gets its referee.
    fake = Fake()
    _, _, saved = _direct(monkeypatch, fake, _record(0.3, 0.7), claude=claude, seasonal=True)
    assert len(fake.requests) == 1 and "referee" in saved["shadow"]


def test_unknown_tournament_fails_closed_to_minibench(monkeypatch, on):
    fake = Fake()
    claude = _budget(day=8.80)
    _, _, saved = _direct(monkeypatch, fake, _record(0.3, 0.7), claude=claude, seasonal=None)
    assert fake.requests == [] and saved["referee"]["status"] == "refused (cap)"


def test_referee_chain_has_no_backups():
    llm = _planner(_budget()).referee_llm()
    assert llm.model == OPUS_55 and llm._backup is None and llm.allowed_tries == 1 and llm._spend is not None


def test_question_cap_refuses_the_referee(monkeypatch, on):
    claude = _budget()
    claude.questions["8"] = {"spent": 0.40}
    claude.set_cap("8", frontier.CLAUDE_MINIBENCH_QUESTION_CAP)  # $0.40 + $0.36 > $0.60
    fake = Fake()
    _, _, saved = _direct(monkeypatch, fake, _record(0.3, 0.7), claude=claude, seasonal=False)
    assert fake.requests == [] and "referee" not in saved["shadow"]


def test_the_question_log_is_saved_before_any_referee_call(monkeypatch, on):
    """The whole question path: forecast, submit, save the log, queue; the referee only at the end."""
    import anthropic

    events: list[str] = []
    monkeypatch.setattr(anthropic, "AsyncAnthropic", Fake(events=events))

    async def forecast(self, question):
        self._record_for(question)["forecasts"] = _record(0.3, 0.7)["forecasts"]
        return SimpleNamespace(prediction=0.5, minutes_taken=1.0, price_estimate=0.0)

    async def submit(self, question, report):
        events.append("submitted")
        return True

    monkeypatch.setattr(main.ForecastBot, "_run_individual_question", forecast)
    monkeypatch.setattr(main.FallBot2026, "_submit_if_still_open", submit)
    bot = _bot(_planner(_budget()))
    bot.question_log = Recorder(events)

    async def go():
        current_question_key.set("8")
        question_seasonal.set(True)
        await bot._run_question_timed(Q)
        events.append("question done")
        await bot._finish_run()

    asyncio.run(go())
    [log] = [e.removeprefix("saved ") for e in events if e.startswith("saved ") and not e.endswith("_shadows.json")]
    assert events == ["submitted", f"saved {log}", "question done", "referee call", f"saved {shadows_path(log)}"]
    assert "referee" not in bot.question_log.files[log].get("shadow", {})
    assert bot.question_log.files[shadows_path(log)]["shadow"]["referee"] == pytest.approx(0.7)  # clip(mean(90, 50), 30, 70)


def test_shadow_forecaster_and_referee_share_one_shadows_file(monkeypatch, on):
    """Both at the end of the run for the same question: one file with both."""
    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", Fake())
    bot = _bot(_planner(_budget()))
    record = _record(0.3, 0.7)

    async def shadow_forecast(self, question, record, timeout):
        record["shadow_model"] = {"model": "m:free", "status": "ok", "seconds": 1.0}
        record.setdefault("shadow", {})[main.SHADOW_VARIANT] = 0.4

    monkeypatch.setattr(main.FallBot2026, "_finish_shadow_forecast", shadow_forecast)

    async def go():
        current_question_key.set("8")
        question_seasonal.set(True)
        bot.__dict__.setdefault("_pending_shadows", []).append((Q, record, STARTED))
        bot._referee_later(Q, record, 0.5, STARTED)
        await bot._finish_run()

    asyncio.run(go())
    assert list(bot.question_log.files) == [shadows_path(LOG)]
    saved = bot.question_log.files[shadows_path(LOG)]
    assert saved["shadow_model"]["status"] == "ok" and saved["referee"]["status"] == "saved"
    assert set(saved["shadow"]) == {main.SHADOW_VARIANT, "referee"}


def test_scoreboard_merges_the_referee_into_its_log():
    import scoreboard

    merged = scoreboard.merge_shadow_files({
        LOG: {"question": {"id_of_post": 8}, "shadow": {"mean": 0.5}},
        shadows_path(LOG): {"referee": {"status": "saved"}, "shadow": {"referee": 0.6}},
    })
    assert merged == [{"question": {"id_of_post": 8}, "shadow": {"mean": 0.5, "referee": 0.6}, "referee": {"status": "saved"}}]


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
    log = Recorder()
    records, planner, calls = fr.run_phase(questions, [], fr._budget(), question_log=log)
    return {r["question"]["id_of_post"]: r for r in records}, planner, calls, log


def test_submitted_forecasts_are_the_same_with_the_referee_on_or_off(monkeypatch):
    off, _, off_calls, off_log = _rehearsal_run(monkeypatch, False)
    on_, planner, on_calls, on_log = _rehearsal_run(monkeypatch, True)
    assert set(off) == set(on_) == {901, 902, 903, 904}
    for post in off:
        assert off[post]["final_forecast"] == on_[post]["final_forecast"]
        assert "referee" not in (on_[post].get("shadow") or {})  # never in the question's log
    # One more Opus call, on the binary question only (non-binary: never a referee).
    assert len(on_calls) == len(off_calls) + 1 and on_calls.count(OPUS_55) == off_calls.count(OPUS_55) + 1
    assert not [p for p in off_log.files if p.endswith("_shadows.json")]
    [extra] = [p for p in on_log.files if p.endswith("_shadows.json")]
    assert "/901_" in extra and on_log.events.index(f"saved {on_log.files[extra]['log']}") < on_log.events.index(f"saved {extra}")
    finished = [float(f["parsed"]) for f in on_[901]["forecasts"] if f.get("status") == "ok"]
    assert max(finished) - min(finished) > 0.15
    expected = shadow.referee_candidate(0.10, float(on_[901]["final_forecast"]), finished)
    assert on_log.files[extra]["shadow"]["referee"] == pytest.approx(expected)
    assert len(planner.claude.questions["901"]["calls"]) == 3  # 2 Opus slots + the referee, in the Claude ledger
