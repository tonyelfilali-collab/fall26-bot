"""
All model choices for FallBot2026, in one place.

Two lineups:
- FREE (active now): OpenRouter ':free' models only, for the bot-testing-area.
  OPENROUTER_API_KEY is a $0 key until Metaculus sends the credit key.
- CREDIT (prepared, switched off): the real lineup for the donated credit key.

To switch lineups, change USE_CREDIT_KEY_LINEUP. Do that only once the
Metaculus credit key is in the OPENROUTER_API_KEY secret.
"""
from __future__ import annotations

from dataclasses import dataclass

from forecasting_tools import GeneralLlm

USE_CREDIT_KEY_LINEUP = False

# AskNews news summaries: 2 AskNews calls per research report (latest news +
# news archive). Reads the ASKNEWS_API_KEY secret.
RESEARCHER = "asknews/news-summaries"

# How OpenRouter is asked for high reasoning effort (see NOTES.md).
HIGH_REASONING = {"reasoning": {"effort": "high"}}

# Free test model. Picked because it is a large free model on OpenRouter
# that supports structured output.
FREE_MODEL = "openrouter/nvidia/nemotron-3-super-120b-a12b:free"

CREDIT_FORECASTER_MODEL = "openrouter/anthropic/claude-opus-5.5"
CREDIT_HELPER_MODEL = "openrouter/google/gemini-3.6-flash"


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
    free_only: bool

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
    )


def get_lineup() -> Lineup:
    lineup = _credit_lineup() if USE_CREDIT_KEY_LINEUP else _free_lineup()
    if lineup.free_only:
        paid = [name for name in lineup.llm_model_names() if not name.endswith(":free")]
        if paid:
            raise ValueError(f"Free lineup contains non-free models: {paid}")
    return lineup
