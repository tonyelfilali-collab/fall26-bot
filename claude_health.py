"""
Health and daily report for the Claude parts of the frontier lineup
(architect, 21 Oct; Tier A: reads only, never on the forecast path).

Sources (all read-only):
- the Claude ledger, fall26-data status/claude_spend.json (frontier.ClaudeBudget):
  per-question calls (model, ok, charged, http_status, agent searches /
  fetches), days, credit_errors {cycle: day}, alerts {kind: day};
- the run logs of the live tournament workflow: the public line "Frontier
  lineup: <mode> (<why>); ..." printed once per run, and the warning "agent
  over its reservation" (health_check.py downloads them);
- the question logs, fall26-data questions/tournament/<date>/ (seasonal or
  MiniBench per question, and which model each finished forecast came from).
The ledger's calls carry no time, so "the last 24 hours" there means
questions last charged on today's or yesterday's UTC date. Test Bot keys
("<post>-test-<run id>") are left out of every live check.

health_check.py: warning when a live run was not in mode "full", the agent
was switched off by an overrun, over 20% of Claude calls failed, or no live
question got a Claude forecast while questions were forecast; red on a
credit error this cycle or a ledger that can't be read. claude_report: the
daily numbers (health report and the Scoreboard). Numbers and labels only.
"""
from __future__ import annotations

import base64
import json
import re
import statistics
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

from question_log import DATA_REPO
from scoreboard import forecast_model, model_group

CLAUDE_SPEND_PATH = "status/claude_spend.json"  # frontier.CLAUDE_SPEND_PATH
AGENT_MODEL = "anthropic/claude-opus-5-5-agent"  # frontier.AGENT
MAX_FAILED_SHARE = 0.20
AGENT_DEAR_CALL = 1.00  # dollars: agent calls over this are listed
TOTALS_TOLERANCE = 0.01  # dollars: ledger days total vs sum of call charges
FRONTIER_LINE = re.compile(r"Frontier lineup: (full|low|minimal|off) \((.*?)\); GPT")
OVERRUN_LINE = "agent over its reservation"


def is_live_key(key: str) -> bool:
    return "-test-" not in str(key)


def window_days(now: datetime) -> set[str]:
    """Today's and yesterday's UTC dates (the ledger keeps days, not times)."""
    return {now.date().isoformat(), (now - timedelta(days=1)).date().isoformat()}


def live_recent(ledger: dict, days: set[str] | None) -> dict[str, dict]:
    """Live question entries last charged on one of `days` (None: every day)."""
    return {
        key: entry for key, entry in (ledger.get("questions") or {}).items()
        if is_live_key(key) and (days is None or entry.get("last") in days)
    }


def _calls(entries: dict[str, dict]) -> list[tuple[str, dict]]:
    return [(key, call) for key, entry in entries.items() for call in entry.get("calls") or []]


def _is_agent(call: dict) -> bool:
    return call.get("model") == AGENT_MODEL or "searches" in call


# ---------------------------------------------------------------- checks (pure)


def check_claude_ledger(ledger: dict | None, today: date, reset: date | None, report: Any) -> bool:
    """Red if the ledger can't be read or there was a credit error this cycle.
    Returns whether the ledger can be used by the other checks."""
    from frontier import cycle_start

    if ledger is None:
        report.red.append("Claude ledger (status/claude_spend.json) could not be read")
        return False
    cycle = cycle_start(today, reset)
    errors = ledger.get("credit_errors") or {}
    if cycle is not None and cycle.isoformat() in errors:
        report.red.append(
            f"Claude credit error (credit balance too low) on {errors[cycle.isoformat()]}: "
            f"no Claude calls until the next cycle"
        )
    elif cycle is None:
        report.warnings.append("Claude billing cycle reset date not set (Claude off)")
    else:
        report.ok.append("No Claude credit error this cycle")
    return True


def check_claude_modes(runs: list[dict], report: Any) -> None:
    """runs: live runs that printed the frontier line, {"mode", "reason"}."""
    if not runs:
        report.ok.append("No live run with the frontier lineup in the last 24 hours")
        return
    other = Counter((r["mode"], r["reason"]) for r in runs if r["mode"] != "full")
    if other:
        parts = [f"{mode} ({reason}) x{n}" for (mode, reason), n in sorted(other.items())]
        report.warnings.append(
            f"{sum(other.values())} of {len(runs)} live run(s) not in Claude mode full: " + ", ".join(parts)
        )
    else:
        report.ok.append(f"All {len(runs)} live run(s) in Claude mode full")


def check_agent_overrun(ledger: dict, runs: list[dict], days: set[str], report: Any) -> None:
    """The agent switched off by an overrun: the ledger's "agent-overrun" alert
    day, or runs that logged the overrun warning."""
    alerted = (ledger.get("alerts") or {}).get("agent-overrun")
    logged = sum(r.get("overruns", 0) for r in runs)
    if alerted in days or logged:
        when = f"alert {alerted}" if alerted in days else "no alert in the ledger"
        report.warnings.append(
            f"Agent switched off by an overrun (over its reservation): {logged} in run logs, {when}"
        )
    else:
        report.ok.append("No agent overrun in the last 24 hours")


def check_claude_failures(ledger: dict, days: set[str], report: Any) -> None:
    calls = [call for _, call in _calls(live_recent(ledger, days))]
    failed = [c for c in calls if not c.get("ok")]
    if calls and len(failed) > MAX_FAILED_SHARE * len(calls):
        by_status = Counter(str(c.get("http_status") or "no status") for c in failed)
        report.warnings.append(
            f"{len(failed)} of {len(calls)} Claude calls failed ({len(failed) / len(calls):.0%}), by HTTP status: "
            + ", ".join(f"{s} x{n}" for s, n in sorted(by_status.items()))
        )
    else:
        report.ok.append(f"Claude calls failed: {len(failed)} of {len(calls)}")


def _forecast_models(record: dict) -> list[str]:
    return [name for entry in record.get("forecasts") or []
            if entry.get("status") == "ok" and (name := forecast_model(entry))]


def check_claude_forecasts(records: list[dict], frontier_runs: int, report: Any) -> None:
    """Warning if questions were forecast in the last 24 h but none got a
    Claude forecast. records: the live question logs of the last 24 hours."""
    forecast = [r for r in records if _forecast_models(r)]
    if not frontier_runs or not forecast:
        report.ok.append(f"Claude forecasts: nothing to check ({len(forecast)} question log(s) with a forecast)")
        return
    with_claude = [r for r in forecast if any(model_group(m) in ("agent", "claude") for m in _forecast_models(r))]
    kinds = Counter("seasonal" if r.get("seasonal") else "MiniBench" for r in forecast)
    counts = ", ".join(f"{k} {n}" for k, n in sorted(kinds.items()))
    if not with_claude:
        report.warnings.append(f"No live question got a Claude forecast, though {len(forecast)} were forecast ({counts})")
    else:
        report.ok.append(f"{len(with_claude)} of {len(forecast)} forecast question log(s) had a Claude forecast")


# ---------------------------------------------------------------- daily numbers (pure)


def kinds_by_key(records: list[dict]) -> dict[str, str]:
    """Question key (post id) -> 'seasonal' / 'MiniBench', from question logs."""
    kinds = {}
    for record in records:
        post = (record.get("question") or {}).get("id_of_post")
        if post is not None and record.get("seasonal") is not None:
            kinds[str(post)] = "seasonal" if record["seasonal"] else "MiniBench"
    return kinds


def claude_report(ledger: dict, days: set[str] | None, kinds: dict[str, str]) -> list[str]:
    """The Claude numbers: $ per question (seasonal, MiniBench), agent searches
    and fetches per call, agent calls over $1, and ledger days total vs the
    sum of call charges. Live questions only, except the totals line (every
    key: the days count Test Bot calls too)."""
    entries = live_recent(ledger, days)
    lines = ["| Kind | Questions | Claude $ | $ per question (median, max) |", "|---|---|---|---|"]
    per_kind: dict[str, list[float]] = {}
    for key, entry in entries.items():
        spent = sum(c.get("charged", 0.0) for c in entry.get("calls") or [])
        if spent > 0:
            per_kind.setdefault(kinds.get(key, "unknown"), []).append(spent)
    for kind in ("seasonal", "MiniBench", "unknown"):
        values = per_kind.get(kind)
        if values:
            lines.append(f"| {kind} | {len(values)} | ${sum(values):.4f} | "
                         f"${sum(values) / len(values):.4f} (${statistics.median(values):.4f}, ${max(values):.4f}) |")
    if not per_kind:
        lines.append("| (no live Claude spend) | 0 | $0.0000 | - |")
    agent = [(key, c) for key, c in _calls(entries) if _is_agent(c)]
    lines.append("")
    if agent:
        searches = [int(c.get("searches") or 0) for _, c in agent]
        fetches = [int(c.get("fetches") or 0) for _, c in agent]
        lines.append(f"Agent calls: {len(agent)}; searches per call median {statistics.median(searches):g}, max "
                     f"{max(searches)}; fetches per call median {statistics.median(fetches):g}, max {max(fetches)}")
    else:
        lines.append("Agent calls: 0")
    dear = sorted({key for key, c in agent if c.get("charged", 0.0) > AGENT_DEAR_CALL})
    count = sum(c.get("charged", 0.0) > AGENT_DEAR_CALL for _, c in agent)
    lines.append(f"Agent calls over ${AGENT_DEAR_CALL:.2f}: {count}" + (f" (questions {', '.join(dear)})" if dear else ""))
    days_total = sum(float(v) for v in (ledger.get("days") or {}).values())
    calls_total = sum(c.get("charged", 0.0) for _, c in _calls(ledger.get("questions") or {}))
    match = abs(days_total - calls_total) <= TOTALS_TOLERANCE
    lines.append(f"Claude ledger total = sum of calls: days ${days_total:.4f}, calls ${calls_total:.4f}: "
                 + ("OK" if match else "**MISMATCH**"))
    return lines


LEDGER_DAYS_SHOWN = 7


def claude_days_lines(ledger: dict, today: date, cycle: date | None, budget: float,
                      shown: int = LEDGER_DAYS_SHOWN) -> list[str]:
    """The ledger's Claude $ per UTC day (the last `shown` days, today
    included) and the cycle total, to compare with the Anthropic Console.
    Dollars only (every key, Test Bot included, as the Console counts it)."""
    days = {str(k): float(v) for k, v in (ledger.get("days") or {}).items()}
    lines = ["| UTC day | Claude $ |", "|---|---|"]
    for back in range(shown - 1, -1, -1):
        day = (today - timedelta(days=back)).isoformat()
        lines.append(f"| {day} | ${days.get(day, 0.0):.4f} |")
    lines.append(f"| last {shown} days | ${sum(days.get((today - timedelta(days=b)).isoformat(), 0.0) for b in range(shown)):.4f} |")
    lines.append("")
    if cycle is None:
        lines.append("Cycle total: n/a (billing cycle reset date not set)")
        return lines
    total = sum(v for d, v in days.items() if cycle.isoformat() <= d <= today.isoformat())
    line = f"Cycle total (since {cycle.isoformat()}): ${total:.4f} of ${budget:.2f}"
    saved = (ledger.get("cycles") or {}).get(cycle.isoformat())
    if saved is not None and abs(float(saved) - total) > TOTALS_TOLERANCE:
        line += f" (ledger cycle field: ${float(saved):.4f})"
    lines.append(line)
    return lines


# ---------------------------------------------------------------- data (fall26-data)


def _data(path: str, token: str) -> Any:
    response = requests.get(
        f"https://api.github.com/repos/{DATA_REPO}/{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def load_claude_ledger(token: str) -> dict | None:
    """The Claude ledger, or None if it is missing or can't be read."""
    from gemini_budget import GitHubFileStore

    try:
        return GitHubFileStore(DATA_REPO, CLAUDE_SPEND_PATH, token).load()
    except Exception:
        return None


def log_time(path: str) -> datetime | None:
    """questions/tournament/<YYYY-MM-DD>/<post>_<HHMMSS>.json -> its UTC time."""
    parts = path.split("/")
    if len(parts) != 4 or parts[3].endswith("_shadows.json"):
        return None
    try:
        stamp = parts[3].removesuffix(".json").rsplit("_", 1)[1]
        return datetime.strptime(f"{parts[2]} {stamp}", "%Y-%m-%d %H%M%S").replace(tzinfo=timezone.utc)
    except (IndexError, ValueError):
        return None


def recent_question_logs(token: str, since: datetime) -> list[dict]:
    """The live tournament question logs saved since `since`."""
    tree = _data("git/trees/HEAD?recursive=1", token)["tree"]
    paths = [
        t["path"] for t in tree
        if t["path"].startswith("questions/tournament/") and (when := log_time(t["path"])) is not None and when >= since
    ]
    return [json.loads(base64.b64decode(_data(f"contents/{path}", token)["content"])) for path in paths]


def run_from_log(text: str) -> dict | None:
    """{"mode", "reason", "overruns"} from one run's log, or None when the run
    never reached the frontier line (e.g. BOT_ENABLED off)."""
    match = FRONTIER_LINE.search(text)
    if match is None:
        return None
    return {"mode": match.group(1), "reason": match.group(2), "overruns": text.count(OVERRUN_LINE)}
