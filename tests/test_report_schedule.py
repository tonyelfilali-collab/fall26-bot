"""report_schedule.py: when the Scoreboard + calibration tables run on their own
(architect, 29 Oct). Fake listings only (0 real calls)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import report_schedule as rs

SWITCH = "2026-10-21T18:41:31+00:00"


def test_log_start_time_from_the_path():
    assert rs.log_started("questions/tournament/2026-10-21/46100_184200.json") == datetime(2026, 10, 21, 18, 42, tzinfo=timezone.utc)
    assert rs.log_started("questions/tournament/2026-10-21/46100_184200_shadows.json") is None
    assert rs.log_started("questions/test_questions/2026-10-21/46100_184200.json") is None
    paths = [
        "questions/tournament/2026-10-21/46100_184130.json",  # one second before
        "questions/tournament/2026-10-21/46101_184131.json",  # the switch-on itself
        "questions/tournament/2026-10-28/46277_180200.json",
        "questions/tournament/2026-10-28/46277_180200_shadows.json",
    ]
    assert rs.agent_era_paths(paths, SWITCH) == paths[1:3]


def _record(post, project, started, submitted=True):  # type: ignore[no-untyped-def]
    return {"question": {"id_of_post": post, "default_project_id": project}, "started_at": started, "submitted": submitted}


def test_latest_submitted_per_post_grouped_by_tournament():
    records = [
        _record(1, 33121, "2026-10-22T00:00:00+00:00"),
        _record(1, 33121, "2026-10-23T00:00:00+00:00"),  # the same post again: counted once
        _record(2, 33132, "2026-10-22T00:00:00+00:00"),
        _record(3, 33132, "2026-10-22T00:00:00+00:00", submitted=False),  # never submitted: not scored
        {"question": {"id_of_post": 4}, "started_at": "2026-10-22", "submitted": True},  # no tournament: skipped
    ]
    assert rs.posts_by_project(records) == {33121: {1}, 33132: {2}}


class Listing:
    """/api/posts/ pages: post ids with their status, 2 per page."""

    def __init__(self, by_project):  # type: ignore[no-untyped-def]
        self.by_project, self.calls = by_project, []

    def __call__(self, url, params=None, headers=None, timeout=None):  # type: ignore[no-untyped-def]
        self.calls.append(params)
        posts = self.by_project[params["tournaments"]]
        page = posts[params["offset"]:params["offset"] + 2]
        more = params["offset"] + 2 < len(posts)
        body = {"results": [{"id": i, "status": s} if n % 2 else {"id": i, "question": {"status": s}}
                            for n, (i, s) in enumerate(page)], "next": "x" if more else None}
        return type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: body})()


def test_resolved_counted_from_the_raw_listing_every_page(monkeypatch):
    monkeypatch.setenv("METACULUS_TOKEN", "test")
    get = Listing({
        33121: [(1, "resolved"), (5, "resolved"), (6, "closed"), (7, "open"), (8, "resolved")],
        33132: [(2, "closed"), (9, "resolved")],
    })
    assert rs.resolved_posts(33121, get) == {1, 5, 8}
    assert [c["offset"] for c in get.calls] == [0, 2, 4]
    # Only our agent-era posts count (5 and 8 resolved but not ours; 2 ours but closed).
    assert rs.agent_era_resolved({33121: {1, 6}, 33132: {2}}, get) == 1


@pytest.mark.parametrize("today, resolved, marked, expected", [
    ("2026-11-07", 0, False, "scheduled date 2026-11-07"),
    ("2026-11-10", None, True, "scheduled date 2026-11-10"),
    ("2026-11-03", 14, False, None),
    ("2026-11-03", 15, False, "15 agent-era questions resolved (>= 15)"),
    ("2026-11-04", 16, True, None),  # the 15+ run happened already
    ("2026-11-04", None, True, None),
])
def test_decide(today, resolved, marked, expected):
    assert rs.decide(today, resolved, marked) == expected


def test_the_15_plus_run_is_marked_once_even_on_a_scheduled_date():
    assert rs.should_mark("scheduled date 2026-11-07", 15, False) is True
    assert rs.should_mark("scheduled date 2026-11-07", 3, False) is False
    assert rs.should_mark("15 agent-era questions resolved (>= 15)", 15, False) is True
    assert rs.should_mark(None, 20, False) is False
    assert rs.should_mark("scheduled date 2026-11-10", None, True) is False


def test_workflow_runs_both_tables_when_the_check_says_so():
    text = Path(".github/workflows/scoreboard.yml").read_text()
    assert 'cron: "45 6 * * *"' in text and 'cron: "30 6 * * 1"' in text
    assert "poetry run python report_schedule.py\n" in text and "report_schedule.py --mark" in text
    score = text.split("- name: Score resolved questions")[1].split("- name:")[0]
    calibration = text.split("- name: Calibration report")[1].split("- name:")[0]
    for step in (score, calibration):
        assert "steps.schedule.outputs.run == 'true'" in step
    assert "'30 6 * * 1'" in score and "'30 6 * * 1'" not in calibration  # Mondays: the Scoreboard only, as before
    check = text.split("- name: Report schedule check")[1].split("- name:")[0]
    assert "inputs.schedule_check == true" in check  # the manual dry run of the daily check
    for step in (score, calibration):
        assert "inputs.schedule_check != true" in step
