"""
Paid-Flash fallback rehearsal (architect, 30 Sep). The REAL gemini-free pool
and bot code with the fallback switched on for this run only, recorded replies
(0 model calls, nothing submitted, both ledgers in memory). Every free Google
Flash call answers 503.

Phase 1: 3 questions closing in 3 hours, a fresh spend ledger. Every Flash
slot ends on the paid Gemini 3.6 Flash; the per-question cap ($0.15) stops
new paid calls once a question's committed spend would pass it.
Phase 2: the day's spend already at $0.97 of the $1.00 daily cap. No paid call
is allowed; the 2 questions closing within 45 minutes still get the Nemotron
backup; the one closing later gets no forecast (left for the next run, never
a guess).

Checks: paid slots used in phase 1; every question's spend <= $0.15; the
day's spend <= $1.00; phase 2 makes 0 paid calls; both window questions are
forecast by Nemotron. Prints a report; exits 1 if a check fails.

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
from gemini_budget import MemoryStore
from llm_throttle import RequestPacer
from replay import ReplayChainLlm, _RecordedAnswer
from spend import SpendGuard, utc_day

PAID = f"replay/{bot_config.PAID_FLASH_MODEL}"
SEEDED_DAY_SPEND = 0.97


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


def run_phase(questions: list, spend: SpendGuard) -> list[dict]:
    """One live-like run; returns the question records."""
    pool = bot_config._gemini_pool(MemoryStore(), llm_class=ReplayChainLlm)
    pool.pacers = {m: RequestPacer(60000) for m in pool.pacers}
    bot_config.enable_paid_flash(pool, spend, llm_class=ReplayChainLlm, model=PAID)
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
    queued, bot.seasonal_by_post = main.run_queue(questions, [])
    asyncio.run(bot.forecast_questions(queued, return_exceptions=True))
    return bot.__dict__.get("kept_records", [])


def run() -> tuple[list[dict], list[dict], SpendGuard, float]:
    original = _RecordedAnswer._mockable_direct_call_to_model

    async def free_flash_503(self, prompt):  # type: ignore[no-untyped-def]
        if self.model.removeprefix("replay/") in GEMINI_FORECAST_MODELS:
            raise litellm.ServiceUnavailableError(message="rehearsal: overloaded", llm_provider="gemini", model=self.model)
        return await original(self, prompt)

    wait = main.QUICK_FORECAST_RETRY_WAIT_SECONDS
    _RecordedAnswer._mockable_direct_call_to_model = free_flash_503
    _RecordedAnswer.binary_percent = {}
    main.QUICK_FORECAST_RETRY_WAIT_SECONDS = 0  # no real waiting between quick-forecast passes
    try:
        spend = SpendGuard(MemoryStore({}))
        phase1 = run_phase([_question(80001, "binary", 180), _question(80002, "numeric", 180),
                            _question(80003, "multiple_choice", 180)], spend)
        phase1_day = spend.day_spent()
        spend.days[utc_day()] = SEEDED_DAY_SPEND
        calls_before = spend.paid_calls
        phase2 = run_phase([_question(80011, "binary", 40), _question(80012, "discrete", 40),
                            _question(80013, "binary", 180)], spend)
        spend.phase2_paid_calls = spend.paid_calls - calls_before  # type: ignore[attr-defined]
        return phase1, phase2, spend, phase1_day
    finally:
        _RecordedAnswer._mockable_direct_call_to_model = original
        main.QUICK_FORECAST_RETRY_WAIT_SECONDS = wait


def _row(record: dict, spend: SpendGuard) -> tuple[str, dict]:
    q = record["question"]
    post = str(q["id_of_post"])
    forecasts = record.get("forecasts", [])
    paid_ok = sum(1 for f in forecasts if f.get("status") == "ok" and PAID in (f.get("answered_models") or []))
    refused = sum(1 for f in forecasts if "SpendCapReached" in str(f.get("error", "")))
    nemotron = sum(1 for f in forecasts if f.get("status") == "ok"
                   and any("nemotron" in m for m in f.get("answered_models") or []))
    forecast = record.get("final_forecast") is not None
    info = {"paid_ok": paid_ok, "refused": refused, "nemotron": nemotron, "forecast": forecast,
            "spent": spend.question_spent(post), "slots": sum(1 for f in forecasts if f.get("kind") == "planned")}
    path = "paid Flash" if paid_ok else ("Nemotron backup" if nemotron else "none: next run")
    line = (f"| {post} | {q['question_type']} | {info['slots']} | {paid_ok} | {refused} | ${info['spent']:.4f} | "
            f"{path} | {'yes' if forecast else 'no'} |")
    return line, info


def report() -> tuple[bool, str]:
    phase1, phase2, spend, phase1_day = run()
    header = ["| Question | Type | Planned slots | Paid answered | Paid refused (cap) | Spent | Forecast by | Forecast |",
              "|---|---|---|---|---|---|---|---|"]
    rows1 = [_row(r, spend) for r in sorted(phase1, key=lambda r: r["question"]["id_of_post"])]
    rows2 = [_row(r, spend) for r in sorted(phase2, key=lambda r: r["question"]["id_of_post"])]
    by_post2 = {r["question"]["id_of_post"]: info for r, (_, info) in zip(sorted(phase2, key=lambda r: r["question"]["id_of_post"]), rows2)}
    checks = [
        ("Phase 1: every question forecast by the paid Flash slots", all(i["forecast"] and i["paid_ok"] > 0 for _, i in rows1)),
        ("Phase 1: per-question cap held (spent <= $0.15; extra paid slots refused)",
         all(i["spent"] <= bot_config.PAID_FLASH_QUESTION_CAP + 1e-9 for _, i in rows1)),
        (f"Phase 2: daily cap held (day spend ${spend.day_spent():.2f} <= ${bot_config.PAID_FLASH_DAILY_CAP:.2f}), "
         f"0 paid calls ({spend.phase2_paid_calls})",  # type: ignore[attr-defined]
         spend.day_spent() <= bot_config.PAID_FLASH_DAILY_CAP + 1e-9 and spend.phase2_paid_calls == 0),  # type: ignore[attr-defined]
        ("Phase 2: both questions closing within 45 min forecast by the Nemotron backup",
         all(by_post2[p]["forecast"] and by_post2[p]["nemotron"] > 0 for p in (80011, 80012))),
        ("Phase 2: the question closing later gets no forecast (next run), never a guess",
         not by_post2[80013]["forecast"]),
    ]
    ok = all(c for _, c in checks)
    lines = [
        "## Paid-Flash fallback rehearsal (recorded replies, 0 model calls, $0)",
        "",
        "Every free Google Flash call answers 503. Paid slot: OpenRouter Gemini 3.6 Flash (recorded cost "
        f"${0.5 * __import__('spend').estimate_cost(PAID):.4f} per call; estimate ${__import__('spend').estimate_cost(PAID):.4f}).",
        "",
        f"### Phase 1: fresh spend ledger (day spend after: ${phase1_day:.4f})",
        "",
        *header, *[line for line, _ in rows1],
        "",
        f"### Phase 2: day spend already ${SEEDED_DAY_SPEND:.2f} of the ${bot_config.PAID_FLASH_DAILY_CAP:.2f} cap",
        "",
        *header, *[line for line, _ in rows2],
        "",
        *[f"- {text}: **{'yes' if good else 'NO'}**" for text, good in checks],
    ]
    return ok, "\n".join(lines)


if __name__ == "__main__":
    ok, text = report()
    print(text)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    sys.exit(0 if ok else 1)
