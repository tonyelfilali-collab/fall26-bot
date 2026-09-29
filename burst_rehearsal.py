"""
MiniBench burst rehearsal (architect, 29 Sep, NEXT 1b). A simulation on a
virtual clock with the REAL bot code (GeminiPool planning, quota ledger and
its rules, retries and backoff, queue order, backup chain) and recorded
replies: 0 real model calls, nothing submitted, the ledger in memory.

- 60 MiniBench questions over 3 days, in bursts of 5 (every 6 hours, 4 minutes
  apart), each open 3 hours; plus 2 seasonal questions a day, open 24 hours.
- A live run every 10 minutes; each run starts fresh (new pool, new bot, the
  ledger and retry state loaded from their stores), like the real workflow.
- 30% of Gemini Flash calls answer 503 (Google overloaded); Flash-Lite and
  Nemotron answer. Each question's research costs 2 Flash-Lite calls
  (planner and dossier), as live.
- Time: each run builds its question objects with close times moved to "now
  + the virtual time left", so every deadline check in the bot is right; the
  quota day and the expected-questions rule use the virtual clock.

Checks: every question gets a real forecast (Flash, or the Nemotron backup)
before it closes; the Flash-Lite reserve is never used outside a question's
last 45 minutes (research is never starved); seasonal questions keep their
share. Prints a report; exits 1 if a check fails.

    poetry run python burst_rehearsal.py
"""
from __future__ import annotations

import asyncio
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import litellm
from forecasting_tools import BinaryQuestion, DiscreteQuestion, MultipleChoiceQuestion, NumericQuestion

import bot_config
import gemini_budget
import main
from bot_config import GEMINI_FORECAST_MODELS, GEMINI_PARSER_MODELS, GeminiPool
from gemini_budget import MemoryStore
from llm_throttle import RequestPacer
from replay import REPLAY_RESEARCH, ReplayChainLlm, _RecordedAnswer

START = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
DAYS = 3
RUN_EVERY = timedelta(minutes=10)
MINIBENCH_QUESTIONS = 60
BURST = 5
BURST_EVERY = timedelta(hours=6)
BURST_SPACING = timedelta(minutes=4)
MINIBENCH_OPEN = timedelta(hours=3)
SEASONAL_PER_DAY = 2
SEASONAL_OPEN = timedelta(hours=24)
FLASH_503_RATE = 0.30
SEED = 29
TYPES = ("binary", "numeric", "discrete", "multiple_choice")


@dataclass
class SimQuestion:
    post: int
    kind: str  # question type
    seasonal: bool
    opens: datetime
    closes: datetime
    done_at: datetime | None = None
    path: str = ""
    forecasts: int = 0
    attempts: int = 0

    def build(self, now_virtual: datetime, now_real: datetime) -> Any:
        close = now_real + (self.closes - now_virtual)
        common = dict(question_text=f"Rehearsal question {self.post}?", id_of_post=self.post,
                      page_url=f"https://www.metaculus.com/questions/{self.post}", close_time=close,
                      open_time=now_real - (now_virtual - self.opens))
        if self.kind == "binary":
            return BinaryQuestion(**common)
        if self.kind == "multiple_choice":
            return MultipleChoiceQuestion(options=["Red", "Green", "Blue"], **common)
        bounds = dict(unit_of_measure="units", open_lower_bound=False, zero_point=None)
        if self.kind == "discrete":
            return DiscreteQuestion(lower_bound=0.0, upper_bound=10.0, open_upper_bound=False, cdf_size=11, **bounds, **common)
        return NumericQuestion(lower_bound=0.0, upper_bound=1000.0, open_upper_bound=True, cdf_size=201, **bounds, **common)


def make_questions() -> list[SimQuestion]:
    questions = []
    for i in range(MINIBENCH_QUESTIONS):
        burst, slot = divmod(i, BURST)
        opens = START + burst * BURST_EVERY + slot * BURST_SPACING
        questions.append(SimQuestion(50000 + i, TYPES[i % 4], False, opens, opens + MINIBENCH_OPEN))
    for day in range(DAYS):
        for j in range(SEASONAL_PER_DAY):
            opens = START + timedelta(days=day, hours=13 + 5 * j)
            questions.append(SimQuestion(60000 + day * 10 + j, TYPES[(day + j) % 4], True, opens, opens + SEASONAL_OPEN))
    return questions


@dataclass
class Stats:
    flash_lite_reserve_misuse: int = 0  # Flash-Lite reserve used outside a last-45-minute question
    reserve_uses: dict = field(default_factory=dict)  # (day, family) -> count
    day_used: dict = field(default_factory=dict)  # day -> {model: used}


def run(seed: int = SEED, days: int = DAYS) -> tuple[list[SimQuestion], Stats]:
    rng = random.Random(seed)
    virtual = [START]
    stats = Stats()
    real_quota_day = gemini_budget.quota_day
    original_answer = _RecordedAnswer._mockable_direct_call_to_model
    original_research = main.FallBot2026._run_research_unguarded
    original_sleep = asyncio.sleep
    original_start = gemini_budget.QuotaLedger.start
    in_window: dict[str, bool] = {}

    async def flaky(self, prompt):  # type: ignore[no-untyped-def]
        model = self.model.removeprefix("replay/")
        if model in GEMINI_FORECAST_MODELS and rng.random() < FLASH_503_RATE:
            raise litellm.ServiceUnavailableError(message="rehearsal: overloaded", llm_provider="gemini", model=model)
        return await original_answer(self, prompt)

    async def research(self, question):  # type: ignore[no-untyped-def]
        # As live: the planner and the dossier writer are 2 Flash-Lite calls.
        parser = self.get_llm("parser", "llm")
        await parser.invoke("plan")
        await parser.invoke("dossier")
        return REPLAY_RESEARCH

    async def no_wait(seconds):  # type: ignore[no-untyped-def]
        await original_sleep(0)

    def watched_start(self, model, booked, allow_reserve):  # type: ignore[no-untyped-def]
        usable_before = self.usable_left(model)
        started = original_start(self, model, booked, allow_reserve)
        if started and usable_before <= 0:
            family = "flash-lite" if model in GEMINI_PARSER_MODELS else "flash"
            key = (self.day, family)
            stats.reserve_uses[key] = stats.reserve_uses.get(key, 0) + 1
            question = gemini_budget.current_question_key.get()
            if family == "flash-lite" and not in_window.get(question or "", False):
                stats.flash_lite_reserve_misuse += 1
        return started

    gemini_budget.quota_day = lambda now=None: real_quota_day(now or virtual[0])
    _RecordedAnswer._mockable_direct_call_to_model = flaky
    main.FallBot2026._run_research_unguarded = research
    asyncio.sleep = no_wait
    gemini_budget.QuotaLedger.start = watched_start
    _RecordedAnswer.binary_percent = {}
    try:
        questions = make_questions()
        ledger_store = MemoryStore()
        retry_state: dict = {}
        steps = int(timedelta(days=days) / RUN_EVERY) + int(SEASONAL_OPEN / RUN_EVERY)
        for step in range(steps):
            now_v = START + step * RUN_EVERY
            virtual[0] = now_v
            now_r = datetime.now(timezone.utc)
            open_q = [q for q in questions if q.opens <= now_v < q.closes and q.done_at is None]
            tried = [q for q in open_q if main.should_try(retry_state.get(str(q.post)), q.closes, now_v)]
            if not tried:
                continue
            pool = bot_config._gemini_pool(ledger_store, llm_class=ReplayChainLlm)
            pool.pacers = {m: RequestPacer(60000) for m in pool.pacers}
            pool.questions_per_day = 1 / 7  # a quiet week before the round
            pool.minibench_active = any(not q.seasonal and q.opens <= now_v < q.opens + timedelta(hours=24) for q in questions)
            pool.expected_questions_left_today = lambda now=None, p=pool, t=now_v: GeminiPool.expected_questions_left_today(p, t)
            parser = pool.parser()
            bot = main.FallBot2026(
                llms={"default": pool.unplanned_forecaster(), "parser": parser, "summarizer": parser, "researcher": "replay"},
                publish_reports_to_metaculus=False, enable_summarize_research=False,
                predictions_per_research_report=3, required_successful_predictions=0,
            )
            bot.planner = pool
            bot.question_log = None
            bot.keep_records = True
            bot.run_mode = "test_questions"
            by_post = {q.post: q for q in tried}
            for q in tried:
                in_window[str(q.post)] = q.closes - now_v <= main.EMERGENCY_WINDOW
            built = {q.post: q.build(now_v, now_r) for q in tried}
            seasonal = [built[q.post] for q in tried if q.seasonal]
            minibench = [built[q.post] for q in tried if not q.seasonal]
            failed: set = set()
            queued, bot.seasonal_by_post = main.run_queue(seasonal, minibench)
            reports = asyncio.run(bot.forecast_questions(queued, return_exceptions=True))
            for question, report in zip(queued, reports):
                if isinstance(report, BaseException):
                    failed.add(question.id_of_post)
            for record in bot.__dict__.get("kept_records", []):
                q = by_post[record["question"]["id_of_post"]]
                ok = [f for f in record["forecasts"] if f.get("status") == "ok"]
                q.attempts += 1
                if q.post not in failed and ok:
                    q.done_at = now_v
                    q.forecasts = len(ok)
                    models = {m for f in ok for m in f.get("answered_models", [])}
                    q.path = record.get("emergency") or ("flash" if models & set(GEMINI_FORECAST_MODELS) else "other")
            retry_state = main.update_retry_state(retry_state, list(by_post), failed, now_v)
            stats.day_used[pool.ledger.day] = dict(pool.ledger.used)
        return questions, stats
    finally:
        gemini_budget.quota_day = real_quota_day
        _RecordedAnswer._mockable_direct_call_to_model = original_answer
        main.FallBot2026._run_research_unguarded = original_research
        asyncio.sleep = original_sleep
        gemini_budget.QuotaLedger.start = original_start


def report(questions: list[SimQuestion], stats: Stats) -> tuple[bool, str]:
    minibench = [q for q in questions if not q.seasonal]
    seasonal = [q for q in questions if q.seasonal]
    missed = [q for q in questions if q.done_at is None or q.done_at >= q.closes]

    def paths(qs):  # type: ignore[no-untyped-def]
        out: dict[str, int] = {}
        for q in qs:
            key = q.path if q.done_at else "MISSED"
            out[key] = out.get(key, 0) + 1
        return ", ".join(f"{k}: {v}" for k, v in sorted(out.items()))

    def minutes(qs):  # type: ignore[no-untyped-def]
        waits = sorted(int((q.done_at - q.opens).total_seconds() // 60) for q in qs if q.done_at)
        return f"median {waits[len(waits) // 2]} min, max {waits[-1]} min" if waits else "-"

    def avg(qs):  # type: ignore[no-untyped-def]
        done = [q.forecasts for q in qs if q.done_at]
        return f"{sum(done) / len(done):.1f}" if done else "-"

    lines = [
        "## MiniBench burst rehearsal (virtual clock, recorded replies, 0 model calls)",
        "",
        f"{MINIBENCH_QUESTIONS} MiniBench questions over {DAYS} days in bursts of {BURST} (open {MINIBENCH_OPEN}), "
        f"{len(seasonal)} seasonal; a run every 10 min; {FLASH_503_RATE:.0%} of Flash calls answer 503.",
        "",
        "| Questions | Forecast before close | Path | Forecasts per question | Time to forecast |",
        "|---|---|---|---|---|",
        f"| MiniBench ({len(minibench)}) | {sum(1 for q in minibench if q not in missed)} | {paths(minibench)} | {avg(minibench)} | {minutes(minibench)} |",
        f"| Seasonal ({len(seasonal)}) | {sum(1 for q in seasonal if q not in missed)} | {paths(seasonal)} | {avg(seasonal)} | {minutes(seasonal)} |",
        "",
        "| Quota day (Pacific) | " + " | ".join(m.split("/")[-1] for m in (*GEMINI_FORECAST_MODELS, "gemini/gemini-3.5-flash-lite", "gemini/gemini-3.1-flash-lite")) + " | Flash reserve used | Flash-Lite reserve used |",
        "|---" * 9 + "|",
    ]
    for day, used in sorted(stats.day_used.items()):
        cells = " | ".join(str(used.get(m, 0)) for m in (*GEMINI_FORECAST_MODELS, "gemini/gemini-3.5-flash-lite", "gemini/gemini-3.1-flash-lite"))
        lines.append(f"| {day} | {cells} | {stats.reserve_uses.get((day, 'flash'), 0)} | {stats.reserve_uses.get((day, 'flash-lite'), 0)} |")
    seasonal_ok = all(q.done_at and q.forecasts >= 1 for q in seasonal)
    ok = not missed and stats.flash_lite_reserve_misuse == 0 and seasonal_ok
    lines += [
        "",
        f"- Every question forecast before close: **{'yes' if not missed else 'NO (' + str(len(missed)) + ' missed)'}**",
        f"- Flash-Lite reserve used outside a question's last 45 minutes: **{stats.flash_lite_reserve_misuse}** (must be 0)",
        f"- Seasonal questions all forecast (share kept): **{'yes' if seasonal_ok else 'NO'}**",
    ]
    return ok, "\n".join(lines)


if __name__ == "__main__":
    questions, stats = run()
    ok, text = report(questions, stats)
    print(text)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    sys.exit(0 if ok else 1)
