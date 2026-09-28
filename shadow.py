"""
PLAN.md Step 10: silent tests (shadow mode).

Shadow variants are extra forecasts for binary questions that are computed and
SAVED (the question's JSON log in fall26-data, field "shadow") but NEVER
submitted. shadow_score.py later scores them against the live forecast on
resolved questions (log score).

Variants:
- "stretch-1.2" and "mean": free (no model call), computed from the forecasts
  already made. stretch-1.2 is the PLAN's stretch setting (switched off live),
  so Step 11 gets real data on it.
- "referee": a model sees each forecaster's number and a two-line reason and
  gives its own number, kept between the lowest and highest forecast; the
  candidate is the average of the referee and the median. SWITCHED OFF until
  credits arrive (REFEREE_ENABLED), because it costs a model call per question.
"""
from __future__ import annotations

import re
import statistics
from typing import Awaitable, Callable

from forecast_safety import adjust_binary, clip_binary, stretch

ZERO_COST_VARIANTS = ("stretch-1.2", "mean")
REFEREE_ENABLED = False


def zero_cost_shadows(forecasts: list[float]) -> dict[str, float]:
    """The free variants, each through the same final steps as the live answer."""
    if not forecasts:
        return {}
    shadows = {}
    if "stretch-1.2" in ZERO_COST_VARIANTS:
        shadows["stretch-1.2"] = adjust_binary(forecasts, k=1.2)
    if "mean" in ZERO_COST_VARIANTS:
        shadows["mean"] = clip_binary(stretch(statistics.fmean(forecasts)))
    return shadows


# ---------------------------------------------------------------- referee (off)


def two_line_reason(reasoning: str) -> str:
    """The last two non-empty lines before the final 'Probability:' line."""
    body = re.split(r"Probability:", reasoning, flags=re.IGNORECASE)[0]
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    return " ".join(lines[-2:])[:400]


def referee_prompt(question_text: str, forecasts: list[tuple[float, str]]) -> str:
    listing = "\n".join(f"- Forecaster {i + 1}: {p:.0%}. {reason}" for i, (p, reason) in enumerate(forecasts))
    return (
        "You are a referee between forecasters. Weigh their reasons and give one probability.\n\n"
        f"Question: {question_text}\n\nForecasts:\n{listing}\n\n"
        'The last thing you write is: "Probability: ZZ%".'
    )


def referee_candidate(referee: float, forecasts: list[float]) -> float:
    """Referee kept between the lowest and highest forecast; candidate = mean of it and the median."""
    bounded = min(max(referee, min(forecasts)), max(forecasts))
    return clip_binary((bounded + statistics.median(forecasts)) / 2)


async def referee_shadow(
    question_text: str,
    forecasts: list[tuple[float, str]],
    invoke: Callable[[str], Awaitable[str]],
    parse: Callable[[str], float | None],
) -> float | None:
    if not REFEREE_ENABLED or len(forecasts) < 2:
        return None
    answer = parse(await invoke(referee_prompt(question_text, forecasts)))
    if answer is None:
        return None
    return referee_candidate(answer, [p for p, _ in forecasts])
