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
from gemini_budget import GitHubFileStore, quota_day  # noqa: E402
from question_log import DATA_REPO, LOG_FAILURE_ANNOTATION  # noqa: E402

TOURNAMENT_WORKFLOW = "run_bot_on_tournament.yaml"
CLOSING_SOON = timedelta(minutes=60)
MAX_TIME_WITHOUT_SUCCESS = timedelta(hours=3)
MIN_QUOTA_LEFT = 0.2
MINIBENCH_QUIET_LIMIT = timedelta(days=3)
WATCHED_ANNOTATIONS = (LOG_FAILURE_ANNOTATION, "quota-ledger-failed")
# summer (BST) -> 06:00 UTC, winter (GMT) -> 07:00 UTC
_UK_7AM_CRON = {timedelta(hours=1): "0 6 * * *", timedelta(0): "0 7 * * *"}


@dataclass
class Report:
    red: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ok: list[str] = field(default_factory=list)


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


def check_log_failures(annotation_titles: list[str], report: Report) -> None:
    failures = [t for t in annotation_titles if t in WATCHED_ANNOTATIONS]
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


def last_successful_tournament_run() -> datetime | None:
    runs = _github(f"actions/workflows/{TOURNAMENT_WORKFLOW}/runs?status=success&per_page=1")["workflow_runs"]
    return _parse_time(runs[0]["updated_at"]) if runs else None


def recent_annotation_titles(since: datetime) -> list[str]:
    titles = []
    runs = _github(
        f"actions/workflows/{TOURNAMENT_WORKFLOW}/runs?per_page=100&created=>={since:%Y-%m-%dT%H:%M:%SZ}"
    )["workflow_runs"]
    for run in runs:
        for job in _github(f"actions/runs/{run['id']}/jobs")["jobs"]:
            for annotation in _github(f"check-runs/{job['id']}/annotations"):
                titles.append(annotation.get("title") or "")
    return titles


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
    ]
    for name, check in checks:
        try:
            check()
        except Exception as e:
            # A check that can't run is itself a problem worth an email.
            report.red.append(f"Could not run the {name} check ({type(e).__name__})")
    if simulate_miss:
        report.red.append("SIMULATED: seasonal question 0 closes in 30 min without our forecast")
    return report


def publish(report: Report) -> None:
    lines = ["## Daily health check", ""]
    lines += [f"- ❌ {p}" for p in report.red]
    lines += [f"- ⚠️ {w}" for w in report.warnings]
    lines += [f"- ✅ {o}" for o in report.ok]
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
