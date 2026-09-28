"""
Scoreboard: scores every resolved tournament question logged in fall26-data.

- Live forecast, per question type:
  - binary: ln(p) if Yes, ln(1 - p) if No
  - multiple choice: ln(probability we gave the option that happened)
  - numeric / discrete: ln(probability mass our CDF put in the grid bucket the
    answer fell in; below / above the range use the tail mass)
  Higher (closer to 0) is better. These are raw log scores, not Metaculus
  peer scores.
- Shadow variants (binary only, PLAN.md Step 10): the same score on the same
  questions, and the mean difference from live.
Counts per type are shown. The report goes to fall26-data (scoreboard/) and
the job page. Weekly workflow "Scoreboard" plus a manual button.
"""
from __future__ import annotations

import base64
import json
import math
import os
import statistics
from datetime import datetime, timezone
from typing import Any

import requests

from question_log import DATA_REPO

EPS = 1e-6
TYPES = ("binary", "multiple_choice", "numeric", "discrete")


# ---------------------------------------------------------------- scores (pure)


def binary_score(p: float, yes: bool) -> float:
    p = min(max(p, EPS), 1 - EPS)
    return math.log(p) if yes else math.log(1 - p)


def multiple_choice_score(forecast: dict, winning_option: str) -> float | None:
    options = {o["option_name"]: o["probability"] for o in forecast.get("predicted_options", [])}
    if winning_option not in options:
        return None
    return math.log(max(options[winning_option], EPS))


def numeric_score(forecast: dict, resolution: str) -> float | None:
    """ln(mass in the bucket containing the answer) on the declared CDF."""
    points = forecast.get("declared_percentiles") or []
    if len(points) < 2:
        return None
    values = [p["value"] for p in points]
    cdf = [p["percentile"] for p in points]
    if resolution == "below_lower_bound":
        return math.log(max(cdf[0], EPS))
    if resolution == "above_upper_bound":
        return math.log(max(1 - cdf[-1], EPS))
    try:
        answer = float(resolution)
    except ValueError:
        return None
    if answer <= values[0]:
        return math.log(max(cdf[0], EPS))
    if answer > values[-1]:
        return math.log(max(1 - cdf[-1], EPS))
    for i in range(1, len(values)):
        if answer <= values[i]:
            return math.log(max(cdf[i] - cdf[i - 1], EPS))
    return None


def live_score(record: dict, resolution: str) -> float | None:
    question_type = record["question"].get("question_type")
    forecast = record.get("final_forecast")
    if forecast is None:
        return None
    if question_type == "binary":
        if resolution.lower() not in ("yes", "no"):
            return None
        return binary_score(float(forecast), resolution.lower() == "yes")
    if question_type == "multiple_choice":
        return multiple_choice_score(forecast, resolution)
    if question_type in ("numeric", "discrete"):
        return numeric_score(forecast, resolution)
    return None


def report_markdown(scored: list[dict]) -> str:
    """scored: {"type", "live": float, "shadow": {name: float} (binary only)}"""
    lines = ["| Type | Resolved questions | Mean live log score |", "|---|---|---|"]
    for t in ("all", *TYPES):
        rows = [r for r in scored if t == "all" or r["type"] == t]
        if rows:
            lines.append(f"| {t} | {len(rows)} | {statistics.fmean(r['live'] for r in rows):.4f} |")
    binary = [r for r in scored if r["type"] == "binary"]
    variants = sorted({name for r in binary for name in r.get("shadow", {})})
    if variants:
        lines += ["", "**Shadow variants (binary, same questions as live):**", "",
                  "| Variant | Questions | Mean log score | Mean diff vs live |", "|---|---|---|---|"]
        for name in variants:
            pairs = [(r["live"], r["shadow"][name]) for r in binary if name in r.get("shadow", {})]
            lines.append(
                f"| {name} | {len(pairs)} | {statistics.fmean(s for _, s in pairs):.4f} | "
                f"{statistics.fmean(s - live for live, s in pairs):+.4f} |"
            )
    if not scored:
        lines.append("| (none resolved yet) | 0 | |")
    return "\n".join(lines)


# ---------------------------------------------------------------- data


def _github(path: str, token: str, method: str = "GET", body: dict | None = None) -> Any:
    response = requests.request(
        method,
        f"https://api.github.com/repos/{DATA_REPO}/{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json=body,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def latest_submitted_records(token: str) -> list[dict]:
    """One record per question: the latest submitted tournament log."""
    tree = _github("git/trees/HEAD?recursive=1", token)["tree"]
    paths = [t["path"] for t in tree if t["path"].startswith("questions/tournament/") and t["path"].endswith(".json")]
    latest: dict[Any, dict] = {}
    for path in paths:
        record = json.loads(base64.b64decode(_github(f"contents/{path}", token)["content"]))
        post = record.get("question", {}).get("id_of_post")
        if record.get("submitted") and (post not in latest or record.get("started_at", "") > latest[post].get("started_at", "")):
            latest[post] = record
    return list(latest.values())


def score_records(records: list[dict], resolution_of) -> list[dict]:  # type: ignore[no-untyped-def]
    scored = []
    for record in records:
        resolution = resolution_of(record["question"].get("id_of_post"))
        if not resolution:
            continue
        live = live_score(record, resolution)
        if live is None:
            continue
        row = {"type": record["question"].get("question_type"), "live": live, "shadow": {}}
        if row["type"] == "binary":
            yes = resolution.lower() == "yes"
            row["shadow"] = {name: binary_score(float(p), yes) for name, p in (record.get("shadow") or {}).items()}
        scored.append(row)
    return scored


def main() -> None:
    from bot_helpers import silence_noisy_dependencies

    silence_noisy_dependencies()
    from forecasting_tools import MetaculusClient

    token = os.environ["DATA_REPO_TOKEN"]
    client = MetaculusClient()
    cache: dict[Any, str | None] = {}

    def resolution_of(post: Any) -> str | None:
        if post not in cache:
            question = client.get_question_by_post_id(post)
            if isinstance(question, list):
                question = question[0]
            cache[post] = getattr(question, "resolution_string", None)
        return cache[post]

    records = latest_submitted_records(token)
    scored = score_records(records, resolution_of)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report = (
        f"## Scoreboard {stamp}\n\n{len(records)} forecast question(s) logged, "
        f"{len(scored)} resolved and scored.\n\n{report_markdown(scored)}\n"
    )
    print(report)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report)
    path = f"scoreboard/{stamp}.md"
    body = {"message": f"Scoreboard {stamp}", "content": base64.b64encode(report.encode()).decode()}
    try:
        body["sha"] = _github(f"contents/{path}", token)["sha"]
    except requests.HTTPError:
        pass
    _github(f"contents/{path}", token, method="PUT", body=body)


if __name__ == "__main__":
    main()
