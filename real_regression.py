"""
Real-model regression run (Test Bot option "real-regression"; test-only,
never submitted): the free test model answers the 37 regression-pack questions
(tests/fixtures/past_questions.json, closed past-tournament questions) with
research frozen (0 AskNews calls), and the table shows, per question type, how
the replies were read: directly (no model call), by the parser model, or
dropped. About 1 request per question plus one per parser use.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from forecasting_tools import BinaryQuestion, DiscreteQuestion, MultipleChoiceQuestion, NumericQuestion

FIXTURES = Path(__file__).parent / "tests" / "fixtures" / "past_questions.json"
TYPES = ("binary", "numeric", "discrete", "multiple_choice")
_CLASSES = {
    "binary": BinaryQuestion,
    "numeric": NumericQuestion,
    "discrete": DiscreteQuestion,
    "multiple_choice": MultipleChoiceQuestion,
}


def build_question(shape: dict) -> Any:
    fields = {k: v for k, v in shape.items() if k != "question_type"}
    return _CLASSES[shape["question_type"]](**fields)


def load_questions(path: Path = FIXTURES) -> list[Any]:
    return [build_question(shape) for shape in json.loads(path.read_text())]


def reading_table(records: list[dict], questions: list[Any]) -> str:
    """Per type: questions, replies read directly / needed the parser / dropped,
    and questions left with no forecast. Counts only (public logs)."""
    asked = Counter(q.question_type for q in questions)
    reading: dict[str, Counter] = {t: Counter() for t in TYPES}
    forecast: Counter = Counter()
    for record in records:
        kind = (record.get("question") or {}).get("question_type")
        if kind not in reading:
            continue
        reading[kind].update(record.get("reading") or [])
        if record.get("final_forecast") is not None:
            forecast[kind] += 1
    lines = [
        "| Type | Questions | Read directly | Needed the parser | Dropped | No forecast |",
        "|---|---|---|---|---|---|",
    ]
    for kind in TYPES:
        counts = reading[kind]
        lines.append(
            f"| {kind} | {asked[kind]} | {counts['direct']} | {counts['parser']} | {counts['dropped']} "
            f"| {asked[kind] - forecast[kind]} |"
        )
    total = Counter()
    for counts in reading.values():
        total.update(counts)
    lines.append(
        f"| all | {sum(asked.values())} | {total['direct']} | {total['parser']} | {total['dropped']} "
        f"| {sum(asked.values()) - sum(forecast.values())} |"
    )
    return "\n".join(lines)
