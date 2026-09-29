"""
Replay lab (architect, 29 Sep, NEXT 2). Never submits; test-only.

For RESOLVED live questions, re-run variant configurations on the FROZEN
dossier sections saved at forecast time (question log "dossier_sections":
base research, follow-up findings, Wikipedia, official data), score them and
report paired differences vs the live forecast, with a bootstrap 90% CI
(bench.bootstrap_ci), per question type.

Experiments (paired difference = score of the variant minus score of live,
except E1, which is always "5 forecasts minus 3 forecasts"):
- E1  3 vs 5 forecasts: free when live already had 5+ (median of the first 3
      vs live); otherwise 2 more Gemini forecasts.
- E2  without the follow-up findings (3 Gemini forecasts).
- E3  without the Wikipedia background (3 Gemini forecasts).
- E4  Nemotron (1 forecast) added to the live forecasts' median.

Scores: binary log score; multiple-choice log score; numeric: log density of
the combined CDF at the resolved value (per unit of the question's range; the
out-of-range mass when it resolved outside).

Quota rules (tested: the lab can never touch the live reserve):
- Nemotron (:free): any time, at most 300 lab calls per UTC day.
- Gemini: only in the last 3 hours of the Pacific quota day; only from
  non-reserve quota beyond 2 x the expected questions left today; never while
  a MiniBench question is open or was seen open in the last 6 hours; single-
  model chains without the reserve, one call per forecast.
Leak guard: a model released after the question's open date is never used
(unknown release date: not used).
"""
from __future__ import annotations

import asyncio
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from bench import bootstrap_ci
from scoreboard import EPS, binary_score, multiple_choice_score

PACIFIC = ZoneInfo("America/Los_Angeles")
NEMOTRON_CALLS_PER_DAY = 300
GEMINI_LAST_HOURS = timedelta(hours=3)
MINIBENCH_QUIET = timedelta(hours=6)
EXPECTED_FACTOR = 2
RESULTS_PATH = "lab/results.json"
NEMOTRON_LEDGER_PATH = "lab/nemotron_calls.json"
EXPERIMENTS = ("E1 3 vs 5 forecasts", "E2 without follow-up", "E3 without Wikipedia", "E4 + Nemotron")


# ---------------------------------------------------------------- quota rules


@dataclass
class LabQuota:
    """What the lab may spend right now. Built once per lab run."""

    now: datetime
    minibench_last_open: datetime | None
    expected_questions_left: float
    nemotron_used_today: int = 0
    gemini_started: int = 0
    nemotron_started: int = 0

    def gemini_window(self) -> str | None:
        """None if Gemini may be used now, else why not."""
        local = self.now.astimezone(PACIFIC)
        midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        if midnight - local > GEMINI_LAST_HOURS:
            return "not the last 3 hours of the Pacific quota day"
        if self.minibench_last_open is not None and self.now - self.minibench_last_open < MINIBENCH_QUIET:
            return "a MiniBench question is open or was open in the last 6 hours"
        return None

    def gemini_calls_left(self, ledger, models: tuple[str, ...]) -> int:  # type: ignore[no-untyped-def]
        """Non-reserve quota beyond 2 x the expected questions left, minus what
        the lab already started this run; 0 outside the window."""
        if self.gemini_window() is not None:
            return 0
        usable = sum(max(0, ledger.usable_left(m)) for m in models)
        spare = usable - EXPECTED_FACTOR * self.expected_questions_left
        return max(0, math.floor(spare))

    def nemotron_calls_left(self) -> int:
        return max(0, NEMOTRON_CALLS_PER_DAY - self.nemotron_used_today - self.nemotron_started)


def gemini_model_for_lab(ledger, quota: LabQuota, models: tuple[str, ...], exclude: set[str]) -> str | None:  # type: ignore[no-untyped-def]
    """A Flash model the lab may call once now (non-reserve quota left on it,
    and lab budget left), or None."""
    if quota.gemini_calls_left(ledger, models) <= 0:
        return None
    ranked = sorted((m for m in models if m not in exclude), key=lambda m: -ledger.usable_left(m))
    for model in ranked:
        if ledger.usable_left(model) > 0:
            return model
    return None


# ---------------------------------------------------------------- leak guard


def leak_safe(model: str, question_open: datetime | None, release_dates: dict[str, datetime]) -> bool:
    """False for a model released after the question opened, or of unknown release date."""
    released = release_dates.get(model)
    if released is None or question_open is None:
        return False
    return released <= question_open


# ---------------------------------------------------------------- frozen dossier


SECTION_ORDER = ("official_data", "base", "wikipedia", "followup")


def frozen_dossier(sections: dict[str, dict], drop: tuple[str, ...] = ()) -> str | None:
    """The dossier as live built it, from the saved sections, minus `drop`.
    None if the base research wasn't saved."""
    from followup import with_followup
    from research import MAX_DOSSIER_TOKENS, WORDS_PER_TOKEN, with_official_line

    text = {k: (v or {}).get("text", "") for k, v in (sections or {}).items() if k not in drop}
    base = text.get("base")
    if not base:
        return None
    body = base
    if text.get("wikipedia"):
        room = int(MAX_DOSSIER_TOKENS * WORDS_PER_TOKEN) - len(text["wikipedia"].split()) - 1
        words = body.split()
        body = (body if len(words) <= room else " ".join(words[:room]) + " [...]") + "\n\n" + text["wikipedia"]
    if text.get("official_data"):
        body = with_official_line(body, text["official_data"])
    if text.get("followup"):
        body = with_followup(body, text["followup"])
    return body


# ---------------------------------------------------------------- scores


def numeric_log_density(forecast: dict, resolution: str, lower: float, upper: float) -> float | None:
    """ln of the combined CDF's density at the answer, per unit of the range
    (the out-of-range mass when it resolved outside)."""
    points = forecast.get("declared_percentiles") or []
    if len(points) < 2 or upper <= lower:
        return None
    values = [p["value"] for p in points]
    cdf = [p["percentile"] for p in points]
    if resolution == "below_lower_bound":
        return math.log(max(cdf[0], EPS))
    if resolution == "above_upper_bound":
        return math.log(max(1 - cdf[-1], EPS))
    try:
        answer = float(resolution)
    except ValueError:
        return None
    if answer < values[0]:
        return math.log(max(cdf[0], EPS))
    if answer > values[-1]:
        return math.log(max(1 - cdf[-1], EPS))
    for i in range(1, len(values)):
        if answer <= values[i]:
            width = (values[i] - values[i - 1]) / (upper - lower)
            if width <= 0:
                continue
            return math.log(max((cdf[i] - cdf[i - 1]) / width, EPS))
    return None


def lab_score(question: dict, forecast: Any, resolution: str) -> float | None:
    kind = question.get("question_type")
    if forecast is None:
        return None
    if kind == "binary":
        if resolution.lower() not in ("yes", "no"):
            return None
        return binary_score(float(forecast), resolution.lower() == "yes")
    if kind == "multiple_choice" and isinstance(forecast, dict):
        return multiple_choice_score(forecast, resolution)
    if kind in ("numeric", "discrete") and isinstance(forecast, dict):
        lower = question.get("nominal_lower_bound", question.get("lower_bound"))
        upper = question.get("nominal_upper_bound", question.get("upper_bound"))
        if lower is None or upper is None:
            return None
        return numeric_log_density(forecast, resolution, float(lower), float(upper))
    return None


# ---------------------------------------------------------------- combining (as live)


def combine(question_obj: Any, predictions: list[Any]) -> Any:
    """Our live combine for each type (median / median per option / pointwise median)."""
    from forecasting_tools import BinaryQuestion, MultipleChoiceQuestion

    from distributions import combine_numeric, median_multiple_choice
    from forecast_safety import adjust_binary

    if isinstance(question_obj, BinaryQuestion):
        return adjust_binary([float(p) for p in predictions])
    if isinstance(question_obj, MultipleChoiceQuestion):
        return median_multiple_choice(predictions)
    return combine_numeric(predictions, question_obj)


def jsonable(value: Any) -> Any:
    from question_log import to_jsonable

    return to_jsonable(value)


# ---------------------------------------------------------------- the runner


@dataclass
class LabResult:
    question: Any
    kind: str
    experiment: str
    diff: float | None
    note: str = ""


@dataclass
class LabContext:
    """What the runner needs from the outside world (all injectable for tests)."""

    quota: LabQuota
    ledger: Any  # the live Gemini QuotaLedger (shared store)
    release_dates: dict[str, datetime]
    forecast: Callable[[Any, str, str], Any]  # (question object, research, model) -> prediction value
    flash_models: tuple[str, ...]
    nemotron_model: str
    done: set = field(default_factory=set)  # (post, experiment) already in the results


def _question_object(snapshot: dict) -> Any:
    from forecasting_tools import BinaryQuestion, DiscreteQuestion, MultipleChoiceQuestion, NumericQuestion

    classes = {"binary": BinaryQuestion, "numeric": NumericQuestion, "discrete": DiscreteQuestion,
               "multiple_choice": MultipleChoiceQuestion}
    return classes[snapshot["question_type"]].model_validate(snapshot)


def _live_predictions(question_obj: Any, record: dict) -> list[Any]:
    from main import _saved_prediction

    out = []
    for f in record.get("forecasts", []):
        if f.get("status") == "ok" and f.get("kind") in ("planned", "round2", "quick", "emergency", "backup"):
            try:
                out.append(_saved_prediction(question_obj, {"value": f.get("parsed"), "reasoning": ""}).prediction_value)
            except Exception:
                continue
    return out


def _open_time(snapshot: dict) -> datetime | None:
    text = snapshot.get("open_time")
    if not text:
        return None
    parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _gemini_forecasts(ctx: LabContext, question_obj: Any, research: str, count: int, opened: datetime | None) -> list[Any] | None:
    """`count` forecasts from different Flash models (leak-safe, lab quota), or None."""
    out, used = [], set()
    for _ in range(count):
        blocked = {m for m in ctx.flash_models if not leak_safe(m, opened, ctx.release_dates)}
        model = gemini_model_for_lab(ctx.ledger, ctx.quota, ctx.flash_models, used | blocked)
        if model is None:
            return None
        used.add(model)
        ctx.quota.gemini_started += 1
        try:
            out.append(ctx.forecast(question_obj, research, model))
        except Exception:
            return None
    return out


def run_question(ctx: LabContext, record: dict, resolution: str) -> list[LabResult]:
    snapshot = record["question"]
    post, kind = snapshot.get("id_of_post"), snapshot.get("question_type")
    question_obj = _question_object(snapshot)
    live = record.get("final_forecast")
    live_score = lab_score(snapshot, live, resolution)
    if live_score is None:
        return []
    opened = _open_time(snapshot)
    sections = record.get("dossier_sections") or {}
    live_preds = _live_predictions(question_obj, record)
    results: list[LabResult] = []

    def scored(experiment: str, forecast: Any, flip: bool = False, note: str = "") -> None:
        score = lab_score(snapshot, jsonable(forecast), resolution)
        if score is None:
            results.append(LabResult(post, kind, experiment, None, "not scorable"))
            return
        diff = (live_score - score) if flip else (score - live_score)
        results.append(LabResult(post, kind, experiment, diff, note))

    todo = [e for e in EXPERIMENTS if (post, e) not in ctx.done]
    for experiment in todo:
        if experiment.startswith("E1"):
            if len(live_preds) >= 5:
                scored(experiment, combine(question_obj, live_preds[:3]), flip=True, note="live had 5+: first 3 vs live")
            else:
                research = frozen_dossier(sections)
                extra = _gemini_forecasts(ctx, question_obj, research, 5 - len(live_preds), opened) if research else None
                if extra is None:
                    results.append(LabResult(post, kind, experiment, None, "pending: no Gemini lab quota now"))
                    continue
                scored(experiment, combine(question_obj, live_preds + extra), note="live + extra Gemini")
        elif experiment.startswith(("E2", "E3")):
            section = "followup" if experiment.startswith("E2") else "wikipedia"
            if not (sections.get(section) or {}).get("text"):
                results.append(LabResult(post, kind, experiment, None, f"no {section} section: not applicable"))
                continue
            research = frozen_dossier(sections, drop=(section,))
            preds = _gemini_forecasts(ctx, question_obj, research, 3, opened) if research else None
            if preds is None:
                results.append(LabResult(post, kind, experiment, None, "pending: no Gemini lab quota now"))
                continue
            scored(experiment, combine(question_obj, preds))
        else:  # E4
            research = frozen_dossier(sections) or (record.get("research") or {}).get("text")
            if not live_preds or not research:
                results.append(LabResult(post, kind, experiment, None, "no live forecasts or research"))
                continue
            if not leak_safe(ctx.nemotron_model, opened, ctx.release_dates) or ctx.quota.nemotron_calls_left() <= 0:
                results.append(LabResult(post, kind, experiment, None, "pending: Nemotron not allowed now"))
                continue
            ctx.quota.nemotron_started += 1
            try:
                extra = ctx.forecast(question_obj, research, ctx.nemotron_model)
            except Exception as e:
                results.append(LabResult(post, kind, experiment, None, f"Nemotron failed ({type(e).__name__})"))
                continue
            scored(experiment, combine(question_obj, live_preds + [extra]))
    return results


def report(results: list[LabResult]) -> str:
    """Paired differences per experiment and type: n, mean, bootstrap 90% CI."""
    lines = ["## Replay lab: paired score differences vs live (positive = better)", "",
             "| Experiment | Type | Questions | Mean difference | 90% CI | Pending / n.a. |",
             "|---|---|---|---|---|---|"]
    for experiment in EXPERIMENTS:
        of_exp = [r for r in results if r.experiment == experiment]
        for kind in sorted({r.kind for r in of_exp}) or ["-"]:
            rows = [r for r in of_exp if r.kind == kind]
            diffs = [r.diff for r in rows if r.diff is not None]
            waiting = len(rows) - len(diffs)
            if diffs:
                low, high = bootstrap_ci(diffs)
                lines.append(f"| {experiment} | {kind} | {len(diffs)} | {statistics.fmean(diffs):+.3f} | [{low:+.3f}, {high:+.3f}] | {waiting} |")
            else:
                lines.append(f"| {experiment} | {kind} | 0 | - | - | {waiting} |")
    lines += ["", "E1 is always \"5 forecasts minus 3 forecasts\"; the others are \"variant minus live\"."]
    return "\n".join(lines)


def to_rows(results: list[LabResult], stamp: str) -> list[dict]:
    return [{"post": r.question, "type": r.kind, "experiment": r.experiment, "diff": r.diff, "note": r.note, "at": stamp}
            for r in results]


def run_all(ctx: LabContext, records: list[dict], resolution_of: Callable[[Any], str | None]) -> list[LabResult]:
    results: list[LabResult] = []
    for record in records:
        resolution = resolution_of(record.get("question", {}).get("id_of_post"))
        if resolution:
            results += run_question(ctx, record, resolution)
    return results


def forecast_with_model(bot_factory: Callable[[Any], Any]) -> Callable[[Any, str, str], Any]:  # type: ignore[no-untyped-def]
    """(question, research, model) -> prediction value, with the bot's own
    prompts and direct reading only (no parser call)."""

    def forecast(question_obj: Any, research: str, model: str) -> Any:
        from forecasting_tools import BinaryQuestion, MultipleChoiceQuestion

        bot = bot_factory(model)
        if isinstance(question_obj, BinaryQuestion):
            run = bot._run_forecast_on_binary(question_obj, research)
        elif isinstance(question_obj, MultipleChoiceQuestion):
            run = bot._run_forecast_on_multiple_choice(question_obj, research)
        else:
            run = bot._run_forecast_on_numeric(question_obj, research)
        return asyncio.run(run).prediction_value

    return forecast


# ---------------------------------------------------------------- entry point

# The lab shares the live queue (it uses the Gemini key): it stops starting new
# forecasts after this long, so it never holds up a live run for long.
TIME_BUDGET = timedelta(minutes=6)


def release_dates_from_openrouter(models: tuple[str, ...], get=None) -> dict[str, datetime]:  # type: ignore[no-untyped-def]
    """Release dates from OpenRouter's public model list ('created'); a Gemini
    AI Studio id maps to OpenRouter's google/<name>. Missing: not in the dict."""
    import requests

    get = get or requests.get
    data = {m["id"]: m for m in get("https://openrouter.ai/api/v1/models", timeout=30).json().get("data", [])}
    out = {}
    for model in models:
        key = model.removeprefix("openrouter/")
        if model.startswith("gemini/"):
            key = "google/" + model.removeprefix("gemini/")
        created = (data.get(key) or {}).get("created")
        if created:
            out[model] = datetime.fromtimestamp(int(created), timezone.utc)
    return out


def _bot_factory(pool, nemotron_model: str):  # type: ignore[no-untyped-def]
    from forecasting_tools import GeneralLlm

    import main
    from probe import NoParser

    def make(model: str):  # type: ignore[no-untyped-def]
        if model == nemotron_model:
            llm = GeneralLlm(model=model, temperature=None, timeout=240, allowed_tries=1)
        else:
            # One model, no backups, never the reserve: exactly one call.
            llm = pool._forecaster(model, allow_reserve=False, booked=False)
        parser = NoParser()
        bot = main.FallBot2026(
            llms={"default": llm, "parser": parser, "summarizer": parser, "researcher": "no_research"},
            publish_reports_to_metaculus=False, enable_summarize_research=False,
        )
        bot._structure_output_validation_samples = 1
        return bot

    return make


def synthetic_run() -> str:
    """End-to-end on a synthetic resolved question: recorded replies, a
    simulated ledger inside the Gemini window. Returns the report."""
    from forecasting_tools import BinaryQuestion

    import bot_config
    from gemini_budget import MemoryStore
    from replay import ReplayChainLlm, current_question

    now = datetime(2026, 10, 6, 5, 30, tzinfo=timezone.utc)  # 22:30 Pacific: the last 3 hours
    question = BinaryQuestion(question_text="Synthetic lab question?", id_of_post=99001,
                              page_url="https://www.metaculus.com/questions/99001",
                              open_time=datetime(2026, 10, 1, tzinfo=timezone.utc))
    record = {
        "question": question.model_dump(mode="json", exclude={"api_json"}),
        "final_forecast": 0.37,
        "forecasts": [{"kind": "planned", "status": "ok", "parsed": 0.37} for _ in range(3)],
        "dossier_sections": {
            "base": {"text": "Synthetic base research.", "at": "2026-10-01T10:00:00+00:00"},
            "followup": {"text": "Synthetic follow-up findings.", "at": "2026-10-01T10:05:00+00:00"},
            "wikipedia": {"text": "## Background (Wikipedia, retrieved 2026-10-01)\nSynthetic.", "at": "2026-10-01T10:01:00+00:00"},
        },
    }
    pool = bot_config._gemini_pool(MemoryStore(), llm_class=ReplayChainLlm)
    quota = LabQuota(now=now, minibench_last_open=None, expected_questions_left=4.0)

    # Each synthetic model answers differently (and more so without a section),
    # so the report shows real differences.
    answers = {"gemini/gemini-3.6-flash": 0.55, "gemini/gemini-3.7-flash": 0.60, "gemini/gemini-3.8-flash": 0.50,
               "gemini/gemini-3.5-flash": 0.45, bot_config.BACKUP_FORECAST_MODEL: 0.65}

    def forecast(question_obj, research, model):  # type: ignore[no-untyped-def]
        current_question.set(question_obj)
        p = answers.get(model, 0.5)
        if "follow-up" not in research:
            p -= 0.10
        if "Wikipedia" not in research:
            p -= 0.05
        return p

    release = {m: datetime(2026, 9, 1, tzinfo=timezone.utc) for m in (*bot_config.GEMINI_FORECAST_MODELS, bot_config.BACKUP_FORECAST_MODEL)}
    ctx = LabContext(quota, pool.ledger, release, forecast, tuple(bot_config.GEMINI_FORECAST_MODELS), bot_config.BACKUP_FORECAST_MODEL)
    results = run_all(ctx, [record], lambda post: "yes")
    return report(results) + f"\n\nSynthetic run: {len(results)} result(s); Gemini lab calls {quota.gemini_started}, Nemotron {quota.nemotron_started}; nothing submitted."


def main_run() -> str:
    """The real lab run (workflow 'Replay lab'): resolved live questions."""
    import os

    import bot_config
    import minibench
    import scoreboard
    from bot_helpers import silence_noisy_dependencies
    from gemini_budget import GitHubFileStore, make_store
    from question_log import DATA_REPO, questions_per_day

    silence_noisy_dependencies()
    from forecasting_tools import MetaculusClient

    token = os.environ["DATA_REPO_TOKEN"]
    now = datetime.now(timezone.utc)
    pool = bot_config._gemini_pool(make_store(bot_config.GEMINI_LEDGER_PATH))
    pool.questions_per_day = questions_per_day(token)
    try:
        saved_mb = minibench._default_store().load() or {}
    except Exception:
        saved_mb = {}
    last_open = minibench._time(saved_mb.get("last_open_seen"))
    pool.minibench_active = last_open is not None and now - last_open <= minibench.ACTIVE_FOR
    nemotron_store = GitHubFileStore(DATA_REPO, NEMOTRON_LEDGER_PATH, token)
    try:
        nemotron_days = nemotron_store.load() or {}
    except Exception:
        nemotron_days = {}
    today = now.date().isoformat()
    quota = LabQuota(now, last_open, pool.expected_questions_left_today(now), int(nemotron_days.get(today, 0)))
    results_store = GitHubFileStore(DATA_REPO, RESULTS_PATH, token)
    try:
        rows = results_store.load() or []
    except Exception:
        rows = []
    done = {(r["post"], r["experiment"]) for r in rows if r.get("diff") is not None or "not applicable" in r.get("note", "")}
    models = (*bot_config.GEMINI_FORECAST_MODELS, bot_config.BACKUP_FORECAST_MODEL)
    try:
        release = release_dates_from_openrouter(models)
    except Exception:
        release = {}  # leak guard: nothing is used without a known date
    deadline = datetime.now(timezone.utc) + TIME_BUDGET
    forecast = forecast_with_model(_bot_factory(pool, bot_config.BACKUP_FORECAST_MODEL))

    def timed(question_obj, research, model):  # type: ignore[no-untyped-def]
        if datetime.now(timezone.utc) > deadline:
            raise TimeoutError("lab time budget used")
        return forecast(question_obj, research, model)

    ctx = LabContext(quota, pool.ledger, release, timed, tuple(bot_config.GEMINI_FORECAST_MODELS), bot_config.BACKUP_FORECAST_MODEL, done)
    client = MetaculusClient()

    def resolution_of(post):  # type: ignore[no-untyped-def]
        question = client.get_question_by_post_id(post)
        if isinstance(question, list):
            question = question[0]
        return getattr(question, "resolution_string", None)

    records = scoreboard.latest_submitted_records(token)
    results = run_all(ctx, records, resolution_of)
    kept = [r for r in rows if (r["post"], r["experiment"]) in done]
    new_rows = to_rows([r for r in results if (r.question, r.experiment) not in done], now.isoformat())
    results_store.save(kept + new_rows, message=f"Replay lab {today}")
    pool.save()
    nemotron_days[today] = int(nemotron_days.get(today, 0)) + quota.nemotron_started
    nemotron_store.save({k: v for k, v in nemotron_days.items() if k >= (now - timedelta(days=7)).date().isoformat()}, message="Lab Nemotron calls")
    all_rows = kept + new_rows
    text = report([LabResult(r["post"], r["type"], r["experiment"], r["diff"], r.get("note", "")) for r in all_rows])
    window = quota.gemini_window()
    return text + (f"\n\nGemini this run: {'not allowed (' + window + ')' if window else str(quota.gemini_started) + ' lab call(s)'}; "
                   f"Nemotron: {quota.nemotron_started}; nothing submitted.")


if __name__ == "__main__":
    import os
    import sys

    text = synthetic_run() if "--synthetic" in sys.argv else main_run()
    print(text)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")
