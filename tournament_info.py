"""
Read-only facts about our target tournaments, for the "Tournament info"
workflow: each tournament's id, slug and dates, and the open/close times of
its currently open questions. Forecasts nothing, spends nothing.
--audit: every question of each tournament (any state), its open/close
times, and whether our bot forecast it: to rule out a silent miss. Plus a RAW
count (architect, 4 Oct) that doesn't use the library: /api/posts/ called
directly, no status or type filter, every page, every post type (a type the
library can't read would show up here and not in the audit). And newer
MiniBench rounds (the next tournament ids). Ids, types, states and times
only, never forecasts.

    poetry run python tournament_info.py [--audit]
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

import requests

from bot_helpers import silence_noisy_dependencies

silence_noisy_dependencies()

from forecasting_tools import ApiFilter, MetaculusClient  # noqa: E402

from main import FALL_2026_TOURNAMENT_ID  # noqa: E402
from minibench import current_minibench  # noqa: E402

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


def audit_lines(slug_or_id: str | int) -> list[str]:
    """Every question (open, closed, resolved, upcoming), and whether we forecast it."""
    api_filter = ApiFilter(
        allowed_tournaments=[slug_or_id],
        allowed_statuses=["upcoming", "open", "closed", "resolved"],
        allowed_types=["binary", "numeric", "discrete", "multiple_choice"],
        group_question_mode="unpack_subquestions",
    )
    questions = asyncio.run(MetaculusClient().get_questions_matching_filter(api_filter))
    now = datetime.now(timezone.utc)
    missed = [
        q for q in questions
        if not q.already_forecasted and q.close_time is not None and q.close_time <= now
        and q.open_time is not None and q.open_time <= now
    ]
    lines = [
        f"- Questions in any state: {len(questions)}; forecast by us: "
        f"{sum(bool(q.already_forecasted) for q in questions)}; closed without our forecast: {len(missed)}",
        "",
        "| Question | Type | State | Opens (UTC) | Closes (UTC) | Forecast by us |",
        "|---|---|---|---|---|---|",
    ]
    def when(t):  # type: ignore[no-untyped-def]
        return f"{t:%Y-%m-%d %H:%M}" if t else "?"

    for q in sorted(questions, key=lambda q: (q.open_time or now, q.id_of_post)):
        state = getattr(q.state, "value", q.state)
        lines.append(
            f"| [{q.id_of_post}]({q.page_url}) | {q.question_type} | {state} | {when(q.open_time)} "
            f"| {when(q.close_time)} | {'yes' if q.already_forecasted else '**no**'} |"
        )
    return lines


def _auth() -> dict:
    return {"Authorization": f"Token {os.environ['METACULUS_TOKEN']}"}


def raw_posts(tournament: str | int, get=requests.get) -> list[dict]:  # type: ignore[no-untyped-def]
    """Every post of a tournament, straight from /api/posts/ (no status or
    type filter, all pages), reduced to id / type / state / times / whether we
    forecast it. Independent of the library's parsing and filters."""
    rows, offset = [], 0
    while True:
        # with_cp only adds data (our forecasts, the community prediction); it filters nothing.
        response = get(f"{API}/posts/", params={"tournaments": tournament, "limit": 100, "offset": offset, "with_cp": "true"},
                       headers=_auth(), timeout=30)
        response.raise_for_status()
        data = response.json()
        results = data.get("results", [])
        for post in results:
            rows.append(raw_row(post))
        if not data.get("next") or not results:
            return rows
        offset += len(results)


def raw_row(post: dict) -> dict:
    """One post: its kind (question type, group, conditional, notebook, ...)."""
    question = post.get("question") or {}
    if question:
        kind = question.get("type") or "question (no type)"
    elif post.get("group_of_questions") is not None:
        kind = "group_of_questions"
    elif post.get("conditional") is not None:
        kind = "conditional"
    elif post.get("notebook") is not None:
        kind = "notebook"
    else:
        kind = "unknown"
    mine = question.get("my_forecasts")
    return {
        "id": post.get("id"),
        "kind": kind,
        "state": post.get("status") or question.get("status"),
        "opens": question.get("open_time") or post.get("open_time"),
        "closes": question.get("scheduled_close_time") or post.get("scheduled_close_time"),
        # None: the API didn't say (no my_forecasts field), not "no".
        "ours": bool(mine.get("latest")) if isinstance(mine, dict) else None,
    }


def raw_lines(tournament: str | int) -> list[str]:
    rows = sorted(raw_posts(tournament), key=lambda r: (r["opens"] or "", r["id"] or 0))
    kinds: dict[str, int] = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    lines = [
        f"- RAW /api/posts/ (no status or type filter, all pages): {len(rows)} post(s); by kind: "
        + (", ".join(f"{k} {v}" for k, v in sorted(kinds.items())) or "none"),
        "",
        "| Post | Kind | State | Opens (UTC) | Closes (UTC) | Forecast by us |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        ours = "?" if r["ours"] is None else ("yes" if r["ours"] else "**no**")
        lines.append(f"| {r['id']} | {r['kind']} | {r['state']} | {(r['opens'] or '?')[:16]} | {(r['closes'] or '?')[:16]} | {ours} |")
    return lines


def newer_minibench_lines(current: str | int, ahead: int = 15) -> list[str]:
    """Tournament ids after the current MiniBench round that look like MiniBench."""
    try:
        start = int(current)
    except (TypeError, ValueError):
        return [f"- Newer rounds: current round id not numeric ({current}); not checked"]
    found = []
    for tid in range(start + 1, start + 1 + ahead):
        response = requests.get(f"{API}/projects/tournaments/{tid}/", headers=_auth(), timeout=30)
        if response.status_code != 200:
            continue
        data = response.json()
        if "minibench" in f"{data.get('slug', '')} {data.get('name', '')}".lower():
            found.append(f"  - {tid} `{data.get('slug')}`: start {data.get('start_date')}, close {data.get('close_date')}")
    return [f"- Newer MiniBench rounds (ids {start + 1}-{start + ahead}): {len(found) or 'none'}", *found]


def main(audit: bool = False) -> None:
    sections: list[str] = []
    minibench_id, how = current_minibench()
    sections += [f"**Current MiniBench round: {minibench_id}** (found by {how})", ""]
    for label, slug_or_id in (
        ("Seasonal tournament", FALL_2026_TOURNAMENT_ID),
        ("MiniBench", minibench_id),
    ):
        sections += [f"## {label} (`{slug_or_id}`)", ""]
        sections += tournament_facts(slug_or_id)
        sections += audit_lines(slug_or_id) if audit else open_question_lines(slug_or_id)
        if audit:
            sections.append("")
            sections += raw_lines(slug_or_id)
        sections.append("")
    if audit:
        sections += ["## MiniBench rounds", ""] + newer_minibench_lines(minibench_id) + [""]
    report = "\n".join(sections)
    print(report)
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(report + "\n")


if __name__ == "__main__":
    if not os.getenv("METACULUS_TOKEN"):
        sys.exit("METACULUS_TOKEN is not set")
    main(audit="--audit" in sys.argv)
