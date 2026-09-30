"""
The daily health check must not depend on a second cron job (architect,
29 Sep): the first tournament run after 06:00 UTC each day starts health.yml
(workflow_dispatch with the run's GITHUB_TOKEN, `actions: write`) if health
hasn't run yet that UTC day. Runs as its own step after the forecast; any
failure is only logged and never fails the job.

    GITHUB_TOKEN=... GITHUB_REPOSITORY=... poetry run python health_dispatch.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import requests

HEALTH_WORKFLOW = "health.yml"
HEALTH_HOUR_UTC = 6
API = "https://api.github.com"
# The replay lab (lab.py): once a Pacific quota day, in its last 3 hours (the
# only time the lab may use Gemini); it re-checks every rule itself.
LAB_WORKFLOW = "replay_lab.yaml"
LAB_FROM_PACIFIC_HOUR = 21


def should_dispatch(now: datetime, runs: list[dict]) -> bool:
    """runs: health.yml runs from the API (created_at, event). A pull-request
    run (the deliberate simulated miss) doesn't count as the day's check."""
    if now.hour < HEALTH_HOUR_UTC:
        return False
    today = now.date().isoformat()
    return not any(
        r.get("created_at", "").startswith(today) and r.get("event") != "pull_request" for r in runs
    )


def started_by_bot(run: dict) -> bool:
    """A run the bot itself started (the tournament run's GITHUB_TOKEN:
    github-actions[bot]), not a manual test run."""
    who = (run.get("triggering_actor") or run.get("actor") or {}).get("login", "")
    return who.endswith("[bot]")


def lab_due(now: datetime, runs: list[dict]) -> bool:
    """The last 3 hours of the Pacific day, and no lab run started by the bot
    yet that Pacific day (manual test runs don't count, architect 30 Sep)."""
    from zoneinfo import ZoneInfo

    pacific = ZoneInfo("America/Los_Angeles")
    local = now.astimezone(pacific)
    if local.hour < LAB_FROM_PACIFIC_HOUR:
        return False
    for r in runs:
        if not started_by_bot(r):
            continue
        created = datetime.fromisoformat(r.get("created_at", "").replace("Z", "+00:00")) if r.get("created_at") else None
        if created and created.astimezone(pacific).date() == local.date():
            return False
    return True


def start_lab_if_due(now: datetime, get=requests.get, post=requests.post) -> str:  # type: ignore[no-untyped-def]
    repo = os.environ["GITHUB_REPOSITORY"]
    headers = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}
    url = f"{API}/repos/{repo}/actions/workflows/{LAB_WORKFLOW}"
    response = get(f"{url}/runs", headers=headers, params={"per_page": 20}, timeout=30)
    response.raise_for_status()
    if not lab_due(now, response.json().get("workflow_runs", [])):
        return "Replay lab: not due"
    post(f"{url}/dispatches", headers=headers, json={"ref": "main"}, timeout=30).raise_for_status()
    return "Replay lab: started (last 3 hours of the Pacific day)"


def main(now: datetime | None = None, get=requests.get, post=requests.post) -> str:  # type: ignore[no-untyped-def]
    now = now or datetime.now(timezone.utc)
    repo = os.environ["GITHUB_REPOSITORY"]
    headers = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}
    url = f"{API}/repos/{repo}/actions/workflows/{HEALTH_WORKFLOW}"
    response = get(f"{url}/runs", headers=headers, params={"created": f">={now.date().isoformat()}", "per_page": 50}, timeout=30)
    response.raise_for_status()
    if not should_dispatch(now, response.json().get("workflow_runs", [])):
        return "Health check: already ran today (UTC), or before 06:00 UTC; nothing to do"
    started = post(f"{url}/dispatches", headers=headers, json={"ref": "main"}, timeout=30)
    started.raise_for_status()
    return "Health check: not run yet today (UTC); started health.yml"


if __name__ == "__main__":
    try:
        message = main()
    except Exception as e:  # never fails the job
        message = f"Health check dispatch failed ({type(e).__name__}); the next run tries again"
        print(f"::warning title=health-dispatch-failed::{message}")
    try:
        message += "\n" + start_lab_if_due(datetime.now(timezone.utc))
    except Exception as e:  # never fails the job
        message += f"\nReplay lab dispatch failed ({type(e).__name__})"
    print(message)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(message + "\n\n")
    sys.exit(0)
