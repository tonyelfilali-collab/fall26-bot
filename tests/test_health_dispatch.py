"""The tournament run starts the daily health check once a UTC day, after 06:00."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import health_dispatch as hd

AT_9 = datetime(2026, 9, 29, 9, 2, tzinfo=timezone.utc)


def test_not_before_6_utc():
    assert not hd.should_dispatch(datetime(2026, 9, 29, 5, 59, tzinfo=timezone.utc), [])
    assert hd.should_dispatch(datetime(2026, 9, 29, 6, 0, tzinfo=timezone.utc), [])


def test_once_a_utc_day_and_pr_runs_dont_count():
    yesterday = {"created_at": "2026-09-28T14:46:42Z", "event": "schedule"}
    pr_today = {"created_at": "2026-09-29T08:00:00Z", "event": "pull_request"}
    today = {"created_at": "2026-09-29T06:07:00Z", "event": "workflow_dispatch"}
    assert hd.should_dispatch(AT_9, [yesterday, pr_today])
    assert not hd.should_dispatch(AT_9, [yesterday, today])
    assert not hd.should_dispatch(AT_9, [{"created_at": "2026-09-29T06:30:00Z", "event": "schedule"}])


def test_main_dispatches_on_main_only_when_due(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    posts = []

    def get(url, headers=None, params=None, timeout=None):
        assert url.endswith("/actions/workflows/health.yml/runs") and params["created"] == ">=2026-09-29"
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"workflow_runs": runs})

    def post(url, headers=None, json=None, timeout=None):
        posts.append((url, json))
        return SimpleNamespace(raise_for_status=lambda: None)

    runs = []
    assert "started" in hd.main(AT_9, get, post)
    assert posts == [("https://api.github.com/repos/o/r/actions/workflows/health.yml/dispatches", {"ref": "main"})]
    runs = [{"created_at": "2026-09-29T09:02:30Z", "event": "workflow_dispatch"}]
    assert "nothing to do" in hd.main(AT_9, get, post)
    assert len(posts) == 1


def test_workflow_step_never_blocks_the_forecast():
    text = open(".github/workflows/run_bot_on_tournament.yaml").read()
    step = text[text.index("Start the daily health check if due"):]
    assert "continue-on-error: true" in step.split("- name:")[0]
    assert text.index("Start the daily health check") > text.index("name: Run bot")
    assert "actions: write" in text


def test_lab_due_once_a_pacific_day_in_its_last_3_hours():
    from zoneinfo import ZoneInfo

    pt = ZoneInfo("America/Los_Angeles")
    evening = datetime(2026, 10, 6, 21, 10, tzinfo=pt).astimezone(timezone.utc)
    assert not hd.lab_due(datetime(2026, 10, 6, 20, 50, tzinfo=pt).astimezone(timezone.utc), [])
    assert hd.lab_due(evening, [])
    bot = {"triggering_actor": {"login": "github-actions[bot]"}}
    same_day = {"created_at": datetime(2026, 10, 6, 21, 5, tzinfo=pt).astimezone(timezone.utc).isoformat(), **bot}
    assert not hd.lab_due(evening, [same_day])
    yesterday = {"created_at": datetime(2026, 10, 5, 22, 0, tzinfo=pt).astimezone(timezone.utc).isoformat(), **bot}
    assert hd.lab_due(evening, [yesterday])


def test_manual_lab_runs_dont_count():
    # 30 Sep: two manual proof runs at 03:26 Pacific kept that night's lab from starting.
    manual = [{"created_at": "2026-09-29T10:27:45Z", "triggering_actor": {"login": "tonyelfilali-collab"}},
              {"created_at": "2026-09-29T10:26:44Z", "actor": {"login": "tonyelfilali-collab"}}]
    night = datetime(2026, 9, 30, 5, 2, tzinfo=timezone.utc)  # 22:02 Pacific, 29 Sep
    assert hd.lab_due(night, manual)
    assert not hd.lab_due(night, manual + [{"created_at": "2026-09-30T04:02:00Z", "triggering_actor": {"login": "github-actions[bot]"}}])


def test_scoreboard_lab_section_without_results(monkeypatch):
    import scoreboard

    def missing(path, token, method="GET", body=None):
        raise RuntimeError("404")

    monkeypatch.setattr(scoreboard, "_github", missing)
    assert "no results yet" in scoreboard.lab_section("t")
