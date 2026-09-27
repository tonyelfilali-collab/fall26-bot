"""
Read-only facts about our target tournaments, for the "Tournament info"
workflow: each tournament's id, slug and dates, and the open/close times of
its currently open questions. Forecasts nothing, spends nothing.

    poetry run python tournament_info.py
"""
from __future__ import annotations

import os
import sys

import requests

from bot_helpers import silence_noisy_dependencies

silence_noisy_dependencies()

from forecasting_tools import MetaculusClient  # noqa: E402

from main import FALL_2026_MINIBENCH_ID, FALL_2026_TOURNAMENT_ID  # noqa: E402

API = "https://www.metaculus.com/api"
MAX_QUESTIONS_SHOWN = 10


def tournament_facts(slug_or_id: str | int) -> list[str]:
    response = requests.get(
        f"{API}/projects/tournaments/{slug_or_id}/",
        headers={"Authorization": f"Token {os.environ['METACULUS_TOKEN']}"},
        timeout=30,
    )
    if response.status_code != 200:
        return [f"- Tournament lookup: HTTP {response.status_code}"]
    data = response.json()
    return [
        f"- Name: {data.get('name')}",
        f"- Id: {data.get('id')}, slug: `{data.get('slug')}`",
        f"- Start: {data.get('start_date')}, close: {data.get('close_date')}",
    ]


def open_question_lines(slug_or_id: str | int) -> list[str]:
    questions = MetaculusClient().get_all_open_questions_from_tournament(slug_or_id)
    lines = [
        f"- Open questions right now: {len(questions)}",
        "",
        "| Question | Type | Opens (UTC) | Closes (UTC) | Open for (hours) |",
        "|---|---|---|---|---|",
    ]
    for q in sorted(questions, key=lambda q: q.close_time or q.open_time)[
        :MAX_QUESTIONS_SHOWN
    ]:
        hours = (
            f"{(q.close_time - q.open_time).total_seconds() / 3600:.1f}"
            if q.open_time and q.close_time
            else "?"
        )
        lines.append(
            f"| [{q.id_of_post}]({q.page_url}) | {q.question_type} "
            f"| {q.open_time:%Y-%m-%d %H:%M} | {q.close_time:%Y-%m-%d %H:%M} | {hours} |"
            if q.open_time and q.close_time
            else f"| [{q.id_of_post}]({q.page_url}) | {q.question_type} | ? | ? | ? |"
        )
    return lines


def main() -> None:
    sections: list[str] = []
    for label, slug_or_id in (
        ("Seasonal tournament", FALL_2026_TOURNAMENT_ID),
        ("MiniBench", FALL_2026_MINIBENCH_ID),
    ):
        sections += [f"## {label} (`{slug_or_id}`)", ""]
        sections += tournament_facts(slug_or_id)
        sections += open_question_lines(slug_or_id)
        sections.append("")
    report = "\n".join(sections)
    print(report)
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(report + "\n")


if __name__ == "__main__":
    if not os.getenv("METACULUS_TOKEN"):
        sys.exit("METACULUS_TOKEN is not set")
    main()
