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
# Free-tier requests per minute we allow ourselves for that model. Kept below
# the free limit; every call (forecasts, parsing, retries) shares it.
GEMINI_FREE_REQUESTS_PER_MINUTE = 8

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
        """Model names of the LLMs (the AskNews researcher is not an LLM)."""
        return [
            llm.model for llm in self.llms.values() if isinstance(llm, GeneralLlm)
        ]


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


def _gemini_free_lineup() -> Lineup:
    # Forecaster and parser are the same model, so they share one pacer (the
    # free quota is per model). Per question: 5 forecasts + 5 parses.
    pacer = RequestPacer(GEMINI_FREE_REQUESTS_PER_MINUTE)
    forecaster = ThrottledLlm(
        model=GEMINI_FREE_MODEL,
        temperature=None,
        timeout=300,
        allowed_tries=3,
        # LiteLLM turns this into Gemini's thinkingLevel "high". (Setting
        # thinkingConfig through extra_body is overwritten with "low".)
        reasoning_effort="high",
        pacer=pacer,
    )
    parser = ThrottledLlm(
        model=GEMINI_FREE_MODEL,
        temperature=None,
        timeout=180,
        allowed_tries=3,
        pacer=pacer,
    )
    return Lineup(
        name="gemini-free",
        llms={
            "default": forecaster,
            "parser": parser,
            "summarizer": parser,
            "researcher": RESEARCHER,
        },
        research_reports_per_question=1,
        predictions_per_research_report=5,
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
