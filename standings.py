"""
Our weekly standing, for the daily health report on Mondays (UTC; Tier A:
reads only, never on the forecast path). For the Fall tournament (project
33121) and the current MiniBench round: rank, score, 95% range (when the
leaderboard gives one), the prize the leaderboard shows us, the scores at
rank 1, 10, 50 and the last paid rank, the number of resolved questions
(architect, 28 Oct) and the change since the last stored Monday.

The values are public on Metaculus (leaderboards/project/<id>/). Each
Monday's values are kept in fall26-data status/standings.json, keyed by UTC
date, so next Monday can show the change. Fail soft: if nothing can be read
the report gets one line "standings: unavailable" and is never made red.
Ranks and scores only, never forecasts.
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any, Callable

import requests

API = "https://www.metaculus.com/api"
FALL_PROJECT_ID = 33121  # main.FALL_2026_TOURNAMENT_ID = "fall-futureeval-2026"
OUR_BOT = {"id": 309271, "username": "Quillfeather-Vex"}
STANDINGS_PATH = "status/standings.json"
UNAVAILABLE = "standings: unavailable"

# get(path) -> parsed JSON, or None if the call failed.
Get = Callable[[str], Any]


def metaculus_get(path: str) -> Any:
    try:
        response = requests.get(
            f"{API}/{path}", headers={"Authorization": f"Token {os.environ['METACULUS_TOKEN']}"}, timeout=60
        )
    except Exception:
        return None
    return response.json() if response.status_code == 200 else None


def is_monday(day: date) -> bool:
    return day.weekday() == 0


def project_id(tid: int | str, get: Get) -> int | None:
    """A tournament id, or a slug looked up as its number."""
    if str(tid).isdigit():
        return int(tid)
    found = get(f"projects/tournaments/{tid}/")
    number = found.get("id") if isinstance(found, dict) else None
    return int(number) if str(number).isdigit() else None


REFERENCE_RANKS = (1, 10, 50)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def our_board(board: Any) -> tuple[dict | None, dict | None]:
    """(the leaderboard we are on, our entry), or (None, None)."""
    from tournament_info import leaderboards, standing_row

    if board is None:
        return None, None
    for _, one in leaderboards(board):
        if isinstance(one, dict):
            entry = standing_row(one, OUR_BOT)
            if entry is not None:
                return one, entry
    return None, None


def our_entry(board: Any) -> dict | None:
    """{"rank", "score", "low", "high", "prize"} from a leaderboard answer, or None."""
    from tournament_info import RANGE_FIELDS

    _, entry = our_board(board)
    if entry is None:
        return None
    low, high = next(((entry.get(a), entry.get(b)) for a, b in RANGE_FIELDS if entry.get(a) is not None),
                     (None, None))
    return {"rank": entry.get("rank"), "score": entry.get("score"), "low": low, "high": high,
            "prize": entry.get("prize") if _is_number(entry.get("prize")) else None}


def ranked(board: dict) -> list[dict]:
    """The leaderboard's entries with a numeric rank, best first."""
    return sorted((e for e in board.get("entries") or [] if isinstance(e, dict) and _is_number(e.get("rank"))),
                  key=lambda e: e["rank"])


def score_at(entries: list[dict], rank: int) -> Any:
    return next((e.get("score") for e in entries if e["rank"] >= rank), None)


def last_paid(entries: list[dict]) -> dict | None:
    """The lowest-ranked entry the leaderboard shows a prize for."""
    paid = [e for e in entries if _is_number(e.get("prize")) and e["prize"] > 0]
    return paid[-1] if paid else None


def field_line(board: dict, size: int) -> str:
    """Scores at rank 1 / 10 / 50 and the last paid rank. Public leaderboard values only."""
    entries = ranked(board)
    parts = [f"#{r} {_number(score_at(entries, r))}" for r in REFERENCE_RANKS]
    paid = last_paid(entries)
    parts.append(f"last paid #{paid['rank']} {_number(paid.get('score'))}" if paid else "last paid rank n/a (no prizes shown)")
    return f"  - field ({size} entries): " + ", ".join(parts)


def resolved_count(number: int, board: dict | None, get: Get) -> str:
    """Resolved questions in the tournament: the posts count, else the most
    questions any leaderboard entry was scored on."""
    found = get(f"posts/?tournaments={number}&statuses=resolved&limit=1")
    if isinstance(found, dict) and _is_number(found.get("count")):
        return str(found["count"])
    counts = [e.get("contribution_count") for e in (board or {}).get("entries") or [] if isinstance(e, dict)]
    counts = [c for c in counts if _is_number(c)]
    return f"{max(counts)} (most questions scored on the leaderboard)" if counts else "n/a"


def missing_reason(answer: Any) -> str:
    from tournament_info import leaderboards

    if answer is None:
        return "leaderboard not readable"
    size = sum(len(one.get("entries") or []) for _, one in leaderboards(answer) if isinstance(one, dict))
    return f"not on the leaderboard yet ({size} entries)"


def _number(value: Any) -> str:
    return f"{value:.2f}" if _is_number(value) else "?"


def change_text(now: dict, before: dict | None, before_day: str | None) -> str:
    if before is None:
        return "change: n/a (no earlier Monday stored)"
    if before.get("tid") != now.get("tid"):
        return f"change: n/a (new round since {before_day})"
    parts = []
    rank, was = now.get("rank"), before.get("rank")
    if isinstance(rank, int) and isinstance(was, int):
        moved = was - rank
        parts.append(f"rank {'up ' + str(moved) if moved > 0 else 'down ' + str(-moved) if moved < 0 else 'same'}"
                     f" (was {was})")
    score, old = now.get("score"), before.get("score")
    if isinstance(score, (int, float)) and isinstance(old, (int, float)):
        parts.append(f"score {score - old:+.2f} (was {old:.2f})")
    return f"change since {before_day}: " + (", ".join(parts) if parts else "n/a")


def previous(history: dict, today: str, label: str) -> tuple[str | None, dict | None]:
    """The latest stored day before today that has this tournament."""
    for day in sorted((d for d in history if d < today), reverse=True):
        if isinstance(history[day], dict) and label in history[day]:
            return day, history[day][label]
    return None, None


def standings_lines(today: date, tournaments: list[tuple[str, int | str]], store: Any, get: Get = metaculus_get) -> list[str]:
    """The Monday lines, and today's values saved to the store. Never raises."""
    try:
        return _standings_lines(today, tournaments, store, get)
    except Exception:
        return [UNAVAILABLE]


def _standings_lines(today: date, tournaments: list[tuple[str, int | str]], store: Any, get: Get) -> list[str]:
    try:
        history = store.load() or {}
    except Exception:
        history = None  # can't read: show the standing, no change, don't save over it
    day = today.isoformat()
    found: dict[str, dict] = {}
    lines = []
    for label, tid in tournaments:
        number = project_id(tid, get)
        answer = get(f"leaderboards/project/{number}/") if number is not None else None
        entry = our_entry(answer)
        if entry is None:
            lines.append(f"- {label}{f' ({number})' if number is not None else ''}: unavailable "
                         f"({missing_reason(answer) if number is not None else 'round not found'})")
            continue
        entry["tid"] = number
        found[label] = entry
        board, _ = our_board(answer)
        low, high = entry["low"], entry["high"]
        span = f"{_number(low)} to {_number(high)}" if low is not None and high is not None else "n/a"
        prize = f"${entry['prize']:,.0f}" if entry["prize"] is not None else "n/a"
        before_day, before = previous(history or {}, day, label)
        lines.append(f"- {label} ({number}): rank {entry['rank'] if entry['rank'] is not None else '?'}, "
                     f"score {_number(entry['score'])}, 95% range {span}, prize shown {prize}; "
                     f"{change_text(entry, before, before_day)}")
        lines.append(field_line(board, len(board.get("entries") or [])))
        lines.append(f"  - resolved questions: {resolved_count(number, board, get)}")
    if not found:
        return [UNAVAILABLE + " (" + "; ".join(line[2:] for line in lines) + ")"]
    if history is not None:
        history[day] = found
        try:
            store.save(history, f"Standings {day}")
        except Exception:
            lines.append("- (today's values could not be stored)")
    return lines


def store(token: str) -> Any:
    from gemini_budget import GitHubFileStore
    from question_log import DATA_REPO

    return GitHubFileStore(DATA_REPO, STANDINGS_PATH, token)
