"""
Safety checks on forecasts (PLAN.md Step 4). Small pure functions, tested in
tests/test_forecast_safety.py.

Binary, always in this order (PLAN.md section 3):
  1. median of the forecasts
  2. stretch in log-odds by STRETCH_K (1.0 = off until we've measured it)
  3. market blend (placeholder, Step 9)
  4. extreme check: below 5% / above 95% only if at least 80% of the
     forecasts that ran are beyond 10% / 90% on the same side; otherwise
     pulled back to 10% / 90%
  5. clip to 2%-98%
Multiple choice: every option at least 1%, summing to 1.
Numeric/discrete: the CDF must follow the platform rules; percentiles given in
reverse are put right; if every percentile is outside the question's range
(e.g. a x1000 unit error) the answer is re-parsed once, then that forecast is
dropped.
Never a pure guess (architect, 27 Sep 2026): an invalid forecast is dropped;
a question with no real model forecast is left for the next run (a guess
scores below zero on average, skipping scores 0).
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from typing import Any

from forecasting_tools import (
    NumericDistribution,
    Percentile,
    PredictedOption,
    PredictedOptionList,
)
from forecasting_tools.data_models.numeric_report import NumericDefaults

# ---------------------------------------------------------------- binary

STRETCH_K = 1.0  # log-odds stretch; 1.0 = off (architect, 27 Sep 2026)
EXTREME_LOW, EXTREME_HIGH = 0.05, 0.95
EXTREME_PULLBACK_LOW, EXTREME_PULLBACK_HIGH = 0.10, 0.90
EXTREME_AGREEMENT = 0.8
BINARY_MIN, BINARY_MAX = 0.02, 0.98


class NoValidForecast(ValueError):
    """Nothing real to submit: the question is left for the next run."""


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _logistic(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def stretch(p: float, k: float = STRETCH_K) -> float:
    """Push p away from 50% in log-odds (k > 1) or leave it (k = 1)."""
    return p if k == 1.0 else _logistic(k * _logit(p))


def extreme_check(p: float, forecasts: list[float]) -> float:
    if p < EXTREME_LOW:
        agreeing = sum(f < EXTREME_PULLBACK_LOW for f in forecasts)
        return p if agreeing >= EXTREME_AGREEMENT * len(forecasts) else EXTREME_PULLBACK_LOW
    if p > EXTREME_HIGH:
        agreeing = sum(f > EXTREME_PULLBACK_HIGH for f in forecasts)
        return p if agreeing >= EXTREME_AGREEMENT * len(forecasts) else EXTREME_PULLBACK_HIGH
    return p


def clip_binary(p: float) -> float:
    return min(BINARY_MAX, max(BINARY_MIN, p))


def is_valid_probability(p: Any) -> bool:
    return isinstance(p, (int, float)) and not math.isnan(p) and 0 <= p <= 1


def adjust_binary(forecasts: list[float], k: float = STRETCH_K) -> float:
    """The 5 steps, in order. Invalid forecasts are ignored; none -> NoValidForecast."""
    valid = [float(f) for f in forecasts if is_valid_probability(f)]
    if not valid:
        raise NoValidForecast("no valid binary forecast")
    p = statistics.median(valid)
    p = stretch(p, k)
    # Step 3, market blend: added in PLAN.md Step 9.
    p = extreme_check(p, valid)
    return clip_binary(p)


# ---------------------------------------------------------------- multiple choice

MULTIPLE_CHOICE_MIN = 0.01


def floor_multiple_choice(options: PredictedOptionList) -> PredictedOptionList:
    """
    Every option at least 1%, summing to 1: each option keeps the floor plus
    its share of what's left (order kept). Unchanged if already fine.
    """
    predicted = options.predicted_options
    total = sum(o.probability for o in predicted)
    count = len(predicted)
    if count == 0 or MULTIPLE_CHOICE_MIN * count >= 1:
        return options
    if min(o.probability for o in predicted) >= MULTIPLE_CHOICE_MIN and abs(total - 1) < 1e-9:
        return options
    remaining = 1 - MULTIPLE_CHOICE_MIN * count
    return PredictedOptionList(
        predicted_options=[
            PredictedOption(
                option_name=o.option_name,
                probability=MULTIPLE_CHOICE_MIN
                + remaining * (o.probability / total if total > 0 else 1 / count),
            )
            for o in predicted
        ]
    )


# ---------------------------------------------------------------- numeric / discrete

MIN_CDF_STEP = 5e-05
MAX_CDF_STEP = 0.2
OPEN_BOUND_MIN = 0.001
_TOLERANCE = 1e-6


def cdf_problems(
    cdf: list[float],
    cdf_size: int,
    open_lower_bound: bool,
    open_upper_bound: bool,
    max_step: float = MAX_CDF_STEP,
) -> list[str]:
    """The platform's CDF rules. Returns what's wrong (empty = valid)."""
    problems = []
    if len(cdf) != cdf_size:
        problems.append(f"CDF has {len(cdf)} points, expected {cdf_size}")
        return problems
    if any(not (0 - _TOLERANCE <= v <= 1 + _TOLERANCE) or math.isnan(v) for v in cdf):
        problems.append("CDF values must be between 0 and 1")
    steps = [b - a for a, b in zip(cdf, cdf[1:])]
    if any(step < MIN_CDF_STEP - _TOLERANCE for step in steps):
        problems.append(f"CDF must increase by at least {MIN_CDF_STEP} at every step")
    if any(step > max_step + _TOLERANCE for step in steps):
        problems.append(f"CDF steps must be at most {max_step}")
    if open_lower_bound and cdf[0] < OPEN_BOUND_MIN - _TOLERANCE:
        problems.append("Open lower bound: CDF must start at 0.001 or more")
    if not open_lower_bound and abs(cdf[0]) > _TOLERANCE:
        problems.append("Closed lower bound: CDF must start at 0")
    if open_upper_bound and cdf[-1] > 1 - OPEN_BOUND_MIN + _TOLERANCE:
        problems.append("Open upper bound: CDF must end at 0.999 or less")
    if not open_upper_bound and abs(cdf[-1] - 1) > _TOLERANCE:
        problems.append("Closed upper bound: CDF must end at 1")
    return problems


def distribution_problems(distribution: NumericDistribution, question: Any) -> list[str]:
    try:
        cdf = [p.percentile for p in distribution.get_cdf()]
    except Exception as e:
        return [f"CDF could not be built ({type(e).__name__})"]
    size = question.cdf_size or len(cdf)
    # The platform allows bigger steps on discrete questions (fewer points):
    # 0.2 x 200 / (points - 1).
    max_step = NumericDefaults.get_max_pmf_value(size, include_wiggle_room=False)
    return cdf_problems(cdf, size, question.open_lower_bound, question.open_upper_bound, max_step)


def fix_reversed_percentiles(percentiles: list[Percentile]) -> list[Percentile]:
    """If the values go down as the percentiles go up, put them the right way round."""
    ordered = sorted(percentiles, key=lambda p: p.percentile)
    values = [p.value for p in ordered]
    if len(values) >= 2 and all(a > b for a, b in zip(values, values[1:])):
        return [
            Percentile(percentile=p.percentile, value=v)
            for p, v in zip(ordered, reversed(values))
        ]
    return ordered


def all_outside_range(percentiles: list[Percentile], lower: float, upper: float) -> bool:
    """True if every value is outside the question's range (likely a unit error)."""
    return bool(percentiles) and all(p.value < lower or p.value > upper for p in percentiles)


def question_range(question: Any) -> tuple[float, float]:
    lower = question.nominal_lower_bound if question.nominal_lower_bound is not None else question.lower_bound
    upper = question.nominal_upper_bound if question.nominal_upper_bound is not None else question.upper_bound
    return float(lower), float(upper)


# ---------------------------------------------------------------- before submitting


def still_open_problem(question: Any, now: datetime | None = None) -> str | None:
    """Why a forecast should NOT be submitted now (None = fine to submit)."""
    now = now or datetime.now(timezone.utc)
    state = getattr(question, "state", None)
    state_name = getattr(state, "value", state)
    if state_name is not None and state_name != "open":
        return f"question is {state_name}"
    if getattr(question, "actual_resolution_time", None) is not None:
        return "question has already resolved"
    if getattr(question, "resolution_string", None):
        return "question has already resolved"
    close = getattr(question, "close_time", None)
    if close is not None:
        close = close if close.tzinfo else close.replace(tzinfo=timezone.utc)
        if close <= now:
            return "question has closed"
    return None


def dates_line(question: Any, now: datetime | None = None) -> str:
    """Today's date and the question's close and resolve dates, for every prompt."""
    now = now or datetime.now(timezone.utc)

    def day(moment: datetime | None) -> str:
        return moment.strftime("%Y-%m-%d") if moment else "not stated"

    return (
        f"Today is {now:%Y-%m-%d}. The question closes on "
        f"{day(getattr(question, 'close_time', None))} and is scheduled to resolve on "
        f"{day(getattr(question, 'scheduled_resolution_time', None))}."
    )
