"""
Credits readiness, 4b (architect, 29 Sep): the real cost table.

$ per question, per spending tier and question type =
  real OpenRouter prices (public model list, as in preflight.py)
  x prompt tokens MEASURED from our question logs: each logged question is
    rebuilt and the bot's own forecast prompt (with its real dossier) is
    captured by a recorder (no model call), then counted with the cl100k
    tokenizer,
  + an ASSUMED high-reasoning output of 8,000 tokens per forecast.
Research, planner, parser and summarizer calls are not counted: in the
credits lineup they run on the free Flash-Lite pool (4c).

Binary: round 1 only, and round 1 + round 2 (round 2 runs only when round 1
disagrees or is extreme). Numeric, discrete and multiple choice: every
forecaster at once (the same models as binary with round 2).

    DATA_REPO_TOKEN=... poetry run python cost_table.py
"""
from __future__ import annotations

import asyncio
import base64
import os
import statistics
import sys
from typing import Any

import requests
from forecasting_tools import (
    BinaryQuestion,
    DiscreteQuestion,
    GeneralLlm,
    MultipleChoiceQuestion,
    NumericQuestion,
)

import ensemble
from preflight import fetch_models, per_million
from question_log import DATA_REPO
from replay import REPLAY_RESEARCH, recorded_reply

ASSUMED_OUTPUT_TOKENS = 8000  # ASSUMPTION: high reasoning + answer, per forecast
TYPES = ("binary", "numeric", "discrete", "multiple_choice")
_CLASSES = {
    "binary": BinaryQuestion,
    "numeric": NumericQuestion,
    "discrete": DiscreteQuestion,
    "multiple_choice": MultipleChoiceQuestion,
}
MIN_RESEARCH_CHARS = 200  # frozen replay research etc. is not a real dossier


def count_tokens(text: str) -> int:
    try:
        import tiktoken

        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:
        return len(text) // 4


class _Recorder(GeneralLlm):
    """Keeps the prompt and answers in the prompt's own format; no model call."""

    def __init__(self, question: Any) -> None:
        super().__init__(model="replay/recorder", temperature=None)
        self.question = question
        self.prompts: list[str] = []

    async def invoke(self, prompt: Any, system_prompt: str | None = None) -> str:
        self.prompts.append(prompt if isinstance(prompt, str) else str(prompt))
        return recorded_reply(self.question)


def forecast_prompt(question: Any, research: str) -> str:
    """The bot's forecast prompt for this question and research, as sent."""
    import main

    recorder = _Recorder(question)
    bot = main.FallBot2026(
        llms={"default": recorder, "parser": recorder, "summarizer": recorder, "researcher": "no_research"},
        publish_reports_to_metaculus=False,
    )
    if isinstance(question, BinaryQuestion):
        run = bot._run_forecast_on_binary(question, research)
    elif isinstance(question, MultipleChoiceQuestion):
        run = bot._run_forecast_on_multiple_choice(question, research)
    else:
        run = bot._run_forecast_on_numeric(question, research)
    try:
        asyncio.run(run)
    except Exception:
        pass  # only the prompt matters
    return recorder.prompts[0]


def prompt_tokens_from_records(records: list[dict]) -> dict[str, list[int]]:
    """Prompt tokens per question type, one value per question (its latest log
    with a real dossier)."""
    latest: dict[Any, dict] = {}
    for record in records:
        research = ((record.get("research") or {}).get("text")) or ""
        question = record.get("question") or {}
        if len(research) < MIN_RESEARCH_CHARS or research.startswith(REPLAY_RESEARCH):
            continue
        if question.get("question_type") not in _CLASSES:
            continue
        key = question.get("id_of_post")
        if key not in latest or (record.get("started_at") or "") > (latest[key].get("started_at") or ""):
            latest[key] = record
    tokens: dict[str, list[int]] = {t: [] for t in TYPES}
    for record in latest.values():
        snapshot = record["question"]
        kind = snapshot["question_type"]
        try:
            question = _CLASSES[kind].model_validate(snapshot)
            tokens[kind].append(count_tokens(forecast_prompt(question, record["research"]["text"])))
        except Exception:
            continue
    return tokens


def load_records(token: str, repo: str = DATA_REPO) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    tree = requests.get(f"https://api.github.com/repos/{repo}/git/trees/HEAD?recursive=1", headers=headers, timeout=60)
    tree.raise_for_status()
    paths = [t["path"] for t in tree.json()["tree"] if t["path"].startswith("questions/") and t["path"].endswith(".json")]
    records = []
    for path in paths:
        response = requests.get(f"https://api.github.com/repos/{repo}/contents/{path}", headers=headers, timeout=60)
        if response.ok:
            body = response.json()
            if body.get("content"):
                import json

                records.append(json.loads(base64.b64decode(body["content"])))
    return records


def forecast_cost(model: str, prompt_tokens: float, prices: dict[str, tuple[float, float]]) -> float:
    """Dollars for one forecast: prompt tokens + the assumed output."""
    prompt_per_m, completion_per_m = prices[model.removeprefix("openrouter/")]
    return (prompt_tokens * prompt_per_m + ASSUMED_OUTPUT_TOKENS * completion_per_m) / 1_000_000


def cost_table(prompt_tokens: dict[str, float], prices: dict[str, tuple[float, float]]) -> dict[str, dict[str, float]]:
    """tier -> column -> dollars per question. Columns: binary (round 1),
    binary+r2, numeric, discrete, multiple_choice."""
    table: dict[str, dict[str, float]] = {}
    for name, tier in ensemble.TIERS.items():
        row = {}
        row["binary"] = sum(forecast_cost(m, prompt_tokens["binary"], prices) for m in tier.binary_round1)
        row["binary+r2"] = sum(forecast_cost(m, prompt_tokens["binary"], prices) for m in tier.all_forecasters)
        for kind in ("numeric", "discrete", "multiple_choice"):
            row[kind] = sum(forecast_cost(m, prompt_tokens[kind], prices) for m in tier.all_forecasters)
        table[name] = row
    return table


def prices_from(models: dict[str, dict]) -> dict[str, tuple[float, float]]:
    return {
        model_id: (per_million(info["pricing"]["prompt"]) or 0.0, per_million(info["pricing"]["completion"]) or 0.0)
        for model_id, info in models.items()
        if "pricing" in info
    }


def render(measured: dict[str, list[int]], used: dict[str, float], table: dict[str, dict[str, float]]) -> str:
    lines = ["## Prompt tokens per forecast (measured from question logs)", "",
             "| Type | Questions measured | Median prompt tokens | Used |", "|---|---|---|---|"]
    for kind in TYPES:
        values = measured.get(kind) or []
        lines.append(
            f"| {kind} | {len(values)} | {int(statistics.median(values)) if values else '-'} | {int(used[kind])} |"
        )
    lines += ["", f"Output per forecast: **{ASSUMED_OUTPUT_TOKENS:,} tokens (ASSUMED)**, high reasoning.", "",
              "## $ per question (real prices x measured prompt + assumed output)", "",
              "| Tier | Binary (round 1) | Binary + round 2 | Numeric | Discrete | Multiple choice |",
              "|---|---|---|---|---|---|"]
    for name, row in table.items():
        lines.append(
            f"| {name} | ${row['binary']:.2f} | ${row['binary+r2']:.2f} | ${row['numeric']:.2f} "
            f"| ${row['discrete']:.2f} | ${row['multiple_choice']:.2f} |"
        )
    return "\n".join(lines)


def main() -> int:
    measured = prompt_tokens_from_records(load_records(os.environ["DATA_REPO_TOKEN"]))
    all_values = [v for values in measured.values() for v in values]
    overall = statistics.median(all_values) if all_values else 3000
    # A type with no measured question uses the median over all types.
    used = {kind: (statistics.median(measured[kind]) if measured[kind] else overall) for kind in TYPES}
    table = cost_table(used, prices_from(fetch_models()))
    out = render(measured, used, table)
    print(out)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(out + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
