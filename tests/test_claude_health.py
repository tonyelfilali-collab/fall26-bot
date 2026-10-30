"""Claude health and daily numbers (claude_health.py, health_check.check_claude):
fake run logs, fake Claude ledgers and fake question logs. No network."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from types import SimpleNamespace

import claude_health
import health_check
import scoreboard

NOW = datetime(2026, 10, 21, 6, 0, tzinfo=timezone.utc)
DAYS = claude_health.window_days(NOW)  # 2026-10-21, 2026-10-20
RESET = date(2026, 11, 18)  # cycle 2026-10-18 .. 2026-11-17
AGENT = claude_health.AGENT_MODEL
OPUS = "anthropic/claude-opus-5-5"
LOG_FULL = "2026-10-21T05:00:01.0Z Frontier lineup: full (98% of the cycle budget left); GPT-5.6 Sol off (switched off); agent on\n"


def call(model=OPUS, ok=True, charged=0.3, **extra):
    return {"model": model, "ok": ok, "charged": charged, "cost_known": ok, "output_tokens": 900, **extra}


def ledger(questions=None, days=None, **extra):
    questions = questions or {}
    if days is None:
        days = {"2026-10-21": sum(c["charged"] for e in questions.values() for c in e.get("calls", []))}
    return {"days": days, "questions": questions, "alerts": {}, "cycles": {"2026-10-18": 1.0}, "credit_errors": {}, **extra}


def entry(*calls, last="2026-10-21"):
    return {"spent": sum(c["charged"] for c in calls), "last": last, "calls": list(calls)}


def record(post, seasonal, *models):
    return {"question": {"id_of_post": post}, "seasonal": seasonal,
            "forecasts": [{"status": "ok", "forecast_model": m} for m in models]}


# ---------------------------------------------------------------- red


def test_unreadable_ledger_is_red():
    report = health_check.Report()
    assert claude_health.check_claude_ledger(None, NOW.date(), RESET, report) is False
    assert any("could not be read" in r for r in report.red)


def test_credit_error_this_cycle_is_red():
    report = health_check.Report()
    claude_health.check_claude_ledger(ledger(credit_errors={"2026-10-18": "2026-10-20"}), NOW.date(), RESET, report)
    assert any("credit balance too low" in r and "2026-10-20" in r for r in report.red)


def test_credit_error_of_an_earlier_cycle_is_not_red():
    report = health_check.Report()
    assert claude_health.check_claude_ledger(ledger(credit_errors={"2026-09-18": "2026-10-01"}), NOW.date(), RESET, report)
    assert not report.red and report.ok


def test_check_claude_red_when_ledger_missing(monkeypatch):
    monkeypatch.setenv("DATA_REPO_TOKEN", "x")
    monkeypatch.setattr(claude_health, "load_claude_ledger", lambda token: None)
    report = health_check.Report()
    health_check.check_claude(NOW, report)
    assert any("Claude ledger" in r for r in report.red)


# ---------------------------------------------------------------- AskNews 402s


def test_asknews_402s_counted_per_question_from_the_run_logs():
    log = (LOG_FULL
           + "Question 46201: AskNews failed (HTTP 402), using free news sources\n"
           + "Question 46202: AskNews failed (HTTP 402), using free news sources\n"
           + "Question 46203: AskNews failed, using free news sources\n"
           + "Question 46278: articles found: 23 (AskNews 23 in 3 call(s), free news 0); 3 queries\n"
           + "Question 46279: articles found: 6 (AskNews 0 in 1 call(s), free news 6); 3 queries\n")
    run = claude_health.run_from_log(log)
    assert (run["asknews_402"], run["asknews_other_failed"], run["asknews_found"]) == (2, 1, 1)
    report = SimpleNamespace(ok=[], warnings=[], red=[])
    claude_health.check_asknews_runs([run, claude_health.run_from_log(LOG_FULL)], report)
    assert report.warnings == ["AskNews, last 24 hours: 402 on 2 question(s) in 1 run(s); other failures 1; "
                               "articles found on 1 question(s) (free news used instead)"] and not report.red
    clean = SimpleNamespace(ok=[], warnings=[], red=[])
    claude_health.check_asknews_runs([claude_health.run_from_log(LOG_FULL)], clean)
    assert clean.ok and not clean.warnings


# ---------------------------------------------------------------- run modes


def test_frontier_line_parsed_from_a_run_log():
    assert claude_health.run_from_log("no frontier here\n") is None
    run = claude_health.run_from_log(LOG_FULL + "## Frontier lineup: calls this run\n")
    assert run == {"mode": "full", "reason": "98% of the cycle budget left", "overruns": 0,
                   "asknews_402": 0, "asknews_other_failed": 0, "asknews_found": 0}
    low = claude_health.run_from_log("Frontier lineup: low (20% of the cycle budget left); GPT-5.6 Sol off (frontier off)")
    assert low["mode"] == "low"


def test_runs_not_in_full_mode_warn_with_mode_and_why():
    runs = [{"mode": "full", "reason": "98% left"}, {"mode": "off", "reason": "Claude spend ledger could not be read"},
            {"mode": "off", "reason": "Claude spend ledger could not be read"}]
    report = health_check.Report()
    claude_health.check_claude_modes(runs, report)
    assert report.warnings == ["2 of 3 live run(s) not in Claude mode full: off (Claude spend ledger could not be read) x2"]


def test_all_full_is_ok():
    report = health_check.Report()
    claude_health.check_claude_modes([{"mode": "full", "reason": "98% left"}], report)
    assert not report.warnings and report.ok


# ---------------------------------------------------------------- agent overrun


def test_agent_overrun_alert_in_the_ledger_warns():
    report = health_check.Report()
    claude_health.check_agent_overrun(ledger(alerts={"agent-overrun": "2026-10-20"}), [], DAYS, report)
    assert any("overrun" in w and "alert 2026-10-20" in w for w in report.warnings)


def test_agent_overrun_in_a_run_log_warns():
    run = claude_health.run_from_log(LOG_FULL + "Question 46200: agent over its reservation ($2.4100); agent off for this run\n")
    assert run["overruns"] == 1
    report = health_check.Report()
    claude_health.check_agent_overrun(ledger(), [run], DAYS, report)
    assert any("1 in run logs" in w for w in report.warnings)


def test_old_overrun_alert_is_ok():
    report = health_check.Report()
    claude_health.check_agent_overrun(ledger(alerts={"agent-overrun": "2026-10-10"}), [], DAYS, report)
    assert not report.warnings


# ---------------------------------------------------------------- failed calls


def test_over_20_percent_failed_warns_by_http_status():
    questions = {
        "46201": entry(call(), call(ok=False, charged=0.0, http_status=529), call(ok=False, charged=0.0, http_status=529)),
        "46202": entry(call(), call(ok=False, charged=0.36)),
        # Test Bot keys and old questions are left out.
        "46201-test-999": entry(*[call(ok=False, charged=0.0, http_status=400)] * 5),
        "46100": entry(*[call(ok=False, charged=0.0, http_status=500)] * 5, last="2026-10-15"),
    }
    report = health_check.Report()
    claude_health.check_claude_failures(ledger(questions), DAYS, report)
    assert report.warnings == ["3 of 5 Claude calls failed (60%), by HTTP status: 529 x2, no status x1"]


def test_few_failures_are_ok():
    questions = {"46201": entry(*[call()] * 9, call(ok=False, charged=0.0, http_status=529))}
    report = health_check.Report()
    claude_health.check_claude_failures(ledger(questions), DAYS, report)
    assert not report.warnings and report.ok == ["Claude calls failed: 1 of 10"]


# ---------------------------------------------------------------- Claude forecasts


def test_no_claude_forecast_while_questions_were_forecast_warns():
    records = [record(46201, True, "gemini/gemini-3.6-flash"), record(46202, False, "openrouter/google/gemini-3.6-flash")]
    report = health_check.Report()
    claude_health.check_claude_forecasts(records, frontier_runs=10, report=report)
    assert report.warnings == ["No live question got a Claude forecast, though 2 were forecast (MiniBench 1, seasonal 1)"]


def test_a_claude_forecast_is_ok():
    records = [record(46201, True, AGENT, "gemini/gemini-3.6-flash"), record(46202, False, "gemini/gemini-3.6-flash")]
    report = health_check.Report()
    claude_health.check_claude_forecasts(records, frontier_runs=10, report=report)
    assert not report.warnings


def test_no_frontier_runs_or_no_forecasts_nothing_to_check():
    report = health_check.Report()
    claude_health.check_claude_forecasts([record(1, True, "gemini/gemini-3.6-flash")], frontier_runs=0, report=report)
    claude_health.check_claude_forecasts([], frontier_runs=5, report=report)
    assert not report.warnings


def test_log_time_reads_the_path():
    assert claude_health.log_time("questions/tournament/2026-10-20/46201_101500.json") == datetime(
        2026, 10, 20, 10, 15, tzinfo=timezone.utc)
    assert claude_health.log_time("questions/tournament/2026-10-20/46201_101500_shadows.json") is None
    assert claude_health.log_time("questions/test_questions/x.json") is None


# ---------------------------------------------------------------- daily numbers


def daily_ledger():
    questions = {
        "46201": entry(call(AGENT, charged=1.20, searches=6, fetches=1), call(charged=0.30)),
        "46202": entry(call(AGENT, charged=0.40, searches=2, fetches=0), call(charged=0.20)),
        "46300": entry(call(AGENT, charged=0.50, searches=3, fetches=2)),
        "46201-test-7": entry(call(AGENT, charged=2.00, searches=8, fetches=3)),
    }
    return ledger(questions)


def test_daily_report_numbers():
    kinds = claude_health.kinds_by_key([record(46201, True), record(46202, True), record(46300, False)])
    lines = claude_health.claude_report(daily_ledger(), DAYS, kinds)
    text = "\n".join(lines)
    assert "| seasonal | 2 | $2.1000 | $1.0500 ($1.0500, $1.5000) |" in text
    assert "| MiniBench | 1 | $0.5000 | $0.5000 ($0.5000, $0.5000) |" in text
    assert "Agent calls: 3; searches per call median 3, max 6; fetches per call median 1, max 2" in text
    assert "Agent calls over $1.00: 1 (questions 46201)" in text
    # The totals line counts every key (Test Bot calls are in the days too).
    assert "Claude ledger total = sum of calls: days $4.6000, calls $4.6000: OK" in text


def test_daily_report_flags_a_total_mismatch_and_unknown_kind():
    data = daily_ledger()
    data["days"] = {"2026-10-21": 3.0}
    text = "\n".join(claude_health.claude_report(data, DAYS, {}))
    assert "days $3.0000, calls $4.6000: **MISMATCH**" in text
    assert "| unknown | 3 |" in text


def test_daily_report_with_no_spend():
    text = "\n".join(claude_health.claude_report(ledger(), DAYS, {}))
    assert "no live Claude spend" in text and "Agent calls: 0" in text and "OK" in text


def test_scoreboard_claude_section(monkeypatch):
    monkeypatch.setattr(claude_health, "load_claude_ledger", lambda token: daily_ledger())
    text = scoreboard.claude_section("x", [record(46201, True), record(46202, True), record(46300, False)])
    assert "Claude (ledger, last 7 days)" in text and "| seasonal | 2 |" in text
    monkeypatch.setattr(claude_health, "load_claude_ledger", lambda token: None)
    assert "could not be read" in scoreboard.claude_section("x", [])


# ---------------------------------------------------------------- the whole Claude check


def test_check_claude_end_to_end(monkeypatch):
    monkeypatch.setenv("DATA_REPO_TOKEN", "x")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setattr(claude_health, "load_claude_ledger", lambda token: daily_ledger())
    monkeypatch.setattr(claude_health, "recent_question_logs",
                        lambda token, since: [record(46201, True, AGENT), record(46300, False, "gemini/gemini-3.6-flash")])
    runs = [{"id": 1, "status": "completed"}, {"id": 2, "status": "completed"}, {"id": 3, "status": "in_progress"}]
    logs = {11: LOG_FULL, 21: "Frontier lineup: low (20% of the cycle budget left); GPT-5.6 Sol off (frontier off)"}
    monkeypatch.setattr(health_check, "recent_runs", lambda since: runs)
    monkeypatch.setattr(health_check, "run_jobs", lambda run_id: [{"id": run_id * 10 + 1, "runner_name": "GitHub Actions 1"}])
    monkeypatch.setattr(health_check, "job_log", lambda job_id: logs[job_id])
    report = health_check.Report()
    health_check.check_claude(NOW, report)
    assert not report.red
    assert report.warnings == ["1 of 2 live run(s) not in Claude mode full: low (20% of the cycle budget left) x1"]
    assert any(line.startswith("Claude ledger total") for line in report.info)


def test_publish_shows_the_claude_numbers(capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    report = health_check.Report(info=["Agent calls: 0"])
    health_check.publish(report)
    assert "### Claude, last 24 hours" in capsys.readouterr().out


def test_recent_runs_reads_every_page(monkeypatch):
    pages = {1: [{"id": i} for i in range(100)], 2: [{"id": 100}]}
    seen = []

    def fake(path):
        page = int(path.split("&page=")[1].split("&")[0])
        seen.append(page)
        return {"workflow_runs": pages.get(page, [])}

    monkeypatch.setattr(health_check, "_github", fake)
    monkeypatch.setattr(health_check, "_RUNS", {})
    assert len(health_check.recent_runs(NOW)) == 101 and seen == [1, 2]


@pytest.mark.parametrize("key,live", [("46201", True), ("46201-test-39664210635", False)])
def test_test_keys_are_not_live(key, live):
    assert claude_health.is_live_key(key) is live


def test_average_claude_dollars_by_type():
    records = [record(46201, True), record(46202, True), record(46300, False)]
    records[0]["question"]["question_type"] = "binary"
    records[1]["question"]["question_type"] = "discrete"
    types = claude_health.types_by_key(records)
    assert types == {"46201": "seasonal binary", "46202": "seasonal numeric+discrete", "46300": "MiniBench"}
    text = "\n".join(claude_health.claude_type_lines(daily_ledger(), DAYS, types))
    assert "| seasonal binary | 1 | $1.5000 | $1.5000 |" in text
    assert "| seasonal numeric+discrete | 1 | $0.6000 | $0.6000 |" in text
    assert "| MiniBench | 1 | $0.5000 | $0.5000 |" in text and "seasonal MC" not in text
    assert "no live Claude spend" in "\n".join(claude_health.claude_type_lines(ledger({}), DAYS, {}))
