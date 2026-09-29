"""
Run timing rehearsal (architect, 29 Sep). The REAL bot code (research queue,
planned research with its deadline, GeminiPool planning, forecasting stages,
log saving, shadow phase) on a VIRTUAL clock: every wait is simulated, so a
45-minute run takes seconds. Recorded replies: 0 model calls, 0 AskNews calls,
nothing submitted, the quota ledger and the question logs in memory.

A. Burst of 5, given in shuffled order, at every limit: Flash-Lite calls take
   100 s and AskNews 90 s (research hits 4 min, in the dossier step); in each
   question one Flash forecast never answers, the others take 5 min (the
   12-minute limit is used in full); the shadow forecaster never answers.
B. Overload of 12: research hits 4 min (AskNews 250 s); forecasts answer
   after 11.5 min (slow but no hung calls, which would get their model
   skipped for the run: 2 failures, #60); the shadow never answers.
C. Late run: the run is already at minute 25.5 when 3 questions arrive, the
   second starts just before minute 30 (the true worst case), the third is
   left for the next run; forecasting ends after minute 45, so the shadows are
   skipped.

Checks: every run ends by minute 48; no question starts after minute 30 and
the ones left for the next run are the latest-closing; every started
question is forecast, in closing order; research <= 4 min and forecasting
<= 12 min per question; and a job killed at any minute from 1 to 60 leaves
every submitted question's log saved (each log is saved at its submission).

    poetry run python timing_rehearsal.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from forecasting_tools import BinaryQuestion, DiscreteQuestion, GeneralLlm, MultipleChoiceQuestion, NumericQuestion

import bot_config
import main
import run_timing
from bot_config import GEMINI_FORECAST_MODELS, GEMINI_PARSER_MODELS
from gemini_budget import MemoryStore, current_question_key
from llm_throttle import RequestPacer
from replay import ReplayChainLlm, _RecordedAnswer

HUNG_SECONDS = 10 * 3600
TYPES = ("binary", "numeric", "discrete", "multiple_choice")
RUN_END_LIMIT_MINUTES = 48
KILL_MINUTES = range(1, 61)


@dataclass
class Scenario:
    name: str
    closes: list[int]  # minutes to close, in the (shuffled) order given
    flash_lite_seconds: float = 0
    asknews_seconds: float = 90
    flash_seconds: float = 300
    hang_one_forecast: bool = False  # per question, the first Flash call never answers
    run_minute_at_start: float = 0.0  # the run clock when the questions arrive


BURST = Scenario("A. Burst of 5 at every limit (given in shuffled order)", [178, 186, 170, 182, 174],
                 flash_lite_seconds=100, asknews_seconds=90, flash_seconds=300, hang_one_forecast=True)
OVERLOAD = Scenario("B. Overload: 12 questions at once", [200 + 7 * ((i * 5) % 12) for i in range(12)],
                    asknews_seconds=250, flash_seconds=11.5 * 60)
LATE = Scenario("C. Late run: 3 questions arrive at minute 25.5", [120, 130, 140],
                asknews_seconds=250, flash_seconds=11.9 * 60, run_minute_at_start=25.5)


class VirtualClockLoop(asyncio.SelectorEventLoop):
    """An event loop whose clock jumps ahead instead of waiting: when nothing
    is ready, time moves straight to the next timer. No real I/O or threads
    may be awaited (asyncio.to_thread is made synchronous meanwhile)."""

    def __init__(self) -> None:
        super().__init__()
        self._now = 0.0
        real_select = self._selector.select

        def select(timeout: float | None = None) -> list:
            events = real_select(0)
            if events:
                return events
            if timeout is None:
                raise RuntimeError("virtual clock: nothing left to wait for (deadlock)")
            self._now += timeout
            return []

        self._selector.select = select  # type: ignore[method-assign]

    def time(self) -> float:
        return self._now


class _HungShadow(GeneralLlm):
    def __init__(self) -> None:
        super().__init__(model="openrouter/rehearsal/hung-shadow:free", temperature=None)

    async def invoke(self, prompt: Any, system_prompt: str | None = None) -> str:
        await asyncio.sleep(HUNG_SECONDS)
        return "never"


@dataclass
class Timeline:
    research: dict = field(default_factory=dict)  # post -> [start, end] (minutes)
    forecast_done: dict = field(default_factory=dict)  # post -> minute submitted
    log_saved: dict = field(default_factory=dict)  # post -> minute its log was saved
    shadow_files: int = 0
    run_minutes: float = 0.0
    deferred: set = field(default_factory=set)
    failed: set = field(default_factory=set)
    time_limit: dict = field(default_factory=dict)  # post -> research stage cut
    shadows_skipped: bool = False


def _question(post: int, kind: str, close: datetime, now: datetime) -> Any:
    common = dict(question_text=f"Rehearsal question {post}?", id_of_post=post,
                  page_url=f"https://www.metaculus.com/questions/{post}", close_time=close,
                  open_time=now - timedelta(minutes=5))
    if kind == "binary":
        return BinaryQuestion(**common)
    if kind == "multiple_choice":
        return MultipleChoiceQuestion(options=["Red", "Green", "Blue"], **common)
    bounds = dict(unit_of_measure="units", open_lower_bound=False, zero_point=None)
    if kind == "discrete":
        return DiscreteQuestion(lower_bound=0.0, upper_bound=10.0, open_upper_bound=False, cdf_size=11, **bounds, **common)
    return NumericQuestion(lower_bound=0.0, upper_bound=1000.0, open_upper_bound=True, cdf_size=201, **bounds, **common)


def run(scenario: Scenario) -> tuple[Timeline, list]:
    """One live-like run on the virtual clock."""
    timeline = Timeline()
    now = datetime.now(timezone.utc)
    questions = [_question(70000 + i, TYPES[i % 4], now + timedelta(minutes=m), now) for i, m in enumerate(scenario.closes)]
    loop = VirtualClockLoop()
    offset = scenario.run_minute_at_start
    minutes = lambda: offset + loop.time() / 60  # noqa: E731
    hung: set = set()

    original_answer = _RecordedAnswer._mockable_direct_call_to_model
    original_research = main.run_planned_research
    original_submit = main.FallBot2026._submit_if_still_open
    original_to_thread = asyncio.to_thread
    original_hard_data = main.hard_data_for
    original_info = main.logger.info

    async def slow_answer(self, prompt):  # type: ignore[no-untyped-def]
        model = self.model.removeprefix("replay/")
        if model in GEMINI_PARSER_MODELS:
            await asyncio.sleep(scenario.flash_lite_seconds)
        elif model in GEMINI_FORECAST_MODELS:
            question = current_question_key.get()
            if scenario.hang_one_forecast and question not in hung:
                hung.add(question)
                await asyncio.sleep(HUNG_SECONDS)  # a call that never answers
            await asyncio.sleep(scenario.flash_seconds)
        return await original_answer(self, prompt)

    async def slow_asknews(query):  # type: ignore[no-untyped-def]
        await asyncio.sleep(scenario.asknews_seconds)
        return []

    async def timed_research(question, *args, **kwargs):  # type: ignore[no-untyped-def]
        timeline.research[question.id_of_post] = [minutes(), None]
        try:
            result = await original_research(
                question, *args, search_asknews=slow_asknews, search_free=lambda q: [],
                fetch_background=lambda entities: ("", []), **kwargs,
            )
        finally:
            timeline.research[question.id_of_post][1] = minutes()
        if result.time_limit_hit:
            timeline.time_limit[question.id_of_post] = result.time_limit_hit
        return result

    async def timed_submit(self, question, report):  # type: ignore[no-untyped-def]
        timeline.forecast_done[question.id_of_post] = minutes()
        return True

    async def sync_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    class LogRecorder:
        saved: list = []

        def save(self, path: str, record: dict) -> bool:
            if path.endswith("_shadows.json"):
                timeline.shadow_files += 1
            else:
                timeline.log_saved[record["question"]["id_of_post"]] = minutes()
            return True

    def watch_info(message, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(message).startswith("shadows skipped: time"):
            timeline.shadows_skipped = True
        return original_info(message, *args, **kwargs)

    _RecordedAnswer._mockable_direct_call_to_model = slow_answer
    main.run_planned_research = timed_research
    main.FallBot2026._submit_if_still_open = timed_submit
    asyncio.to_thread = sync_to_thread
    main.hard_data_for = lambda question: None
    main.logger.info = watch_info
    _RecordedAnswer.binary_percent = {}
    try:
        pool = bot_config._gemini_pool(MemoryStore(), llm_class=ReplayChainLlm)
        pool.pacers = {m: RequestPacer(60000) for m in pool.pacers}
        parser = pool.parser()
        bot = main.FallBot2026(
            llms={"default": pool.unplanned_forecaster(), "parser": parser, "summarizer": parser,
                  "researcher": "planned-research"},
            publish_reports_to_metaculus=False, enable_summarize_research=False,
            predictions_per_research_report=3, required_successful_predictions=0,
        )
        bot.planner = pool
        bot.question_log = LogRecorder()
        bot.run_mode = "test_questions"
        bot.shadow_llm = _HungShadow()
        bot.run_started = -offset * 60  # the virtual clock starts at 0
        queued, bot.seasonal_by_post = main.run_queue([], questions)
        reports = loop.run_until_complete(bot.forecast_questions(queued, return_exceptions=True))
        timeline.run_minutes = minutes()
        timeline.deferred = set(bot.__dict__.get("deferred_posts", set()))
        timeline.failed = {q.id_of_post for q, r in zip(queued, reports) if isinstance(r, BaseException)}
        return timeline, questions
    finally:
        _RecordedAnswer._mockable_direct_call_to_model = original_answer
        main.run_planned_research = original_research
        main.FallBot2026._submit_if_still_open = original_submit
        asyncio.to_thread = original_to_thread
        main.hard_data_for = original_hard_data
        main.logger.info = original_info
        loop.close()


def _closing_order(questions: list) -> list[int]:
    return [q.id_of_post for q in sorted(questions, key=lambda q: q.close_time)]


def killed_at(timeline: Timeline, minute: float) -> set:
    """Questions submitted by `minute` whose log was NOT saved by then."""
    return {p for p, t in timeline.forecast_done.items() if t <= minute and timeline.log_saved.get(p, 1e9) > minute}


def check(timeline: Timeline, questions: list) -> tuple[bool, list[str]]:
    posts = _closing_order(questions)
    started = [p for p in posts if p in timeline.research]
    cutoff = run_timing.START_CUTOFF_SECONDS / 60
    lost = {m: killed_at(timeline, m) for m in KILL_MINUTES}
    checks = [
        (f"Run ends by minute {RUN_END_LIMIT_MINUTES} (ends at {timeline.run_minutes:.1f})",
         timeline.run_minutes <= RUN_END_LIMIT_MINUTES),
        (f"No question starts after minute {cutoff:.0f} (last start {max(s for s, _ in timeline.research.values()):.1f})",
         all(s < cutoff for s, _ in timeline.research.values())),
        (f"Left for the next run: {len(timeline.deferred)}, the latest-closing ones",
         timeline.deferred == set(posts[len(started):])),
        (f"Every started question forecast ({len(timeline.forecast_done)} of {len(started)}), in closing order",
         set(timeline.forecast_done) == set(started) and not (timeline.failed - timeline.deferred)
         and sorted(timeline.forecast_done, key=timeline.forecast_done.get) == started),
        ("Research at most 4 min, every question",
         all(end - start <= run_timing.RESEARCH_LIMIT_SECONDS / 60 + 1e-6 for start, end in timeline.research.values())),
        ("Forecasting at most 12 min after research, every question",
         all(timeline.forecast_done[p] - timeline.research[p][1] <= run_timing.FORECAST_STAGES_LIMIT_SECONDS / 60 + 0.1
             for p in timeline.forecast_done)),
        ("Job killed at any minute 1-60: every submitted question's log already saved",
         not any(lost.values())),
    ]
    return all(ok for _, ok in checks), [f"- {text}: **{'yes' if ok else 'NO'}**" for text, ok in checks]


def table(timeline: Timeline, questions: list) -> list[str]:
    by_post = {q.id_of_post: q for q in questions}
    first_close = min(q.close_time for q in questions)
    lines = ["| Question | Type | Closes (min after the first) | Research (run minute) | Research cut in | Submitted | Log saved |",
             "|---|---|---|---|---|---|---|"]
    for post in _closing_order(questions):
        q = by_post[post]
        research = timeline.research.get(post)
        done = timeline.forecast_done.get(post)
        saved = timeline.log_saved.get(post)
        lines.append(
            f"| {post} | {q.question_type} | {int((q.close_time - first_close).total_seconds() // 60)} | "
            + (f"{research[0]:.1f} - {research[1]:.1f}" if research else "not started")
            + f" | {timeline.time_limit.get(post, '-')} | "
            + (f"{done:.1f}" if done is not None else ("next run" if post in timeline.deferred else "-"))
            + f" | {f'{saved:.1f}' if saved is not None else '-'} |"
        )
    return lines


def describe(s: Scenario) -> str:
    parts = [f"research: Flash-Lite {s.flash_lite_seconds:.0f} s, AskNews {s.asknews_seconds:.0f} s per call",
             f"forecasts {s.flash_seconds / 60:.1f} min" + (", one hung per question" if s.hang_one_forecast else ""),
             "shadow forecaster never answers"]
    return "Forced: " + "; ".join(parts) + "."


def report() -> tuple[bool, str]:
    lines = ["## Run timing rehearsal (virtual clock, recorded replies, 0 model calls)", ""]
    ok_all = True
    for scenario in (BURST, OVERLOAD, LATE):
        timeline, questions = run(scenario)
        ok, checks = check(timeline, questions)
        ok_all = ok_all and ok
        shadows = ("skipped (\"shadows skipped: time\")" if timeline.shadows_skipped
                   else f"{timeline.shadow_files} shadow file(s) saved at the end")
        lines += [f"### {scenario.name}", "", describe(scenario), "", *table(timeline, questions), "", *checks,
                  f"- Shadows: {shadows}. Whole run: **{timeline.run_minutes:.1f} min**", ""]
    lines.append(
        f"Worst case: a question starting just before minute {run_timing.START_CUTOFF_SECONDS // 60} ends by "
        f"{run_timing.START_CUTOFF_SECONDS // 60 + 16}; shadows start only before minute "
        f"{run_timing.SHADOW_START_BEFORE_SECONDS // 60} and end by {run_timing.SHADOW_PHASE_END_SECONDS // 60}."
    )
    return ok_all, "\n".join(lines)


if __name__ == "__main__":
    ok, text = report()
    print(text)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    sys.exit(0 if ok else 1)
