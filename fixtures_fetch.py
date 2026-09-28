"""
One-off fetch for the regression pack (Tier A): about 40 CLOSED questions from
past bot tournaments, saved as shapes only (type, bounds, scaling, options).
No community predictions, no resolutions, no forecasts. Runs in evaluate.yml
with the read-only token; the file comes back as a workflow artifact and is
committed as tests/fixtures/past_questions.json.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections import Counter

OUT = "past_questions.json"
TOURNAMENTS = [32813, 32916, "minibench"]  # Fall 2025, Spring 2026, MiniBench
# Fields a question shape needs; everything else (texts, CP, resolution) is dropped.
KEEP = {
    "id_of_post", "id_of_question", "question_text", "unit_of_measure", "options",
    "upper_bound", "lower_bound", "open_upper_bound", "open_lower_bound",
    "zero_point", "cdf_size", "nominal_upper_bound", "nominal_lower_bound",
}


def kind(question) -> str:
    t = question.question_type
    if t == "multiple_choice":
        n = len(question.options)
        return "mc_2" if n == 2 else "mc_3_5" if n <= 5 else "mc_6_9" if n <= 9 else "mc_10plus"
    if t == "numeric":
        if question.zero_point is not None:
            return "numeric_log"
        return "numeric_open" if (question.open_lower_bound or question.open_upper_bound) else "numeric_closed"
    return t  # binary, discrete


WANT = {"binary": 6, "numeric_open": 6, "numeric_closed": 5, "numeric_log": 5, "discrete": 6,
        "mc_2": 3, "mc_3_5": 4, "mc_6_9": 3, "mc_10plus": 3}


def shape(question) -> dict:
    data = question.to_json()
    return {"question_type": question.question_type, **{k: v for k, v in data.items() if k in KEEP}}


def main() -> None:
    from bot_helpers import silence_noisy_dependencies

    silence_noisy_dependencies()
    from forecasting_tools import ApiFilter, MetaculusClient

    client = MetaculusClient(token=os.environ["METACULUS_READ_TOKEN"])
    picked: dict[str, list[dict]] = {k: [] for k in WANT}
    for tournament in TOURNAMENTS:
        found = asyncio.run(
            client.get_questions_matching_filter(
                ApiFilter(allowed_statuses=["resolved", "closed"], allowed_tournaments=[tournament],
                          allowed_types=["binary", "numeric", "discrete", "multiple_choice"]),
                num_questions=400, randomly_sample=False, error_if_question_target_missed=False,
            )
        )
        print(f"tournament {tournament}: {len(found)} closed questions")
        for question in found:
            k = kind(question)
            if len(picked[k]) < WANT[k]:
                picked[k].append(shape(question))
    rows = [q for k in WANT for q in picked[k]]
    print("picked:", dict(Counter({k: len(v) for k, v in picked.items()})), "total", len(rows))
    with open(OUT, "w") as f:
        json.dump(rows, f, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
