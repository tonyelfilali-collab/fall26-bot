"""Stretch monitor (stretch_monitor.py; architect, 16 Nov): agent-era binary only,
applied stretch k = 1.2 / 1.5 vs submitted, logged shadows, leave-one-out, report at 30.
Fake logs, no network."""
from __future__ import annotations

import math

import pytest

import health_check
import stretch_monitor
from calibration import stretched

SWITCH = "2026-10-21T18:41:31+00:00"


def _record(post, p, kind="binary", started="2026-11-01T00:00:00+00:00", shadow=None):
    return {"question": {"id_of_post": post, "question_type": kind}, "final_forecast": p,
            "started_at": started, "shadow": shadow or {}}


def _rows(records, resolutions):
    return stretch_monitor.rows(records, resolutions.get, SWITCH)


def test_rows_agent_era_binary_resolved_only():
    records = [
        _record(1, 0.8, shadow={"stretch-1.2": 0.84, "stretch-1.5": 0.89, "mean": 0.7}),
        _record(2, 0.3),
        _record(3, 0.6, started="2026-10-10T00:00:00+00:00"),  # before the switch-on
        _record(4, {"declared_percentiles": []}, kind="numeric"),  # not binary
        _record(5, 0.5),  # not resolved
        _record(6, 0.4),  # annulled
    ]
    rows = _rows(records, {1: "yes", 2: "no", 3: "yes", 4: "5", 6: "annulled"})
    assert [(r["post"], r["yes"]) for r in rows] == [(1, True), (2, False)]
    assert rows[0]["shadow"] == {1.2: 0.84, 1.5: 0.89} and rows[1]["shadow"] == {}


def test_result_gains_and_loo():
    # Confident and right every time: bolder is better.
    table = [{"post": i, "p": 0.8 if i % 2 else 0.2, "yes": bool(i % 2), "shadow": {1.2: 0.85 if i % 2 else 0.15}}
             for i in range(10)]
    res = stretch_monitor.result(table)
    base = math.log(0.8)
    assert res["questions"] == 10 and res["report_due"] is False
    assert res["submitted"] == pytest.approx(base)
    assert res["applied_gain"]["1.2"] == pytest.approx(math.log(stretched(0.8, 1.2)) - base)
    assert res["applied_gain"]["1.5"] > res["applied_gain"]["1.2"] > 0
    assert res["shadow_gain"]["1.2"] == {"questions": 10, "gain": pytest.approx(math.log(0.85) - base)}
    assert "1.5" not in res["shadow_gain"]
    assert res["loo_picks"] == {"1": 0, "1.2": 0, "1.5": 10} and res["loo_gain"] == pytest.approx(res["applied_gain"]["1.5"])


def test_empty_and_report_due():
    assert stretch_monitor.result([]) == {"questions": 0, "report_at": 30, "report_due": False}
    table = [{"post": i, "p": 0.6, "yes": i % 3 == 0, "shadow": {}} for i in range(30)]
    assert stretch_monitor.result(table)["report_due"] is True


def test_health_line_counts_only_and_warns_at_30():
    line, warn = stretch_monitor.health_line({"questions": 12, "checked_at": "2026-11-16T14:00:00+00:00",
                                              "applied_gain": {"1.2": 0.0123}})
    assert line == "Stretch monitor: 12 of 30 agent-era binary questions resolved (checked 2026-11-16)" and not warn
    line, warn = stretch_monitor.health_line({"questions": 31, "checked_at": "2026-11-30T14:00:00+00:00",
                                              "applied_gain": {"1.2": 0.0123}})
    assert warn and "report due: tell the architect" in line and "0.0123" not in line
    assert stretch_monitor.health_line(None) == ("Stretch monitor: not checked yet (the Scoreboard writes it)", False)
    report = health_check.Report()
    health_check.check_stretch({"questions": 31, "checked_at": "2026-11-30"}, report)
    assert len(report.warnings) == 1


def test_markdown():
    table = [{"post": i, "p": 0.7, "yes": i % 2 == 0, "shadow": {1.5: 0.8}} for i in range(4)]
    text = stretch_monitor.markdown(stretch_monitor.result(table))
    assert "submitted stretched k=1.2" in text and "logged shadow stretch-1.5" in text and "leave-one-out" in text
    assert "No agent-era binary" in stretch_monitor.markdown(stretch_monitor.result([]))
