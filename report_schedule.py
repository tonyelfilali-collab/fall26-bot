"""
When the Scoreboard + calibration tables run on their own (architect, 29 Oct).
Read-only on Metaculus; forecasts nothing, spends nothing.

The Scoreboard workflow's daily trigger asks `decide`:
- on REPORT_DATES (Sat 7 Nov, Tue 10 Nov): run both;
- otherwise, run both once as soon as AGENT_ERA_THRESHOLD (15) agent-era
  questions have resolved (agent era: the latest submitted log started at or
  after scoreboard.FRONTIER_SWITCH_ON), then never again for that reason
  (marked in fall26-data status/report_schedule.json).

Resolved = the post's own status in the raw /api/posts/ listing of its
tournament (the log's default_project_id), as in Tournament info; no library
filter. The job log shows the count and the decision only.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable

import requests

import scoreboard

REPORT_DATES = ("2026-11-07", "2026-11-10")
AGENT_ERA_THRESHOLD = 15
MARK_PATH = "status/report_schedule.json"
MARK_KEY = "agent_era_15"
API = "https://www.metaculus.com/api"
LOG_PATH = re.compile(r"^questions/tournament/(\d{4}-\d{2}-\d{2})/(\d+)_(\d{6})\.json$")


def log_started(path: str) -> datetime | None:
    """A question log's start time (UTC) from its path, or None (shadow files, others)."""
    match = LOG_PATH.match(path)
    if match is None:
        return None
    day, _, hms = match.groups()
    return datetime.strptime(f"{day} {hms}", "%Y-%m-%d %H%M%S").replace(tzinfo=timezone.utc)


def agent_era_paths(paths: list[str], switch_on: str) -> list[str]:
    """Question logs that started at or after the switch-on (every attempt)."""
    start = datetime.fromisoformat(switch_on.replace("Z", "+00:00"))
    return [p for p in paths if (started := log_started(p)) is not None and started >= start]


def posts_by_project(records: list[dict]) -> dict[int, set[int]]:
    """Latest submitted log per post (the Scoreboard's rule), grouped by tournament."""
    found: dict[int, set[int]] = {}
    for record in scoreboard.latest_submitted_records("", records):
        question = record.get("question") or {}
        post, project = question.get("id_of_post"), question.get("default_project_id")
        if post is not None and project is not None:
            found.setdefault(int(project), set()).add(int(post))
    return found


def resolved_posts(project: int, get: Callable[..., Any] = requests.get) -> set[int]:
    """Post ids with status 'resolved' in a tournament's raw /api/posts/ listing (all pages)."""
    resolved: set[int] = set()
    offset = 0
    while True:
        response = get(f"{API}/posts/", params={"tournaments": project, "limit": 100, "offset": offset},
                       headers={"Authorization": f"Token {os.environ['METACULUS_TOKEN']}"}, timeout=30)
        response.raise_for_status()
        data = response.json()
        results = data.get("results") or []
        for post in results:
            status = post.get("status") or (post.get("question") or {}).get("status")
            if status == "resolved" and post.get("id") is not None:
                resolved.add(int(post["id"]))
        if not data.get("next") or not results:
            return resolved
        offset += len(results)


def agent_era_resolved(by_project: dict[int, set[int]], get: Callable[..., Any] = requests.get) -> int:
    return sum(len(posts & resolved_posts(project, get)) for project, posts in by_project.items())


def decide(today: str, resolved: int | None, marked: bool) -> str | None:
    """Why the tables run today, or None."""
    if today in REPORT_DATES:
        return f"scheduled date {today}"
    if resolved is not None and resolved >= AGENT_ERA_THRESHOLD and not marked:
        return f"{resolved} agent-era questions resolved (>= {AGENT_ERA_THRESHOLD})"
    return None


def should_mark(reason: str | None, resolved: int | None, marked: bool) -> bool:
    """The tables run today with 15+ agent-era resolved (a scheduled date too):
    that is the one 15+ run."""
    return reason is not None and not marked and resolved is not None and resolved >= AGENT_ERA_THRESHOLD


def _data(path: str, token: str) -> Any:
    try:
        return json.loads(base64.b64decode(scoreboard._github(f"contents/{path}", token)["content"]))
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mark", action="store_true", help="note that the 15+ run happened")
    args = parser.parse_args()
    token = os.environ["DATA_REPO_TOKEN"]
    marks = _data(MARK_PATH, token) or {}
    today = datetime.now(timezone.utc).date().isoformat()
    if args.mark:
        marks[MARK_KEY] = today
        scoreboard.save_markdown(MARK_PATH, json.dumps(marks, indent=1) + "\n", token, "Report schedule: 15+ run done")
        print(f"marked {MARK_KEY} = {today}")
        return
    resolved: int | None = None
    if MARK_KEY not in marks and scoreboard.FRONTIER_SWITCH_ON:
        tree = scoreboard._github("git/trees/HEAD?recursive=1", token)["tree"]
        paths = agent_era_paths([t["path"] for t in tree], scoreboard.FRONTIER_SWITCH_ON)
        records = [json.loads(base64.b64decode(scoreboard._github(f"contents/{p}", token)["content"])) for p in paths]
        resolved = agent_era_resolved(posts_by_project(records))
    reason = decide(today, resolved, MARK_KEY in marks)
    print(f"Agent-era questions resolved: {'already marked' if resolved is None else resolved}; "
          f"tables today: {reason or 'no'}")
    output = os.getenv("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as f:
            f.write(f"run={'true' if reason else 'false'}\n")
            f.write(f"mark={'true' if should_mark(reason, resolved, MARK_KEY in marks) else 'false'}\n")


if __name__ == "__main__":
    main()
