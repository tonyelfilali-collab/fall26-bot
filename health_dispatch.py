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


def should_dispatch(now: datetime, runs: list[dict]) -> bool:
    """runs: health.yml runs from the API (created_at, event). A pull-request
    run (the deliberate simulated miss) doesn't count as the day's check."""
    if now.hour < HEALTH_HOUR_UTC:
        return False
    today = now.date().isoformat()
    return not any(
        r.get("created_at", "").startswith(today) and r.get("event") != "pull_request" for r in runs
    )


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
    print(message)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(message + "\n\n")
    sys.exit(0)
