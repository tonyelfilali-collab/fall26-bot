"""
PLAN.md Step 8: numeric and multiple-choice handling.

Numeric / discrete:
- each model gives percentiles 2.5, 5, 10, 25, 50, 75, 90, 95, 97.5
- each model's CDF is built with PCHIP interpolation (smooth, never goes down)
  in the question's own x-axis (so log-scaled questions work)
- the models' CDFs are combined with a pointwise median (the library's
  aggregation), then mixed 95% with 5% uniform over the question range
- the result must pass the Step 4 checks (forecast_safety.distribution_problems)

Multiple choice: median per option across models, renormalise, every option
at least 1%, renormalise.
"""
from __future__ import annotations

import statistics
from typing import Any

import numpy as np
from forecasting_tools import (
    NumericDistribution,
    Percentile,
    PredictedOption,
    PredictedOptionList,
)

from forecast_safety import floor_probabilities

STEP8_PERCENTILES = (0.025, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.975)
UNIFORM_WEIGHT = 0.05


# ---------------------------------------------------------------- PCHIP


def pchip_slopes(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Fritsch-Carlson slopes: the curve keeps the data's up/down shape (no overshoot)."""
    n = len(x)
    h = np.diff(x)
    delta = np.diff(y) / h
    slopes = np.zeros(n)
    if n == 2:
        slopes[:] = delta[0]
        return slopes
    for k in range(1, n - 1):
        if delta[k - 1] * delta[k] <= 0:
            slopes[k] = 0.0
        else:
            w1 = 2 * h[k] + h[k - 1]
            w2 = h[k] + 2 * h[k - 1]
            slopes[k] = (w1 + w2) / (w1 / delta[k - 1] + w2 / delta[k])

    def end_slope(h0: float, h1: float, d0: float, d1: float) -> float:
        slope = ((2 * h0 + h1) * d0 - h0 * d1) / (h0 + h1)
        if np.sign(slope) != np.sign(d0):
            return 0.0
        if np.sign(d0) != np.sign(d1) and abs(slope) > abs(3 * d0):
            return 3 * d0
        return slope

    slopes[0] = end_slope(h[0], h[1], delta[0], delta[1])
    slopes[-1] = end_slope(h[-1], h[-2], delta[-1], delta[-2])
    return slopes


def pchip_eval(x: np.ndarray, y: np.ndarray, xq: np.ndarray) -> np.ndarray:
    """Evaluate the PCHIP curve through (x, y) at xq (held flat outside x)."""
    slopes = pchip_slopes(x, y)
    xq = np.clip(xq, x[0], x[-1])
    idx = np.clip(np.searchsorted(x, xq, side="right") - 1, 0, len(x) - 2)
    h = x[idx + 1] - x[idx]
    t = (xq - x[idx]) / h
    h00 = 2 * t**3 - 3 * t**2 + 1
    h10 = t**3 - 2 * t**2 + t
    h01 = -2 * t**3 + 3 * t**2
    h11 = t**3 - t**2
    return h00 * y[idx] + h10 * h * slopes[idx] + h01 * y[idx + 1] + h11 * h * slopes[idx + 1]


# ---------------------------------------------------------------- numeric


def _template(question: Any) -> NumericDistribution:
    """A throwaway distribution for the question, to reuse the library's
    x-axis scaling (log-scaled questions) and CDF standardising."""
    lower, upper = float(question.lower_bound), float(question.upper_bound)
    span = upper - lower
    return NumericDistribution.from_question(
        [
            Percentile(percentile=0.25, value=lower + 0.25 * span),
            Percentile(percentile=0.75, value=lower + 0.75 * span),
        ],
        question,
    )


def _grid(question: Any) -> np.ndarray:
    size = question.cdf_size or 201
    return np.array([i / (size - 1) for i in range(size)])


def _to_distribution(cdf: np.ndarray, question: Any, template: NumericDistribution) -> NumericDistribution:
    standardized = template._standardize_cdf(np.clip(cdf, 0.0, 1.0))
    values = [template._cdf_location_to_nominal_location(loc) for loc in _grid(question)]
    return NumericDistribution.from_question(
        [Percentile(value=v, percentile=float(p)) for v, p in zip(values, standardized)],
        question,
    )


def pchip_cdf(percentiles: list[Percentile], question: Any) -> np.ndarray:
    """
    The model's CDF on the question's grid (before standardising): PCHIP
    through its percentiles, in the question's own x-axis (cdf locations).
    Closed bounds pin the CDF to 0 / 1; open bounds extend the tails in line.
    """
    template = _template(question)
    points = sorted(
        (template._nominal_location_to_cdf_location(p.value), p.percentile)
        for p in percentiles
    )
    # Drop repeated locations (keep the first).
    locations, heights = [], []
    for location, height in points:
        if locations and location <= locations[-1] + 1e-12:
            continue
        locations.append(location)
        heights.append(height)
    if len(locations) < 2:
        raise ValueError("need at least 2 distinct percentile values")
    if locations[0] > 0:
        if question.open_lower_bound:
            slope = (heights[1] - heights[0]) / (locations[1] - locations[0])
            low = min(heights[0], max(0.0, heights[0] - slope * locations[0]))
        else:
            low = 0.0
        locations.insert(0, 0.0)
        heights.insert(0, low)
    if locations[-1] < 1:
        if question.open_upper_bound:
            slope = (heights[-1] - heights[-2]) / (locations[-1] - locations[-2])
            high = max(heights[-1], min(1.0, heights[-1] + slope * (1 - locations[-1])))
        else:
            high = 1.0
        locations.append(1.0)
        heights.append(high)
    cdf = pchip_eval(np.array(locations), np.array(heights), _grid(question))
    return np.maximum.accumulate(np.clip(cdf, 0.0, 1.0))


def pchip_distribution(percentiles: list[Percentile], question: Any) -> NumericDistribution:
    """One model's forecast as a full, platform-ready distribution."""
    template = _template(question)
    return _to_distribution(pchip_cdf(percentiles, question), question, template)


def _declared_cdf(distribution: NumericDistribution, question: Any) -> np.ndarray:
    """The CDF the model's distribution declares on the question's grid."""
    size = question.cdf_size or 201
    if len(distribution.declared_percentiles) == size:
        return np.array([p.percentile for p in distribution.declared_percentiles])
    return np.array([p.percentile for p in distribution.get_cdf()])


def combine_numeric(
    distributions: list[NumericDistribution],
    question: Any,
    weight: float = UNIFORM_WEIGHT,
    center: str = "median",
) -> NumericDistribution:
    """
    Pointwise median of the models' CDFs, then (1 - weight) x that + weight x
    uniform over the question's range (uniform in the question's own x-axis):
        final(x) = 0.95 x median_k F_k(x) + 0.05 x location(x)
    Metaculus' own standardising (0.99 F + 0.01 location for closed bounds) is
    applied once more by the library when the forecast is submitted.
    """
    cdfs = np.array([_declared_cdf(d, question) for d in distributions])
    # "mean" is only used by a shadow variant (shadow.py); live is the median.
    median = np.mean(cdfs, axis=0) if center == "mean" else np.median(cdfs, axis=0)
    mixed = (1 - weight) * median + weight * _grid(question)
    template = _template(question)
    values = [template._cdf_location_to_nominal_location(loc) for loc in _grid(question)]
    try:
        return NumericDistribution.from_question(
            [Percentile(value=v, percentile=float(p)) for v, p in zip(values, mixed)],
            question,
        )
    except ValueError:
        # E.g. an open bound's tail just under the minimum: let the library
        # standardise it.
        return _to_distribution(mixed, question, template)


def mix_with_uniform(
    distribution: NumericDistribution, question: Any, weight: float = UNIFORM_WEIGHT
) -> NumericDistribution:
    """combine_numeric for a single distribution."""
    return combine_numeric([distribution], question, weight)


# ---------------------------------------------------------------- multiple choice


def median_multiple_choice(predictions: list[PredictedOptionList]) -> PredictedOptionList:
    """Median per option, renormalise, every option at least 1%, renormalise."""
    names = [o.option_name for o in predictions[0].predicted_options]
    medians = []
    for name in names:
        values = [
            o.probability
            for prediction in predictions
            for o in prediction.predicted_options
            if o.option_name == name
        ]
        medians.append(statistics.median(values) if values else 0.0)
    # Floor on plain numbers, then build the list once.
    floored = floor_probabilities(medians)
    return PredictedOptionList(
        predicted_options=[
            PredictedOption(option_name=name, probability=p) for name, p in zip(names, floored)
        ]
    )
