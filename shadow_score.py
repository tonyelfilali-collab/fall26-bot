"""
PLAN.md Step 10: score shadow variants against the live forecast on resolved
binary questions (log score: ln p if Yes, ln(1 - p) if No; higher is better).

Reads the per-question JSON logs in fall26-data (questions/tournament/...),
looks up each question's resolution, and reports per variant: questions
scored, mean log score, and the mean difference from live on the same
questions. Manual workflow "Shadow scores".

    poetry run python shadow_score.py
"""
from __future__ import annotations

import base64
import json
import math
import os
import statistics
from typing import Any

import requests

from question_log import DATA_REPO

EPS = 1e-6


def log_score(probability: float, outcome_yes: bool) -> float:
    p = min(max(probability, EPS), 1 - EPS)
    return math.log(p) if outcome_yes else math.log(1 - p)


def score_table(rows: list[dict]) -> str:
    """rows: {"post_id", "outcome": bool, "live": float, "shadow": {name: float}}"""
    variants = sorted({name for r in rows for name in r["shadow"]})
    lines = [
        "| Forecast | Questions | Mean log score | Mean diff vs live (same questions) |",
        "|---|---|---|---|",
    ]
    if rows:
        live = [log_score(r["live"], r["outcome"]) for r in rows]
        lines.append(f"| live | {len(rows)} | {statistics.fmean(live):.4f} | |")
    for name in variants:
        pairs = [(r, r["shadow"][name]) for r in rows if name in r["shadow"]]
        scores = [log_score(p, r["outcome"]) for r, p in pairs]
        diffs = [log_score(p, r["outcome"]) - log_score(r["live"], r["outcome"]) for r, p in pairs]
        lines.append(f"| {name} | {len(pairs)} | {statistics.fmean(scores):.4f} | {statistics.fmean(diffs):+.4f} |")
    return "\n".join(lines)


def _github(path: str, token: str) -> Any:
    response = requests.get(
        f"https://api.github.com/repos/{DATA_REPO}/{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def load_records(token: str) -> list[dict]:
    tree = _github("git/trees/HEAD?recursive=1", token)["tree"]
    paths = [t["path"] for t in tree if t["path"].startswith("questions/tournament/") and t["path"].endswith(".json")]
    records = []
    for path in paths:
        content = _github(f"contents/{path}", token)["content"]
        records.append(json.loads(base64.b64decode(content)))
    return records


def main() -> None:
    from bot_helpers import silence_noisy_dependencies

    silence_noisy_dependencies()
    from forecasting_tools import MetaculusClient

    token = os.environ["DATA_REPO_TOKEN"]
    client = MetaculusClient()
    rows = []
    outcomes: dict[int, bool | None] = {}
    # A question can have several logs (e.g. a failed run, then a retry): keep
    # the latest submitted one.
    latest: dict[int, dict] = {}
    for record in load_records(token):
        post = record.get("question", {}).get("id_of_post")
        if record.get("submitted") and (post not in latest or record.get("started_at", "") > latest[post].get("started_at", "")):
            latest[post] = record
    for record in latest.values():
        question = record.get("question", {})
        post = question.get("id_of_post")
        if question.get("question_type") != "binary" or not record.get("submitted") or not record.get("shadow"):
            continue
        if post not in outcomes:
            fresh = client.get_question_by_post_id(post)
            resolution = (getattr(fresh, "resolution_string", None) or "").lower()
            outcomes[post] = True if resolution == "yes" else False if resolution == "no" else None
        if outcomes[post] is None:
            continue
        rows.append({"post_id": post, "outcome": outcomes[post], "live": float(record["final_forecast"]), "shadow": record["shadow"]})
    table = score_table(rows)
    report = f"## Shadow scores (resolved binary questions: {len(rows)})\n\n{table}\n"
    print(report)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report)


if __name__ == "__main__":
    main()
