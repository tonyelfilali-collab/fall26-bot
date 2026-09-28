"""
Reading a forecaster's final answer without a model call.

The prompts ask for a fixed last-lines format ("Probability: ZZ%", one line per
option, "Percentile 2.5: XX" ...). When the answer follows it exactly, these
functions read it directly; otherwise they return None and the bot falls back
to the parser model (structure_output), as before. This saves a parser call
per forecast on the scarce free quota. Worked examples: tests/test_answer_parsing.py.
"""
from __future__ import annotations

import re

from forecasting_tools import Percentile, PredictedOption, PredictedOptionList

from forecast_safety import floor_probabilities

_NUMBER = r"[-+]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?"


def _to_float(text: str) -> float:
    return float(text.replace(",", ""))


def parse_binary_answer(text: str) -> float | None:
    """The last 'Probability: ZZ%' line, as a decimal (0.73), or None."""
    matches = re.findall(r"Probability:\s*\**\s*(\d+(?:\.\d+)?)\s*%", text, re.IGNORECASE)
    if not matches:
        return None
    value = float(matches[-1]) / 100
    return value if 0 <= value <= 1 else None


def parse_multiple_choice_answer(text: str, options: list[str]) -> PredictedOptionList | None:
    """
    One 'Option: probability' line per option (the last one wins). All options
    must be found; percentages or decimals; the sum must be close to 1 (or
    100%). Otherwise None.
    """
    values = read_multiple_choice_values(text, options)
    if values is None:
        return None
    # Every option at least 1% here, on plain numbers: the library's own
    # clamp rejects a confident answer (100% / 0%s) on a 10+ option question.
    return PredictedOptionList(
        predicted_options=[
            PredictedOption(option_name=o, probability=p)
            for o, p in zip(options, floor_probabilities(values))
        ]
    )


def read_multiple_choice_values(text: str, options: list[str]) -> list[float] | None:
    """The answer's probabilities as written, summing to 1, before any floor
    (parse_multiple_choice_answer's rules). None if they can't be read."""
    values: list[float] = []
    for option in options:
        pattern = rf"^\W*(?:Option[_ ]?)?{re.escape(option)}\W*[:=-]\s*({_NUMBER})\s*(%?)\s*$"
        found = re.findall(pattern, text, re.IGNORECASE | re.MULTILINE)
        if not found:
            return None
        number, percent = found[-1]
        values.append(_to_float(number) / (100 if percent else 1))
    total = sum(values)
    if total > 1.5:  # written as percentages without the % sign
        values = [v / 100 for v in values]
        total = sum(values)
    if not 0.9 <= total <= 1.1 or any(v < 0 for v in values):
        return None
    return [v / total for v in values]


def parse_percentile_answer(text: str, expected: tuple[float, ...], unit: str | None = None) -> list[Percentile] | None:
    """
    'Percentile P: value' lines for every expected percentile (the last one
    of each wins). A value may only be followed by the question's unit;
    anything else (e.g. "1.2 million") returns None so the parser model can
    handle the units.
    """
    unit_pattern = rf"(?:\s*{re.escape(unit)})?" if unit else ""
    pattern = rf"^\W*Percentile\s*(\d+(?:\.\d+)?)\s*:\s*\$?\s*({_NUMBER}){unit_pattern}\s*(?:\([^)]*\))?\s*$"
    found: dict[float, float] = {}
    for p, value in re.findall(pattern, text, re.IGNORECASE | re.MULTILINE):
        found[round(float(p) / 100, 4)] = _to_float(value)
    if any(round(p, 4) not in found for p in expected):
        return None
    return [Percentile(percentile=p, value=found[round(p, 4)]) for p in expected]
