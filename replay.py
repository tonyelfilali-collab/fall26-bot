"""
Replay mode for Test Bot (the Tier B merge gate): the real pipeline end to end
on the bot-testing-area (fetch, read answers, combine, platform checks,
submit), with 0 model calls and 0 AskNews calls.

- Every model call returns a recorded reply, built from the question's own
  shape in exactly the format the prompts ask for (fixtures, not real
  reasoning, since the repo is public).
- The numeric reply lists its percentiles in REVERSE order (a real model
  mistake), so the "put reversed percentiles right" fix is exercised: break
  that fix and replay goes red.
- Research is frozen: a fixed text, no searches.
"""
from __future__ import annotations

import contextvars
from typing import Any

from forecasting_tools import (
    BinaryQuestion,
    DateQuestion,
    GeneralLlm,
    MultipleChoiceQuestion,
    NumericQuestion,
)

REPLAY_MODEL = "replay/recorded"
REPLAY_RESEARCH = "Replay research (frozen): no searches were made."
STEP8_HEIGHTS = ("2.5", "5", "10", "25", "50", "75", "90", "95", "97.5")
_POSITIONS = (0.05, 0.08, 0.12, 0.25, 0.40, 0.55, 0.70, 0.78, 0.85)

# The question being forecast in the current asyncio task (set by main.py).
current_question: contextvars.ContextVar[Any] = contextvars.ContextVar("replay_question", default=None)


def recorded_reply(question: Any) -> str:
    """A reply in the prompt's own answer format for this question."""
    if isinstance(question, BinaryQuestion):
        return "Replayed reasoning.\nProbability: 37%"
    if isinstance(question, MultipleChoiceQuestion):
        options = list(question.options)
        first = 40 if len(options) > 1 else 100
        rest = (100 - first) / max(len(options) - 1, 1)
        lines = [f"{options[0]}: {first}%"] + [f"{o}: {rest:.4f}%" for o in options[1:]]
        return "Replayed reasoning.\n" + "\n".join(lines)
    if isinstance(question, NumericQuestion) and not isinstance(question, DateQuestion):
        lower = question.nominal_lower_bound if question.nominal_lower_bound is not None else question.lower_bound
        upper = question.nominal_upper_bound if question.nominal_upper_bound is not None else question.upper_bound
        values = [lower + (upper - lower) * p for p in _POSITIONS]
        if question.zero_point is not None and lower > question.zero_point:
            values = [lower * (upper / lower) ** p for p in _POSITIONS]
        pairs = list(zip(STEP8_HEIGHTS, values))
        if question.question_type == "numeric":
            # Reversed on purpose: highest value first.
            pairs = list(zip(STEP8_HEIGHTS, reversed(values)))
        return "Replayed reasoning.\n" + "\n".join(f"Percentile {h}: {v:.6f}" for h, v in pairs)
    raise ValueError(f"No recorded reply for {type(question).__name__}")


class ReplayLlm(GeneralLlm):
    """Answers from recorded replies; never calls a model."""

    calls = 0  # recorded replies served in this run

    def __init__(self) -> None:
        super().__init__(model=REPLAY_MODEL, temperature=None)

    async def invoke(self, prompt: Any, system_prompt: str | None = None) -> str:
        question = current_question.get()
        if question is None:
            raise RuntimeError("replay: no current question")
        ReplayLlm.calls += 1
        return recorded_reply(question)
