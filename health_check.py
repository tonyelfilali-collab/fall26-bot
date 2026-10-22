"""
Daily health check (health.yml, 07:00 UK time). Fails the run (red, so GitHub
emails Tony) if:
- a seasonal or MiniBench question closed in the last 24 hours without our
  forecast (missed: the bot never guesses, so a question with no real model
  forecast by its close is skipped);
- an open seasonal or MiniBench question closes within 60 minutes without our
  forecast;
- there has been no successful tournament run for 3 hours;
- any workflow is not "active" (e.g. paused for inactivity), named.
Warns (yellow, not red) if MiniBench has had no open question for 3 days
(e.g. a new round the bot can't find), if any Gemini model has less than 20% of its daily
quota left (07:00 UK is the end of Google's quota day, so this is normal on
busy days), if AskNews still returns 402, if any tournament run in the last
24 hours could not save a question's JSON log or the quota ledger, or if main's
last commit is older than 21 days (standing rule: a notes PR at least every
21 days, so GitHub never pauses the workflows).
Claude (frontier lineup, claude_health.py; architect, 21 Oct): red on a
credit error this cycle or a Claude ledger that can't be read; warns if a
live run in the last 24 hours was not in Claude mode "full", the agent was
switched off by an overrun, over 20% of Claude calls failed, or no live
question got a Claude forecast while questions were forecast. The report
ends with the day's Claude numbers ($ per question, agent searches / fetches,
dear agent calls, ledger total = sum of calls), then the Claude ledger $ per
UTC day for the last 7 days and the cycle total (to compare with the
Anthropic Console). On Mondays (UTC) it adds our standing in the Fall
tournament and the current MiniBench round (standings.py; never red).

    poetry run python health_check.py [--simulate-miss] [--cron "0 6 * * *"]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from bot_helpers import silence_noisy_dependencies

silence_noisy_dependencies()

from bot_config import (  # noqa: E402
    GEMINI_DAILY_LIMITS,
    GEMINI_LEDGER_PATH,
    GEMINI_QUOTA_BUCKETS,
)
import claude_health  # noqa: E402
import standings  # noqa: E402
from gemini_budget import GitHubFileStore, quota_day  # noqa: E402
from comment_post import COMMENT_FAILED_ANNOTATION  # noqa: E402
from question_log import DATA_REPO, LOG_FAILURE_ANNOTATION  # noqa: E402

TOURNAMENT_WORKFLOW = "run_bot_on_tournament.yaml"
CLOSING_SOON = timedelta(minutes=60)
MAX_TIME_WITHOUT_SUCCESS = timedelta(hours=3)
MIN_QUOTA_LEFT = 0.2
MINIBENCH_QUIET_LIMIT = timedelta(days=3)
WATCHED_ANNOTATIONS = (LOG_FAILURE_ANNOTATION, "quota-ledger-failed", COMMENT_FAILED_ANNOTATION)
# summer (BST) -> 06:00 UTC, winter (GMT) -> 07:00 UTC
_UK_7AM_CRON = {timedelta(hours=1): "0 6 * * *", timedelta(0): "0 7 * * *"}


@dataclass
class Report:
    red: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ok: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)  # the day's Claude numbers (not checks)
    claude_days: list[str] = field(default_factory=list)  # Claude ledger $ per UTC day + cycle total
    standings: list[str] = field(default_factory=list)  # Mondays: our rank and score (never red)


def is_uk_7am_trigger(cron: str, now: datetime) -> bool:
    offset = now.astimezone(ZoneInfo("Europe/London")).utcoffset()
    return _UK_7AM_CRON.get(offset) == cron


# ---------------------------------------------------------------- checks


def check_open_questions(questions_by_tournament: dict, now: datetime, report: Report) -> None:
    missing = []
    checked = 0
    for tournament, questions in questions_by_tournament.items():
        for q in questions:
            checked += 1
            if q.close_time is None or q.already_forecasted:
                continue
            close = q.close_time if q.close_time.tzinfo else q.close_time.replace(tzinfo=timezone.utc)
            if close - now <= CLOSING_SOON:
                minutes = int((close - now).total_seconds() // 60)
                missing.append(f"{tournament} question {q.id_of_post} closes in {minutes} min without our forecast")
    if missing:
        report.red += missing
    else:
        report.ok.append(f"No open question closes within 60 minutes without our forecast ({checked} open)")


def check_missed_questions(missed_by_tournament: dict, report: Report) -> None:
    missed = [
        f"{tournament} question {q.id_of_post} closed without our forecast (missed)"
        for tournament, questions in missed_by_tournament.items()
        for q in questions
        if not q.already_forecasted
    ]
    if missed:
        report.red += missed
    else:
        report.ok.append("No question closed without our forecast in the last 24 hours")


def check_recent_success(last_success: datetime | None, now: datetime, report: Report) -> None:
    if last_success is None:
        report.red.append("No successful tournament run found")
    elif now - last_success > MAX_TIME_WITHOUT_SUCCESS:
        hours = (now - last_success).total_seconds() / 3600
        report.red.append(f"No successful tournament run for {hours:.1f} hours")
    else:
        minutes = int((now - last_success).total_seconds() // 60)
        report.ok.append(f"Last successful tournament run {minutes} min ago")


def check_minibench_activity(recently_open: int, report: Report) -> None:
    """recently_open: MiniBench questions open at some point in the last 3 days."""
    if recently_open == 0:
        report.warnings.append(
            "MiniBench has had no open questions for 3 days (new round not found, or a pause)"
        )
    else:
        report.ok.append(f"MiniBench had {recently_open} open question(s) in the last 3 days")


def check_gemini_quota(ledger: dict | None, today: str, report: Report) -> None:
    saved = (ledger or {}).get("used", {}) if (ledger or {}).get("day") == today else {}
    # Per quota bucket (a model sharing another's quota counts against it).
    used: dict[str, int] = {}
    for model, count in saved.items():
        bucket = GEMINI_QUOTA_BUCKETS.get(model, model)
        used[bucket] = used.get(bucket, 0) + int(count)
    low = []
    for bucket, limit in GEMINI_DAILY_LIMITS.items():
        left = limit - used.get(bucket, 0)
        if left < MIN_QUOTA_LEFT * limit:
            low.append(f"{bucket.removeprefix('gemini/')} {max(left, 0)}/{limit}")
    if low:
        report.warnings.append("Gemini quota below 20%: " + ", ".join(low))
    else:
        report.ok.append("Every Gemini model has at least 20% of its daily quota left")


def check_asknews_status(status: int | None, report: Report) -> None:
    if status == 402:
        report.warnings.append("AskNews still returns 402 (Payment Required); using free news sources")
    elif status is None or status >= 400:
        report.warnings.append(f"AskNews check failed ({status or 'no answer'})")
    else:
        report.ok.append("AskNews answers")


ACCOUNT_BALANCE_ALERT = 4.00  # USD; under it Tony adds $10 (architect, 22 Oct)


def openrouter_account() -> tuple[int, dict | None]:
    """OpenRouter's ACCOUNT credits (all keys): HTTP status and data (numbers only)."""
    try:  # a warning, never red, if it can't be read
        response = requests.get(
            "https://openrouter.ai/api/v1/credits",
            headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"}, timeout=30,
        )
        return response.status_code, (response.json().get("data") if response.status_code == 200 else None)
    except Exception:
        return 0, None


def check_account_balance(status: int, data: dict | None, report: Report, today: str, notify=None) -> None:  # type: ignore[no-untyped-def]
    if data is None:
        report.warnings.append(f"OpenRouter account balance not readable (HTTP {status})")
        return
    balance = float(data["total_credits"]) - float(data["total_usage"])
    line = f"OpenRouter account balance ${balance:.2f} (bought ${float(data['total_credits']):.2f}, used ${float(data['total_usage']):.2f})"
    if balance >= ACCOUNT_BALANCE_ALERT:
        report.ok.append(line)
        return
    # The daily health check runs once a day; the issue title carries the date (once a UTC day).
    from ensemble import open_alert_issue

    notify = notify or open_alert_issue
    opened = notify(f"OpenRouter account balance under ${ACCOUNT_BALANCE_ALERT:.0f} ({today})",
                    f"{line}. Under ${ACCOUNT_BALANCE_ALERT:.2f}: Tony adds $10 (architect, 22 Oct). Never turn on auto top-up.")
    report.warnings.append(f"{line}: under ${ACCOUNT_BALANCE_ALERT:.2f}, Tony adds $10"
                           + ("" if opened else " (alert issue not opened)"))


def check_log_failures(annotation_titles: list[str], report: Report) -> None:
    comments = [t for t in annotation_titles if t == COMMENT_FAILED_ANNOTATION]
    if comments:
        # Comments are required for prizes (architect, 22 Oct).
        report.warnings.append(f"{len(comments)} private comment post failure(s) in the last 24 hours (comment-failed)")
    failures = [t for t in annotation_titles if t in WATCHED_ANNOTATIONS and t != COMMENT_FAILED_ANNOTATION]
    if failures:
        report.warnings.append(
            f"{len(failures)} JSON log / quota ledger save failure(s) in the last 24 hours"
        )
    else:
        report.ok.append("No JSON log or quota ledger save failures in the last 24 hours")


# ---------------------------------------------------------------- data sources


def _github(path: str) -> dict:
    response = requests.get(
        f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/{path}",
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def latest_success(filtered: list[dict], recent: list[dict]) -> datetime | None:
    """The newest successful run in either list. GitHub's "status=success"
    list sometimes answers with an old run (6 and 7 Oct: "no successful run
    for 11.6 / 190.5 hours" while runs succeeded every 10 min), so the plain
    list of recent runs is checked too."""
    times = [_parse_time(r["updated_at"]) for r in filtered if r.get("updated_at")]
    times += [_parse_time(r["updated_at"]) for r in recent if r.get("conclusion") == "success" and r.get("updated_at")]
    return max(times) if times else None


def last_successful_tournament_run() -> datetime | None:
    base = f"actions/workflows/{TOURNAMENT_WORKFLOW}/runs"
    filtered = _github(f"{base}?status=success&per_page=1")["workflow_runs"]
    recent = _github(f"{base}?per_page=30")["workflow_runs"]
    return latest_success(filtered, recent)


_RUNS: dict[str, list[dict]] = {}
_JOBS: dict[int, list[dict]] = {}


def recent_runs(since: datetime) -> list[dict]:
    """Tournament runs created since `since`, every page (a day has ~144);
    kept for the run, so the log-failure and Claude checks share one listing."""
    key = f"{since:%Y-%m-%dT%H:%M:%SZ}"
    if key not in _RUNS:
        runs: list[dict] = []
        for page in range(1, 6):
            found = _github(
                f"actions/workflows/{TOURNAMENT_WORKFLOW}/runs?per_page=100&page={page}&created=>={key}"
            )["workflow_runs"]
            runs += found
            if len(found) < 100:
                break
        _RUNS[key] = runs
    return _RUNS[key]


def run_jobs(run_id: int) -> list[dict]:
    if run_id not in _JOBS:
        _JOBS[run_id] = _github(f"actions/runs/{run_id}/jobs")["jobs"]
    return _JOBS[run_id]


def recent_annotation_titles(since: datetime) -> list[str]:
    titles = []
    for run in recent_runs(since):
        for job in run_jobs(run["id"]):
            for annotation in _github(f"check-runs/{job['id']}/annotations"):
                titles.append(annotation.get("title") or "")
    return titles


def job_log(job_id: int) -> str:
    """One job's plain-text log (GitHub answers with a redirect to it)."""
    response = requests.get(
        f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/actions/jobs/{job_id}/logs",
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"},
        timeout=60,
    )
    response.raise_for_status()
    return response.text


def frontier_runs(since: datetime) -> list[dict]:
    """Completed live runs since `since` that printed the frontier line, as
    {"mode", "reason", "overruns"} (nothing else is read from the logs)."""
    found = []
    for run in recent_runs(since):
        if run.get("status") != "completed":
            continue
        for job in run_jobs(run["id"]):
            parsed = claude_health.run_from_log(job_log(job["id"]))
            if parsed is not None:
                found.append(parsed)
                break
    return found


def check_claude(now: datetime, report: Report) -> None:
    """The Claude checks and the day's Claude numbers (claude_health.py)."""
    from bot_config import CLAUDE_BUDGET_PER_CYCLE, CLAUDE_CYCLE_RESET_DATE
    from frontier import cycle_start, reset_date

    token = os.environ["DATA_REPO_TOKEN"]
    ledger = claude_health.load_claude_ledger(token)
    if not claude_health.check_claude_ledger(ledger, now.date(), reset_date(CLAUDE_CYCLE_RESET_DATE), report):
        return
    assert ledger is not None
    report.claude_days += claude_health.claude_days_lines(
        ledger, now.date(), cycle_start(now.date(), reset_date(CLAUDE_CYCLE_RESET_DATE)), CLAUDE_BUDGET_PER_CYCLE
    )
    days = claude_health.window_days(now)
    runs = frontier_runs(now - timedelta(hours=24))
    records = claude_health.recent_question_logs(token, now - timedelta(hours=24))
    claude_health.check_claude_modes(runs, report)
    claude_health.check_agent_overrun(ledger, runs, days, report)
    claude_health.check_claude_failures(ledger, days, report)
    claude_health.check_claude_forecasts(records, len(runs), report)
    report.info += claude_health.claude_report(ledger, days, claude_health.kinds_by_key(records))


def workflows() -> list[dict]:
    return _github("actions/workflows?per_page=100")["workflows"]


def check_workflow_states(found: list[dict], report: Report) -> None:
    """Red if any workflow isn't active (a paused workflow can stop the bot).
    'deleted' (its file was removed) can't run or be re-enabled: not counted."""
    bad = [f"{w.get('name')} ({w.get('state')})" for w in found if w.get("state") not in ("active", "deleted")]
    if bad:
        report.red.append(
            "Workflow(s) not active: " + ", ".join(bad) + ". Re-enable: run Keepalive, or the Actions tab"
        )
    else:
        report.ok.append(f"All {sum(w.get('state') == 'active' for w in found)} workflows active")


MAIN_ACTIVITY_DAYS = 21


def main_last_commit() -> datetime:
    return _parse_time(_github("commits/main")["commit"]["committer"]["date"])


def check_main_activity(last_commit: datetime, now: datetime, report: Report) -> None:
    age = now - last_commit
    if age > timedelta(days=MAIN_ACTIVITY_DAYS):
        report.warnings.append(
            f"main's last commit is {age.days} days old (rule: a notes PR at least every {MAIN_ACTIVITY_DAYS} days, "
            "so GitHub never pauses the workflows)"
        )
    else:
        report.ok.append(f"main's last commit is {age.days} day(s) old")


def open_questions(minibench_id: int | str) -> dict:
    from forecasting_tools import MetaculusClient

    from main import FALL_2026_TOURNAMENT_ID

    client = MetaculusClient()
    return {
        "seasonal": client.get_all_open_questions_from_tournament(FALL_2026_TOURNAMENT_ID),
        "MiniBench": client.get_all_open_questions_from_tournament(minibench_id),
    }


def minibench_recently_open(minibench_id: int | str, now: datetime) -> int:
    """MiniBench questions (current round and the slug's) open at some point in the last 3 days."""
    from forecasting_tools import ApiFilter, MetaculusClient

    from minibench import MINIBENCH_SLUG

    client = MetaculusClient()
    seen = set()
    for tournament in dict.fromkeys([minibench_id, MINIBENCH_SLUG]):
        api_filter = ApiFilter(
            allowed_tournaments=[tournament],
            allowed_statuses=["open", "closed", "resolved"],
            close_time_gt=now - MINIBENCH_QUIET_LIMIT,
            open_time_lt=now,
            group_question_mode="unpack_subquestions",
        )
        questions = asyncio.run(
            client.get_questions_matching_filter(api_filter, error_if_question_target_missed=False)
        )
        seen |= {q.id_of_question or q.id_of_post for q in questions}
    return len(seen)


def missed_questions(since: datetime, minibench_id: int | str) -> dict:
    """Tournament questions that closed since `since` (ours or not)."""
    from forecasting_tools import ApiFilter, MetaculusClient

    from main import FALL_2026_TOURNAMENT_ID

    client = MetaculusClient()
    missed = {}
    for label, tournament in (("seasonal", FALL_2026_TOURNAMENT_ID), ("MiniBench", minibench_id)):
        api_filter = ApiFilter(
            allowed_tournaments=[tournament],
            allowed_statuses=["closed", "resolved"],
            close_time_gt=since,
            # (The library ignores is_previously_forecasted_by_user=False, so
            # questions we forecast are filtered out in check_missed_questions.)
            group_question_mode="unpack_subquestions",
        )
        missed[label] = asyncio.run(
            client.get_questions_matching_filter(api_filter, error_if_question_target_missed=False)
        )
    return missed


def asknews_status() -> int | None:
    from asknews_sdk import AsyncAskNewsSDK

    async def one_search() -> int:
        async with AsyncAskNewsSDK(api_key=os.getenv("ASKNEWS_API_KEY"), scopes={"news"}) as ask:
            await ask.news.search_news(query="Metaculus forecasting", n_articles=1, strategy="latest news")
        return 200

    try:
        return asyncio.run(one_search())
    except Exception as e:
        current: BaseException | None = e
        while current is not None:
            status = getattr(getattr(current, "response", None), "status_code", None)
            if isinstance(status, int):
                return status
            current = current.__cause__ or current.__context__
        return None


def add_standings(now: datetime, minibench_id: int | str, report: Report, store=None, get=None) -> None:
    """Mondays: our Fall and MiniBench standing (standings.py). Fails soft:
    one line "standings: unavailable", never red."""
    try:
        report.standings += standings.standings_lines(
            now.date(),
            [("Fall", standings.FALL_PROJECT_ID), ("MiniBench", minibench_id)],
            store if store is not None else standings.store(os.environ["DATA_REPO_TOKEN"]),
            get or standings.metaculus_get,
        )
    except Exception:
        report.standings.append(standings.UNAVAILABLE)


# ---------------------------------------------------------------- main


def run(simulate_miss: bool) -> Report:
    now = datetime.now(timezone.utc)
    report = Report()
    from minibench import MINIBENCH_SLUG, current_minibench_id

    try:
        minibench_id = current_minibench_id()
    except Exception:
        minibench_id = MINIBENCH_SLUG
    checks = [
        ("missed questions", lambda: check_missed_questions(missed_questions(now - timedelta(hours=24), minibench_id), report)),
        ("open questions", lambda: check_open_questions(open_questions(minibench_id), now, report)),
        ("MiniBench activity", lambda: check_minibench_activity(minibench_recently_open(minibench_id, now), report)),
        ("recent runs", lambda: check_recent_success(last_successful_tournament_run(), now, report)),
        (
            "Gemini quota",
            lambda: check_gemini_quota(
                GitHubFileStore(DATA_REPO, GEMINI_LEDGER_PATH, os.environ["DATA_REPO_TOKEN"]).load(),
                quota_day(),
                report,
            ),
        ),
        ("AskNews", lambda: check_asknews_status(asknews_status(), report)),
        ("log failures", lambda: check_log_failures(recent_annotation_titles(now - timedelta(hours=24)), report)),
        ("workflow states", lambda: check_workflow_states(workflows(), report)),
        ("main activity", lambda: check_main_activity(main_last_commit(), now, report)),
        ("Claude", lambda: check_claude(now, report)),
        ("OpenRouter account", lambda: check_account_balance(*openrouter_account(), report, now.date().isoformat())),
    ]
    for name, check in checks:
        try:
            check()
        except Exception as e:
            # A check that can't run is itself a problem worth an email.
            report.red.append(f"Could not run the {name} check ({type(e).__name__})")
    if standings.is_monday(now.date()):
        add_standings(now, minibench_id, report)
    if simulate_miss:
        report.red.append("SIMULATED: seasonal question 0 closes in 30 min without our forecast")
    return report


def publish(report: Report) -> None:
    lines = ["## Daily health check", ""]
    lines += [f"- ❌ {p}" for p in report.red]
    lines += [f"- ⚠️ {w}" for w in report.warnings]
    lines += [f"- ✅ {o}" for o in report.ok]
    if report.info:
        lines += ["", "### Claude, last 24 hours (ledger: UTC today and yesterday)", "", *report.info]
    if report.claude_days:
        lines += ["", "### Claude ledger per UTC day (compare with the Anthropic Console)", "", *report.claude_days]
    if report.standings:
        lines += ["", "### Our standing (Mondays)", "", *report.standings]
    text = "\n".join(lines)
    print(text)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    for problem in report.red:
        print(f"::error title=health-check::{problem}")
    for warning in report.warnings:
        print(f"::warning title=health-check::{warning}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulate-miss", action="store_true")
    parser.add_argument("--cron", default="", help="github.event.schedule, for scheduled runs")
    args = parser.parse_args()
    if args.cron and not is_uk_7am_trigger(args.cron, datetime.now(timezone.utc)):
        print("Not 07:00 UK time for this trigger (summer/winter time); nothing to do.")
        return
    report = run(args.simulate_miss)
    publish(report)
    if report.red:
        sys.exit(1)


if __name__ == "__main__":
    main()
