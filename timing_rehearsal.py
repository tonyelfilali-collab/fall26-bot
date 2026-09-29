"""
Run timing rehearsal (architect, 29 Sep). The REAL bot code (research queue,
planned research with its deadline, GeminiPool planning, forecasting stages,
shadow phase) on a VIRTUAL clock: every wait is simulated, so a 30-minute run
takes seconds. Recorded replies: 0 model calls, 0 AskNews calls, nothing
submitted, the quota ledger in memory.

Forced slow research and timeouts:
- every Flash-Lite call (research planner, dossier writer) takes 100 s and
  every AskNews search 90 s, so each question's research hits the 4-minute
  limit (in the dossier step) and is forecast with what was gathered;
- in each question, the first Flash forecast never answers (a hung call), the
  others answer after 5 minutes, so every question uses its full 12 minutes;
- the shadow forecaster never answers.

Scenario A: a burst of 5 questions, given in shuffled order. Checks: the run
ends within 35 minutes, every question is forecast, the soonest-closing
question is done first (done in closing order), research <= 4 min and
forecasting <= 12 min per question.
Scenario B (overload): 12 questions; research hits its limit through a slow
AskNews search (250 s), forecasts answer in 5 minutes (no hung calls, which
would get their model skipped for the run: 2 failures, #60). Checks: no
question starts after 40 minutes; the ones left for the next run are the
latest-closing; every started question is forecast.

    poetry run python timing_rehearsal.py
"""
from __future__ import annotations

import asyncio
import functools
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

FLASH_LITE_SECONDS = 100
ASKNEWS_SECONDS = 90
OVERLOAD_ASKNEWS_SECONDS = 250
FLASH_SECONDS = 300
HUNG_SECONDS = 10 * 3600
TYPES = ("binary", "numeric", "discrete", "multiple_choice")
# Minutes to close, in the (shuffled) order the questions are given.
BURST_CLOSES = [178, 186, 170, 182, 174]
OVERLOAD_CLOSES = [200 + 7 * ((i * 5) % 12) for i in range(12)]
RUN_LIMIT_MINUTES = 35


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
    forecast_done: dict = field(default_factory=dict)  # post -> minutes
    run_minutes: float = 0.0
    deferred: set = field(default_factory=set)
    failed: set = field(default_factory=set)
    time_limit: dict = field(default_factory=dict)  # post -> research stage cut


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


def run(closes: list[int], overload: bool = False) -> tuple[Timeline, list]:
    """One live-like run on the virtual clock; closes = minutes to close.
    overload: slow AskNews only (Flash-Lite instant, no hung forecasts)."""
    timeline = Timeline()
    now = datetime.now(timezone.utc)
    questions = [_question(70000 + i, TYPES[i % 4], now + timedelta(minutes=m), now) for i, m in enumerate(closes)]
    loop = VirtualClockLoop()
    minutes = lambda: loop.time() / 60  # noqa: E731
    hung: set = set()

    original_answer = _RecordedAnswer._mockable_direct_call_to_model
    original_research = main.run_planned_research
    original_submit = main.FallBot2026._submit_if_still_open
    original_to_thread = asyncio.to_thread
    original_hard_data = main.hard_data_for

    async def slow_answer(self, prompt):  # type: ignore[no-untyped-def]
        model = self.model.removeprefix("replay/")
        if model in GEMINI_PARSER_MODELS and not overload:
            await asyncio.sleep(FLASH_LITE_SECONDS)
        elif model in GEMINI_FORECAST_MODELS:
            question = current_question_key.get()
            if question not in hung and not overload:
                hung.add(question)
                await asyncio.sleep(HUNG_SECONDS)  # a call that never answers
            await asyncio.sleep(FLASH_SECONDS)
        return await original_answer(self, prompt)

    async def slow_asknews(query):  # type: ignore[no-untyped-def]
        await asyncio.sleep(OVERLOAD_ASKNEWS_SECONDS if overload else ASKNEWS_SECONDS)
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
        return False

    async def sync_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    _RecordedAnswer._mockable_direct_call_to_model = slow_answer
    main.run_planned_research = timed_research
    main.FallBot2026._submit_if_still_open = timed_submit
    asyncio.to_thread = sync_to_thread
    main.hard_data_for = lambda question: None
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
        bot.question_log = None
        bot.keep_records = True
        bot.run_mode = "test_questions"
        bot.shadow_llm = _HungShadow()
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
        loop.close()


def _closing_order(questions: list) -> list[int]:
    return [q.id_of_post for q in sorted(questions, key=lambda q: q.close_time)]


def check_burst(timeline: Timeline, questions: list) -> tuple[bool, list[str]]:
    posts = _closing_order(questions)
    done_order = sorted(timeline.forecast_done, key=timeline.forecast_done.get)
    research_ok = all(end - start <= run_timing.RESEARCH_LIMIT_SECONDS / 60 + 1e-6 for start, end in timeline.research.values())
    stage_ok = all(
        timeline.forecast_done[p] - timeline.research[p][1] <= run_timing.FORECAST_STAGES_LIMIT_SECONDS / 60 + 0.1
        for p in timeline.forecast_done
    )
    checks = [
        (f"Run ends within {RUN_LIMIT_MINUTES} min ({timeline.run_minutes:.1f} min)", timeline.run_minutes < RUN_LIMIT_MINUTES),
        (f"Every question forecast ({len(timeline.forecast_done)} of {len(posts)})", set(timeline.forecast_done) == set(posts) and not timeline.failed),
        ("Soonest-closing question done first; all done in closing order", done_order == posts),
        ("Research hit the 4-min limit and stopped there, every question", research_ok and set(timeline.time_limit) == set(posts)),
        ("Forecasting stages within 12 min, every question", stage_ok),
    ]
    return all(ok for _, ok in checks), [f"- {text}: **{'yes' if ok else 'NO'}**" for text, ok in checks]


def check_overload(timeline: Timeline, questions: list) -> tuple[bool, list[str]]:
    posts = _closing_order(questions)
    started = [p for p in posts if p in timeline.research]
    latest = set(posts[len(started):])
    checks = [
        (f"No question starts after {run_timing.START_CUTOFF_SECONDS // 60} min "
         f"(last start {max(s for s, _ in timeline.research.values()):.1f} min)",
         all(s < run_timing.START_CUTOFF_SECONDS / 60 for s, _ in timeline.research.values())),
        (f"Left for the next run: {len(timeline.deferred)}, the latest-closing ones", bool(timeline.deferred) and timeline.deferred == latest),
        (f"Every started question forecast ({len(timeline.forecast_done)} of {len(started)})", set(timeline.forecast_done) == set(started)),
        ("Started in closing order", started == posts[: len(started)] and sorted(started, key=lambda p: timeline.research[p][0]) == started),
    ]
    return all(ok for _, ok in checks), [f"- {text}: **{'yes' if ok else 'NO'}**" for text, ok in checks]


def table(timeline: Timeline, questions: list) -> list[str]:
    by_post = {q.id_of_post: q for q in questions}
    first_close = min(q.close_time for q in questions)
    lines = ["| Question | Type | Closes (min after the first) | Research (min) | Research cut in | Forecast done (min) |", "|---|---|---|---|---|---|"]
    for post in _closing_order(questions):
        q = by_post[post]
        research = timeline.research.get(post)
        lines.append(
            f"| {post} | {q.question_type} | {int((q.close_time - first_close).total_seconds() // 60)} | "
            + (f"{research[0]:.1f} - {research[1]:.1f}" if research else "not started")
            + f" | {timeline.time_limit.get(post, '-')} | "
            + (f"{timeline.forecast_done[post]:.1f}" if post in timeline.forecast_done else ("next run" if post in timeline.deferred else "-"))
            + " |"
        )
    return lines


def report() -> tuple[bool, str]:
    burst, burst_questions = run(BURST_CLOSES)
    ok_a, lines_a = check_burst(burst, burst_questions)
    overload, overload_questions = run(OVERLOAD_CLOSES, overload=True)
    ok_b, lines_b = check_overload(overload, overload_questions)
    text = "\n".join([
        "## Run timing rehearsal (virtual clock, recorded replies, 0 model calls)",
        "",
        f"Forced: Flash-Lite calls {FLASH_LITE_SECONDS} s and AskNews {ASKNEWS_SECONDS} s each (research hits the "
        f"4-min limit); in each question one Flash forecast never answers, the others take {FLASH_SECONDS // 60} min "
        "(the 12-min limit is used in full); the shadow forecaster never answers.",
        "",
        "### A. Burst of 5 (given in shuffled order)",
        "",
        *table(burst, burst_questions),
        "",
        *lines_a,
        f"- Whole run: **{burst.run_minutes:.1f} min** (shadow phase ends by minute "
        f"{run_timing.SHADOW_PHASE_END_SECONDS // 60})",
        "",
        "### B. Overload: 12 questions at once",
        "",
        f"Research hits the 4-min limit through a {OVERLOAD_ASKNEWS_SECONDS} s AskNews search; forecasts take "
        f"{FLASH_SECONDS // 60} min.",
        "",
        *table(overload, overload_questions),
        "",
        *lines_b,
        f"- Whole run: **{overload.run_minutes:.1f} min** (the last started question finishes; shadows skipped). "
        f"Worst case: a question starting just before minute {run_timing.START_CUTOFF_SECONDS // 60} with "
        f"4-min research and 12-min forecasting ends by minute {run_timing.START_CUTOFF_SECONDS // 60 + 16}.",
    ])
    return ok_a and ok_b, text


if __name__ == "__main__":
    ok, text = report()
    print(text)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    sys.exit(0 if ok else 1)
