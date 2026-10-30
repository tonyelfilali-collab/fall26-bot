"""queue_watchdog.py: cancels only never-started live runs stuck in the queue
over 10 minutes (architect, 30 Oct). Fake run lists only (0 real calls)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import health_check
import queue_watchdog as qw

NOW = datetime(2026, 10, 30, 9, 30, tzinfo=timezone.utc)


def _at(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# The 30 Oct stuck job: started_at filled in, but no runner and no step.
NEVER_STARTED = {"status": "queued", "started_at": _at(11), "runner_id": 0, "runner_name": "", "steps": []}
STARTED = {"status": "in_progress", "started_at": _at(11), "runner_id": 1000005966,
           "runner_name": "GitHub Actions 1000005966", "steps": [{"name": "Set up job"}]}


def test_job_started_by_runner_or_steps_not_by_started_at():
    assert not qw.job_started(NEVER_STARTED)
    assert qw.job_started(STARTED)
    assert qw.job_started({"status": "queued", "runner_name": "GitHub Actions 7"})


def test_stuck_runs_started_never_queued_11_cancelled_queued_9_kept():
    runs = [
        {"id": 1, "status": "queued", "created_at": _at(11)},   # never started, 11 min: cancelled
        {"id": 2, "status": "queued", "created_at": _at(9)},    # 9 min: kept
        {"id": 3, "status": "queued", "created_at": _at(30)},   # started (a runner has it): never cancelled
        {"id": 4, "status": "pending", "created_at": _at(30)},  # behind another run in the queue: normal
        {"id": 5, "status": "waiting", "created_at": _at(12)},  # no job created yet: cancelled
        {"id": 6, "status": "in_progress", "created_at": _at(40)},
    ]
    jobs = {1: [NEVER_STARTED], 2: [NEVER_STARTED], 3: [STARTED], 4: [], 5: [], 6: [STARTED]}
    asked = []

    def jobs_of(run_id):  # type: ignore[no-untyped-def]
        asked.append(run_id)
        return jobs[run_id]

    assert qw.stuck_runs(runs, jobs_of, NOW) == [(1, 11), (5, 12)]
    assert 2 not in asked and 4 not in asked and 6 not in asked  # never looked at


class FakeGitHub:
    def __init__(self, runs, jobs, issues=()):  # type: ignore[no-untyped-def]
        self.runs, self.jobs, self.issues, self.posts = runs, jobs, list(issues), []

    def get(self, url, headers=None, params=None, timeout=None):  # type: ignore[no-untyped-def]
        if url.endswith(f"/actions/workflows/{qw.TOURNAMENT_WORKFLOW}/runs"):
            body = {"workflow_runs": [r for r in self.runs if r["status"] == params["status"]]}
        elif url.endswith("/jobs"):
            body = {"jobs": self.jobs[int(url.split("/")[-2])]}
        else:
            assert url.endswith("/issues")
            body = self.issues
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)

    def post(self, url, headers=None, json=None, timeout=None):  # type: ignore[no-untyped-def]
        self.posts.append((url, json))
        return SimpleNamespace(raise_for_status=lambda: None)


def test_main_cancels_the_stuck_run_only_and_opens_one_alert_a_day(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    runs = [{"id": 41029354779, "status": "queued", "created_at": _at(11)},
            {"id": 42, "status": "queued", "created_at": _at(25)}]
    gh = FakeGitHub(runs, {41029354779: [NEVER_STARTED], 42: [STARTED]})
    lines = qw.main(NOW, gh.get, gh.post)
    assert lines[0] == "run 41029354779: waited 11 min with no job started; cancelled"
    assert [u for u, _ in gh.posts] == ["https://api.github.com/repos/o/r/actions/runs/41029354779/cancel",
                                        "https://api.github.com/repos/o/r/issues"]
    assert gh.posts[1][1]["title"] == "Queue watchdog cancelled a stuck live run (2026-10-30)"
    assert lines[-1] == "Alert issue opened"
    # Later the same UTC day: the issue is open already, so no second one.
    again = FakeGitHub(runs, {41029354779: [NEVER_STARTED], 42: [STARTED]}, issues=[{"title": gh.posts[1][1]["title"]}])
    qw.main(NOW, again.get, again.post)
    assert [u for u, _ in again.posts] == ["https://api.github.com/repos/o/r/actions/runs/41029354779/cancel"]


def test_main_does_nothing_when_nothing_is_stuck(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    gh = FakeGitHub([{"id": 2, "status": "queued", "created_at": _at(9)}], {2: [NEVER_STARTED]})
    assert qw.main(NOW, gh.get, gh.post) == ["No stuck tournament run (1 queued/waiting, none over 10 min with no job started)"]
    assert gh.posts == []


def test_workflow_has_its_own_queue_and_only_the_rights_it_needs():
    text = Path(".github/workflows/queue_watchdog.yaml").read_text()
    assert "group: queue-watchdog" in text and "group: forecast-bot" not in text
    assert "workflow_dispatch:" in text and "schedule:" in text
    assert "actions: write" in text and "issues: write" in text and "secrets." not in text
    assert "python queue_watchdog.py" in text


def test_health_red_when_no_live_run_started_in_45_minutes():
    runs = [
        {"id": 3, "created_at": _at(40)},  # the stuck one: never started
        {"id": 2, "created_at": _at(50)},  # started 50 min ago
        {"id": 1, "created_at": _at(200)},  # too old to look at
    ]
    jobs = {3: [NEVER_STARTED], 2: [dict(STARTED, started_at=_at(50))], 1: [STARTED]}
    last = health_check.last_started_run(runs, lambda i: jobs[i], NOW)
    assert last == NOW - timedelta(minutes=50)
    red = health_check.Report()
    health_check.check_recent_start(last, NOW, red)
    assert red.red == ["No live tournament run has started in the last 45 minutes (last start 50 min ago)"]
    ok = health_check.Report()
    health_check.check_recent_start(NOW - timedelta(minutes=12), NOW, ok)
    assert ok.red == [] and ok.ok == ["Last live tournament run started 12 min ago"]
    none = health_check.Report()
    health_check.check_recent_start(health_check.last_started_run([], lambda i: [], NOW), NOW, none)
    assert none.red == ["No live tournament run has started in the last 45 minutes"]


def test_health_skips_the_log_of_a_run_cancelled_before_it_started(monkeypatch):
    # 30 Oct: a never-started run has no log (HTTP 404), which broke the Claude and AskNews 402 checks.
    runs = [{"id": 1, "status": "completed"}, {"id": 2, "status": "completed"}]
    jobs = {1: [dict(NEVER_STARTED, id=11, status="completed")], 2: [dict(STARTED, id=22, status="completed")]}
    read = []

    def job_log(job_id):  # type: ignore[no-untyped-def]
        read.append(job_id)
        return "log"

    monkeypatch.setattr(health_check, "recent_runs", lambda since: runs)
    monkeypatch.setattr(health_check, "run_jobs", lambda run_id: jobs[run_id])
    monkeypatch.setattr(health_check, "job_log", job_log)
    monkeypatch.setattr(health_check.claude_health, "run_from_log", lambda text: {"mode": "full"})
    assert health_check.frontier_runs(NOW) == [{"mode": "full"}]
    assert read == [22]
