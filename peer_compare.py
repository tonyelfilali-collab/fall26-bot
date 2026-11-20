"""Peer score per question by type and era: us vs reference bots (architect, 20 Nov).

Read-only. Reads the Fall leaderboard and each chosen bot's per-question
contributions (/api/leaderboards/project/<id>/contributions/?user_id=...), joins
them with the resolved Fall posts (question type) and splits by era (before /
after 21 Oct 18:41 UTC: our forecast time from the latest resolved review, else
the question's open time). Saves a private table to fall26-data reviews/; the
public job log gets counts and field names only (no scores).
"""
from __future__ import annotations

import base64
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import requests

from resolved_review import TOURNAMENT, resolved_posts
from scoreboard import _github, save_markdown
from tournament_info import _entry_user, leaderboards

API = "https://www.metaculus.com/api"
ME = "Quillfeather-Vex"
REFERENCE_BOTS = ("metac-claude-opus-5-5-high+asknews", "metac-agent-claude-opus-5-5")
NEIGHBOURS = 2  # bots ranked just above and just below us
ERA_CUT = "2026-10-21T18:41"
TYPES = ("binary", "multiple_choice", "numeric", "discrete")


def _headers() -> dict:
    return {"Authorization": f"Token {os.environ['METACULUS_TOKEN']}"}


def leaderboard_entries(board: Any) -> list[dict]:
    """All entries of the (first) Fall leaderboard, rank order."""
    for _, one in leaderboards(board):
        entries = [e for e in (one.get("entries") or []) if isinstance(e, dict)]
        if entries:
            return sorted(entries, key=lambda e: (e.get("rank") is None, e.get("rank") or 0))
    return []


def pick_users(entries: list[dict]) -> list[tuple[str, int, Any]]:
    """(username, user id, rank): us, the reference bots, and our neighbours."""
    names = [(_entry_user(e).get("username") or "", _entry_user(e).get("id"), e.get("rank")) for e in entries]
    picked: list[tuple[str, int, Any]] = []
    mine = next((i for i, n in enumerate(names) if n[0] == ME), None)
    if mine is not None:
        lo, hi = max(0, mine - NEIGHBOURS), min(len(names), mine + NEIGHBOURS + 1)
        picked += [n for n in names[lo:hi] if n[1] is not None]
    picked += [n for n in names if n[0] in REFERENCE_BOTS and n[1] is not None and n not in picked]
    return picked


def contribution_post(c: dict) -> int | None:
    """The post id of one contribution (the API names it a few ways)."""
    for key in ("post_id", "postId"):
        if c.get(key) is not None:
            return int(c[key])
    for key in ("post", "question"):
        inner = c.get(key)
        if isinstance(inner, dict):
            for k in ("post_id", "id"):
                if inner.get(k) is not None and (key == "post" or k == "post_id"):
                    return int(inner[k])
    return None


def contribution_score(c: dict) -> float | None:
    for key in ("score", "peer_score", "contribution"):
        if isinstance(c.get(key), (int, float)):
            return float(c[key])
    return None


def post_info(posts: list[dict], our_times: dict[int, str]) -> dict[int, tuple[str, str]]:
    """post id -> (question type, era)."""
    out = {}
    for p in posts:
        q = p.get("question") or {}
        kind = q.get("type")
        if kind not in TYPES:
            continue
        when = our_times.get(p["id"]) or str(q.get("open_time") or p.get("open_time") or "")
        out[p["id"]] = (kind, "after" if when[:16] >= ERA_CUT else "before")
    # A question id may differ from the post id: map both.
    for p in posts:
        q = p.get("question") or {}
        if q.get("id") is not None and p["id"] in out:
            out.setdefault(q["id"], out[p["id"]])
    return out


def table(per_user: dict[str, list[tuple[int, float]]], info: dict[int, tuple[str, str]],
          ranks: dict[str, Any]) -> list[str]:
    lines = ["| Bot (rank) | Era | " + " | ".join(TYPES) + " | all |", "|---|---|" + "---|" * (len(TYPES) + 1)]
    for user, rows in per_user.items():
        for era in ("before", "after"):
            cells, every = [], []
            for kind in TYPES:
                vals = [s for post, s in rows if info.get(post) == (kind, era)]
                every += vals
                cells.append(f"{statistics.mean(vals):+.1f} (n={len(vals)})" if vals else "-")
            total = f"{statistics.mean(every):+.1f} (n={len(every)})" if every else "-"
            lines.append(f"| {user} ({ranks.get(user)}) | {era} | " + " | ".join(cells) + f" | {total} |")
    return lines


def latest_review_times(token: str) -> dict[int, str]:
    tree = _github("git/trees/HEAD?recursive=1", token)["tree"]
    paths = sorted(t["path"] for t in tree if t["path"].startswith("reviews/resolved_") and t["path"].endswith(".json"))
    if not paths:
        return {}
    rows = json.loads(base64.b64decode(_github(f"contents/{paths[-1]}", token)["content"]))
    return {int(r["post"]): str(r.get("forecast_at") or "") for r in rows if r.get("forecast_at")}


def main() -> None:
    now = datetime.now(timezone.utc)
    token = os.environ["DATA_REPO_TOKEN"]
    board = requests.get(f"{API}/leaderboards/project/{TOURNAMENT}/", headers=_headers(), timeout=60)
    board.raise_for_status()
    entries = leaderboard_entries(board.json())
    users = pick_users(entries)
    info = post_info(resolved_posts(), latest_review_times(token))
    per_user: dict[str, list[tuple[int, float]]] = {}
    ranks, keys = {}, set()
    for name, uid, rank in users:
        response = requests.get(f"{API}/leaderboards/project/{TOURNAMENT}/contributions/", params={"user_id": uid},
                                headers=_headers(), timeout=60)
        if response.status_code != 200:
            print(f"contributions for one bot: HTTP {response.status_code}")
            continue
        data = response.json()
        items = data.get("contributions", data.get("results", data)) if isinstance(data, dict) else data
        items = [c for c in (items or []) if isinstance(c, dict)]
        keys |= {k for c in items for k in c}
        per_user[name] = [(p, s) for c in items if (p := contribution_post(c)) is not None
                          and (s := contribution_score(c)) is not None]
        ranks[name] = rank
    lines = [f"## Peer score per question by type and era ({now:%Y-%m-%d %H:%M} UTC)", "",
             f"Fall tournament {TOURNAMENT}; resolved questions with a known type: {len({p for p in info})}. "
             f"Era: before / after {ERA_CUT} UTC (our forecast time, else the question's open time). "
             "Contributions are the leaderboard's per-question scores (peer score x question weight).", "",
             *table(per_user, info, ranks), "",
             f"Contribution fields seen: {', '.join(sorted(keys)) or 'none'}"]
    base = f"reviews/peer_compare_{now:%Y-%m-%d_%H%M}"
    save_markdown(f"{base}.md", "\n".join(lines) + "\n", token, "Peer compare (private)")
    save_markdown(f"{base}.json", json.dumps({"ranks": ranks, "per_user": per_user,
                                              "info": {str(k): v for k, v in info.items()}}, indent=1),
                  token, "Peer compare details (private)")
    # Public log: counts and field names only.
    print(f"Leaderboard entries: {len(entries)}; bots compared: {len(per_user)}; "
          f"contributions matched to a type: {sum(1 for r in per_user.values() for p, _ in r if p in info)}")
    print(f"Contribution fields seen: {', '.join(sorted(keys)) or 'none'}")
    print(f"Saved to fall26-data {base}.md and .json")


if __name__ == "__main__":
    sys.exit(main())
