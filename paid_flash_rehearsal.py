"""
Paid-Flash fallback rehearsal (architect, 30 Sep; rules of 2 Oct). The REAL
gemini-free pool and bot code with the fallback on for this run only, recorded
replies (0 model calls, nothing submitted, both ledgers in memory).

Rules: paid Flash only fills a question up to 3 real forecasts (extra slots are
free only); per-question cap $0.09 (3 x the measured $0.03 per call); daily cap
$1.00, MiniBench only while the day's paid spend is under $0.50.

A. Every free Flash call answers 503: exactly 3 forecasts per question, all paid.
B. One free Flash forecast answers per question (the other free calls answer
   503 a moment later): 1 free + 2 paid = 3.
C. The day's paid spend already $0.50: the MiniBench question gets no paid call
   (left for the next run); the seasonal question still gets its 3.
D. The day's paid spend already $0.98 of $1.00 (+ $0.03 would pass it): no paid call; the 2 questions
   closing within 45 minutes get the Nemotron backup; the later one waits.

Prints a report; exits 1 if a check fails.

    poetry run python paid_flash_rehearsal.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import litellm
from forecasting_tools import BinaryQuestion, DiscreteQuestion, MultipleChoiceQuestion, NumericQuestion

import bot_config
import main
from bot_config import GEMINI_FORECAST_MODELS
from gemini_budget import MemoryStore, current_question_key
from llm_throttle import RequestPacer
from replay import ReplayChainLlm, _RecordedAnswer
from spend import SpendGuard, utc_day

PAID = f"replay/{bot_config.PAID_FLASH_MODEL}"


def _question(post: int, kind: str, minutes: int) -> Any:
    now = datetime.now(timezone.utc)
    common = dict(question_text=f"Rehearsal question {post}?", id_of_post=post,
                  page_url=f"https://www.metaculus.com/questions/{post}", close_time=now + timedelta(minutes=minutes),
                  open_time=now - timedelta(hours=1))
    if kind == "binary":
        return BinaryQuestion(**common)
    if kind == "multiple_choice":
        return MultipleChoiceQuestion(options=["Red", "Green", "Blue"], **common)
    bounds = dict(unit_of_measure="units", open_lower_bound=False, zero_point=None)
    if kind == "discrete":
        return DiscreteQuestion(lower_bound=0.0, upper_bound=10.0, open_upper_bound=False, cdf_size=11, **bounds, **common)
    return NumericQuestion(lower_bound=0.0, upper_bound=1000.0, open_upper_bound=True, cdf_size=201, **bounds, **common)


def run_phase(seasonal: list, minibench: list, spend: SpendGuard, one_free: bool = False) -> list[dict]:
    """One live-like run; returns the question records."""
    original = _RecordedAnswer._mockable_direct_call_to_model
    answered_free: set = set()

    async def free_flash(self, prompt):  # type: ignore[no-untyped-def]
        if self.model.removeprefix("replay/") in GEMINI_FORECAST_MODELS:
            question = current_question_key.get()
            if one_free and question not in answered_free:
                answered_free.add(question)  # this question's first free Flash call answers
                return await original(self, prompt)
            await asyncio.sleep(0.2 if one_free else 0)
            raise litellm.ServiceUnavailableError(message="rehearsal: overloaded", llm_provider="gemini", model=self.model)
        return await original(self, prompt)

    _RecordedAnswer._mockable_direct_call_to_model = free_flash
    try:
        pool = bot_config._gemini_pool(MemoryStore(), llm_class=ReplayChainLlm)
        pool.pacers = {m: RequestPacer(60000) for m in pool.pacers}
        bot_config.enable_paid_flash(pool, spend, llm_class=ReplayChainLlm, model=PAID)
        spend.answered_count = None  # each run's bot counts its own forecasts
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
        queued, bot.seasonal_by_post = main.run_queue(seasonal, minibench)
        asyncio.run(bot.forecast_questions(queued, return_exceptions=True))
        return bot.__dict__.get("kept_records", [])
    finally:
        _RecordedAnswer._mockable_direct_call_to_model = original


def _info(record: dict, spend: SpendGuard) -> dict:
    ok = [f for f in record.get("forecasts", []) if f.get("status") == "ok"]
    models = [m for f in ok for m in f.get("answered_models") or []]
    post = record["question"]["id_of_post"]
    return {
        "post": post, "type": record["question"]["question_type"], "seasonal": record.get("seasonal"),
        "forecasts": len(ok), "paid": sum(m == PAID for m in models),
        "free": sum(m.startswith("gemini/") and "lite" not in m for m in models),
        "nemotron": sum("nemotron" in m for m in models),
        "spent": spend.question_spent(str(post)), "submitted": record.get("final_forecast") is not None,
    }


def _rows(infos: list[dict]) -> list[str]:
    lines = ["| Question | Type | Tournament | Real forecasts | Free Flash | Paid Flash | Nemotron | Spent |",
             "|---|---|---|---|---|---|---|---|"]
    for i in infos:
        lines.append(f"| {i['post']} | {i['type']} | {'seasonal' if i['seasonal'] else 'MiniBench'} | {i['forecasts']} | "
                     f"{i['free']} | {i['paid']} | {i['nemotron']} | ${i['spent']:.4f} |")
    return lines


def run() -> dict:
    wait = main.QUICK_FORECAST_RETRY_WAIT_SECONDS
    _RecordedAnswer.binary_percent = {}
    main.QUICK_FORECAST_RETRY_WAIT_SECONDS = 0  # no real waiting between quick-forecast passes
    try:
        out = {}
        spend = SpendGuard(MemoryStore({}))
        out["A"] = [_info(r, spend) for r in run_phase(
            [_question(81001, "binary", 180), _question(81002, "numeric", 180), _question(81003, "multiple_choice", 180)], [], spend)]
        spend = SpendGuard(MemoryStore({}))
        out["B"] = [_info(r, spend) for r in run_phase(
            [_question(82001, "binary", 180), _question(82002, "discrete", 180)], [], spend, one_free=True)]
        spend = SpendGuard(MemoryStore({}))
        spend.days[utc_day()] = 0.50
        calls = spend.paid_calls
        out["C"] = [_info(r, spend) for r in run_phase([_question(83001, "binary", 180)], [_question(83002, "binary", 180)], spend)]
        out["C_day"] = spend.day_spent()
        spend = SpendGuard(MemoryStore({}))
        spend.days[utc_day()] = 0.98
        calls = spend.paid_calls
        out["D"] = [_info(r, spend) for r in run_phase(
            [_question(84001, "binary", 40), _question(84002, "discrete", 40), _question(84003, "binary", 180)], [], spend)]
        out["D_paid_calls"] = spend.paid_calls - calls
        out["D_day"] = spend.day_spent()
        return out
    finally:
        main.QUICK_FORECAST_RETRY_WAIT_SECONDS = wait


def report() -> tuple[bool, str]:
    out = run()
    by = {i["post"]: i for phase in ("A", "B", "C", "D") for i in out[phase]}
    cap = bot_config.PAID_FLASH_QUESTION_CAP
    checks = [
        ("A: every free Flash 503 -> exactly 3 forecasts per question, all paid, each <= $0.09",
         all(i["forecasts"] == 3 and i["paid"] == 3 and i["spent"] <= cap + 1e-9 for i in out["A"])),
        ("B: one free Flash forecast answers -> 1 free + 2 paid = 3",
         all(i["forecasts"] == 3 and i["free"] == 1 and i["paid"] == 2 for i in out["B"])),
        ("C: day at $0.50 -> MiniBench refused paid (no forecast, next run); seasonal still gets 3 paid",
         by[83002]["paid"] == 0 and not by[83002]["submitted"] and by[83001]["paid"] == 3 and by[83001]["forecasts"] == 3),
        (f"C: day spend after ${out['C_day']:.4f} <= $1.00", out["C_day"] <= bot_config.PAID_FLASH_DAILY_CAP + 1e-9),
        (f"D: day at $0.98 -> 0 paid calls ({out['D_paid_calls']}), day spend ${out['D_day']:.2f} <= $1.00",
         out["D_paid_calls"] == 0 and out["D_day"] <= bot_config.PAID_FLASH_DAILY_CAP + 1e-9),
        ("D: both questions closing within 45 min forecast by the Nemotron backup",
         all(by[p]["submitted"] and by[p]["nemotron"] > 0 for p in (84001, 84002))),
        ("D: the question closing later gets no forecast (next run), never a guess", not by[84003]["submitted"]),
    ]
    ok = all(c for _, c in checks)
    lines = [
        "## Paid-Flash fallback rehearsal (recorded replies, 0 model calls, $0)",
        "",
        f"Rules: paid Flash fills a question up to {bot_config.PAID_FLASH_TARGET_FORECASTS} real forecasts; cap "
        f"${cap:.2f}/question (estimate ${bot_config.PAID_FLASH_ESTIMATE:.2f}/call); ${bot_config.PAID_FLASH_DAILY_CAP:.2f}/UTC day, "
        f"MiniBench only under ${bot_config.PAID_FLASH_MINIBENCH_DAILY_CAP:.2f}.",
        "",
    ]
    for phase, title in (("A", "A. Every free Flash call answers 503"), ("B", "B. One free Flash forecast answers per question"),
                         ("C", "C. Day's paid spend already $0.50 (MiniBench limit)"), ("D", "D. Day's paid spend already $0.98 of $1.00")):
        lines += [f"### {title}", "", *_rows(out[phase]), ""]
    lines += [f"- {text}: **{'yes' if good else 'NO'}**" for text, good in checks]
    return ok, "\n".join(lines)


if __name__ == "__main__":
    ok, text = report()
    print(text)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    sys.exit(0 if ok else 1)
