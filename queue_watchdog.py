"""
Queue watchdog (architect, 30 Oct; Tier A, never touches forecasting code).

On 30 Oct a live tournament run sat "queued" from 08:52 to 10:42 UTC with no
runner, and every newer run waiting behind it in the forecast-bot queue was
replaced. This script, run every 10 minutes by queue_watchdog.yaml (its own
concurrency group), lists the tournament workflow's runs that are "queued"
or "waiting" and cancels each one that has waited more than 10 minutes with
NO job started (no runner, no step).

Standing rule (pre-approved): it may cancel only never-started runs of the
live tournament workflow. A started run, or a run "pending" behind another
run in the queue (normal), is never touched. When it cancels anything it
opens an alert issue (once a UTC day; GitHub emails Tony). The job log shows
run ids and wait times only.

    GITHUB_TOKEN=... GITHUB_REPOSITORY=... poetry run python queue_watchdog.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests

TOURNAMENT_WORKFLOW = "run_bot_on_tournament.yaml"
STUCK_AFTER = timedelta(minutes=10)
# "pending" = waiting for the run ahead in the forecast-bot queue: normal, never cancelled.
STUCK_STATUSES = ("queued", "waiting")
API = "https://api.github.com"


def _parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def job_started(job: dict) -> bool:
    """A runner picked the job up. GitHub fills started_at even for a job that
    never left the queue (30 Oct), so the runner and the steps decide."""
    return job.get("status") == "in_progress" or bool(job.get("runner_name")) or bool(job.get("runner_id")) \
        or bool(job.get("steps"))


def stuck_runs(runs: list[dict], jobs_of: Callable[[int], list[dict]], now: datetime) -> list[tuple[int, int]]:
    """(run id, minutes waited) for each queued/waiting run older than 10
    minutes with no job started."""
    stuck = []
    for run in runs:
        if run.get("status") not in STUCK_STATUSES or not run.get("created_at"):
            continue
        waited = now - _parse_time(run["created_at"])
        if waited <= STUCK_AFTER:
            continue
        if any(job_started(job) for job in jobs_of(run["id"])):
            continue
        stuck.append((run["id"], int(waited.total_seconds() // 60)))
    return stuck


def open_alert_issue(title: str, body: str, get: Callable[..., Any], post: Callable[..., Any],
                     headers: dict, repo: str) -> bool:
    """ensemble.open_alert_issue without its imports (this workflow installs
    requests only): no new issue if an open one has this title. Never raises."""
    try:
        existing = get(f"{API}/repos/{repo}/issues", headers=headers, params={"state": "open", "per_page": 100}, timeout=30)
        existing.raise_for_status()
        if any(issue.get("title") == title for issue in existing.json()):
            return False
        post(f"{API}/repos/{repo}/issues", headers=headers, json={"title": title, "body": body}, timeout=30).raise_for_status()
        return True
    except Exception:
        return False


def main(now: datetime | None = None, get: Callable[..., Any] = requests.get,
         post: Callable[..., Any] = requests.post, notify: Callable[[str, str], bool] | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    repo = os.environ["GITHUB_REPOSITORY"]
    headers = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}

    def api(path: str, **params: Any) -> dict:
        response = get(f"{API}/repos/{repo}/{path}", headers=headers, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    runs: list[dict] = []
    for status in STUCK_STATUSES:
        runs += api(f"actions/workflows/{TOURNAMENT_WORKFLOW}/runs", status=status, per_page=50)["workflow_runs"]
    stuck = stuck_runs(runs, lambda run_id: api(f"actions/runs/{run_id}/jobs")["jobs"], now)
    lines = []
    for run_id, minutes in stuck:
        try:
            post(f"{API}/repos/{repo}/actions/runs/{run_id}/cancel", headers=headers, timeout=30).raise_for_status()
            lines.append(f"run {run_id}: waited {minutes} min with no job started; cancelled")
        except Exception as e:
            lines.append(f"run {run_id}: waited {minutes} min with no job started; cancel failed ({type(e).__name__})")
    if not stuck:
        lines.append(f"No stuck tournament run ({len(runs)} queued/waiting, none over 10 min with no job started)")
    else:
        notify = notify or (lambda title, body: open_alert_issue(title, body, get, post, headers, repo))
        today = now.date().isoformat()
        opened = notify(f"Queue watchdog cancelled a stuck live run ({today})",
                        "Never-started runs of the live tournament workflow, cancelled by the queue watchdog "
                        "(queue_watchdog.yaml) so the queue can move:\n\n" + "\n".join(f"- {line}" for line in lines))
        lines.append("Alert issue opened" if opened else "Alert issue already open today (or not opened)")
    return lines


if __name__ == "__main__":
    try:
        output = main()
        failed = any("cancel failed" in line for line in output)
    except Exception as e:
        output, failed = [f"Queue watchdog could not run ({type(e).__name__})"], True
    text = "\n".join(output)
    print(text)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## Queue watchdog\n\n" + "\n".join(f"- {line}" for line in output) + "\n")
    sys.exit(1 if failed else 0)
