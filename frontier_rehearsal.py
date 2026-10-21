"""
Frontier lineup rehearsal (architect, 21 Oct; PR 2). The REAL frontier planner,
spend guards and bot code with recorded replies: 0 model calls, $0, nothing
submitted, every ledger in memory.

A. Full mode: seasonal (4 types) = Fable + Opus 5.5 + GPT + 2 Flash, no round 2
   even when they disagree; MiniBench = Opus + Sonnet + today's Flash path;
   Claude <= $1.50 / $0.60 per question, OpenRouter <= $0.30 per seasonal one.
B. Under 25% of the cycle budget left: seasonal without Fable, MiniBench Sonnet only.
C. Under 10% left: seasonal Opus only (+ GPT + Flash), MiniBench no Claude.
D. "Credit balance too low": never retried, no Claude backup tried, no more
   Claude this run; every question still forecast; the next run: today's setup.
E. GPT fails: its slot goes to the Flash chain (still 5 forecasts), never retried.
F. The Claude day at $11.50: no call may take it over $12.
G. The Claude ledger can't be read: no Claude, no GPT (today's setup).
H. The OpenRouter key under $2 left: no GPT, a third Flash instead.
I. Timing: a Fable call slower than the planned-forecast wait is cut; the
   question is still forecast with the others.

Prints a report; exits 1 if a check fails.

    poetry run python frontier_rehearsal.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

from forecasting_tools import BinaryQuestion, DiscreteQuestion, MultipleChoiceQuestion, NumericQuestion

import frontier
import main
from frontier import FABLE, GPT_SOL, HAIKU, OPUS_5, OPUS_55, SONNET, ClaudeBudget, FrontierPlanner
from gemini_budget import MemoryStore
from llm_throttle import RequestPacer
from replay import ReplayChainLlm, _RecordedAnswer
from spend import SpendGuard, utc_day

RESET = date.today().replace(day=1)  # any reset day: the cycle started on the 1st
BUDGET = 170.0


class CreditError(Exception):
    """What the Anthropic SDK raises when the credits are used up (HTTP 400)."""

    status_code = 400


class _Broken(Exception):
    pass


def _question(post: int, kind: str, minutes: int = 180) -> Any:
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


def _budget(spent: float = 0.0, day: float = 0.0, store: Any = None) -> ClaudeBudget:
    claude = ClaudeBudget(store or MemoryStore({"days": {}}), BUDGET, RESET)
    claude.cycles[claude.cycle.isoformat()] = spent
    if day:
        claude.days[utc_day()] = day
    return claude


def run_phase(seasonal: list, minibench: list, claude: ClaudeBudget, key_left: float = 5.0,
              fail: dict | None = None, slow: dict | None = None) -> tuple[list[dict], FrontierPlanner, list[str]]:
    """One live-like run; returns (question records, planner, real model of each recorded call)."""
    fail, slow, calls = fail or {}, slow or {}, []
    original = _RecordedAnswer._mockable_direct_call_to_model

    async def recorded(self, prompt):  # type: ignore[no-untyped-def]
        real = self.model.removeprefix("replay/")
        calls.append(real)
        if real in slow:
            await asyncio.sleep(slow[real])
        if real in fail:
            raise fail[real]()
        return await original(self, prompt)

    _RecordedAnswer._mockable_direct_call_to_model = recorded
    try:
        spend = SpendGuard(MemoryStore({"days": {}}))
        planner = frontier._frontier_pool(MemoryStore(), spend, claude, llm_class=ReplayChainLlm)
        planner.claude_llm_class = planner.gpt_llm_class = ReplayChainLlm
        planner.model_prefix = "replay/"
        planner.pacers = {m: RequestPacer(60000) for m in planner.pacers}
        planner.claude_pacer = planner.gpt_pacer = RequestPacer(60000)
        planner.start_run(key_left, api_key=True)
        parser = planner.parser()
        bot = main.FallBot2026(
            llms={"default": planner.unplanned_forecaster(), "parser": parser, "summarizer": parser, "researcher": "replay"},
            publish_reports_to_metaculus=False, enable_summarize_research=False,
            predictions_per_research_report=3, required_successful_predictions=0,
        )
        bot.planner = planner
        bot.question_log = None
        bot.keep_records = True
        bot.run_mode = "test_questions"
        queued, bot.seasonal_by_post = main.run_queue(seasonal, minibench)
        asyncio.run(bot.forecast_questions(queued, return_exceptions=True))
        return bot.__dict__.get("kept_records", []), planner, calls
    finally:
        _RecordedAnswer._mockable_direct_call_to_model = original


def _info(record: dict, planner: FrontierPlanner) -> dict:
    ok = [f for f in record.get("forecasts", []) if f.get("status") == "ok"]
    models = [str(f.get("forecast_model", "")).removeprefix("replay/") for f in ok]
    post = str(record["question"]["id_of_post"])
    return {
        "post": int(post), "type": record["question"]["question_type"], "seasonal": record.get("seasonal"),
        "forecasts": len(ok), "models": models, "round2": record.get("round2"),
        "claude": sum(m.startswith("anthropic/") for m in models), "gpt": models.count(GPT_SOL),
        "flash": sum("flash" in m for m in models), "submitted": record.get("final_forecast") is not None,
        "claude_spent": planner.claude.question_spent(post) if planner.claude else 0.0,
        "openrouter_spent": planner.spend.question_spent(post) if planner.spend else 0.0,
        "failed": [f.get("planned_model", "").removeprefix("replay/") for f in record.get("forecasts", []) if f.get("status") == "failed"],
    }


def _short(model: str) -> str:
    return model.split("/")[-1]


def _rows(infos: list[dict]) -> list[str]:
    lines = ["| Question | Type | Tournament | Forecasts | Models | Claude $ | OpenRouter $ | Submitted |",
             "|---|---|---|---|---|---|---|---|"]
    for i in infos:
        lines.append(f"| {i['post']} | {i['type']} | {'seasonal' if i['seasonal'] else 'MiniBench'} | {i['forecasts']} | "
                     f"{', '.join(_short(m) for m in i['models'])} | ${i['claude_spent']:.3f} | ${i['openrouter_spent']:.3f} | "
                     f"{'yes' if i['submitted'] else 'no'} |")
    return lines


SEASONAL_TYPES = ("binary", "numeric", "multiple_choice", "discrete")


def run() -> dict:
    wait, stages = main.PLANNED_WAIT_SECONDS, main.FORECAST_STAGES_LIMIT_SECONDS
    main.QUICK_FORECAST_RETRY_WAIT_SECONDS, quick_wait = 0, main.QUICK_FORECAST_RETRY_WAIT_SECONDS
    # Round 1 disagrees on binary questions (Fable 20%, Opus 70%): round 2 would run on the old path.
    _RecordedAnswer.binary_percent = {FABLE: 20, OPUS_55: 70, GPT_SOL: 45, SONNET: 40, OPUS_5: 50, HAIKU: 50}
    out: dict = {}
    try:
        def phase(name: str, base: int, claude: ClaudeBudget, **kw: Any) -> None:
            seasonal = [_question(base + i, kind) for i, kind in enumerate(SEASONAL_TYPES)]
            records, planner, calls = run_phase(seasonal, [_question(base + 9, "binary")], claude, **kw)
            out[name] = {"infos": [_info(r, planner) for r in records], "planner": planner, "calls": calls}

        phase("A", 91000, _budget())
        phase("B", 92000, _budget(spent=0.80 * BUDGET))
        phase("C", 93000, _budget(spent=0.95 * BUDGET))
        store = MemoryStore({"days": {}})
        phase("D", 94000, _budget(store=store), fail={m: lambda: CreditError("Your credit balance is too low to access the Anthropic API.")
                                                   for m in frontier.CLAUDE_PRICES})
        out["D_next"] = frontier.claude_mode(ClaudeBudget(store, BUDGET, RESET))
        phase("E", 95000, _budget(), fail={GPT_SOL: lambda: _Broken("rehearsal: GPT down")})
        phase("F", 96000, _budget(day=11.50))

        class Unreadable(MemoryStore):
            def load(self):  # type: ignore[no-untyped-def]
                raise OSError("rehearsal: ledger unreadable")

        phase("G", 97000, ClaudeBudget(Unreadable(), BUDGET, RESET))
        phase("H", 98000, _budget(), key_left=1.50)
        main.PLANNED_WAIT_SECONDS = 3
        phase("I", 99000, _budget(), slow={FABLE: 6.0})
        return out
    finally:
        main.PLANNED_WAIT_SECONDS, main.FORECAST_STAGES_LIMIT_SECONDS = wait, stages
        main.QUICK_FORECAST_RETRY_WAIT_SECONDS = quick_wait
        _RecordedAnswer.binary_percent = {}


def report() -> tuple[bool, str]:
    out = run()

    def infos(phase: str, seasonal: bool | None = None) -> list[dict]:
        return [i for i in out[phase]["infos"] if seasonal is None or bool(i["seasonal"]) == seasonal]

    def models(phase: str, seasonal: bool) -> list[set]:
        return [set(i["models"]) for i in infos(phase, seasonal)]

    a_s, a_m = infos("A", True), infos("A", False)
    d_claude_calls = [m for m in out["D"]["calls"] if m.startswith("anthropic/")]
    f_day = out["F"]["planner"].claude.day_spent()
    checks = [
        ("A: seasonal, every type = Fable + Opus 5.5 + GPT + 2 Flash (5 forecasts)",
         len(a_s) == 4 and all(i["forecasts"] == 5 and {FABLE, OPUS_55, GPT_SOL} <= set(i["models"]) and i["flash"] == 2 for i in a_s)),
        ("A: seasonal binary with round 1 disagreeing: no round 2", all(not i["round2"] for i in a_s if i["type"] == "binary")),
        ("A: MiniBench = Opus 5.5 + Sonnet 5.5 + Flash", all({OPUS_55, SONNET} <= set(i["models"]) and i["flash"] >= 1 for i in a_m)),
        (f"A: Claude <= ${frontier.CLAUDE_SEASONAL_QUESTION_CAP:.2f} seasonal / ${frontier.CLAUDE_MINIBENCH_QUESTION_CAP:.2f} MiniBench; OpenRouter <= ${frontier.OPENROUTER_SEASONAL_QUESTION_CAP:.2f} seasonal",
         all(i["claude_spent"] <= frontier.CLAUDE_SEASONAL_QUESTION_CAP + 1e-9 and i["openrouter_spent"] <= frontier.OPENROUTER_SEASONAL_QUESTION_CAP + 1e-9 for i in a_s)
         and all(i["claude_spent"] <= frontier.CLAUDE_MINIBENCH_QUESTION_CAP + 1e-9 for i in a_m)),
        ("B (20% left): seasonal without Fable (Opus + GPT + Flash); MiniBench Sonnet only",
         out["B"]["planner"].mode == "low" and all(FABLE not in m and {OPUS_55, GPT_SOL} <= m for m in models("B", True))
         and all(SONNET in m and OPUS_55 not in m for m in models("B", False))),
        ("C (5% left): seasonal Opus only (+ GPT + Flash); MiniBench no Claude",
         out["C"]["planner"].mode == "minimal" and all(FABLE not in m and OPUS_55 in m and GPT_SOL in m for m in models("C", True))
         and all(not any(x.startswith("anthropic/") for x in m) for m in models("C", False))),
        (f"D: credit error: {len(d_claude_calls)} Claude call(s), each model at most once, no Claude backup ({', '.join(_short(m) for m in d_claude_calls)})",
         len(d_claude_calls) == len(set(d_claude_calls)) and OPUS_5 not in d_claude_calls and len(d_claude_calls) <= 2),
        ("D: every question still forecast (GPT / Flash)", all(i["submitted"] for i in infos("D"))),
        (f"D: the next run: {out['D_next'][0]} ({out['D_next'][1]})", out["D_next"][0] == "off"),
        ("E: GPT fails -> its slot answered by Flash (5 forecasts, 3 Flash), GPT called once per seasonal question",
         all(i["forecasts"] == 5 and i["flash"] == 3 and i["gpt"] == 0 for i in infos("E", True))
         and out["E"]["calls"].count(GPT_SOL) == len(infos("E", True))),
        (f"F: Claude day at $11.50 -> ${f_day:.2f} <= $12.00", f_day <= frontier.CLAUDE_DAILY_CAP + 1e-9),
        ("F: a Claude slot refused by a cap goes to Flash (every seasonal question still 5 forecasts)",
         all(i["forecasts"] == 5 for i in infos("F", True))),
        ("G: ledger unreadable -> no Claude, no GPT (today's setup)",
         out["G"]["planner"].mode == "off" and all(i["claude"] == 0 and i["gpt"] == 0 and i["submitted"] for i in infos("G"))),
        ("H: key under $2 -> no GPT; Fable + Opus + 3 Flash",
         all(i["gpt"] == 0 and i["flash"] == 3 and {FABLE, OPUS_55} <= set(i["models"]) for i in infos("H", True))
         and GPT_SOL not in out["H"]["calls"]),
        ("I: Fable slower than the planned wait -> cut; question forecast with the others",
         all(i["submitted"] and FABLE not in i["models"] and i["forecasts"] >= 4 for i in infos("I", True))),
    ]
    ok = all(c for _, c in checks)
    lines = ["## Frontier lineup rehearsal (recorded replies, 0 model calls, $0)", "",
             f"Budget ${BUDGET:.0f} per cycle; caps ${frontier.CLAUDE_DAILY_CAP:.0f}/UTC day, "
             f"${frontier.CLAUDE_SEASONAL_QUESTION_CAP:.2f} seasonal / ${frontier.CLAUDE_MINIBENCH_QUESTION_CAP:.2f} MiniBench "
             f"per question (Claude), ${frontier.OPENROUTER_SEASONAL_QUESTION_CAP:.2f} per seasonal question (OpenRouter).", ""]
    titles = {"A": "A. Full", "B": "B. Under 25% left", "C": "C. Under 10% left", "D": "D. Credit balance too low",
              "E": "E. GPT fails", "F": "F. Claude day at $11.50", "G": "G. Claude ledger unreadable",
              "H": "H. OpenRouter key under $2", "I": "I. Fable slower than the planned wait"}
    for phase, title in titles.items():
        lines += [f"### {title} (mode {out[phase]['planner'].mode})", "", *_rows(out[phase]["infos"]), ""]
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
