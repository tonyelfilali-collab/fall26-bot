"""
All model choices for FallBot2026, in one place.

Three lineups:
- "free": OpenRouter ':free' models, for the bot-testing-area only
  (OPENROUTER_API_KEY is a $0 key until Metaculus sends the credit key).
- "gemini-free" (active now, PLAN.md "Plan B"): Gemini 3.6 Flash on the free
  Google AI Studio key (GEMINI_API_KEY, no billing), paced to stay inside the
  free rate limits. Allowed on real questions once BOT_ENABLED is true.
- "credit" (prepared, off): the lineup for the Metaculus credit key.

To switch lineups, change ACTIVE_LINEUP.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from forecasting_tools import GeneralLlm

from llm_throttle import RequestPacer, ThrottledLlm

ACTIVE_LINEUP: Literal["free", "gemini-free", "credit"] = "gemini-free"

# AskNews news summaries: 2 AskNews calls per research report (latest news +
# news archive). Reads the ASKNEWS_API_KEY secret.
RESEARCHER = "asknews/news-summaries"

# How OpenRouter is asked for high reasoning effort (see NOTES.md).
HIGH_REASONING = {"reasoning": {"effort": "high"}}

# Free test model. Picked because it is a large free model on OpenRouter
# that supports structured output.
FREE_MODEL = "openrouter/nvidia/nemotron-3-super-120b-a12b:free"

# Google AI Studio directly (LiteLLM "gemini/" prefix, reads GEMINI_API_KEY).
GEMINI_FREE_MODEL = "gemini/gemini-3.6-flash"
# Tried in order when the model before is overloaded or out of quota. Each
# has its own free quota on the same key (limits are per model).
GEMINI_FREE_BACKUP_MODELS = ("gemini/gemini-3.7-flash", "gemini/gemini-3.5-flash")
# Free-tier limits for Gemini 3.6 Flash, from the AI Studio rate-limit page
# (27 Sep 2026): 5 requests/minute, 250K tokens/minute, 20 requests/day
# (per project; the day resets at midnight Pacific).
GEMINI_FREE_REQUESTS_PER_DAY = 20
# Every call (forecasts, parsing, retries) shares this pace, kept under 5/min.
GEMINI_FREE_REQUESTS_PER_MINUTE = 4
# Daily budget: keep 20% in reserve and plan for about 8 questions a day, so
# 16 calls a day = 2 per question = 1 forecast + 1 parse.
GEMINI_FREE_RESERVE = 0.2
GEMINI_FREE_QUESTIONS_PER_DAY = 8
GEMINI_FREE_CALLS_PER_FORECAST = 2  # the forecast itself + parsing it
GEMINI_FREE_FORECASTS_PER_QUESTION = max(
    1,
    int(
        GEMINI_FREE_REQUESTS_PER_DAY
        * (1 - GEMINI_FREE_RESERVE)
        / GEMINI_FREE_QUESTIONS_PER_DAY
        / GEMINI_FREE_CALLS_PER_FORECAST
    ),
)

CREDIT_FORECASTER_MODEL = "openrouter/anthropic/claude-opus-5.5"
CREDIT_HELPER_MODEL = "openrouter/google/gemini-3.6-flash"


def is_free_model(model: str) -> bool:
    """
    True for models that cannot be charged: OpenRouter ':free' models, and
    Gemini through the Google AI Studio key (free tier, no billing on it).
    Every other model, including every paid OpenRouter model, is False.
    """
    if model.startswith("openrouter/"):
        return model.endswith(":free")
    return model.startswith("gemini/")


@dataclass(frozen=True)
class Lineup:
    name: str
    llms: dict[str, str | GeneralLlm]
    research_reports_per_question: int
    predictions_per_research_report: int
    # structure_output parses each forecast this many times and checks the
    # answers agree. Each sample is one extra parser call.
    parser_validation_samples: int
    # The research summary only goes into the comment posted with the forecast.
    summarize_research: bool
    # Every LLM must pass is_free_model.
    free_only: bool
    # Only allowed in test_questions mode (the bot-testing-area).
    test_only: bool

    def llm_model_names(self) -> list[str]:
        """Model names of the LLMs and their backups (AskNews is not an LLM)."""
        names = []
        for llm in self.llms.values():
            while isinstance(llm, GeneralLlm):
                names.append(llm.model)
                llm = getattr(llm, "_backup", None)
        return names


def _free_lineup() -> Lineup:
    # Free models have tight rate limits, so keep calls per question low:
    # 1 forecast + 1 parse, no research summary.
    free_llm = GeneralLlm(
        model=FREE_MODEL,
        temperature=0.3,
        timeout=180,
        allowed_tries=3,
    )
    return Lineup(
        name="free",
        llms={
            "default": free_llm,
            "parser": free_llm,
            "summarizer": free_llm,
            "researcher": RESEARCHER,
        },
        research_reports_per_question=1,
        predictions_per_research_report=1,
        parser_validation_samples=1,
        summarize_research=False,
        free_only=True,
        test_only=True,
    )


def _gemini_chain(pacers: dict[str, RequestPacer], **kwargs) -> ThrottledLlm:
    """GEMINI_FREE_MODEL with its backups behind it, each on its model's pacer."""
    llm: ThrottledLlm | None = None
    for model in reversed((GEMINI_FREE_MODEL, *GEMINI_FREE_BACKUP_MODELS)):
        llm = ThrottledLlm(model=model, pacer=pacers[model], backup=llm, **kwargs)
    assert llm is not None
    return llm


def _gemini_free_lineup() -> Lineup:
    # One pacer per model (the free quota is per model), shared by forecaster
    # and parser. Per question: GEMINI_FREE_FORECASTS_PER_QUESTION forecasts,
    # each parsed once. Only 2 tries per call: a failed call can still use up
    # the day's quota.
    pacers = {
        model: RequestPacer(GEMINI_FREE_REQUESTS_PER_MINUTE)
        for model in (GEMINI_FREE_MODEL, *GEMINI_FREE_BACKUP_MODELS)
    }
    forecaster = _gemini_chain(
        pacers,
        temperature=None,
        timeout=300,
        allowed_tries=2,
        # LiteLLM turns this into Gemini's thinkingLevel "high". (Setting
        # thinkingConfig through extra_body is overwritten with "low".)
        reasoning_effort="high",
    )
    parser = _gemini_chain(pacers, temperature=None, timeout=180, allowed_tries=2)
    return Lineup(
        name="gemini-free",
        llms={
            "default": forecaster,
            "parser": parser,
            "summarizer": parser,
            "researcher": RESEARCHER,
        },
        research_reports_per_question=1,
        predictions_per_research_report=GEMINI_FREE_FORECASTS_PER_QUESTION,
        parser_validation_samples=1,
        summarize_research=False,
        free_only=True,
        test_only=False,
    )


def _credit_lineup() -> Lineup:
    forecaster = GeneralLlm(
        model=CREDIT_FORECASTER_MODEL,
        temperature=None,  # reasoning models: leave temperature unset
        timeout=600,
        allowed_tries=3,
        extra_body=HIGH_REASONING,
    )
    helper = GeneralLlm(
        model=CREDIT_HELPER_MODEL,
        temperature=None,
        timeout=180,
        allowed_tries=3,
    )
    return Lineup(
        name="credit",
        llms={
            "default": forecaster,
            "parser": helper,
            "summarizer": helper,
            "researcher": RESEARCHER,
        },
        research_reports_per_question=1,
        predictions_per_research_report=5,
        parser_validation_samples=2,
        summarize_research=True,
        free_only=False,
        test_only=False,
    )


_LINEUPS = {
    "free": _free_lineup,
    "gemini-free": _gemini_free_lineup,
    "credit": _credit_lineup,
}


def get_lineup() -> Lineup:
    lineup = _LINEUPS[ACTIVE_LINEUP]()
    if lineup.free_only:
        paid = [name for name in lineup.llm_model_names() if not is_free_model(name)]
        if paid:
            raise ValueError(f"Free lineup contains models that can be charged: {paid}")
    return lineup
