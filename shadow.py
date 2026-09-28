"""
PLAN.md Step 10: silent tests (shadow mode).

Shadow variants are extra forecasts that are computed and SAVED (the
question's JSON log in fall26-data, field "shadow") but NEVER submitted.
scoreboard.py scores them against the live forecast on resolved questions
(log score), per question type.

Zero-cost variants (no model call, computed from the forecasts already made):
- Binary: "stretch-1.2" (the PLAN's stretch setting, off live), "stretch-1.5",
  "mean", "geo-mean-odds" (geometric mean of the odds), "trimmed-mean" (drops
  the highest and lowest when there are 5 or more forecasts).
- Multiple choice (live: median per option, 1% floor): "mean" (mean per
  option). (A 0.5% floor variant was dropped: the library clamps options
  below ~1%, so it could never be submitted.)
- Numeric / discrete (live: pointwise median of the CDFs, 5% uniform):
  "mean-cdf" (pointwise mean), "uniform-2%" (median, 2% uniform).
- "referee": a model sees each forecaster's number and a two-line reason and
  gives its own number, kept between the lowest and highest forecast; the
  candidate is the average of the referee and the median. SWITCHED OFF until
  credits arrive (REFEREE_ENABLED), because it costs a model call per question.
"""
from __future__ import annotations

import logging
import math
import re
import statistics
from typing import Any, Awaitable, Callable

from forecasting_tools import PredictedOptionList

from bot_helpers import PUBLIC_LOGGER_NAME
from forecast_safety import adjust_binary, clip_binary, floor_probabilities, is_valid_probability, stretch

logger = logging.getLogger(PUBLIC_LOGGER_NAME)

ZERO_COST_VARIANTS = ("stretch-1.2", "stretch-1.5", "mean", "geo-mean-odds", "trimmed-mean")
MULTIPLE_CHOICE_VARIANTS = ("mean",)
NUMERIC_VARIANTS = ("mean-cdf", "uniform-2%")
TRIM_FROM = 5  # trimmed mean drops the highest and lowest from this many forecasts
_ODDS_CLAMP = 1e-3
REFEREE_ENABLED = False


def geometric_mean_of_odds(forecasts: list[float]) -> float:
    odds = [math.log(p / (1 - p)) for p in (min(max(f, _ODDS_CLAMP), 1 - _ODDS_CLAMP) for f in forecasts)]
    return 1 / (1 + math.exp(-statistics.fmean(odds)))


def trimmed_mean(forecasts: list[float]) -> float:
    ordered = sorted(forecasts)
    return statistics.fmean(ordered[1:-1] if len(ordered) >= TRIM_FROM else ordered)


def zero_cost_shadows(forecasts: list[float]) -> dict[str, float]:
    """The free binary variants, each through the same final steps as the live answer."""
    forecasts = [float(f) for f in forecasts if is_valid_probability(f)]
    if not forecasts:
        return {}
    shadows = {
        "stretch-1.2": adjust_binary(forecasts, k=1.2),
        "stretch-1.5": adjust_binary(forecasts, k=1.5),
        "mean": clip_binary(stretch(statistics.fmean(forecasts))),
        "geo-mean-odds": clip_binary(stretch(geometric_mean_of_odds(forecasts))),
        "trimmed-mean": clip_binary(stretch(trimmed_mean(forecasts))),
    }
    return {name: p for name, p in shadows.items() if name in ZERO_COST_VARIANTS}


def _option_list(names: list[str], probabilities: list[float]) -> dict:
    # Plain numbers, in the saved form of a PredictedOptionList.
    return {"predicted_options": [{"option_name": n, "probability": p} for n, p in zip(names, probabilities)]}


def multiple_choice_shadows(
    predictions: list[PredictedOptionList], raw: list[list[float]] | None = None
) -> dict[str, dict]:
    """
    Mean per option (1% floor).
    raw: each model's probabilities as written, before the 1% floor applied
    when the answer is read. Used when every model's text could be read;
    otherwise the predictions themselves.
    """
    if not predictions:
        return {}
    names = [o.option_name for o in predictions[0].predicted_options]
    if raw and len(raw) == len(predictions) and all(len(r) == len(names) for r in raw):
        per_option = [[r[i] for r in raw] for i in range(len(names))]
    else:
        per_option = [
            [o.probability for p in predictions for o in p.predicted_options if o.option_name == name] or [0.0]
            for name in names
        ]
    return {
        "mean": _option_list(names, floor_probabilities([statistics.fmean(v) for v in per_option])),
    }


def numeric_shadows(distributions: list[Any], question: Any) -> dict[str, Any]:
    """Pointwise mean of the CDFs (5% uniform), and the live median with 2% uniform."""
    from distributions import combine_numeric

    shadows = {}
    for name, kwargs in (("mean-cdf", {"center": "mean"}), ("uniform-2%", {"weight": 0.02})):
        try:
            shadows[name] = combine_numeric(distributions, question, **kwargs)
        except Exception as e:  # a shadow never stops the live forecast
            logger.warning(f"Shadow {name} could not be built ({type(e).__name__})")
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


def combine_with_shadow(question: Any, predictions: list[Any]) -> Any:
    """'Live median with the shadow forecaster added': the live combine
    (median and the Step 4/8 checks) over the live forecasts plus the shadow's."""
    from forecasting_tools import BinaryQuestion, MultipleChoiceQuestion, NumericQuestion

    from distributions import combine_numeric, median_multiple_choice

    if isinstance(question, BinaryQuestion):
        return adjust_binary(predictions)
    if isinstance(question, MultipleChoiceQuestion):
        return median_multiple_choice(predictions)
    if isinstance(question, NumericQuestion):
        return combine_numeric(predictions, question)
    raise ValueError(f"no combine for {type(question).__name__}")
