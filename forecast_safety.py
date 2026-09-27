"""
Interim safety limits on final forecasts (until PLAN.md Step 4 replaces them):
- binary: kept between 3% and 97%
- multiple choice: every option gets at least 1%, and the options sum to 1
"""
from __future__ import annotations

from forecasting_tools import PredictedOption, PredictedOptionList

BINARY_MIN = 0.03
BINARY_MAX = 0.97
MULTIPLE_CHOICE_MIN = 0.01


def clip_binary(probability: float) -> float:
    return min(BINARY_MAX, max(BINARY_MIN, probability))


def floor_multiple_choice(options: PredictedOptionList) -> PredictedOptionList:
    """
    Give every option at least MULTIPLE_CHOICE_MIN: each option keeps the floor
    plus its share of what's left, so the total is 1 and the order is kept.
    """
    predicted = options.predicted_options
    total = sum(o.probability for o in predicted)
    count = len(predicted)
    if count == 0 or MULTIPLE_CHOICE_MIN * count >= 1:
        return options
    if min(o.probability for o in predicted) >= MULTIPLE_CHOICE_MIN:
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
