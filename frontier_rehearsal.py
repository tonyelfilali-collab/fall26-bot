"""
Frontier lineup rehearsal (architect, 21 Oct; PR 2, second version). The REAL
frontier planner, spend guards and bot code with recorded replies: 0 model
calls, $0, nothing submitted, every ledger in memory.

A. Full mode: seasonal (4 types) = Opus 5.5 x2 + Flash x2; MiniBench = Opus 5.5
   + Flash x2; no round 2 even when they disagree; never more than 2 Flash;
   Claude <= $1.50 / $0.60 per question.
B. Under 25% of the cycle budget left: seasonal Opus x1; MiniBench Opus x1.
C. Under 10% left: seasonal Opus x1; MiniBench no Claude (today's setup).
D. "Credit balance too low": never retried, no Claude backup tried, no more
   Claude this run; every question still forecast; the next run: today's setup.
E. GPT switched on and failing: its slot goes to the Flash chain, never retried.
F. The Claude day at $14.50: no call takes it over $15; a refused Opus slot
   passes to a backup that fits (Haiku).
G. The Claude ledger can't be read: no Claude (today's setup).
H. GPT switched on, the OpenRouter key under $2 left: no GPT.
I. Timing: Opus calls slower than the planned-forecast wait are cut; the
   question is still forecast with the Flash ones.
J. Every free Flash call answers 503: paid Flash fills both Flash slots,
   although the Opus forecasts are in.
R. A burst of 40 MiniBench questions on a Claude day at $8: MiniBench stops
   using Claude at $9 (reservations included); a seasonal question in the same
   run still gets both Opus slots; the day stays under $15.

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

import litellm

import bot_config
import frontier
import main
from frontier import GPT_SOL, HAIKU, OPUS_5, OPUS_55, ClaudeBudget, FrontierPlanner
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
              fail: dict | None = None, slow: dict | None = None, gpt: bool = False) -> tuple[list[dict], FrontierPlanner, list[str]]:
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
        planner.gpt_enabled = gpt
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
        "opus": models.count(OPUS_55), "flash": sum("flash" in m for m in models),
        "paid_flash": models.count(bot_config.PAID_FLASH_MODEL), "submitted": record.get("final_forecast") is not None,
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
    wait = main.PLANNED_WAIT_SECONDS
    main.QUICK_FORECAST_RETRY_WAIT_SECONDS, quick_wait = 0, main.QUICK_FORECAST_RETRY_WAIT_SECONDS
    # Round 1 disagrees on binary questions (Opus 70%, Flash 20%): round 2 would run on today's path.
    _RecordedAnswer.binary_percent = {OPUS_55: 70, OPUS_5: 60, HAIKU: 60, GPT_SOL: 45,
                                      **{m: 20 for m in bot_config.GEMINI_FORECAST_MODELS}}
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
        phase("E", 95000, _budget(), fail={GPT_SOL: lambda: _Broken("rehearsal: GPT down")}, gpt=True)
        phase("F", 96000, _budget(day=14.50))

        class Unreadable(MemoryStore):
            def load(self):  # type: ignore[no-untyped-def]
                raise OSError("rehearsal: ledger unreadable")

        phase("G", 97000, ClaudeBudget(Unreadable(), BUDGET, RESET))
        phase("H", 98000, _budget(), key_left=1.50, gpt=True)
        main.PLANNED_WAIT_SECONDS = 3
        phase("I", 99000, _budget(), slow={OPUS_55: 6.0})
        main.PLANNED_WAIT_SECONDS = wait
        # R: 40 MiniBench questions closing first, one seasonal question closing later.
        burst = [_question(89000 + i, "binary", minutes=60) for i in range(40)]
        records, planner, calls = run_phase([_question(88999, "binary", minutes=600)], burst, _budget(day=8.0))
        out["R"] = {"infos": [_info(r, planner) for r in records], "planner": planner, "calls": calls}
        phase("J", 90000, _budget(), fail={m: lambda: litellm.ServiceUnavailableError(
            message="rehearsal: overloaded", llm_provider="gemini", model="gemini") for m in bot_config.GEMINI_FORECAST_MODELS})
        return out
    finally:
        main.PLANNED_WAIT_SECONDS = wait
        main.QUICK_FORECAST_RETRY_WAIT_SECONDS = quick_wait
        _RecordedAnswer.binary_percent = {}


def report() -> tuple[bool, str]:
    out = run()

    def infos(phase: str, seasonal: bool | None = None) -> list[dict]:
        return [i for i in out[phase]["infos"] if seasonal is None or bool(i["seasonal"]) == seasonal]

    def mode(phase: str) -> str:
        return out[phase]["planner"].mode

    frontier_phases = ("A", "B", "E", "F", "H", "I", "J")
    a_s, a_m = infos("A", True), infos("A", False)
    d_claude_calls = [m for m in out["D"]["calls"] if m.startswith("anthropic/")]
    f_day = out["F"]["planner"].claude.day_spent()
    r_day = out["R"]["planner"].claude.day_spent()
    r_mini_spent = 8.0 + sum(i["claude_spent"] for i in infos("R", False))
    r_mini_claude = sum(1 for i in infos("R", False) if i["claude"])
    checks = [
        ("A: seasonal, every type = Opus 5.5 x2 + Flash x2 (4 forecasts)",
         len(a_s) == 4 and all(i["forecasts"] == 4 and i["opus"] == 2 and i["flash"] == 2 for i in a_s)),
        ("A: MiniBench = Opus 5.5 + Flash x2 (3 forecasts)", all(i["forecasts"] == 3 and i["opus"] == 1 and i["flash"] == 2 for i in a_m)),
        ("A: binary questions with round 1 disagreeing: no round 2 (seasonal and MiniBench)",
         all(not i["round2"] for i in infos("A") if i["type"] == "binary")),
        (f"A: Claude <= ${frontier.CLAUDE_SEASONAL_QUESTION_CAP:.2f} seasonal / ${frontier.CLAUDE_MINIBENCH_QUESTION_CAP:.2f} MiniBench per question",
         all(i["claude_spent"] <= frontier.CLAUDE_SEASONAL_QUESTION_CAP + 1e-9 for i in a_s)
         and all(i["claude_spent"] <= frontier.CLAUDE_MINIBENCH_QUESTION_CAP + 1e-9 for i in a_m)),
        ("Never more than 2 Flash on a question with a Claude forecast (A, B, F, H, I, J)",
         all(i["flash"] <= 2 for p in ("A", "B", "F", "H", "I", "J") for i in infos(p) if i["claude"])),
        ("B (20% left): seasonal Opus x1 + Flash x2; MiniBench Opus x1 + Flash x2",
         mode("B") == "low" and all(i["opus"] == 1 and i["flash"] == 2 for i in infos("B"))),
        ("C (5% left): seasonal Opus x1 + Flash x2; MiniBench no Claude (today's setup)",
         mode("C") == "minimal" and all(i["opus"] == 1 and i["flash"] == 2 for i in infos("C", True))
         and all(i["claude"] == 0 and i["flash"] >= 3 for i in infos("C", False))),
        (f"D: credit error: {len(d_claude_calls)} Claude call(s), never retried, no Claude backup ({', '.join(_short(m) for m in d_claude_calls)})",
         OPUS_5 not in d_claude_calls and HAIKU not in d_claude_calls and len(d_claude_calls) <= 2),
        ("D: every question still forecast (today's setup)", all(i["submitted"] for i in infos("D"))),
        (f"D: the next run: {out['D_next'][0]} ({out['D_next'][1]})", out["D_next"][0] == "off"),
        ("E: GPT on and failing -> its slot answered by Flash, GPT called once per seasonal question",
         all(i["forecasts"] == 5 and i["gpt"] == 0 and i["flash"] == 3 for i in infos("E", True))
         and out["E"]["calls"].count(GPT_SOL) == len(infos("E", True))),
        (f"F: Claude day at $14.50 -> ${f_day:.2f} <= $15.00; every question forecast",
         f_day <= frontier.CLAUDE_DAILY_CAP + 1e-9 and all(i["submitted"] for i in infos("F"))),
        ("F: an Opus slot refused by the day cap passed to a backup that fits (Haiku)", HAIKU in out["F"]["calls"]),
        ("G: ledger unreadable -> no Claude (today's setup)",
         mode("G") == "off" and all(i["claude"] == 0 and i["submitted"] for i in infos("G"))),
        ("H: GPT on, key under $2 -> no GPT; Opus x2 + Flash x2",
         all(i["gpt"] == 0 and i["opus"] == 2 and i["flash"] == 2 for i in infos("H", True)) and GPT_SOL not in out["H"]["calls"]),
        ("I: Opus slower than the planned wait -> cut; every question forecast with Flash",
         all(i["submitted"] and i["opus"] == 0 for i in infos("I"))),
        ("J: every free Flash 503 -> both Flash slots paid, with the Opus forecasts in",
         all(i["paid_flash"] == 2 and i["opus"] >= 1 for i in infos("J"))),
        (f"R: 40-question MiniBench burst from $8: MiniBench Claude stopped by ${frontier.CLAUDE_MINIBENCH_DAY_LIMIT:.0f} "
         f"({r_mini_claude} with Claude), day ${r_day:.2f} <= ${frontier.CLAUDE_DAILY_CAP:.0f}",
         r_mini_spent <= frontier.CLAUDE_MINIBENCH_DAY_LIMIT + 1e-9 and r_day <= frontier.CLAUDE_DAILY_CAP + 1e-9
         and 0 < r_mini_claude < 40 and len(infos("R")) == 41 and all(i["submitted"] for i in infos("R"))),
        ("R: the seasonal question after the burst still gets both Opus slots",
         [i["opus"] for i in infos("R", True)] == [2]),
        ("Modes: A, B, E, F, H, I, J frontier; G off", all(mode(p) != "off" for p in frontier_phases) and mode("G") == "off"),
    ]
    ok = all(c for _, c in checks)
    lines = ["## Frontier lineup rehearsal (recorded replies, 0 model calls, $0)", "",
             f"Budget ${BUDGET:.0f} per cycle; caps ${frontier.CLAUDE_DAILY_CAP:.0f}/UTC day, "
             f"${frontier.CLAUDE_SEASONAL_QUESTION_CAP:.2f} seasonal / ${frontier.CLAUDE_MINIBENCH_QUESTION_CAP:.2f} MiniBench "
             f"per question (Claude); at most {frontier.FLASH_FORECASTS} Flash per question.", ""]
    titles = {"A": "A. Full", "B": "B. Under 25% left", "C": "C. Under 10% left", "D": "D. Credit balance too low",
              "E": "E. GPT on and failing", "F": "F. Claude day at $14.50", "G": "G. Claude ledger unreadable",
              "H": "H. GPT on, OpenRouter key under $2", "I": "I. Opus slower than the planned wait",
              "J": "J. Every free Flash 503", "R": "R. MiniBench burst of 40 from a $8 Claude day"}
    for phase, title in titles.items():
        lines += [f"### {title} (mode {mode(phase)})", "", *_rows(out[phase]["infos"]), ""]
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
