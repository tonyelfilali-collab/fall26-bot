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
- Numeric / discrete with a window baseline (window_baseline.py, shadow
  "window"): "window-blend" = 0.5 x the live CDF + 0.5 x the window CDF,
  pointwise on the question's grid (21 Oct). No baseline: no blend.
- "referee" (binary, built OFF: REFEREE_ENABLED): only when 2+ forecasts
  finished and they are more than 15 points apart. After submission, Claude
  Opus 5.5 (effort high, the Claude budget, one try, no backups, 240 s, not
  after minute 40) sees each forecaster's number and a two-line reason and
  gives its own; candidate = mean of it and the submitted forecast, kept
  between the lowest and highest forecast. Claude off: no referee.
"""
from __future__ import annotations

import asyncio
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
WINDOW_BLEND = "window-blend"
WINDOW_BLEND_WEIGHT = 0.5  # share of the window baseline in the blend
TRIM_FROM = 5  # trimmed mean drops the highest and lowest from this many forecasts
_ODDS_CLAMP = 1e-3
REFEREE_ENABLED = False
REFEREE_MIN_FORECASTS = 2
REFEREE_MIN_SPREAD = 0.15  # max - min must be over this (15 percentage points)
REFEREE_TIMEOUT_SECONDS = 240  # then cancelled (charged as any Claude timeout)
REFEREE_LATEST_SECONDS = 40 * 60  # no referee once the run is past minute 40


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


def _cdf_on_grid(forecast: Any, question: Any) -> Any:
    """A forecast's CDF on the question's grid, from a distribution or its saved
    JSON form. Its points are put in the question's own x-axis (log-scaled
    questions) and interpolated onto the grid, so a different point set still
    lines up with the live CDF."""
    import numpy as np

    from distributions import _grid, _template

    if isinstance(forecast, dict):
        points = [(float(p["value"]), float(p["percentile"])) for p in forecast.get("declared_percentiles") or []]
    else:
        points = [(float(p.value), float(p.percentile)) for p in forecast.declared_percentiles]
    if len(points) < 2:
        raise ValueError("need at least 2 CDF points")
    template = _template(question)
    located = sorted((template._nominal_location_to_cdf_location(v), h) for v, h in points)
    return np.interp(_grid(question), [x for x, _ in located], [h for _, h in located])


def window_blend(live: Any, window: Any, question: Any, weight: float = WINDOW_BLEND_WEIGHT) -> Any | None:
    """(1 - weight) x the live CDF + weight x the window baseline's CDF, point by
    point on the question's grid. None if the blend breaks the platform rules
    (forecast_safety.distribution_problems)."""
    from forecasting_tools import NumericDistribution, Percentile

    from distributions import _grid, _template, _to_distribution
    from forecast_safety import distribution_problems

    blend = (1 - weight) * _cdf_on_grid(live, question) + weight * _cdf_on_grid(window, question)
    template = _template(question)
    values = [template._cdf_location_to_nominal_location(loc) for loc in _grid(question)]
    try:
        distribution = NumericDistribution.from_question(
            [Percentile(value=v, percentile=float(p)) for v, p in zip(values, blend)], question
        )
    except ValueError:
        # As combine_numeric: let the library standardise an open tail just off.
        try:
            distribution = _to_distribution(blend, question, template)
        except ValueError:
            return None
    return None if distribution_problems(distribution, question) else distribution


# ---------------------------------------------------------------- referee (built off)


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


def referee_needed(forecasts: list[float]) -> bool:
    """2+ finished forecasts more than 15 percentage points apart."""
    return len(forecasts) >= REFEREE_MIN_FORECASTS and round(max(forecasts) - min(forecasts), 6) > REFEREE_MIN_SPREAD


def referee_candidate(referee: float, submitted: float, forecasts: list[float]) -> float:
    """Mean of the referee and the submitted forecast, kept between the lowest and highest forecast."""
    return min(max((referee + submitted) / 2, min(forecasts)), max(forecasts))


async def referee_shadow(
    question_text: str,
    forecasts: list[tuple[float, str]],
    submitted: float,
    invoke: Callable[[str], Awaitable[str]],
    parse: Callable[[str], float | None],
) -> float | None:
    """The referee candidate, or None (off, not needed, no answer). Over
    REFEREE_TIMEOUT_SECONDS: asyncio.TimeoutError (the call is cancelled)."""
    if not REFEREE_ENABLED or not referee_needed([p for p, _ in forecasts]):
        return None
    reply = await asyncio.wait_for(invoke(referee_prompt(question_text, forecasts)), REFEREE_TIMEOUT_SECONDS)
    answer = parse(reply)
    if answer is None:
        return None
    return referee_candidate(answer, submitted, [p for p, _ in forecasts])


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
