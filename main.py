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
    MARKET_MODE,
    REHEARSAL_CREDIT,
    CreditsPlanner,
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
from deadlines import planned_forecast_timeout, quick_forecast_timeout
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
    multiple_choice_shadows,
    numeric_shadows,
    referee_shadow,
    two_line_reason,
    zero_cost_shadows,
)
from markets import match_question
from markets import to_records as market_records
from minibench import current_minibench_id
from real_regression import load_questions as load_regression_questions
from real_regression import reading_table
from replay import REPLAY_RESEARCH, ReplayLlm, _RecordedAnswer
from replay import current_question as replay_question
from research import run_planned_research
from question_log import QuestionLogWriter, question_snapshot, record_path, to_jsonable, utc_now

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


def choose_spending_tier() -> str:
    """
    Step 6: target spend per question = (remaining credit - 15% reserve) /
    expected remaining questions -> Full / Standard / Lean. A change from the
    last run's tier (kept in fall26-data) opens a GitHub issue (emails Tony).
    """
    import ensemble
    from gemini_budget import make_store

    credit = ensemble.openrouter_credit()
    if credit is None:
        logger.warning("Credit unknown; using the standard tier")
        return "standard"
    remaining, limit = credit
    target = ensemble.target_spend_per_question(
        remaining, limit, ensemble.expected_remaining_questions()
    )
    tier = ensemble.choose_tier(target)
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


def rehearsal_table(records: list[dict]) -> str:
    """Credits rehearsal summary: per question, the model slots used (planned
    -> answered, a backup shows as a different answered model), the tier and
    whether round 2 ran. Model names only: no forecast values (public logs)."""

    def short(model: str | None) -> str:
        return (model or "none").removeprefix("replay/").split("/")[-1]

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


# Real-regression: questions forecast at the same time.
REGRESSION_BATCH = 4

# A MiniBench question closing within this time is forecast before the
# seasonal ones (seasonal otherwise goes first, for time and for quota).
URGENT_MINIBENCH = timedelta(minutes=30)


def queue_batches(
    seasonal: list, minibench: list, now: datetime
) -> list[tuple[bool, list]]:
    """(is seasonal, questions) batches in forecasting order: MiniBench questions
    closing within 30 minutes (soonest first), then seasonal, then the rest of
    MiniBench."""

    def closes_soon(q) -> bool:  # type: ignore[no-untyped-def]
        close = getattr(q, "close_time", None)
        if close is None:
            return False
        close = close if close.tzinfo else close.replace(tzinfo=timezone.utc)
        return close - now <= URGENT_MINIBENCH

    urgent = sorted((q for q in minibench if closes_soon(q)), key=lambda q: q.close_time)
    rest = [q for q in minibench if not closes_soon(q)]
    batches = [(False, urgent), (True, list(seasonal)), (False, rest)]
    return [(flag, questions) for flag, questions in batches if questions]


# A question with no forecast that closes within this time can't count on a
# later run to retry it, so the run goes red.
RETRY_WINDOW = timedelta(minutes=25)


def closes_within(close_time: datetime, window: timedelta) -> bool:
    close = close_time if close_time.tzinfo else close_time.replace(tzinfo=timezone.utc)
    return close - datetime.now(timezone.utc) <= window


# The quick forecast (nothing else finished) tries this many passes through
# the Gemini models, this far apart.
QUICK_FORECAST_PASSES = 3
QUICK_FORECAST_RETRY_WAIT_SECONDS = 30

# The forecaster chain for the forecast running in the current asyncio task.
_planned_forecaster: contextvars.ContextVar = contextvars.ContextVar(
    "planned_forecaster", default=None
)


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

    _max_concurrent_questions = (
        1  # Set this to whatever works for your search-provider/ai-model rate limits
    )
    _concurrency_limiter = asyncio.Semaphore(_max_concurrent_questions)
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
    # priority when the budget is low). Set per tournament in __main__.
    forecasting_seasonal = True
    # Test switch: forecast with only this Gemini model.
    only_model: str | None = None
    # Credits rehearsal: keep each question's record for the job summary.
    keep_records = False

    async def _aggregate_predictions(
        self, predictions: list[PredictionTypes], question: MetaculusQuestion
    ) -> PredictionTypes:
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
        research = await self.run_research(question)
        record["research"] = {"fetched_at": utc_now(), "text": research}
        summary_report = await self.summarize_research(question, research)
        is_binary = isinstance(question, BinaryQuestion)
        forecasters = self.planner.plan(
            seasonal=self.forecasting_seasonal,
            only_model=self.only_model,
            binary=is_binary,
        )
        if hasattr(self.planner, "tier_name"):
            record["tier"] = self.planner.tier_name(self.forecasting_seasonal)
        record["round2"] = False
        logger.info(
            f"Question {question.id_of_post}: {len(forecasters)} forecast(s) planned "
            f"({', '.join(f.model for f in forecasters) or 'none: no quota left'})"
        )

        async def forecast_with(forecaster, kind: str, timeout: float | None):  # type: ignore[no-untyped-def]
            _planned_forecaster.set(forecaster)
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
            return prediction

        # The full set must be done 15 minutes before the close; what finished
        # by then is combined (median).
        timeout = planned_forecast_timeout(question.close_time)
        valid_predictions, errors, exception_group = (
            await self._gather_results_and_exceptions(
                [forecast_with(f, "planned", timeout) for f in forecasters]
            )
        )
        # Step 6: binary round 2 only when round 1 disagrees or is extreme.
        if is_binary and valid_predictions and not self.only_model and round2_needed(
            [p.prediction_value for p in valid_predictions]  # type: ignore[misc]
        ):
            extra = self.planner.round2(
                seasonal=self.forecasting_seasonal,
                used_models=[f.model for f in forecasters],
            )
            logger.info(
                f"Question {question.id_of_post}: round 1 disagrees or is extreme, "
                f"round 2 with {len(extra)} more forecast(s)"
            )
            record["round2"] = bool(extra)
            if extra:
                more, more_errors, _ = await self._gather_results_and_exceptions(
                    [
                        forecast_with(f, "round2", planned_forecast_timeout(question.close_time))
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
            if quick_timeout == 0 or not self.planner.any_quota_left():
                break
            if quick_pass > 0:
                await asyncio.sleep(QUICK_FORECAST_RETRY_WAIT_SECONDS)
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
        record.setdefault("reading", []).append(how)
        if how != "direct" and reply:
            record.setdefault("unread_replies", []).append(reply[-4000:])
        logger.info(f"Question {question.id_of_post}: answer read: {how}")

    async def _run_individual_question(self, question: MetaculusQuestion) -> ForecastReport:
        replay_question.set(question)  # replay mode answers from the question's shape
        started = datetime.now(timezone.utc)
        record = self._record_for(question)
        try:
            report = await super()._run_individual_question(question)
        except Exception as e:
            # Never a pure guess: with no real model forecast, nothing is
            # submitted and the next run tries again (it isn't marked as
            # forecast), until the question closes.
            record.update(submitted=False, error=describe_exception(e))
            self.__dict__.setdefault("unforecast_close_times", {})[
                question.id_of_post
            ] = question.close_time
            logger.warning(
                f"Question {question.id_of_post}: no real forecast this run, "
                "left for the next run"
            )
            await self._save_record(question, record, started)
            raise
        submitted = await self._submit_if_still_open(question, report)
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
        record.update(
            submitted=submitted,
            final_forecast=to_jsonable(report.prediction),
            minutes=report.minutes_taken,
            list_price_cost=report.price_estimate,
        )
        await self._save_record(question, record, started)
        return report

    async def _submit_if_still_open(
        self, question: MetaculusQuestion, report: ForecastReport
    ) -> bool:
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
        return await asyncio.wait_for(
            super()._make_prediction(question, research),
            planned_forecast_timeout(question.close_time),
        )

    ##################################### RESEARCH #####################################

    async def run_research(self, question: MetaculusQuestion) -> str:
        if self.break_on_purpose:
            raise RuntimeError("Deliberate failure (--break-on-purpose)")
        async with self._concurrency_limiter:
            try:
                research = await self._run_research_unguarded(question)
            except Exception as e:
                # A missed question scores 0: forecast without news rather
                # than not at all.
                logger.warning(
                    f"Question {question.id_of_post}: research failed"
                    f"{_http_status_note(e)}, forecasting without it "
                    f"({describe_exception(e)})"
                )
                return "No research is available: the news search failed."
            logger.info(f"Question {question.id_of_post}: research done")
            return research

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
            question, dates_line(question), helper.invoke
        )
        logger.info(
            f"Question {question.id_of_post}: articles found: {result.articles} "
            f"(AskNews {result.asknews_articles} in {result.asknews_calls} call(s), "
            f"free news {result.free_articles}); {len(result.queries)} queries; "
            f"dossier {'written' if result.dossier_written else 'not written, using the articles'}"
            f"{', gap-filled' if result.gap_filled else ''}; "
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
        }
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
        current = self._record_for(question).get("research_detail", {}).get("current_value")
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
        choices=["free", "gemini-free", "credits", "replay", "replay-credits"],
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
        "--fail-model",
        default=None,
        help="replay-credits only: this model (e.g. openrouter/anthropic/claude-opus-5.5) "
        "fails every call, so its backup chain runs",
    )
    args = parser.parse_args()
    run_mode: Literal["tournament", "metaculus_cup", "test_questions"] = args.mode

    check_environment(strict=True)
    publish_to_metaculus = True
    print_startup_banner(run_mode, will_publish=publish_to_metaculus)

    # All model choices live in bot_config.py.
    lineup = get_lineup(args.lineup)
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
    if args.fail_model:
        if lineup.name != "replay-credits":
            raise SystemExit("--fail-model only works with the replay-credits lineup.")
        _RecordedAnswer.failing_model = args.fail_model
        print(f"Deliberate outage: {args.fail_model} fails every call")
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
    if isinstance(lineup.planner, ReplayCreditsPlanner):
        # Rehearsal: the real tier choice on a made-up credit (no credit
        # lookup, no saved tier, no GitHub issue).
        lineup.planner.seasonal_tier = ensemble.choose_tier(
            ensemble.target_spend_per_question(
                REHEARSAL_CREDIT, REHEARSAL_CREDIT, ensemble.expected_remaining_questions()
            )
        )
        template_bot.keep_records = True
    elif isinstance(lineup.planner, CreditsPlanner):
        # Step 6: pick this run's spending tier from the remaining credit.
        lineup.planner.seasonal_tier = choose_spending_tier()
    template_bot.only_model = args.only_model
    template_bot.fail_planned_forecasts = args.fail_planned_forecasts
    template_bot.run_mode = run_mode
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
        # Seasonal first, except MiniBench questions closing within 30 minutes.
        for seasonal, questions in queue_batches(
            open_by_tournament.get(FALL_2026_TOURNAMENT_ID, []),
            open_by_tournament.get(minibench_id, []),
            datetime.now(timezone.utc),
        ):
            template_bot.forecasting_seasonal = seasonal
            try:
                forecast_reports += asyncio.run(
                    template_bot.forecast_questions(questions, return_exceptions=True)
                )
            except Exception as e:
                logger.error(f"Forecasting could not run, {describe_exception(e)}")
                forecast_reports.append(e)
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
        forecast_reports = asyncio.run(
            template_bot.forecast_questions(test_questions, return_exceptions=True)
        )
        if lineup.name == "replay-credits":
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
            print(table)
            if summary_path:
                with open(summary_path, "a", encoding="utf-8") as f:
                    f.write(table + "\n\n")
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
