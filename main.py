import argparse
import asyncio
import os
import contextvars
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import dotenv

# Runtime helpers (env validation, banners, dependency-warning suppression).
from bot_helpers import (
    PUBLIC_LOGGER_NAME,
    check_environment,
    configure_public_logging,
    describe_exception,
    log_question_statuses,
    print_run_summary_banner,
    print_startup_banner,
    silence_noisy_dependencies,
    write_cost_summary,
)

silence_noisy_dependencies()

from forecasting_tools import (
    AskNewsSearcher,
    BinaryQuestion,
    ForecastBot,
    GeneralLlm,
    MetaculusClient,
    MetaculusQuestion,
    MultipleChoiceQuestion,
    NumericDistribution,
    NumericQuestion,
    DateQuestion,
    DatePercentile,
    Percentile,
    ConditionalQuestion,
    ConditionalPrediction,
    ForecastReport,
    PredictionTypes,
    PredictionAffirmed,
    BinaryPrediction,
    PredictedOptionList,
    ReasonedPrediction,
    SmartSearcher,
    clean_indents,
    structure_output,
)

from forecasting_tools.data_models.forecast_report import ResearchWithPredictions

import ensemble
from bot_config import (
    BACKUP_FORECASTS,
    BACKUP_FORECAST_TIMEOUT_SECONDS,
    MARKET_MODE,
    SHADOW_FORECAST_MODEL,
    REHEARSAL_CREDIT,
    CreditsPlanner,
    GEMINI_FORECAST_MODELS,
    GEMINI_PARSER_MODELS,
    GeminiPool,
    ReplayCreditsPlanner,
    get_lineup,
)
from ensemble import round2_needed
from forecast_safety import (
    NoValidForecast,
    adjust_binary,
    all_outside_range,
    dates_line,
    distribution_problems,
    fix_reversed_percentiles,
    floor_multiple_choice,
    off_by_10x,
    question_range,
    still_open_problem,
)
from deadlines import MIN_FORECAST_SECONDS, MIN_QUICK_FORECAST_SECONDS, planned_forecast_timeout, quick_forecast_timeout
from run_timing import (
    FORECAST_STAGES_LIMIT_SECONDS,
    PLANNED_WAIT_SECONDS,
    RESEARCH_GRACE_SECONDS,
    RESEARCH_LIMIT_SECONDS,
    START_CUTOFF_SECONDS,
    QuestionDeferred,
    ResearchQueue,
    capped,
    research_key,
    research_order,
    shadow_seconds,
)
from answer_parsing import (
    parse_binary_answer,
    parse_multiple_choice_answer,
    read_multiple_choice_values,
    parse_percentile_answer,
)
from distributions import (
    STEP8_PERCENTILES,
    combine_numeric,
    median_multiple_choice,
    pchip_distribution,
)
from free_news import collect_free_news, count_asknews_articles, format_articles
from llm_throttle import answered_models
from shadow import (
    REFEREE_ENABLED,
    combine_with_shadow,
    multiple_choice_shadows,
    numeric_shadows,
    referee_shadow,
    two_line_reason,
    zero_cost_shadows,
)
from markets import match_question
from markets import to_records as market_records
from minibench import current_minibench_id, minibench_active
from real_regression import load_questions as load_regression_questions
from real_regression import reading_table
from replay import REPLAY_RESEARCH, ReplayLlm, _RecordedAnswer
from replay import current_question as replay_question
from followup import FOLLOWUP_ENABLED, MIN_MINUTES_TO_CLOSE, run_followup, with_followup
from followup import disagreement as followup_disagreement
from followup import reason_of as followup_reason
from research import MAX_ASKNEWS_CALLS, asknews_search, run_planned_research, with_official_line
from gemini_budget import current_question_key
from consistency import INDEX_PATH, ForecastIndex
from consistency import shadow_for as consistency_shadow_for
from hard_data import MAX_LINE_WORDS, hard_data_for, official_line
from spend import SpendGuard, error_details, guard_tier, key_limit_alert, paid_fallback_guard, unknown_cost_alert, utc_day
from spend import question_seasonal as spend_question_seasonal
from window_baseline import fetch_full_history, window_baseline
from question_log import QuestionLogWriter, question_snapshot, questions_per_day, record_path, shadows_path, to_jsonable, utc_now

dotenv.load_dotenv()
# Only this logger's messages reach the public Actions log in full; see
# bot_helpers.configure_public_logging.
logger = logging.getLogger(PUBLIC_LOGGER_NAME)


# Fall 2026 targets. Set explicitly rather than via the forecasting-tools
# MetaculusClient.CURRENT_* constants, so a library update can't silently
# move the bot to another tournament.
FALL_2026_TOURNAMENT_ID = "fall-futureeval-2026"  # id 33121
# The Fall 2026 MiniBench: the 'minibench' slug, which the library also uses.
# MiniBench runs in rounds; each live run looks up the current round
# (minibench.current_minibench_id) and falls back to this slug.
FALL_2026_MINIBENCH_ID = "minibench"
BOT_TESTING_AREA_ID = 32977  # https://www.metaculus.com/tournament/bot-testing-area/
# Question types the Fall tournament uses, and how many of each to forecast
# in a test run.
TOURNAMENT_QUESTION_TYPES = ("binary", "numeric", "discrete", "multiple_choice")
TEST_QUESTIONS_PER_TYPE = 1


def pick_test_questions(
    questions: list[MetaculusQuestion],
) -> list[MetaculusQuestion]:
    picked: list[MetaculusQuestion] = []
    for question_type in TOURNAMENT_QUESTION_TYPES:
        of_type = [q for q in questions if q.question_type == question_type]
        picked.extend(of_type[:TEST_QUESTIONS_PER_TYPE])
    return picked


def _http_status_note(error: BaseException) -> str:
    """ " (HTTP 429)" if an HTTP status code is somewhere in the error chain."""
    current: BaseException | None = error
    for _ in range(6):
        if current is None:
            break
        status = getattr(getattr(current, "response", None), "status_code", None)
        if isinstance(status, int):
            return f" (HTTP {status})"
        current = current.__cause__ or current.__context__
    return ""


def choose_spending_tier(spend=None) -> str:  # type: ignore[no-untyped-def]
    """
    Step 6: target spend per question = (remaining credit - 15% reserve) /
    expected remaining questions -> Full / Standard / Lean. A change from the
    last run's tier (kept in fall26-data) opens a GitHub issue (emails Tony).
    """
    import ensemble
    from gemini_budget import make_store

    credit = ensemble.openrouter_credit()
    if credit is None:
        # Fail closed (4d): unknown credit, cheapest tier.
        logger.warning("Credit unknown; Lean tier for this run")
        tier, target = "lean", ensemble.TIERS["lean"].rough_cost
    else:
        remaining, limit = credit
        target = ensemble.target_spend_per_question(
            remaining, limit, ensemble.expected_remaining_questions()
        )
        tier = ensemble.choose_tier(target)
    # 4d: ledger unreadable, daily cap, key usage vs ledger -> Lean (+ alert).
    tier = guard_tier(tier, target, spend, ensemble.openrouter_key_usage(), ensemble.open_alert_issue)
    if spend is not None:
        spend.save()
    store = make_store("status/spending_tier.json")
    try:
        previous = (store.load() or {}).get("tier")
    except Exception:
        previous = None
    if previous != tier:
        ensemble.notify_tier_change(previous, tier, target)
        try:
            store.save({"tier": tier, "target": round(target, 4)})
        except Exception:
            logger.warning("Spending tier could not be saved")
    logger.info(f"Spending tier: {tier} (target ${target:.2f} per question)")
    return tier


def _saved_prediction(question: MetaculusQuestion, saved: dict) -> ReasonedPrediction:
    """A finished forecast saved by an earlier run (credits 4d), rebuilt."""
    value = saved["value"]
    if isinstance(question, BinaryQuestion):
        prediction: Any = float(value)
    elif isinstance(question, MultipleChoiceQuestion):
        prediction = PredictedOptionList.model_validate(value)
    elif isinstance(question, NumericQuestion):
        prediction = NumericDistribution.model_validate(value)
    else:
        raise ValueError(f"no saved forecasts for {type(question).__name__}")
    return ReasonedPrediction(prediction_value=prediction, reasoning=saved.get("reasoning") or "")


# Spend-guard rehearsals (Test Bot replay-credits, credits 4d).
SPEND_REHEARSALS = {
    "runaway": "every model times out; each question stays under its cap",
    "retry-run": "pass 1's submission fails; pass 2 buys nothing",
    "unknown-cost": "paid replies have no cost; they are charged the estimate, hit the (small) caps, alert",
    "missing-ledger": "the spend ledger is missing; Lean tier + alert",
    "key-mismatch": "the key spent $1 more than our ledger; Lean tier + alert",
}


def runaway_table(spend) -> tuple[bool, str]:  # type: ignore[no-untyped-def]
    """Credits 4d runaway rehearsal: each question's charged spend vs its cap
    (estimates only: every call timed out). Dollars, no forecasts."""
    lines = [
        "## Spend caps: each question's charged spend vs its cap",
        "",
        "| Question | Charged (estimates) | Cap (2x tier cost) | Under the cap |",
        "|---|---|---|---|",
    ]
    ok = bool(spend.caps)
    for question, cap in sorted(spend.caps.items()):
        spent = spend.question_spent(question)
        under = spent <= cap + 1e-9
        ok = ok and under
        lines.append(f"| {question} | ${spent:.2f} | ${cap:.2f} | {'yes' if under else '**NO**'} |")
    lines.append("")
    lines.append(f"Paid calls started: {spend.paid_calls}; refused at the cap: {spend.refused}. **All under the cap: {'yes' if ok else 'NO'}**")
    return ok, "\n".join(lines)


def rehearsal_table(records: list[dict]) -> str:
    """Credits rehearsal summary: per question, the model slots used (planned
    -> answered, a backup shows as a different answered model), the tier and
    whether round 2 ran. Model names only: no forecast values (public logs)."""

    def short(model: str | None) -> str:
        name = (model or "none").removeprefix("replay/")
        return name.split("/")[-1] + (" (free key)" if name.startswith("gemini/") else "")

    lines = [
        "| Question | Type | Tournament | Tier | Round 2 | Slots (kind: planned -> answered) | Submitted |",
        "|---|---|---|---|---|---|---|",
    ]
    for record in records:
        question = record.get("question") or {}
        slots = "; ".join(
            f"{f.get('kind')}: {short(f.get('planned_model'))} -> "
            + (", ".join(short(m) for m in f.get("answered_models") or []) or "failed")
            for f in record.get("forecasts", [])
        )
        lines.append(
            f"| {question.get('id_of_post')} | {question.get('question_type', '?')} "
            f"| {'seasonal' if record.get('seasonal') else 'MiniBench'} | {record.get('tier', '?')} "
            f"| {'yes' if record.get('round2') else 'no'} | {slots} | {'yes' if record.get('submitted') else 'no'} |"
        )
    return "\n".join(lines)


def backup_chain_summary(records: list[dict], ledger, flash_lite_before: dict[str, int]) -> str:  # type: ignore[no-untyped-def]
    """replay-gemini: which backup chain each question used, and whether the
    Flash-Lite reserve was touched. Model names and counts only."""
    lines = [
        "| Question | Type | Backup chain | Answered by | Submitted |",
        "|---|---|---|---|---|",
    ]
    for record in records:
        question = record.get("question") or {}
        answered = sorted({
            m.split("/")[-1] for f in record.get("forecasts", []) if f.get("status") == "ok"
            for m in f.get("answered_models") or []
        })
        lines.append(
            f"| {question.get('id_of_post')} | {question.get('question_type', '?')} "
            f"| {record.get('emergency', 'none')} | {', '.join(answered) or 'none'} "
            f"| {'yes' if record.get('submitted') else 'no'} |"
        )
    if flash_lite_before:
        after = {m: ledger.used.get(m, 0) for m in flash_lite_before}
        untouched = after == flash_lite_before
        lines.append("")
        lines.append(
            "Flash-Lite used (before -> after): "
            + ", ".join(f"{m.split('/')[-1]} {flash_lite_before[m]} -> {after[m]}" for m in flash_lite_before)
            + f". **Reserve untouched: {'yes' if untouched else 'NO'}**"
        )
    return "\n".join(lines)


# Real-regression: questions forecast at the same time.
REGRESSION_BATCH = 4

# Retry backoff (28 Sep): a question that failed is tried again in the next
# run, then only every 30 minutes, except that every run tries it in the
# last 45 minutes before its close (the Flash-Lite emergency window).
RETRY_BACKOFF = timedelta(minutes=30)
RETRY_STATE_PATH = "status/retry_state.json"
RETRY_STATE_KEEP = timedelta(days=3)


def should_try(entry: dict | None, close_time: datetime | None, now: datetime) -> bool:
    """entry: this question's retry state ({"failures", "last_failure"}) or None."""
    if not entry or int(entry.get("failures", 0)) <= 1:
        return True
    if close_time is not None and closes_within_at(close_time, EMERGENCY_WINDOW, now):
        return True
    last = datetime.fromisoformat(entry["last_failure"])
    return now - last >= RETRY_BACKOFF


def closes_within_at(close_time: datetime, window: timedelta, now: datetime) -> bool:
    close = close_time if close_time.tzinfo else close_time.replace(tzinfo=timezone.utc)
    return close - now <= window


def update_retry_state(state: dict, attempted: list, failed: set, now: datetime) -> dict:
    """After a run: failures +1 for failed questions, cleared for the others;
    entries older than 3 days are dropped."""
    for post in attempted:
        key = str(post)
        if post in failed:
            entry = state.get(key, {})
            state[key] = {"failures": int(entry.get("failures", 0)) + 1, "last_failure": now.isoformat()}
        else:
            state.pop(key, None)
    return {
        k: v for k, v in state.items()
        if now - datetime.fromisoformat(v["last_failure"]) <= RETRY_STATE_KEEP
    }


def run_queue(seasonal: list, minibench: list) -> tuple[list, dict]:
    """
    Run timing (architect, 29 Sep): seasonal and MiniBench questions go in ONE
    run queue (no batch waits for another), researched soonest-closing first.
    Returns (questions in research order, post id -> is seasonal). Seasonal
    keeps its quota priority in planning (GeminiPool.plan).
    """
    seasonal_by_post = {q.id_of_post: True for q in seasonal}
    seasonal_by_post.update({q.id_of_post: False for q in minibench})
    return research_order([*seasonal, *minibench]), seasonal_by_post


# A question with no forecast that closes within this time can't count on a
# later run to retry it, so the run goes red.
RETRY_WINDOW = timedelta(minutes=25)


def closes_within(close_time: datetime, window: timedelta) -> bool:
    close = close_time if close_time.tzinfo else close_time.replace(tzinfo=timezone.utc)
    return close - datetime.now(timezone.utc) <= window


# The quick forecast (nothing else finished) tries this many passes through
# the Gemini models, this far apart.
QUICK_FORECAST_PASSES = 3
# Emergency backup chain (Flash-Lite + Nemotron, reserve allowed) for a
# question closing within this time.
EMERGENCY_WINDOW = timedelta(minutes=45)
QUICK_FORECAST_RETRY_WAIT_SECONDS = 30

# The forecaster chain for the forecast running in the current asyncio task.
_planned_forecaster: contextvars.ContextVar = contextvars.ContextVar(
    "planned_forecaster", default=None
)
# True inside a backup-chain forecast made before the 45-min window: its
# answer may be parsed only with Flash-Lite quota above the reserve.
_parser_above_reserve: contextvars.ContextVar = contextvars.ContextVar(
    "parser_above_reserve", default=False
)
# Run timing: the question in this task is seasonal (True), MiniBench (False),
# or not set (the bot's forecasting_seasonal applies).
_question_seasonal: contextvars.ContextVar = contextvars.ContextVar("question_seasonal", default=None)
# Run timing: event-loop time when this question's research must stop.
_research_deadline: contextvars.ContextVar = contextvars.ContextVar("research_deadline", default=None)
# True inside the shadow forecaster's own task (Build 1b): its answer is only
# read directly (no parser call) and its reading is logged apart from live.
_in_shadow: contextvars.ContextVar = contextvars.ContextVar("in_shadow", default=False)
# Build 2c: how long research waits for the official data fetch (it runs
# beside research and is usually done first).
HARD_DATA_WAIT_SECONDS = 45
# The window baseline's full-history fetch (after submission; a shadow only).
WINDOW_HISTORY_WAIT_SECONDS = 60
# The shadow forecaster (never submitted): its hard time limit, and its name
# in the question log's "shadow" variants.
SHADOW_FORECAST_TIMEOUT_SECONDS = 240
SHADOW_VARIANT = "free-shadow"


class FallBot2026(ForecastBot):
    """
    Our bot for the Fall 2026 FutureEval bot tournament and MiniBench, built on
    the Metaculus template bot (identical to Metaculus's FallTemplateBot2026).
    This is a copy of what is used by Metaculus to run the Metac Bots in our benchmark, provided as a template for new bot makers.
    This template is given as-is, and is use-at-your-own-risk.
    We have covered most test cases in forecasting-tools it may be worth double checking key components locally.
    So far our track record has been 1 mentionable bug per season (affecting forecasts for 1-2% of total questions)

    Note: the Fall tournament uses binary, numeric, discrete and multiple choice questions.
    Date and conditional question support is kept only for forecasting on the main site.

    The main entry point of this bot is `bot.forecast_on_tournament(tournament_id)` in the parent class.
    See the script at the bottom of the file for more details on how to run the bot.
    Ignoring the finer details, the general flow is:
    - Load questions from Metaculus
    - For each question
        - Execute run_research a number of times equal to research_reports_per_question
        - Execute respective run_forecast function `predictions_per_research_report * research_reports_per_question` times
        - Aggregate the predictions
        - Submit prediction (if publish_reports_to_metaculus is True)
    - Return a list of ForecastReport objects

    Alternatively, you can use the MetaculusClient to make a custom filter of questions to forecast on
    and forecast them with `bot.forecast_questions(questions)`

    Only the research and forecast functions need to be implemented in ForecastBot subclasses,
    though you may want to override other ForecastBot functions.
    In this example, you can change the prompts to be whatever you want since,
    structure_output uses an LLM to intelligently reformat the output into the needed structure.

    By default (i.e. 'tournament' mode), when you run this script, it will forecast on any open questions in the
    primary bot tournament and MiniBench. If you want to forecast on only one or the other, you can remove one
    of them from the 'tournament' mode code at the bottom of the file.

    You can experiment with what models work best with your bot by using the `llms` parameter when initializing the bot.
    You can initialize the bot with any number of models. For example,
    ```python
    my_bot = MyBot(
        ...
        llms={  # choose your model names or GeneralLlm llms here, otherwise defaults will be chosen for you
            "default": GeneralLlm(
                model="openrouter/openai/gpt-4o", # "anthropic/claude-sonnet-4-20250514", etc (see docs for litellm)
                temperature=0.3,
                timeout=40,
                allowed_tries=2,
            ),
            "summarizer": "openai/gpt-4o-mini",
            "researcher": "asknews/news-summaries",
            "parser": "openai/gpt-4o-mini",
        },
    )
    ```

    Then you can access the model in custom functions like this:
    ```python
    research_strategy = self.get_llm("researcher", "model_name"
    if research_strategy == "asknews/news-summaries":
        ...
    # OR
    summarizer = await self.get_llm("summarizer", "llm").invoke(prompt)
    # OR
    reasoning = await self.get_llm("default", "llm").invoke(prompt)
    ```

    If you end up having trouble with rate limits and want to try a more sophisticated rate limiter try:
    ```python
    from forecasting_tools import RefreshingBucketRateLimiter
    rate_limiter = RefreshingBucketRateLimiter(
        capacity=2,
        refresh_rate=1,
    ) # Allows 1 request per second on average with a burst of 2 requests initially. Set this as a class variable
    await self.rate_limiter.wait_till_able_to_acquire_resources(1) # 1 because it's consuming 1 request (use more if you are adding a token limit)
    ```
    Additionally OpenRouter has large rate limits immediately on account creation
    """

    # One question researches at a time, soonest-closing first: the run
    # timing's ResearchQueue (run_timing.py) replaces the template's semaphore.
    _structure_output_validation_samples = 2
    # Test switch: make every question fail, to prove a failed run shows red.
    break_on_purpose = False
    # Test switch: make every planned forecast fail, to prove the quick
    # forecast still submits one.
    fail_planned_forecasts = False
    # Where per-question JSON records go (None = not saved), and the run mode
    # used in their path. Set in __main__.
    question_log: QuestionLogWriter | None = None
    run_mode = "tournament"
    # Whether to submit forecasts to Metaculus. The library's own
    # publish_reports_to_metaculus stays False, so the bot can check that the
    # question is still open right before submitting.
    submit_forecasts = False
    # Close times of questions that got no forecast this run (post id -> close).
    unforecast_close_times: dict = {}
    # Free Gemini lineup: plans each question's forecasters within the day's
    # budget. Set in __main__ from the lineup.
    planner: GeminiPool | None = None
    # Whether the tournament being forecast is the seasonal one (it gets
    # priority when the budget is low). The default for questions not in
    # seasonal_by_post (the run queue sets it per question).
    _forecasting_seasonal = True
    # Run timing: post id -> seasonal (True) or MiniBench (False).
    seasonal_by_post: dict = {}
    # Run timing: event-loop time the run started (None = at the first
    # forecast_questions call). Set in __main__ from the process start.
    run_started: float | None = None
    # Test switch: forecast with only this Gemini model.
    only_model: str | None = None
    # Credits rehearsal: keep each question's record for the job summary.
    keep_records = False
    # Build 1b: the shadow forecaster's LLM (never submitted), or None.
    shadow_llm = None
    # Test switch (replay-credits --retry-run): the submission fails, so the
    # question is retried with its finished forecasts saved.
    fail_submission = False
    # Related-question consistency shadow: our latest submitted binary
    # forecasts (fall26-data index in tournament mode), or None.
    consistency_index: ForecastIndex | None = None

    @property
    def forecasting_seasonal(self) -> bool:
        flag = _question_seasonal.get()
        return self._forecasting_seasonal if flag is None else flag

    @forecasting_seasonal.setter
    def forecasting_seasonal(self, value: bool) -> None:
        self._forecasting_seasonal = value

    def _run_elapsed(self) -> float:
        loop = asyncio.get_running_loop()
        if self.run_started is None:
            self.run_started = loop.time()
        return loop.time() - self.run_started

    async def _aggregate_predictions(
        self, predictions: list[PredictionTypes], question: MetaculusQuestion
    ) -> PredictionTypes:
        # Kept for the shadow forecaster's "live median with it added".
        if not _in_shadow.get():
            self.__dict__.setdefault("_live_predictions", {})[id(question)] = list(predictions)
        # Safety checks on the final forecast (forecast_safety.py, PLAN.md Step 4).
        if isinstance(question, BinaryQuestion):
            # Step 10: free shadow variants, saved but never submitted.
            self._record_for(question)["shadow"] = zero_cost_shadows(predictions)  # type: ignore[arg-type]
            # Median, stretch (off), [market blend], extreme check, clip 2-98%.
            return adjust_binary(predictions)  # type: ignore[arg-type]
        if isinstance(question, MultipleChoiceQuestion):
            # Step 10: free shadow variants, saved but never submitted.
            record = self._record_for(question)
            raw = [
                read_multiple_choice_values(f.get("raw_output") or "", question.options)
                for f in record["forecasts"]
                if f.get("status") == "ok"
            ]
            record["shadow"] = to_jsonable(
                multiple_choice_shadows(
                    predictions,  # type: ignore[arg-type]
                    raw=[r for r in raw if r is not None] if None not in raw else None,
                )
            )
            # Step 8: median per option, renormalise, 1% floor, renormalise.
            return median_multiple_choice(predictions)  # type: ignore[arg-type]
        if isinstance(question, NumericQuestion):
            # Unit check: drop models still over 10x away from the current
            # value, unless every model is (then the value is probably wrong).
            predictions = self._drop_unit_flagged(question, predictions)  # type: ignore[arg-type]
            # Step 8: pointwise median of the models' CDFs, then 95% of it with
            # 5% uniform over the question's range; then the Step 4 checks.
            aggregated = combine_numeric(predictions, question)  # type: ignore[arg-type]
            # Step 10: free shadow variants, saved but never submitted.
            self._record_for(question)["shadow"] = to_jsonable(
                numeric_shadows(predictions, question)
            )
            problems = distribution_problems(aggregated, question)
            if problems:
                raise NoValidForecast(
                    f"combined distribution broke the platform rules ({'; '.join(problems)})"
                )
            return aggregated
        return await super()._aggregate_predictions(predictions, question)

    def _drop_unit_flagged(self, question: MetaculusQuestion, predictions: list) -> list:  # type: ignore[type-arg]
        flagged_ids = self.__dict__.get("_unit_flagged", {})
        flagged = [p for p in predictions if flagged_ids.get(id(p)) is p]
        for p in flagged:
            flagged_ids.pop(id(p), None)
        if not flagged:
            return predictions
        if len(flagged) == len(predictions):
            logger.warning(
                f"Question {question.id_of_post}: every model is over 10x away from the "
                "current value in research; keeping them all (the current value is "
                "probably wrong)"
            )
            return predictions
        logger.warning(
            f"Question {question.id_of_post}: dropping {len(flagged)} of "
            f"{len(predictions)} forecast(s) over 10x away from the current value"
        )
        return [p for p in predictions if not any(p is f for f in flagged)]

    def get_llm(self, purpose="default", guarantee_type=None):  # type: ignore[override]
        if purpose == "parser" and _in_shadow.get():
            # The shadow forecaster never uses the live parser quota.
            raise NoValidForecast("shadow forecaster: answer not readable directly")
        if purpose == "parser" and _parser_above_reserve.get() and isinstance(self.planner, GeminiPool):
            return self.planner.parser(allow_reserve=False)
        # A planned forecast uses the model chain chosen for it.
        if purpose == "default":
            planned = _planned_forecaster.get()
            if planned is not None:
                return planned
        return super().get_llm(purpose, guarantee_type)

    async def _research_and_make_predictions(
        self, question: MetaculusQuestion
    ) -> ResearchWithPredictions[PredictionTypes]:
        if self.planner is None:
            return await super()._research_and_make_predictions(question)
        # As in ForecastBot, but the number of forecasts and their models come
        # from the day's Gemini budget (up to 3 different models, at least 1).
        notepad = await self._get_notepad(question)
        notepad.total_research_reports_attempted += 1
        record = self._record_for(question)
        # Paid-Flash fallback: paid only until the question has 3 real forecasts.
        spend = getattr(self.planner, "spend", None)
        if spend is not None and spend.answered_count is None:
            spend.answered_count = self.ok_forecasts
        research = await self.run_research(question)
        record["research"] = {"fetched_at": utc_now(), "text": research}
        summary_report = await self.summarize_research(question, research)
        # Run timing: planned forecasts, quick forecast, follow-up search and
        # round 2 all end within 12 minutes of the end of research; planned
        # forecasts are waited for until minute 9, leaving the quick forecast
        # minutes 9-12 if none finished.
        loop = asyncio.get_running_loop()
        stages_end = loop.time() + FORECAST_STAGES_LIMIT_SECONDS
        planned_end = loop.time() + PLANNED_WAIT_SECONDS

        def stage_left() -> float:
            return max(0.0, stages_end - loop.time())

        is_binary = isinstance(question, BinaryQuestion)
        forecasters = self.planner.plan(
            seasonal=self.forecasting_seasonal,
            only_model=self.only_model,
            binary=is_binary,
        )
        if hasattr(self.planner, "tier_name"):
            record["tier"] = self.planner.tier_name(self.forecasting_seasonal)
        record["round2"] = False
        plan_note = ""
        if isinstance(self.planner, GeminiPool):
            # Build 4: how many forecasts and why (the Scoreboard can split by it).
            record["extra_forecasts"] = dict(self.planner.last_plan)
            plan_note = f"; {self.planner.last_plan.get('reason')}"
        logger.info(
            f"Question {question.id_of_post}: {len(forecasters)} forecast(s) planned "
            f"({', '.join(f.model for f in forecasters) or 'none: no quota left'}){plan_note}"
        )

        async def forecast_with(forecaster, kind: str, timeout: float | None, above_reserve: bool = False):  # type: ignore[no-untyped-def]
            _planned_forecaster.set(forecaster)
            _parser_above_reserve.set(above_reserve)
            answered_models.set([])
            entry = {"kind": kind, "planned_model": forecaster.model, "started_at": utc_now()}
            record["forecasts"].append(entry)
            started = time.monotonic()
            try:
                if kind == "planned" and self.fail_planned_forecasts:
                    raise RuntimeError("Deliberate failure (--fail-planned-forecasts)")
                # ForecastBot's own _make_prediction: the time limit is set here.
                prediction = await asyncio.wait_for(
                    ForecastBot._make_prediction(self, question, research), timeout
                )
            except BaseException as e:
                entry.update(status="failed", error=describe_exception(e))
                # 7 Oct: the HTTP status code and the provider's error type, if any
                # (private question log only; numbers and short labels).
                entry.update(error_details(e))
                if isinstance(e, asyncio.TimeoutError):
                    logger.warning(
                        f"Question {question.id_of_post}: a {kind} forecast ran out of time"
                    )
                raise
            finally:
                entry["seconds"] = round(time.monotonic() - started, 1)
                entry["answered_models"] = list(answered_models.get() or [])
            entry.update(
                status="ok",
                raw_output=prediction.reasoning,
                parsed=to_jsonable(prediction.prediction_value),
            )
            self._count_ok(question)
            return prediction

        # The full set must be done 15 minutes before the close (and by minute
        # 9 of the forecasting stages); what finished by then is combined (median).
        timeout = capped(planned_forecast_timeout(question.close_time), max(0.0, planned_end - loop.time()))
        planned_models = [f.model for f in forecasters]
        # Credits 4d, never re-buy: forecasts this question already finished in
        # an earlier run are reused, not bought again.
        saved = self._saved_forecasts(question)
        forecasters, reused = self._reuse_finished(question, record, forecasters, saved, "planned")
        valid_predictions, errors, exception_group = (
            await self._gather_results_and_exceptions(
                [forecast_with(f, "planned", timeout) for f in forecasters]
            )
        )
        valid_predictions = reused + valid_predictions
        # Build 5: round 1 disagrees -> a follow-up search aimed at the
        # disagreement; round 2 uses the updated dossier.
        try:
            research, followed_up = await asyncio.wait_for(
                self._followup_search(question, record, valid_predictions, research), stage_left()
            )
        except asyncio.TimeoutError:
            record.setdefault("followup", {})["skipped"] = "12-minute stage limit"
            logger.info(f"Question {question.id_of_post}: follow-up search stopped (12-minute limit)")
            followed_up = False
        # Step 6: binary round 2 only when round 1 disagrees or is extreme;
        # numeric / multiple choice (Gemini pool) only after a follow-up search.
        binary_round2 = is_binary and round2_needed(
            [p.prediction_value for p in valid_predictions]  # type: ignore[misc]
        )
        other_round2 = not is_binary and followed_up and isinstance(self.planner, GeminiPool)
        wants_round2 = valid_predictions and not self.only_model and (binary_round2 or other_round2)
        if wants_round2 and stage_left() < MIN_FORECAST_SECONDS:
            record["round2_skipped"] = "12-minute stage limit"
            logger.info(f"Question {question.id_of_post}: round 2 skipped (12-minute limit)")
        elif wants_round2:
            extra = self.planner.round2(
                seasonal=self.forecasting_seasonal,
                used_models=planned_models,
            )
            logger.info(
                f"Question {question.id_of_post}: round 1 disagrees or is extreme, "
                f"round 2 with {len(extra)} more forecast(s)"
            )
            record["round2"] = bool(extra)
            extra, reused = self._reuse_finished(question, record, extra, saved, "round2")
            valid_predictions += reused
            if extra:
                more, more_errors, _ = await self._gather_results_and_exceptions(
                    [
                        forecast_with(f, "round2", capped(planned_forecast_timeout(question.close_time), stage_left()))
                        for f in extra
                    ]
                )
                valid_predictions += more
                errors += more_errors
        # Nothing finished: one quick forecast with whichever model has quota.
        # Google's free tier is often briefly overloaded, so it gets a few
        # passes through the models, while time before the close allows.
        for quick_pass in range(QUICK_FORECAST_PASSES):
            if valid_predictions:
                break
            quick_timeout = quick_forecast_timeout(question.close_time)
            if quick_timeout != 0:
                quick_timeout = capped(quick_timeout, stage_left())
            if quick_timeout is not None and quick_timeout < MIN_QUICK_FORECAST_SECONDS:
                break  # no time left (to the close, or in the 12-minute limit)
            if not self.planner.any_quota_left():
                break
            if quick_pass > 0:
                await asyncio.sleep(QUICK_FORECAST_RETRY_WAIT_SECONDS)
                if stage_left() < MIN_QUICK_FORECAST_SECONDS:
                    break
                quick_timeout = capped(quick_timeout, stage_left())
            logger.warning(
                f"Question {question.id_of_post}: no planned forecast finished, "
                f"making one quick forecast (pass {quick_pass + 1})"
            )
            valid_predictions, quick_errors, exception_group = (
                await self._gather_results_and_exceptions(
                    [forecast_with(self.planner.quick_forecaster(), "quick", quick_timeout)]
                )
            )
            errors = errors + quick_errors
        # Backup chain (architect, 29 Sep): no Flash forecaster answered.
        if (
            not valid_predictions
            and isinstance(self.planner, GeminiPool)
            and not self.only_model
            and quick_forecast_timeout(question.close_time) != 0
        ):
            valid_predictions, backup_errors, backup_group = await self._backup_forecasts(
                question, record, forecast_with
            )
            errors = errors + backup_errors
            exception_group = backup_group or exception_group
        self._store_finished(question, record)
        await asyncio.to_thread(self.planner.save)
        if len(valid_predictions) == 0:
            if exception_group is None:
                raise RuntimeError(
                    f"Question {question.id_of_post}: no forecast could be made"
                )
            self._reraise_exception_with_prepended_message(
                exception_group, "Error while running research and predictions"
            )
        return ResearchWithPredictions(
            research_report=research,
            summary_report=summary_report,
            errors=errors,
            predictions=valid_predictions,
        )

    async def _backup_forecasts(self, question: MetaculusQuestion, record: dict, forecast_with) -> tuple:  # type: ignore[no-untyped-def]
        """
        The backup chain when no Flash forecaster answered (architect, 29 Sep):
        Nemotron first: 2 forecasts at the same time, median of what answers.
        Only if Nemotron gives no answer: up to 2 Flash-Lite forecasts. Then
        the normal combine and checks. When it runs:
        - Closing within 45 min: Flash-Lite may use its reserve.
        - Earlier, only if EVERY Flash model is out of quota today (429 or
          ledger at 0; a 503 is not, the normal retries go on): Flash-Lite
          uses only quota above its reserve, and answers are parsed above
          the reserve too, so research and parsing for later questions are
          never starved.
        The path used is saved in the question log ("emergency").
        Returns (predictions, errors, exception group or None).
        """
        pool = self.planner
        assert isinstance(pool, GeminiPool)
        in_window = question.close_time is not None and closes_within(question.close_time, EMERGENCY_WINDOW)
        if not in_window and not pool.flash_exhausted():
            return [], [], None
        prefix = "window" if in_window else "flash-exhausted"
        kind = "emergency" if in_window else "backup"
        above_reserve = not in_window
        logger.warning(
            f"Question {question.id_of_post}: "
            + ("closes within 45 min with no Flash forecast" if in_window else "every Flash model is out of quota today")
            + "; backup chain: Nemotron first"
        )
        valid: list = []
        errors: list = []
        group = None
        nemotron = [pool.backup_forecaster() for _ in range(BACKUP_FORECASTS)] if pool.nemotron else []
        if nemotron:
            time_left = quick_forecast_timeout(question.close_time)
            timeout = min(BACKUP_FORECAST_TIMEOUT_SECONDS, time_left or BACKUP_FORECAST_TIMEOUT_SECONDS)
            valid, errors, group = await self._gather_results_and_exceptions(
                [forecast_with(n, kind, timeout, above_reserve=above_reserve) for n in nemotron]
            )
            if valid:
                record["emergency"] = f"{prefix}: nemotron"
                logger.info(f"Question {question.id_of_post}: backup chain: {len(valid)} Nemotron forecast(s)")
                return valid, errors, group
        time_left = quick_forecast_timeout(question.close_time)
        flash_lite = pool.emergency_forecasters(allow_reserve=in_window) if time_left != 0 else []
        if not flash_lite:
            if nemotron:
                record["emergency"] = f"{prefix}: nemotron failed, no flash-lite"
            return valid, errors, group
        record["emergency"] = f"{prefix}: " + ("nemotron failed -> flash-lite" if nemotron else "flash-lite")
        more, more_errors, more_group = await self._gather_results_and_exceptions(
            [forecast_with(f, kind, time_left, above_reserve=above_reserve) for f in flash_lite]
        )
        logger.info(f"Question {question.id_of_post}: backup chain: {len(more)} Flash-Lite forecast(s)")
        return more, errors + more_errors, more_group or group

    # ------------------------------------------------ follow-up search (build 5)

    async def _followup_search(self, question: MetaculusQuestion, record: dict, predictions: list, research: str) -> tuple[str, bool]:
        """(research for round 2, whether a follow-up search was added). A
        failure is only logged; research is then unchanged."""
        record["followup"] = {"enabled": FOLLOWUP_ENABLED, "triggered": False}
        if not FOLLOWUP_ENABLED or len(predictions) < 2:
            return research, False
        if question.close_time is not None and closes_within(question.close_time, timedelta(minutes=MIN_MINUTES_TO_CLOSE)):
            record["followup"]["skipped"] = "closes within 25 min"
            return research, False
        why = followup_disagreement(question, [p.prediction_value for p in predictions])
        if why is None:
            return research, False
        record["followup"].update(triggered=True, trigger=why)
        try:
            if self.get_llm("researcher") == "replay":
                findings, detail = "Replay follow-up (frozen): no searches were made.", {"queries": [], "replay": True}
            else:
                used = (record.get("research_detail") or {}).get("asknews_calls")
                asknews_left = MAX_ASKNEWS_CALLS - used if used is not None else 0
                findings, detail = await run_followup(
                    question.question_text,
                    [followup_reason(p.reasoning) for p in predictions],
                    self.get_llm("parser", "llm").invoke,
                    asknews_left,
                    asknews_search,
                    lambda query: collect_free_news(query),
                )
        except Exception as e:
            logger.warning(f"Question {question.id_of_post}: follow-up search failed ({type(e).__name__})")
            record["followup"]["error"] = type(e).__name__
            return research, False
        record["followup"].update(detail)
        if not findings:
            logger.info(f"Question {question.id_of_post}: round 1 disagrees ({why}); follow-up search found nothing")
            return research, False
        logger.info(
            f"Question {question.id_of_post}: round 1 disagrees ({why}); follow-up search: "
            f"{len(detail.get('queries', []))} query(ies), AskNews {detail.get('asknews_articles', 0)}, "
            f"free news {detail.get('free_articles', 0)} article(s)"
        )
        self._save_section(question, "followup", findings)
        return with_followup(research, findings), True

    # ------------------------------------------------ never re-buy (credits 4d)

    def _saved_forecasts(self, question: MetaculusQuestion) -> list[dict]:
        finished = getattr(self.planner, "finished", None)
        return finished.get(str(question.id_of_post)) if finished is not None else []

    def _reuse_finished(self, question: MetaculusQuestion, record: dict, forecasters: list, saved: list[dict], kind: str) -> tuple[list, list]:
        """(forecasters still to run, reused predictions): a planned slot whose
        model already finished a forecast of this kind is reused."""
        remaining, reused = [], []
        for forecaster in forecasters:
            match = next((s for s in saved if s.get("kind") == kind and s.get("planned_model") == forecaster.model), None)
            prediction = None
            if match is not None:
                saved.remove(match)
                try:
                    prediction = _saved_prediction(question, match)
                except Exception:
                    prediction = None
            if prediction is None:
                remaining.append(forecaster)
                continue
            record["forecasts"].append({
                "kind": kind, "reused": True, "planned_model": forecaster.model, "status": "ok",
                "answered_models": match.get("answered_models", []), "raw_output": match.get("reasoning", ""),
                "parsed": match.get("value"), "seconds": 0,
            })
            reused.append(prediction)
            self._count_ok(question)
        if reused:
            logger.info(f"Question {question.id_of_post}: {len(reused)} finished forecast(s) reused, not bought again")
        return remaining, reused

    def _count_ok(self, question: MetaculusQuestion) -> None:
        counts = self.__dict__.setdefault("_ok_forecasts", {})
        key = str(question.id_of_post)
        counts[key] = counts.get(key, 0) + 1

    def ok_forecasts(self, question_key: str) -> int:
        """Real forecasts finished so far for a question (this run)."""
        return self.__dict__.get("_ok_forecasts", {}).get(question_key, 0)

    def _store_finished(self, question: MetaculusQuestion, record: dict) -> None:
        finished = getattr(self.planner, "finished", None)
        if finished is None:
            return
        finished.put(str(question.id_of_post), [
            {"kind": f["kind"], "planned_model": f["planned_model"], "value": f.get("parsed"),
             "reasoning": f.get("raw_output", ""), "answered_models": f.get("answered_models", [])}
            for f in record["forecasts"]
            if f.get("status") == "ok" and f.get("kind") in ("planned", "round2")
        ])

    def _record_for(self, question: MetaculusQuestion) -> dict:
        records = self.__dict__.setdefault("_question_records", {})
        return records.setdefault(
            id(question),
            {
                "question": question_snapshot(question),
                "mode": self.run_mode,
                "seasonal": self.forecasting_seasonal,
                "started_at": utc_now(),
                "forecasts": [],
            },
        )

    def _note_reading(self, question: MetaculusQuestion, how: str, reply: str = "") -> None:
        """How a model's answer was read: 'direct' (no model call), 'parser'
        (the parser model was needed) or 'dropped' (couldn't be read). Counts
        only in the public log; a reply that couldn't be read directly is kept
        in the question log (private fall26-data), to fix the reader."""
        record = self._record_for(question)
        if _in_shadow.get():
            record.setdefault("shadow_model", {}).setdefault("reading", []).append(how)
            return
        record.setdefault("reading", []).append(how)
        if how != "direct" and reply:
            record.setdefault("unread_replies", []).append(reply[-4000:])
        logger.info(f"Question {question.id_of_post}: answer read: {how}")

    async def forecast_questions(self, questions, return_exceptions=False):  # type: ignore[no-untyped-def,override]
        """As ForecastBot's, plus run timing: research one question at a time,
        soonest-closing first; then, at the end of the run, the shadow
        forecasts (time-bounded) and the question logs."""
        self._run_elapsed()  # starts the run clock if __main__ didn't
        self.__dict__["_research_queue"] = ResearchQueue()
        try:
            return await super().forecast_questions(questions, return_exceptions=return_exceptions)
        finally:
            await self._finish_run()

    def _queue(self) -> ResearchQueue:
        queue = self.__dict__.get("_research_queue")
        if queue is None:  # research outside forecast_questions
            queue = self.__dict__["_research_queue"] = ResearchQueue()
        return queue

    async def _run_individual_question(self, question: MetaculusQuestion) -> ForecastReport:
        replay_question.set(question)  # replay mode answers from the question's shape
        # Gemini calls count per question too (a cap per model per day).
        current_question_key.set(str(question.id_of_post))
        seasonal = self.seasonal_by_post.get(question.id_of_post)
        if seasonal is not None:
            _question_seasonal.set(seasonal)
        # Paid-Flash fallback: MiniBench has the lower daily paid cap.
        spend_question_seasonal.set(self.forecasting_seasonal)
        # Run timing: join the research queue at once, before anything slow.
        queue = self._queue()
        queue.join(id(question), research_key(question))
        try:
            return await self._run_question_timed(question)
        finally:
            queue.leave(id(question))

    async def _run_question_timed(self, question: MetaculusQuestion) -> ForecastReport:
        started = datetime.now(timezone.utc)
        record = self._record_for(question)
        # Build 2a: official data (FRED / CoinGecko) for a clearly matching
        # numeric question, fetched beside research and saved in the question
        # log. Build 2c: one line of it goes into the dossier (run_research).
        hard_data_task = asyncio.create_task(asyncio.to_thread(hard_data_for, question))
        self.__dict__.setdefault("_hard_data_tasks", {})[id(question)] = hard_data_task
        try:
            report = await super()._run_individual_question(question)
        except Exception as e:
            # Never a pure guess: with no real model forecast, nothing is
            # submitted and the next run tries again (it isn't marked as
            # forecast), until the question closes.
            self.__dict__.setdefault("unforecast_close_times", {})[
                question.id_of_post
            ] = question.close_time
            if question.id_of_post in self.__dict__.get("deferred_posts", set()):
                # Run timing: never started (the run is past 30 min); nothing
                # to log, and not a failure for the retry backoff.
                self.__dict__.get("_question_records", {}).pop(id(question), None)
                self.__dict__.get("_hard_data_tasks", {}).pop(id(question), None)
                hard_data_task.cancel()
                raise
            record.update(submitted=False, error=describe_exception(e))
            logger.warning(
                f"Question {question.id_of_post}: no real forecast this run, "
                "left for the next run"
            )
            await self._attach_hard_data(question, record, hard_data_task)
            await self._save_record(question, record, started)
            self._shadow_later(question, record, started)
            raise
        submitted = await self._submit_if_still_open(question, report)
        if submitted and isinstance(question, BinaryQuestion):
            self._consistency_shadow(question, record, report.prediction)
        # Step 10: the referee shadow (OFF until credits), saved, never submitted.
        if REFEREE_ENABLED and isinstance(question, BinaryQuestion) and self.planner is not None:
            try:
                finished = [
                    (float(f["parsed"]), two_line_reason(f["raw_output"]))
                    for f in record["forecasts"]
                    if f.get("status") == "ok"
                ]
                referee = await referee_shadow(
                    question.question_text,
                    finished,
                    self.planner.quick_forecaster().invoke,
                    parse_binary_answer,
                )
                if referee is not None:
                    record.setdefault("shadow", {})["referee"] = referee
            except Exception as e:
                logger.warning(f"Question {question.id_of_post}: referee shadow failed ({type(e).__name__})")
        # Step 9, log-only: after submitting, find and judge matching markets
        # and save them. Never changes the forecast; a failure is only logged.
        if MARKET_MODE == "log-only" and isinstance(question, BinaryQuestion):
            try:
                candidates = await match_question(
                    question, self.get_llm("parser", "llm").invoke
                )
                record["market_candidates"] = market_records(candidates)
                logger.info(
                    f"Question {question.id_of_post}: {len(candidates)} market candidate(s), "
                    f"{sum(c.accepted for c in candidates)} accepted (log-only)"
                )
            except Exception as e:
                logger.warning(
                    f"Question {question.id_of_post}: market matching failed ({type(e).__name__})"
                )
        await self._attach_hard_data(question, record, hard_data_task)
        record.update(
            submitted=submitted,
            final_forecast=to_jsonable(report.prediction),
            minutes=report.minutes_taken,
            list_price_cost=report.price_estimate,
        )
        # Saved at once: a job killed later still leaves this question's log.
        await self._save_record(question, record, started)
        self._shadow_later(question, record, started)
        return report

    def _shadow_later(self, question: MetaculusQuestion, record: dict, started: datetime) -> None:
        """Run timing: the shadow forecast waits for the end of the run."""
        if id(question) in self.__dict__.get("_shadow_research", {}):
            self.__dict__.setdefault("_pending_shadows", []).append((question, record, started))

    async def _finish_run(self) -> None:
        """End of the run: every pending shadow forecast at once, only if the
        run is before minute 45 (ending by minute 47); each result goes in the
        question's '<log>_shadows.json' file."""
        pending = self.__dict__.pop("_pending_shadows", [])
        if not pending:
            return
        elapsed = self._run_elapsed()
        seconds = shadow_seconds(elapsed, SHADOW_FORECAST_TIMEOUT_SECONDS)
        if seconds <= 0:
            for question, _, _ in pending:
                self.__dict__.get("_shadow_research", {}).pop(id(question), None)
                self.__dict__.get("_live_predictions", {}).pop(id(question), None)
            logger.info(f"shadows skipped: time (run at minute {int(elapsed // 60)}, {len(pending)} question(s))")
            return
        results = await asyncio.gather(
            *[self._finish_shadow_forecast(question, record, seconds) for question, record, _ in pending],
            return_exceptions=True,
        )
        for (question, record, started), result in zip(pending, results):
            if isinstance(result, BaseException):
                logger.warning(f"Question {question.id_of_post}: shadow forecaster failed ({type(result).__name__})")
                continue
            await self._save_shadows(question, record, started)

    async def _save_shadows(self, question, record: dict, started: datetime) -> None:  # type: ignore[no-untyped-def]
        """The shadow forecaster's result in its own file next to the log."""
        if self.question_log is None or "shadow_model" not in record:
            return
        variants = {k: v for k, v in (record.get("shadow") or {}).items() if SHADOW_VARIANT in k}
        path = record_path(self.run_mode, question, started)
        payload = {
            "question": {"id_of_post": question.id_of_post, "question_type": record["question"].get("question_type")},
            "log": path,
            "shadow_model": record["shadow_model"],
            "shadow": variants,
            "saved_at": utc_now(),
        }
        await asyncio.to_thread(self.question_log.save, shadows_path(path), payload)

    def _consistency_shadow(self, question: MetaculusQuestion, record: dict, prediction) -> None:  # type: ignore[no-untyped-def]
        """Sibling binary questions (differ in one number or date) must go the
        right way; the isotonic-adjusted forecast is saved as the 'consistent'
        shadow (never submitted). A failure is only logged."""
        index = self.consistency_index
        if index is None:
            return
        try:
            tournament = "seasonal" if self.forecasting_seasonal else "minibench"
            index.put(question.id_of_post, question.question_text, question.close_time, float(prediction), tournament)
            found = consistency_shadow_for(index, question.id_of_post, tournament)
            index.save()
        except Exception as e:
            logger.warning(f"Question {question.id_of_post}: consistency shadow failed ({type(e).__name__})")
            return
        if found is None:
            return
        record.setdefault("shadow", {})["consistent"] = found["consistent"]
        record["consistency"] = found
        logger.info(
            f"Question {question.id_of_post}: sibling group of {len(found['group'])} "
            f"({found['kind']} ladder, {found['direction']}); {found['violations']} violation(s)"
        )

    async def _attach_hard_data(self, question: MetaculusQuestion, record: dict, task) -> None:  # type: ignore[no-untyped-def]
        self.__dict__.get("_hard_data_tasks", {}).pop(id(question), None)
        try:
            found = await task
        except Exception as e:
            logger.warning(f"Question {question.id_of_post}: hard data failed ({type(e).__name__})")
            return
        if found is None:
            return
        record["hard_data"] = found
        # The empirical window baseline (architect, 1 Oct; replaces the
        # random walk of build 2b), a shadow only: the series' full history,
        # every past window of the question's length, the statistic it asks for.
        if "latest" in found and isinstance(question, NumericQuestion):
            try:
                history = await asyncio.wait_for(asyncio.to_thread(fetch_full_history, found), WINDOW_HISTORY_WAIT_SECONDS)
                distribution, detail = window_baseline(question, found, history)
            except Exception as e:
                distribution, detail = None, {"skipped": f"failed ({type(e).__name__})"}
            if distribution is not None:
                record.setdefault("shadow", {})["window"] = to_jsonable(distribution)
            found["window_baseline"] = detail
            logger.info(
                f"Question {question.id_of_post}: window baseline: "
                + (f"none, {detail['skipped']}" if distribution is None else
                   f"{detail['statistic']} over {detail['window_days']} days, {detail['windows']} windows "
                   f"({'within 25% of today' if detail['similar_level'] else 'all levels'})"
                   + (f"; stand-in: {detail['warning']}" if detail.get("warning") else ""))
            )
        logger.info(
            f"Question {question.id_of_post}: hard data {found['source']} {found['series']}: "
            + ("fetched" if "latest" in found else f"fetch failed ({found.get('error')})")
        )

    async def _submit_if_still_open(
        self, question: MetaculusQuestion, report: ForecastReport
    ) -> bool:
        if self.fail_submission:
            raise RuntimeError("Deliberate submission failure (--retry-run rehearsal)")
        if not self.submit_forecasts:
            return False
        try:
            fresh = await asyncio.to_thread(
                self.metaculus_client.get_question_by_post_id,
                question.id_of_post,
                "unpack_subquestions",
            )
            if isinstance(fresh, list):
                fresh = next(
                    (q for q in fresh if q.id_of_question == question.id_of_question),
                    question,
                )
            problem = still_open_problem(fresh)
        except Exception as e:
            # Can't check: submit anyway rather than risk skipping the question.
            logger.warning(
                f"Question {question.id_of_post}: could not re-check that it's open "
                f"({type(e).__name__}); submitting anyway"
            )
            problem = None
        if problem:
            logger.warning(f"Question {question.id_of_post}: not submitted, {problem}")
            return False
        await report.publish_report_to_metaculus(metaculus_client=self.metaculus_client)
        return True

    async def _save_record(self, question, record: dict, started: datetime) -> None:  # type: ignore[no-untyped-def]
        # Saved after submission; a failure here never affects the forecast.
        self.__dict__.get("_question_records", {}).pop(id(question), None)
        if self.keep_records:
            self.__dict__.setdefault("kept_records", []).append(record)
        if self.question_log is None:
            return
        record["finished_at"] = utc_now()
        await asyncio.to_thread(
            self.question_log.save, record_path(self.run_mode, question, started), record
        )

    async def _make_prediction(
        self, question: MetaculusQuestion, research: str
    ) -> ReasonedPrediction[PredictionTypes]:
        # Lineups without the Gemini pool: forecasts not done by the 15-minute
        # cut-off count as failed; the finished ones are combined.
        # Run timing: at most 12 minutes (the forecasts run side by side).
        return await asyncio.wait_for(
            super()._make_prediction(question, research),
            capped(planned_forecast_timeout(question.close_time), FORECAST_STAGES_LIMIT_SECONDS),
        )

    ##################################### RESEARCH #####################################

    async def run_research(self, question: MetaculusQuestion) -> str:
        if self.break_on_purpose:
            raise RuntimeError("Deliberate failure (--break-on-purpose)")
        # Run timing: one question researches at a time, soonest-closing first.
        queue = self._queue()
        await queue.turn(id(question), research_key(question))
        try:
            return await self._research_turn(question)
        finally:
            queue.release()

    async def _research_turn(self, question: MetaculusQuestion) -> str:
        if self._run_elapsed() >= START_CUTOFF_SECONDS:
            # Run timing: no new question after 30 min; the next run takes it.
            self.__dict__.setdefault("deferred_posts", set()).add(question.id_of_post)
            logger.warning(
                f"Question {question.id_of_post}: not started, the run is past "
                f"{START_CUTOFF_SECONDS // 60} min; left for the next run"
            )
            raise QuestionDeferred(f"run past {START_CUTOFF_SECONDS // 60} min")
        # Run timing: at most 4 minutes, then forecast with what was gathered
        # (planned research stops at the deadline; any other research is cut
        # a little after it).
        deadline = asyncio.get_running_loop().time() + RESEARCH_LIMIT_SECONDS
        _research_deadline.set(deadline)
        try:
            research = await asyncio.wait_for(
                self._run_research_unguarded(question), RESEARCH_LIMIT_SECONDS + RESEARCH_GRACE_SECONDS
            )
        except asyncio.TimeoutError:
            self._record_for(question)["research_time_limit"] = "cut"
            logger.warning(
                f"Question {question.id_of_post}: research time limit "
                f"({RESEARCH_LIMIT_SECONDS // 60} min) reached, forecasting without it"
            )
            research = "No research is available: the research time limit was reached."
            research = await self._with_official_data(question, research, deadline)
            self._start_shadow_forecast(question, research)
            return research
        except Exception as e:
            # A missed question scores 0: forecast without news rather
            # than not at all.
            logger.warning(
                f"Question {question.id_of_post}: research failed"
                f"{_http_status_note(e)}, forecasting without it "
                f"({describe_exception(e)})"
            )
            research = "No research is available: the news search failed."
            research = await self._with_official_data(question, research, deadline)
            self._start_shadow_forecast(question, research)
            return research
        logger.info(f"Question {question.id_of_post}: research done")
        if "base" not in self._record_for(question).get("dossier_sections", {}):
            self._save_section(question, "base", research)
        research = await self._with_official_data(question, research, deadline)
        self._start_shadow_forecast(question, research)
        return research

    def _save_section(self, question: MetaculusQuestion, name: str, text: str) -> None:
        """Replay lab: one dossier part (base research, follow-up findings,
        Wikipedia, official data) saved in the question log with its time."""
        if text:
            self._record_for(question).setdefault("dossier_sections", {})[name] = {"text": text, "at": utc_now()}

    async def _with_official_data(self, question: MetaculusQuestion, research: str, deadline: float | None = None) -> str:
        """
        Build 2c, partial (architect, 29 Sep): for a question matched to
        official data, ONE line first in the dossier (series, latest value
        and date, 1-year min/max, 30-day change, and a warning when the series
        is only a stand-in). Only an exact match's latest value is given to
        the unit check. The window baseline is never added (a shadow).
        """
        task = self.__dict__.get("_hard_data_tasks", {}).get(id(question))
        if task is None:
            return research
        try:
            # Run timing: the wait counts in the research time limit.
            wait = HARD_DATA_WAIT_SECONDS
            if deadline is not None:
                wait = max(0.0, min(wait, deadline - asyncio.get_running_loop().time()))
            found = await asyncio.wait_for(asyncio.shield(task), wait)
            line = official_line(question, found)
        except Exception as e:
            logger.warning(f"Question {question.id_of_post}: official data line skipped ({type(e).__name__})")
            return research
        if line is None or len(line.text.split()) > MAX_LINE_WORDS:
            return research
        record = self._record_for(question)
        record["official_data"] = {"line": line.text, "exact": line.exact}
        self._save_section(question, "official_data", line.text)
        if line.exact:
            record["official_current_value"] = line.latest
        logger.info(
            f"Question {question.id_of_post}: official data line added "
            f"({'exact: feeds the unit check' if line.exact else 'stand-in: not used by the unit check'})"
        )
        return with_official_line(research, line.text)

    # ------------------------------------------------ shadow forecaster (Build 1b)

    def _start_shadow_forecast(self, question: MetaculusQuestion, research: str) -> None:
        """Keep the research for the shadow forecaster, which runs only AFTER
        the live forecast is submitted (or has failed), so its time limit
        never delays a real submission."""
        if self.shadow_llm is None or _in_shadow.get():
            return
        self.__dict__.setdefault("_shadow_research", {}).setdefault(id(question), research)

    async def _shadow_forecast(self, question: MetaculusQuestion, research: str, timeout: float) -> tuple:
        _in_shadow.set(True)
        _planned_forecaster.set(self.shadow_llm)
        # It runs at the end of the run, outside the question's own task.
        replay_question.set(question)
        current_question_key.set(str(question.id_of_post))
        started = time.monotonic()
        try:
            # The per-type forecast directly: the library's _make_prediction
            # needs the question's notepad, gone once the live forecast is done.
            if isinstance(question, BinaryQuestion):
                forecast = self._run_forecast_on_binary(question, research)
            elif isinstance(question, MultipleChoiceQuestion):
                forecast = self._run_forecast_on_multiple_choice(question, research)
            elif isinstance(question, NumericQuestion):
                forecast = self._run_forecast_on_numeric(question, research)
            else:
                raise ValueError(f"no shadow forecast for {type(question).__name__}")
            prediction = await asyncio.wait_for(forecast, timeout)
            status, value = "ok", prediction.prediction_value
        except asyncio.TimeoutError:
            status, value = "timeout", None
        except Exception as e:
            status, value = f"failed: {describe_exception(e)}"[:200], None
        return status, value, round(time.monotonic() - started, 1)

    async def _finish_shadow_forecast(self, question: MetaculusQuestion, record: dict, timeout: float) -> None:
        """At the end of the run (the live forecast submitted or not): run the
        shadow in its own task (hard time limit `timeout`), keep it and 'live
        median with it added' in the record (saved as '<log>_shadows.json'). A
        failure is only logged."""
        research = self.__dict__.get("_shadow_research", {}).pop(id(question), None)
        live = self.__dict__.get("_live_predictions", {}).pop(id(question), None)
        if research is None:
            return
        # The log is already saved: the record is back only while the shadow
        # notes how its answer was read.
        records = self.__dict__.setdefault("_question_records", {})
        records[id(question)] = record
        try:
            status, value, seconds = await asyncio.create_task(self._shadow_forecast(question, research, timeout))
        except Exception as e:  # never raised by _shadow_forecast, but be safe
            status, value, seconds = f"failed: {describe_exception(e)}"[:200], None, None
        finally:
            records.pop(id(question), None)
        shadow = record.setdefault("shadow_model", {})
        shadow.update(model=getattr(self.shadow_llm, "model", str(self.shadow_llm)), status=status, seconds=seconds)
        stats = self.__dict__.setdefault("shadow_stats", {"asked": 0, "answered": 0})
        stats["asked"] += 1
        if status == "ok" and value is not None:
            stats["answered"] += 1
            variants = record.setdefault("shadow", {})
            variants[SHADOW_VARIANT] = to_jsonable(value)
            if live:
                try:
                    variants[f"live+{SHADOW_VARIANT}"] = to_jsonable(combine_with_shadow(question, [*live, value]))
                except Exception as e:
                    logger.warning(f"Question {question.id_of_post}: live+shadow could not be combined ({type(e).__name__})")
        logger.info(f"Question {question.id_of_post}: shadow forecaster: {status.split(':')[0]}")

    async def _run_research_unguarded(self, question: MetaculusQuestion) -> str:
        research = ""
        researcher = self.get_llm("researcher")

        prompt = self._get_research_prompt(question, researcher)

        if isinstance(researcher, GeneralLlm):
            research = await researcher.invoke(prompt)
        elif researcher == "replay":
            research = REPLAY_RESEARCH  # frozen: no searches
        elif researcher == "planned-research":
            research = await self._planned_research(question)
        elif researcher == "asknews/news-summaries":
            research = await self._asknews_with_free_fallback(question, prompt)
        elif (
            researcher == "asknews/deep-research/low-depth"
            or researcher == "asknews/deep-research/medium-depth"
            or researcher == "asknews/deep-research/high-depth"
        ):
            research = await AskNewsSearcher().call_preconfigured_version(
                researcher, prompt
            )
        elif researcher.startswith("smart-searcher"):
            model_name = researcher.removeprefix("smart-searcher/")
            searcher = SmartSearcher(
                model=model_name,
                temperature=0,
                num_searches_to_run=2,
                num_sites_per_search=10,
                use_advanced_filters=False,
            )
            research = await searcher.invoke(prompt)
        elif not researcher or researcher == "None" or researcher == "no_research":
            research = ""
        else:
            research = await self.get_llm("researcher", "llm").invoke(prompt)
        return research

    async def _planned_research(self, question: MetaculusQuestion) -> str:
        """
        PLAN.md Step 7 (research.py): planner -> AskNews (max 3 calls) or free
        news -> dossier -> gap-fill. The planner and dossier writer use the
        parser model (Flash-Lite on the free key), never the forecasting quota.
        Logs counts only.
        """
        helper = self.get_llm("parser", "llm")
        result = await run_planned_research(
            question, dates_line(question), helper.invoke, deadline=_research_deadline.get()
        )
        if result.time_limit_hit:
            self._record_for(question)["research_time_limit"] = result.time_limit_hit
            logger.warning(
                f"Question {question.id_of_post}: research time limit reached "
                f"({result.time_limit_hit}); forecasting with what was gathered"
            )
        logger.info(
            f"Question {question.id_of_post}: articles found: {result.articles} "
            f"(AskNews {result.asknews_articles} in {result.asknews_calls} call(s), "
            f"free news {result.free_articles}); {len(result.queries)} queries; "
            f"dossier {'written' if result.dossier_written else 'not written, using the articles'}"
            f"{', gap-filled' if result.gap_filled else ''}; "
            f"Wikipedia background: {len(result.wikipedia)} page(s); "
            f"{len(result.dossier.split())} words"
        )
        self._record_for(question)["research_detail"] = {
            "current_value": (
                {"value": result.current_value.value, "unit": result.current_value.unit, "date": result.current_value.date}
                if result.current_value
                else None
            ),
            "queries": result.queries,
            "asknews_calls": result.asknews_calls,
            "asknews_articles": result.asknews_articles,
            "free_articles": result.free_articles,
            "gap_filled": result.gap_filled,
            "dossier_written": result.dossier_written,
            "wikipedia": result.wikipedia,
        }
        # Replay lab: each part of the dossier, frozen with its time.
        self._save_section(question, "base", result.base_dossier)
        self._save_section(question, "wikipedia", result.wikipedia_section)
        return result.dossier

    # AskNews finding fewer articles than this gets topped up with free news.
    min_asknews_articles = 3

    async def _asknews_with_free_fallback(
        self, question: MetaculusQuestion, prompt: str
    ) -> str:
        """
        AskNews first. If it fails (e.g. 402) or finds fewer than 3 articles,
        add up to 10 recent articles from free sources (Google News, GDELT).
        Logs only the article counts, never the text.
        """
        asknews_research = ""
        try:
            asknews_research = await AskNewsSearcher().call_preconfigured_version(
                "asknews/news-summaries", prompt
            )
        except Exception as e:
            logger.warning(
                f"Question {question.id_of_post}: AskNews failed{_http_status_note(e)}, "
                "using free news sources"
            )
        asknews_count = count_asknews_articles(asknews_research)
        free_articles = []
        if asknews_count < self.min_asknews_articles:
            free_articles = await asyncio.to_thread(
                collect_free_news, question.question_text
            )
        logger.info(
            f"Question {question.id_of_post}: articles found: "
            f"{asknews_count + len(free_articles)} "
            f"(AskNews {asknews_count}, free news {len(free_articles)})"
        )
        parts = [
            part
            for part in (
                asknews_research if asknews_count else "",
                format_articles(free_articles),
            )
            if part
        ]
        return "\n\n".join(parts) or "No recent news articles were found."

    @staticmethod
    def _get_research_prompt(
        question: MetaculusQuestion, researcher: str | GeneralLlm
    ) -> str:
        # AskNews searches with this text, so give it just the question
        # rather than the long assistant prompt (as Metaculus's
        # FallTemplateBot2026 does).
        if GeneralLlm.to_model_name(researcher) == "asknews/news-summaries":
            return question.question_text

        return clean_indents(
            f"""
            You are an assistant to a superforecaster.
            The superforecaster will give you a question they intend to forecast on.
            To be a great assistant, you generate a concise but detailed rundown of the most relevant news, including if the question would resolve Yes or No based on current information.
            You do not produce forecasts yourself.

            Question:
            {question.question_text}

            This question's outcome will be determined by the specific criteria below:
            {question.resolution_criteria}

            {question.fine_print}
            """
        )

    ##################################### BINARY QUESTIONS #####################################

    async def _run_forecast_on_binary(
        self, question: BinaryQuestion, research: str
    ) -> ReasonedPrediction[float]:
        prompt = clean_indents(
            f"""
            You are a professional forecaster interviewing for a job.

            Your interview question is:
            {question.question_text}

            Question background:
            {question.background_info}


            This question's outcome will be determined by the specific criteria below. These criteria have not yet been satisfied:
            {question.resolution_criteria}

            {question.fine_print}


            Your research assistant says:
            {research}

            {dates_line(question)}

            Before answering you write:
            (a) The time left until the outcome to the question is known.
            (b) The status quo outcome if nothing changed.
            (c) A brief description of a scenario that results in a No outcome.
            (d) A brief description of a scenario that results in a Yes outcome.

            You write your rationale remembering that good forecasters put extra weight on the status quo outcome since the world changes slowly most of the time.
            {self._get_conditional_disclaimer_if_necessary(question)}

            The last thing you write is your final answer as: "Probability: ZZ%", 0-100
            """
        )

        return await self._binary_prompt_to_forecast(question, prompt)

    async def _binary_prompt_to_forecast(
        self,
        question: BinaryQuestion,
        prompt: str,
    ) -> ReasonedPrediction[float]:
        reasoning = await self.get_llm("default", "llm").invoke(prompt)
        # Read "Probability: ZZ%" directly; the parser model only if that fails.
        parsed = parse_binary_answer(reasoning)
        if parsed is None:
            try:
                binary_prediction: BinaryPrediction = await structure_output(
                    reasoning,
                    BinaryPrediction,
                    model=self.get_llm("parser", "llm"),
                    num_validation_samples=self._structure_output_validation_samples,
                )
            except Exception:
                self._note_reading(question, "dropped", reasoning)
                raise
            parsed = binary_prediction.prediction_in_decimal
            self._note_reading(question, "parser", reasoning)
        else:
            self._note_reading(question, "direct")
        decimal_pred = max(0.01, min(0.99, parsed))

        logger.info(f"Question {question.id_of_post}: forecast made")
        return ReasonedPrediction(prediction_value=decimal_pred, reasoning=reasoning)

    ##################################### MULTIPLE CHOICE QUESTIONS #####################################

    async def _run_forecast_on_multiple_choice(
        self, question: MultipleChoiceQuestion, research: str
    ) -> ReasonedPrediction[PredictedOptionList]:
        prompt = clean_indents(
            f"""
            You are a professional forecaster interviewing for a job.

            Your interview question is:
            {question.question_text}

            The options are: {question.options}


            Background:
            {question.background_info}

            {question.resolution_criteria}

            {question.fine_print}


            Your research assistant says:
            {research}

            {dates_line(question)}

            Before answering you write:
            (a) The time left until the outcome to the question is known.
            (b) The status quo outcome if nothing changed.
            (c) A description of an scenario that results in an unexpected outcome.

            {self._get_conditional_disclaimer_if_necessary(question)}
            You write your rationale remembering that (1) good forecasters put extra weight on the status quo outcome since the world changes slowly most of the time, and (2) good forecasters leave some moderate probability on most options to account for unexpected outcomes.

            The last thing you write is your final probabilities for the N options in this order {question.options} as:
            Option_A: Probability_A
            Option_B: Probability_B
            ...
            Option_N: Probability_N
            """
        )
        return await self._multiple_choice_prompt_to_forecast(question, prompt)

    async def _multiple_choice_prompt_to_forecast(
        self,
        question: MultipleChoiceQuestion,
        prompt: str,
    ) -> ReasonedPrediction[PredictedOptionList]:
        parsing_instructions = clean_indents(
            f"""
            Make sure that all option names are one of the following:
            {question.options}

            The text you are parsing may prepend these options with some variation of "Option" which you should remove if not part of the option names I just gave you.
            Additionally, you may sometimes need to parse a 0% probability. Please do not skip options with 0% but rather make it an entry in your final list with 0% probability.
            """
        )
        reasoning = await self.get_llm("default", "llm").invoke(prompt)
        # Read the option lines directly; the parser model only if that fails.
        predicted_option_list = parse_multiple_choice_answer(reasoning, question.options)
        if predicted_option_list is None:
            try:
                predicted_option_list = await structure_output(
                    text_to_structure=reasoning,
                    output_type=PredictedOptionList,
                    model=self.get_llm("parser", "llm"),
                    num_validation_samples=self._structure_output_validation_samples,
                    additional_instructions=parsing_instructions,
                )
            except Exception:
                self._note_reading(question, "dropped", reasoning)
                raise
            self._note_reading(question, "parser", reasoning)
        else:
            self._note_reading(question, "direct")

        logger.info(f"Question {question.id_of_post}: forecast made")
        return ReasonedPrediction(
            prediction_value=predicted_option_list, reasoning=reasoning
        )

    ##################################### NUMERIC QUESTIONS #####################################

    async def _run_forecast_on_numeric(
        self, question: NumericQuestion, research: str
    ) -> ReasonedPrediction[NumericDistribution]:
        upper_bound_message, lower_bound_message = (
            self._create_upper_and_lower_bound_messages(question)
        )
        prompt = clean_indents(
            f"""
            You are a professional forecaster interviewing for a job.

            Your interview question is:
            {question.question_text}

            Background:
            {question.background_info}

            {question.resolution_criteria}

            {question.fine_print}

            Units for answer: {question.unit_of_measure if question.unit_of_measure else "Not stated (please infer this)"}

            Your research assistant says:
            {research}

            {dates_line(question)}

            {lower_bound_message}
            {upper_bound_message}

            Formatting Instructions:
            - Please notice the units requested and give your answer in these units (e.g. whether you represent a number as 1,000,000 or 1 million).
            - Never use scientific notation.
            - Always start with a smaller number (more negative if negative) and then increase from there. The value for percentile 2.5 should always be less than the value for percentile 5, and so on.

            Before answering you write:
            (a) The time left until the outcome to the question is known.
            (b) The outcome if nothing changed.
            (c) The outcome if the current trend continued.
            (d) The expectations of experts and markets.
            (e) A brief description of an unexpected scenario that results in a low outcome.
            (f) A brief description of an unexpected scenario that results in a high outcome.

            {self._get_conditional_disclaimer_if_necessary(question)}
            You remind yourself that good forecasters are humble and set wide 90/10 confidence intervals to account for unknown unknowns.

            The last thing you write is your final answer as:
            "
            Percentile 2.5: XX (lowest number value)
            Percentile 5: XX
            Percentile 10: XX
            Percentile 25: XX
            Percentile 50: XX
            Percentile 75: XX
            Percentile 90: XX
            Percentile 95: XX
            Percentile 97.5: XX (highest number value)
            "
            """
        )
        return await self._numeric_prompt_to_forecast(question, prompt)

    async def _numeric_prompt_to_forecast(
        self,
        question: NumericQuestion,
        prompt: str,
    ) -> ReasonedPrediction[NumericDistribution]:
        reasoning = await self.get_llm("default", "llm").invoke(prompt)
        parsing_instructions = clean_indents(
            f"""
            The text given to you is trying to give a forecast distribution for a numeric question.
            - This text is trying to answer the numeric question: "{question.question_text}".
            - When parsing the text, please make sure to give the values (the ones assigned to percentiles) in terms of the correct units.
            - The units for the forecast are: {question.unit_of_measure}
            - Your work will be shown publicly with these units stated verbatim after the numbers your parse.
            - As an example, someone else guessed that the answer will be between {question.lower_bound} {question.unit_of_measure} and {question.upper_bound} {question.unit_of_measure}, so the numbers parsed from an answer like this would be verbatim "{question.lower_bound}" and "{question.upper_bound}".
            - If the answer doesn't give the answer in the correct units, you should parse it in the right units. For instance if the answer gives numbers as $500,000,000 and units are "B $" then you should parse the answer as 0.5 (since $500,000,000 is $0.5 billion).
            - If percentiles are not explicitly given (e.g. only a single value is given) please don't return a parsed output, but rather indicate that the answer is not explicitly given in the text.
            - Turn any values that are in scientific notation into regular numbers.
            """
        )
        prediction = await self._parse_numeric_safely(
            question, reasoning, parsing_instructions
        )
        logger.info(f"Question {question.id_of_post}: forecast made")
        return ReasonedPrediction(prediction_value=prediction, reasoning=reasoning)

    async def _parse_numeric_safely(
        self, question: NumericQuestion, reasoning: str, parsing_instructions: str
    ) -> NumericDistribution:
        try:
            distribution, used_parser = await self._read_numeric(
                question, reasoning, parsing_instructions
            )
        except Exception:
            self._note_reading(question, "dropped", reasoning)
            raise
        self._note_reading(question, "parser" if used_parser else "direct", reasoning)
        return distribution

    async def _read_numeric(
        self, question: NumericQuestion, reasoning: str, parsing_instructions: str
    ) -> tuple[NumericDistribution, bool]:
        """
        Parse the percentiles, put reversed ones right, and catch unit errors
        (Step 4): if every value is outside the question's range, or the
        median is more than 10x / less than 0.1x the current value found in
        research, re-ask the parser once with a warning about units.
        - Still outside the range: this forecast is dropped.
        - Still over 10x away from the current value: the forecast is kept
          but flagged; when combining, flagged models are dropped, unless
          EVERY model is flagged (then the current value is probably wrong:
          all are kept and a warning is logged). Never a made-up forecast.
        """
        lower, upper = question_range(question)
        record = self._record_for(question)
        current = record.get("research_detail", {}).get("current_value")
        # Build 2c: an exact official-data match beats the dossier's own value.
        current_value = record.get("official_current_value")
        if current_value is None:
            current_value = current["value"] if current else None
        instructions = parsing_instructions
        # First try reading the "Percentile P: value" lines directly (no model
        # call); the parser model if that fails or looks like a unit error.
        direct = parse_percentile_answer(
            reasoning, STEP8_PERCENTILES, question.unit_of_measure
        )
        used_parser = False
        outside = off = False
        for attempt in range(2):
            if attempt == 0 and direct is not None:
                percentile_list = direct
            else:
                used_parser = True
                percentile_list = await structure_output(
                    reasoning,
                    list[Percentile],
                    model=self.get_llm("parser", "llm"),
                    additional_instructions=instructions,
                    num_validation_samples=self._structure_output_validation_samples,
                )
            percentile_list = fix_reversed_percentiles(percentile_list)
            outside = all_outside_range(percentile_list, lower, upper)
            off = off_by_10x(percentile_list, current_value)
            if not outside and not off:
                break
            logger.warning(
                f"Question {question.id_of_post}: "
                + ("every parsed value is outside the question's range" if outside
                   else "the median is over 10x away from the current value in research")
                + f" (attempt {attempt + 1}); possible unit error"
            )
            instructions = parsing_instructions + clean_indents(
                f"""
                - IMPORTANT: a previous parse looked like a unit error. The question's range is
                  {lower} to {upper} {question.unit_of_measure}"""
                + (f"; the latest known value is {current_value} {question.unit_of_measure}" if current_value else "")
                + """. Check the units very carefully (thousands vs millions vs billions, percent
                  vs fraction).
                """
            )
        if outside:
            raise NoValidForecast("every parsed value is outside the question's range, twice")
        # Step 8: this model's CDF via PCHIP through its percentiles.
        distribution = pchip_distribution(percentile_list, question)
        problems = distribution_problems(distribution, question)
        if problems:
            raise NoValidForecast(
                f"distribution broke the platform rules ({'; '.join(problems)})"
            )
        if off:
            # Still over 10x away from the current value: decided when combining.
            # Keep the object itself so its id can't be reused by another one.
            self.__dict__.setdefault("_unit_flagged", {})[id(distribution)] = distribution
        return distribution, used_parser

    ##################################### DATE QUESTIONS #####################################

    async def _run_forecast_on_date(
        self, question: DateQuestion, research: str
    ) -> ReasonedPrediction[NumericDistribution]:
        upper_bound_message, lower_bound_message = (
            self._create_upper_and_lower_bound_messages(question)
        )
        prompt = clean_indents(
            f"""
            You are a professional forecaster interviewing for a job.

            Your interview question is:
            {question.question_text}

            Background:
            {question.background_info}

            {question.resolution_criteria}

            {question.fine_print}

            Your research assistant says:
            {research}

            {dates_line(question)}

            {lower_bound_message}
            {upper_bound_message}

            Formatting Instructions:
            - This is a date question, and as such, the answer must be expressed in terms of dates.
            - The dates must be written in the format of YYYY-MM-DD. If hours matter, please append the date with the hour in UTC and military time: YYYY-MM-DDTHH:MM:SSZ.No other formatting is allowed.
            - Always start with a lower date chronologically and then increase from there.
            - Do NOT forget this. The dates must be written in chronological order starting at the earliest time at percentile 10 and increasing from there.

            Before answering you write:
            (a) The time left until the outcome to the question is known.
            (b) The outcome if nothing changed.
            (c) The outcome if the current trend continued.
            (d) The expectations of experts and markets.
            (e) A brief description of an unexpected scenario that results in a low outcome.
            (f) A brief description of an unexpected scenario that results in a high outcome.

            {self._get_conditional_disclaimer_if_necessary(question)}
            You remind yourself that good forecasters are humble and set wide 90/10 confidence intervals to account for unknown unknowns.

            The last thing you write is your final answer as:
            "
            Percentile 10: YYYY-MM-DD (oldest date)
            Percentile 20: YYYY-MM-DD
            Percentile 40: YYYY-MM-DD
            Percentile 60: YYYY-MM-DD
            Percentile 80: YYYY-MM-DD
            Percentile 90: YYYY-MM-DD (newest date)
            "
            """
        )
        forecast = await self._date_prompt_to_forecast(question, prompt)
        return forecast

    async def _date_prompt_to_forecast(
        self,
        question: DateQuestion,
        prompt: str,
    ) -> ReasonedPrediction[NumericDistribution]:
        reasoning = await self.get_llm("default", "llm").invoke(prompt)
        parsing_instructions = clean_indents(
            f"""
            The text given to you is trying to give a forecast distribution for a date question.
            - This text is trying to answer the question: "{question.question_text}".
            - As an example, someone else guessed that the answer will be between {question.lower_bound} and {question.upper_bound}, so the numbers parsed from an answer like this would be verbatim "{question.lower_bound}" and "{question.upper_bound}".
            - The output is given as dates/times please format it into a valid datetime parsable string. Assume midnight UTC if no hour is given.
            - If percentiles are not explicitly given (e.g. only a single value is given) please don't return a parsed output, but rather indicate that the answer is not explicitly given in the text.
            """
        )
        date_percentile_list: list[DatePercentile] = await structure_output(
            reasoning,
            list[DatePercentile],
            model=self.get_llm("parser", "llm"),
            additional_instructions=parsing_instructions,
            num_validation_samples=self._structure_output_validation_samples,
        )

        percentile_list = [
            Percentile(
                percentile=percentile.percentile,
                value=percentile.value.timestamp(),
            )
            for percentile in date_percentile_list
        ]
        prediction = NumericDistribution.from_question(percentile_list, question)
        logger.info(f"Question {question.id_of_post}: forecast made")
        return ReasonedPrediction(prediction_value=prediction, reasoning=reasoning)

    def _create_upper_and_lower_bound_messages(
        self, question: NumericQuestion | DateQuestion
    ) -> tuple[str, str]:
        if isinstance(question, NumericQuestion):
            if question.nominal_upper_bound is not None:
                upper_bound_number = question.nominal_upper_bound
            else:
                upper_bound_number = question.upper_bound
            if question.nominal_lower_bound is not None:
                lower_bound_number = question.nominal_lower_bound
            else:
                lower_bound_number = question.lower_bound
            unit_of_measure = question.unit_of_measure
        elif isinstance(question, DateQuestion):
            upper_bound_number = question.upper_bound.date().isoformat()
            lower_bound_number = question.lower_bound.date().isoformat()
            unit_of_measure = ""
        else:
            raise ValueError()

        if question.open_upper_bound:
            upper_bound_message = f"The question creator thinks the number is likely not higher than {upper_bound_number} {unit_of_measure}."
        else:
            upper_bound_message = f"The outcome can not be higher than {upper_bound_number} {unit_of_measure}."

        if question.open_lower_bound:
            lower_bound_message = f"The question creator thinks the number is likely not lower than {lower_bound_number} {unit_of_measure}."
        else:
            lower_bound_message = f"The outcome can not be lower than {lower_bound_number} {unit_of_measure}."
        return upper_bound_message, lower_bound_message

    ##################################### CONDITIONAL QUESTIONS #####################################

    async def _run_forecast_on_conditional(
        self, question: ConditionalQuestion, research: str
    ) -> ReasonedPrediction[ConditionalPrediction]:
        parent_info, full_research = await self._get_question_prediction_info(
            question.parent, research, "parent"
        )
        child_info, full_research = await self._get_question_prediction_info(
            question.child, research, "child"
        )
        yes_info, full_research = await self._get_question_prediction_info(
            question.question_yes, full_research, "yes"
        )
        no_info, full_research = await self._get_question_prediction_info(
            question.question_no, full_research, "no"
        )
        full_reasoning = clean_indents(
            f"""
            ## Parent Question Reasoning
            {parent_info.reasoning}
            ## Child Question Reasoning
            {child_info.reasoning}
            ## Yes Question Reasoning
            {yes_info.reasoning}
            ## No Question Reasoning
            {no_info.reasoning}
        """
        )
        full_prediction = ConditionalPrediction(
            parent=parent_info.prediction_value,  # type: ignore
            child=child_info.prediction_value,  # type: ignore
            prediction_yes=yes_info.prediction_value,  # type: ignore
            prediction_no=no_info.prediction_value,  # type: ignore
        )
        return ReasonedPrediction(
            reasoning=full_reasoning, prediction_value=full_prediction
        )

    async def _get_question_prediction_info(
        self, question: MetaculusQuestion, research: str, question_type: str
    ) -> tuple[ReasonedPrediction[PredictionTypes | PredictionAffirmed], str]:
        from forecasting_tools.data_models.data_organizer import DataOrganizer

        previous_forecasts = question.previous_forecasts
        if (
            question_type in ["parent", "child"]
            and previous_forecasts
            and question_type not in self.force_reforecast_in_conditional
        ):
            # TODO: add option to not affirm current parent/child forecasts, create new forecast
            previous_forecast = previous_forecasts[-1]
            current_utc_time = datetime.now(timezone.utc)
            if (
                previous_forecast.timestamp_end is None
                or previous_forecast.timestamp_end > current_utc_time
            ):
                pretty_value = DataOrganizer.get_readable_prediction(previous_forecast)  # type: ignore
                prediction = ReasonedPrediction(
                    prediction_value=PredictionAffirmed(),
                    reasoning=f"Already existing forecast reaffirmed at {pretty_value}.",
                )
                return (prediction, research)  # type: ignore
        info = await self._make_prediction(question, research)
        full_research = self._add_reasoning_to_research(research, info, question_type)
        return info, full_research  # type: ignore

    def _add_reasoning_to_research(
        self,
        research: str,
        reasoning: ReasonedPrediction[PredictionTypes],
        question_type: str,
    ) -> str:
        from forecasting_tools.data_models.data_organizer import DataOrganizer

        question_type = question_type.title()
        return clean_indents(
            f"""
            {research}
            ---
            ## {question_type} Question Information
            You have previously forecasted the {question_type} Question to the value: {DataOrganizer.get_readable_prediction(reasoning.prediction_value)}
            This is relevant information for your current forecast, but it is NOT your current forecast, but previous forecasting information that is relevant to your current forecast.
            The reasoning for the {question_type} Question was as such:
            ```
            {reasoning.reasoning}
            ```
            This is absolutely essential: do NOT use this reasoning to re-forecast the {question_type} question.
            """
        )

    def _get_conditional_disclaimer_if_necessary(
        self, question: MetaculusQuestion
    ) -> str:
        if question.conditional_type not in ["yes", "no"]:
            return ""
        return clean_indents(
            """
            As you are given a conditional question with a parent and child, you are to only forecast the **CHILD** question, given the parent question's resolution.
            You never re-forecast the parent question under any circumstances, but you use probabilistic reasoning, strongly considering the parent question's resolution, to forecast the child question.
            """
        )


if __name__ == "__main__":
    # Run timing: the run clock (for the 30-minute start cutoff and the shadow
    # phase) starts with the process; loop.time() is time.monotonic().
    RUN_STARTED = time.monotonic()
    configure_public_logging()
    if os.getenv("METACULUS_READ_TOKEN"):
        # Tony's personal read-only token is for the test bench only: the live
        # bot must never use it or see community predictions.
        raise SystemExit("METACULUS_READ_TOKEN must not be given to the bot.")

    parser = argparse.ArgumentParser(description="Run the template forecasting bot")
    parser.add_argument(
        "--mode",
        type=str,
        choices=["tournament", "metaculus_cup", "test_questions"],
        default="tournament",
        help="What to forecast on (default: tournament)",
    )
    parser.add_argument(
        "--break-on-purpose",
        action="store_true",
        help="Make every question fail (to check a failed run shows red)",
    )
    parser.add_argument(
        "--free-model",
        default=None,
        help="Test mode, free lineup only: use this OpenRouter ':free' model instead",
    )
    parser.add_argument(
        "--real-regression",
        action="store_true",
        help="Test mode, free lineup only: the regression-pack questions, research frozen, "
        "never submitted; prints how the replies were read",
    )
    parser.add_argument(
        "--one-binary",
        action="store_true",
        help="Test mode: forecast only one binary question",
    )
    parser.add_argument(
        "--lineup",
        choices=["free", "gemini-free", "credits", "replay", "replay-credits", "replay-gemini"],
        default=None,
        help="Model lineup (default: ACTIVE_LINEUP in bot_config.py). Test Bot uses 'free'.",
    )
    parser.add_argument(
        "--fail-planned-forecasts",
        action="store_true",
        help="Make every planned forecast fail (to check the quick forecast still submits)",
    )
    parser.add_argument(
        "--only-model",
        default=None,
        help="Forecast with only this Gemini model (e.g. gemini/gemini-3.8-flash)",
    )
    parser.add_argument(
        "--rehearsal",
        choices=list(SPEND_REHEARSALS),
        default=None,
        help="replay-credits only, spend-guard rehearsals: " + "; ".join(f"{k}: {v}" for k, v in SPEND_REHEARSALS.items()),
    )
    parser.add_argument(
        "--fail-model",
        default=None,
        help="replay-credits only: this model (e.g. openrouter/anthropic/claude-opus-5.5) "
        "fails every call, so its backup chain runs",
    )
    parser.add_argument(
        "--exhaust-flash",
        action="store_true",
        help="replay-gemini only: every Flash model starts the day used up and Flash-Lite "
        "at its reserve, so the backup chain (Nemotron) must answer",
    )
    args = parser.parse_args()
    run_mode: Literal["tournament", "metaculus_cup", "test_questions"] = args.mode

    check_environment(strict=True)
    publish_to_metaculus = True
    print_startup_banner(run_mode, will_publish=publish_to_metaculus)

    # All model choices live in bot_config.py.
    lineup = get_lineup(args.lineup, free_model=args.free_model)
    if args.real_regression:
        if lineup.name != "free" or run_mode != "test_questions":
            raise SystemExit("--real-regression only runs with --mode test_questions --lineup free.")
        # Research frozen: 0 AskNews calls (its free quota is for the live bot).
        lineup.llms["researcher"] = "replay"
    if lineup.test_only and run_mode != "test_questions":
        # OpenRouter ':free' models are for the bot-testing-area only (see CLAUDE.md).
        raise SystemExit(
            f"The {lineup.name} lineup may only run in test_questions mode, not {run_mode}. "
            "Change ACTIVE_LINEUP in bot_config.py."
        )
    if args.rehearsal and lineup.name != "replay-credits":
        raise SystemExit("--rehearsal only works with the replay-credits lineup.")
    if args.rehearsal:
        print(f"Spend-guard rehearsal: {args.rehearsal} ({SPEND_REHEARSALS[args.rehearsal]})")
    if args.rehearsal == "runaway":
        _RecordedAnswer.timeout_all = True
    if args.rehearsal == "unknown-cost":
        _RecordedAnswer.unknown_cost = True
    if args.fail_model:
        if lineup.name != "replay-credits":
            raise SystemExit("--fail-model only works with the replay-credits lineup.")
        _RecordedAnswer.failing_model = args.fail_model
        print(f"Deliberate outage: {args.fail_model} fails every call")
    flash_lite_before: dict[str, int] = {}
    if args.exhaust_flash:
        if lineup.name != "replay-gemini" or not isinstance(lineup.planner, GeminiPool):
            raise SystemExit("--exhaust-flash only works with the replay-gemini lineup.")
        exhausted_ledger = lineup.planner.ledger
        for model in GEMINI_FORECAST_MODELS:
            exhausted_ledger.mark_used_up(model)
        for bucket in dict.fromkeys(exhausted_ledger.bucket(m) for m in GEMINI_PARSER_MODELS):
            # Flash-Lite exactly at its reserve: usable quota 0, reserve full.
            exhausted_ledger.used[bucket] = exhausted_ledger.used.get(bucket, 0) + exhausted_ledger.usable_left(bucket)
            flash_lite_before[bucket] = exhausted_ledger.used[bucket]
        print("Forced: every Flash model used up today; Flash-Lite at its reserve")
    print(
        f"Model lineup: {lineup.name} "
        f"({', '.join(dict.fromkeys(lineup.llm_model_names()))})"
    )

    template_bot = FallBot2026(
        research_reports_per_question=lineup.research_reports_per_question,
        predictions_per_research_report=lineup.predictions_per_research_report,
        use_research_summary_to_forecast=False,
        enable_summarize_research=lineup.summarize_research,
        # The bot submits itself, after checking the question is still open.
        publish_reports_to_metaculus=False,
        folder_to_save_reports_to=None,
        skip_previously_forecasted_questions=True,
        extra_metadata_in_explanation=True,
        llms=lineup.llms,
        # The Gemini pool may plan fewer than the maximum forecasts; any
        # successful forecast is enough (0 forecasts still fails the question).
        required_successful_predictions=0 if lineup.planner else 0.5,
    )
    template_bot._structure_output_validation_samples = (
        lineup.parser_validation_samples
    )
    template_bot.break_on_purpose = args.break_on_purpose
    template_bot.planner = lineup.planner
    rehearsal_alerts: list[str] = []

    def rehearsal_notify(title: str, body: str) -> None:
        rehearsal_alerts.append(title)
        print(f"ALERT (rehearsal, no issue opened): {title}")

    if isinstance(lineup.planner, ReplayCreditsPlanner):
        # Rehearsal: the real tier choice and spend guards on a made-up credit
        # and key usage (no credit lookup, no saved tier, no GitHub issue).
        planner = lineup.planner
        target = ensemble.target_spend_per_question(
            REHEARSAL_CREDIT, REHEARSAL_CREDIT, ensemble.expected_remaining_questions()
        )
        key_usage = 0.0
        if args.rehearsal == "missing-ledger":
            from gemini_budget import MemoryStore

            planner.spend = SpendGuard(MemoryStore(None))
        if args.rehearsal == "key-mismatch":
            # The key says $1 more was spent today than our ledger knows.
            planner.spend.key_day_start[utc_day()] = 0.0
            key_usage = planner.spend.day_spent() + 1.0
        if args.rehearsal == "unknown-cost":
            planner.cap_factor = 0.5  # small caps, so unknown-cost calls reach them
        planner.seasonal_tier = guard_tier(ensemble.choose_tier(target), target, planner.spend, key_usage, rehearsal_notify)
        print(f"Spending tier: {planner.seasonal_tier}")
        template_bot.keep_records = True
    elif isinstance(lineup.planner, CreditsPlanner):
        # Step 6: pick this run's spending tier from the remaining credit.
        lineup.planner.seasonal_tier = choose_spending_tier(lineup.planner.spend)
    elif isinstance(lineup.planner, GeminiPool) and lineup.planner.spend is not None:
        # Paid-Flash fallback (bot_config.PAID_FLASH_FALLBACK): fail-closed
        # checks before any paid call (ledger readable, key usage vs ledger).
        allowed = paid_fallback_guard(lineup.planner.spend, ensemble.openrouter_key_usage(), ensemble.open_alert_issue)
        print(f"Paid Flash fallback: ON, {'allowed' if allowed else 'blocked: ' + str(lineup.planner.spend.blocked)}")
        # Remaining key limit under $2: an alert issue (Tony decides on a top-up).
        credit = ensemble.openrouter_credit()
        key_limit_alert(lineup.planner.spend, credit[0] if credit else None, ensemble.open_alert_issue)
    template_bot.only_model = args.only_model
    # Build 1b: the free shadow forecaster (never submitted) on the live and
    # the free test lineups; the replay model in replay (0 calls).
    if lineup.name in ("gemini-free", "free"):
        template_bot.shadow_llm = GeneralLlm(
            model=SHADOW_FORECAST_MODEL,
            temperature=None,
            timeout=SHADOW_FORECAST_TIMEOUT_SECONDS,
            allowed_tries=1,
        )
    elif lineup.name in ("replay", "replay-gemini"):
        template_bot.shadow_llm = ReplayLlm()
    if lineup.name == "replay-gemini":
        template_bot.keep_records = True
    # Consistency shadow index: the real one only for live tournament runs.
    from gemini_budget import MemoryStore as _MemoryStore
    from gemini_budget import make_store as _make_store

    template_bot.consistency_index = ForecastIndex(
        _make_store(INDEX_PATH) if run_mode == "tournament" else _MemoryStore({})
    )
    if isinstance(lineup.planner, GeminiPool) and run_mode == "tournament":
        # Build 4: the 7-day average of live questions per day (None = unknown).
        lineup.planner.questions_per_day = questions_per_day(os.getenv("DATA_REPO_TOKEN"))
        print(f"Live questions per day (7-day average): {lineup.planner.questions_per_day}")
    template_bot.fail_planned_forecasts = args.fail_planned_forecasts
    template_bot.run_mode = run_mode
    template_bot.run_started = RUN_STARTED
    template_bot.submit_forecasts = publish_to_metaculus
    template_bot.question_log = QuestionLogWriter()

    # Per-mode tournament URL shown in the summary banner footer.
    TOURNAMENT_URLS = {
        "tournament": "https://www.metaculus.com/tournament/fall-futureeval-2026/",
        "metaculus_cup": "https://www.metaculus.com/tournament/metaculus-cup-fall-2026/",
        "test_questions": "https://www.metaculus.com/tournament/bot-testing-area/",
    }

    # Dispatch on mode. Each branch produces a list of ForecastReport (or
    # exceptions, since return_exceptions=True) which then flows into the
    # summary printers below.
    client = MetaculusClient()
    if run_mode == "tournament":
        # Each tournament's list is fetched on its own, so a failure in one
        # (e.g. the question list not loading) doesn't stop the other.
        # Seasonal first: it's worth more, and gets priority for the budget;
        # but a MiniBench question about to close goes before it.
        forecast_reports = []
        minibench_id = current_minibench_id()
        open_by_tournament: dict = {}
        for tournament_id in (FALL_2026_TOURNAMENT_ID, minibench_id):
            try:
                open_by_tournament[tournament_id] = (
                    template_bot.metaculus_client.get_all_open_questions_from_tournament(tournament_id)
                )
            except Exception as e:
                logger.error(
                    f"Tournament {tournament_id}: could not run, {describe_exception(e)}"
                )
                forecast_reports.append(e)
        # Build 4b: a MiniBench round is active -> expect 25 questions a day.
        if isinstance(template_bot.planner, GeminiPool):
            template_bot.planner.minibench_active = minibench_active(len(open_by_tournament.get(minibench_id, [])))
            print(f"MiniBench round active (for the spare-quota rule): {template_bot.planner.minibench_active}")
        # Retry backoff for questions that failed in earlier runs.
        from gemini_budget import make_store

        retry_store = make_store(RETRY_STATE_PATH)
        try:
            retry_state = retry_store.load() or {}
        except Exception:
            retry_state = {}
        now = datetime.now(timezone.utc)
        attempted: list = []
        for tournament_id, questions in open_by_tournament.items():
            kept = []
            for q in questions:
                if should_try(retry_state.get(str(q.id_of_post)), q.close_time, now):
                    kept.append(q)
                    attempted.append(q.id_of_post)
                else:
                    logger.info(f"Question {q.id_of_post}: failed before, next try within 30 min (backoff)")
            open_by_tournament[tournament_id] = kept
        # Run timing: one queue, researched soonest-closing first.
        queued, template_bot.seasonal_by_post = run_queue(
            open_by_tournament.get(FALL_2026_TOURNAMENT_ID, []),
            open_by_tournament.get(minibench_id, []),
        )
        if queued:
            try:
                forecast_reports += asyncio.run(
                    template_bot.forecast_questions(queued, return_exceptions=True)
                )
            except Exception as e:
                logger.error(f"Forecasting could not run, {describe_exception(e)}")
                forecast_reports.append(e)
        # A question deferred by the 30-minute cutoff was never tried: no backoff.
        deferred = template_bot.__dict__.get("deferred_posts", set())
        attempted = [post for post in attempted if post not in deferred]
        failed = set(template_bot.__dict__.get("unforecast_close_times", {})) - deferred
        try:
            retry_store.save(update_retry_state(retry_state, attempted, failed, datetime.now(timezone.utc)))
        except Exception:
            logger.warning("Retry state could not be saved")
    elif run_mode == "metaculus_cup":
        # The Metaculus Cup may be uninitialized near the start of a season
        # (Jan/May/Sep). MetaculusClient.ACX_2025_TOURNAMENT = 32564 and
        # MetaculusClient.AI_2027_TOURNAMENT_ID = "ai-2027" are also valid
        # targets here.
        template_bot.skip_previously_forecasted_questions = False
        forecast_reports = asyncio.run(
            template_bot.forecast_on_tournament(
                client.CURRENT_METACULUS_CUP_ID, return_exceptions=True
            )
        )
    elif run_mode == "test_questions":
        # The bot-testing-area tournament contains all question types and is
        # the recommended target for smoke-testing your bot.
        # https://www.metaculus.com/tournament/bot-testing-area/
        # Free models have small daily limits, so forecast only the first
        # few open questions of each type the tournament uses.
        template_bot.skip_previously_forecasted_questions = False
    # Test mode, two kinds: real-regression (the regression pack) or the
    # bot-testing-area.
    if run_mode == "test_questions" and args.real_regression:
        # Closed past questions, never submitted: dry run.
        template_bot.submit_forecasts = False
        template_bot.keep_records = True
        regression_questions = load_regression_questions()
        print(f"Real-regression: {len(regression_questions)} regression-pack questions, never submitted")
        forecast_reports = []
        # A few at a time, to stay under the free model's per-minute limit.
        for start in range(0, len(regression_questions), REGRESSION_BATCH):
            forecast_reports += asyncio.run(
                template_bot.forecast_questions(
                    regression_questions[start : start + REGRESSION_BATCH], return_exceptions=True
                )
            )
        table = reading_table(template_bot.__dict__.get("kept_records", []), regression_questions)
        print(table)
        summary_path = os.getenv("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write("## Real-model regression: how replies were read\n\n" + table + "\n\n")
    elif run_mode == "test_questions":
        open_questions = client.get_all_open_questions_from_tournament(
            BOT_TESTING_AREA_ID
        )
        test_questions = pick_test_questions(open_questions)
        if args.one_binary:
            test_questions = [q for q in test_questions if q.question_type == "binary"][:1]
        print(
            f"Testing {len(test_questions)} of {len(open_questions)} open questions: "
            + ", ".join(f"{q.id_of_post} ({q.question_type})" for q in test_questions)
        )
        retry_lines: list[str] = []
        if args.rehearsal == "retry-run":
            # Pass 1: forecasts finish, the submission fails on purpose.
            spend = lineup.planner.spend
            template_bot.fail_submission = True
            asyncio.run(template_bot.forecast_questions(test_questions, return_exceptions=True))
            template_bot.fail_submission = False
            first = (spend.paid_calls, ReplayLlm.calls)
            # The retry pass is like a new run: new records.
            template_bot.__dict__.pop("kept_records", None)
            template_bot.__dict__.pop("_question_records", None)
            template_bot.__dict__["unforecast_close_times"] = {}
            retry_lines.append(f"Pass 1 (submission failed on purpose): {first[0]} paid call(s), {first[1]} recorded replies")
        forecast_reports = asyncio.run(
            template_bot.forecast_questions(test_questions, return_exceptions=True)
        )
        if args.rehearsal == "retry-run":
            spend = lineup.planner.spend
            paid, replies = spend.paid_calls - first[0], ReplayLlm.calls - first[1]
            reused = sum(1 for r in template_bot.__dict__.get("kept_records", []) for f in r["forecasts"] if f.get("reused"))
            retry_lines.append(f"Pass 2 (retry run): {paid} paid call(s), {replies} recorded replies, {reused} forecast(s) reused")
            retry_lines.append(f"**Nothing bought twice: {'yes' if paid == 0 and replies == 0 and reused > 0 else 'NO'}**")
            if not (paid == 0 and replies == 0 and reused > 0):
                forecast_reports.append(RuntimeError("retry run bought forecasts again"))
        if lineup.name == "replay-credits" and not args.rehearsal:
            # MiniBench runs one tier lower: forecast the binary question
            # again as if it were a MiniBench question.
            template_bot.forecasting_seasonal = False
            forecast_reports += asyncio.run(
                template_bot.forecast_questions(
                    [q for q in test_questions if q.question_type == "binary"][:1],
                    return_exceptions=True,
                )
            )
            template_bot.forecasting_seasonal = True

    if lineup.name.startswith("replay"):
        line = (
            f"Model calls: 0 (replay: {ReplayLlm.calls} recorded replies served; "
            "0 AskNews calls, research frozen)"
        )
        print(line)
        summary_path = os.getenv("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(f"**{line}**\n\n")
        if template_bot.keep_records:
            table = rehearsal_table(template_bot.__dict__.get("kept_records", []))
            if lineup.name == "replay-gemini" and isinstance(lineup.planner, GeminiPool):
                table += "\n\n" + backup_chain_summary(
                    template_bot.__dict__.get("kept_records", []), lineup.planner.ledger, flash_lite_before
                )
            print(table)
            if summary_path:
                with open(summary_path, "a", encoding="utf-8") as f:
                    f.write(table + "\n\n")
    runaway_ok = False
    if isinstance(lineup.planner, GeminiPool) and lineup.planner.spend is not None:
        # Paid-Flash fallback: any paid call with unknown cost opens an alert.
        unknown_cost_alert(lineup.planner.spend, ensemble.open_alert_issue)
    if isinstance(lineup.planner, CreditsPlanner) and lineup.planner.spend is not None:
        guard = lineup.planner.spend
        # 4d: a run with any unknown-cost paid call opens an alert (once a day).
        unknown_cost_alert(guard, rehearsal_notify if lineup.name == "replay-credits" else ensemble.open_alert_issue)
        guard.save()
        if args.rehearsal in ("runaway", "unknown-cost"):
            runaway_ok, table = runaway_table(guard)
            if args.rehearsal == "unknown-cost":
                runaway_ok = runaway_ok and guard.refused > 0 and guard.unknown_cost_calls > 0 and any(
                    "unknown cost" in a for a in rehearsal_alerts
                )
                table += f"\n\nUnknown-cost calls (charged the estimate): {guard.unknown_cost_calls}"
                if not runaway_ok:
                    forecast_reports.append(RuntimeError("unknown-cost rehearsal failed"))
            print(table)
            summary_path = os.getenv("GITHUB_STEP_SUMMARY")
            if summary_path:
                with open(summary_path, "a", encoding="utf-8") as f:
                    f.write(table + "\n\n")
        if args.rehearsal in ("missing-ledger", "key-mismatch"):
            wanted = "could not be loaded" if args.rehearsal == "missing-ledger" else "key usage above"
            ok = lineup.planner.seasonal_tier == "lean" and any(wanted in a for a in rehearsal_alerts)
            line = f"**{args.rehearsal}: tier {lineup.planner.seasonal_tier}, alerts {rehearsal_alerts or 'none'}: {'OK' if ok else 'FAILED'}**"
            print(line)
            summary_path = os.getenv("GITHUB_STEP_SUMMARY")
            if summary_path:
                with open(summary_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n\n")
            if not ok:
                forecast_reports.append(RuntimeError(f"{args.rehearsal} rehearsal failed"))
    if args.rehearsal == "retry-run" and retry_lines:
        print("\n".join(retry_lines))
        summary_path = os.getenv("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write("## Retry run (never re-buy)\n\n" + "\n\n".join(retry_lines) + "\n\n")
    shadow_stats = template_bot.__dict__.get("shadow_stats")
    if shadow_stats:
        line = (
            f"Shadow forecaster {getattr(template_bot.shadow_llm, 'model', '?')}: "
            f"answered {shadow_stats['answered']} of {shadow_stats['asked']} (never submitted)"
        )
        print(line)
        summary_path = os.getenv("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(f"{line}\n\n")
    if template_bot.question_log.saved:
        print(f"Question JSON logs saved to fall26-data: {len(template_bot.question_log.saved)}")
    if lineup.planner is not None:
        lineup.planner.save()
    if isinstance(lineup.planner, GeminiPool):
        ledger = lineup.planner.ledger
        print(
            f"Gemini requests used today ({ledger.day}, Pacific): "
            + ", ".join(f"{m.removeprefix('gemini/')} {n}" for m, n in sorted(ledger.used.items()))
        )

    # Not template_bot.log_report_summary: it prints reasoning, and the logs
    # are public.
    failures = log_question_statuses(forecast_reports)
    write_cost_summary(
        forecast_reports, lineup_name=lineup.name, billed=not lineup.free_only
    )
    print_run_summary_banner(
        forecast_reports,
        will_publish=publish_to_metaculus,
        tournament_url=TOURNAMENT_URLS.get(run_mode),
    )
    if failures and runaway_ok and args.rehearsal == "runaway":
        # A runaway rehearsal: no forecast is expected; the caps held.
        print(f"{failures} question(s) got no forecast, as expected: every model timed out")
        failures = 0
    if failures and args.real_regression:
        # A report run: failures are what the table is for.
        print(f"{failures} regression question(s) got no forecast (see the table)")
        failures = 0
    if failures:
        # Red (non-zero exit) when a question may now be missed: in test mode
        # always; in live modes only if it closes before the next runs can
        # retry it. Otherwise it's retried by the next run (a warning).
        closing_soon = [
            post
            for post, close in template_bot.__dict__.get("unforecast_close_times", {}).items()
            if close is None or closes_within(close, RETRY_WINDOW)
        ]
        if run_mode == "test_questions" or closing_soon or failures > len(
            template_bot.__dict__.get("unforecast_close_times", {})
        ):
            logger.error(f"{failures} question(s) failed")
            sys.exit(1)
        logger.warning(f"{failures} question(s) left for the next run")
        print(
            f"::warning title=question-retry::{failures} question(s) got no forecast "
            "this run; the next run will try again"
        )
