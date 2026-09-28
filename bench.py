"""
PLAN.md Step 5: the test bench. Never publishes anything.

1. Picks open main-site Metaculus questions with a visible community
   prediction and at least 30 forecasters (about 60% binary, 20% numeric /
   discrete, 20% multiple choice): 60 ("full") or 30 ("quick"). The list is
   saved in fall26-data and reused.
2. Gathers research once per question (AskNews + free news, no model calls)
   and freezes it in fall26-data, so every config sees identical research.
3. Runs config A (and optionally B) with the bot's own forecasting code, and
   caches each result in fall26-data, so an interrupted run resumes.
4. Scores each forecast's distance from the community prediction (KL
   divergence; lower is better) and reports, overall and per question type:
   the mean KL, and for A vs B the paired mean difference (B - A) with a
   bootstrap 90% confidence interval, plus cost.

Configs are lineups from bot_config ("free", "credits"). "gemini-free" is
refused: the bench must never use the live Gemini quota. Running the same
config twice under two labels shows the noise level.

    poetry run python bench.py --size quick --config-a free --label-a free-1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from bot_helpers import configure_public_logging, silence_noisy_dependencies

silence_noisy_dependencies()

from forecasting_tools import (  # noqa: E402
    ApiFilter,
    BinaryQuestion,
    MetaculusClient,
    MultipleChoiceQuestion,
    NumericDistribution,
    NumericQuestion,
    PredictedOptionList,
)

from gemini_budget import GitHubFileStore, MemoryStore  # noqa: E402
from question_log import DATA_REPO, to_jsonable  # noqa: E402

SIZES = {"mini": 10, "quick": 30, "full": 60}
TYPE_SHARES = {"binary": 0.6, "numeric": 0.1, "discrete": 0.1, "multiple_choice": 0.2}
MIN_FORECASTERS = 30
EPS = 1e-6
BOOTSTRAP_SAMPLES = 2000
ALLOWED_CONFIGS = ("free", "credits")


# ---------------------------------------------------------------- scoring (pure)


def kl_binary(community: float, ours: float) -> float:
    c = min(max(community, EPS), 1 - EPS)
    p = min(max(ours, EPS), 1 - EPS)
    return c * math.log(c / p) + (1 - c) * math.log((1 - c) / (1 - p))


def kl_discrete(community: list[float], ours: list[float]) -> float:
    """KL(community || ours) for two probability lists over the same outcomes."""
    c_total, o_total = sum(community), sum(ours)
    kl = 0.0
    for c, o in zip(community, ours):
        c = max(c / c_total, EPS)
        o = max(o / o_total, EPS)
        kl += c * math.log(c / o)
    return kl


def cdf_to_pmf(cdf: list[float]) -> list[float]:
    """Mass below the range, in each bucket, and above the range."""
    return [cdf[0]] + [max(b - a, 0.0) for a, b in zip(cdf, cdf[1:])] + [max(1 - cdf[-1], 0.0)]


def kl_cdf(community_cdf: list[float], our_cdf: list[float]) -> float:
    return kl_discrete(cdf_to_pmf(community_cdf), cdf_to_pmf(our_cdf))


def bootstrap_ci(differences: list[float], level: float = 0.90, seed: int = 0) -> tuple[float, float]:
    """Bootstrap confidence interval of the mean (fixed seed: reproducible)."""
    if not differences:
        return (math.nan, math.nan)
    rng = random.Random(seed)
    n = len(differences)
    means = sorted(
        statistics.fmean(rng.choice(differences) for _ in range(n)) for _ in range(BOOTSTRAP_SAMPLES)
    )
    low = means[int((1 - level) / 2 * BOOTSTRAP_SAMPLES)]
    high = means[int((1 + level) / 2 * BOOTSTRAP_SAMPLES) - 1]
    return low, high


# ---------------------------------------------------------------- community prediction


def _latest_aggregation(question: Any) -> dict | None:
    aggregations = (question.api_json or {}).get("question", {}).get("aggregations", {})
    for key in ("recency_weighted", "unweighted"):
        latest = (aggregations.get(key) or {}).get("latest")
        if latest:
            return latest
    return None


def community_prediction(question: Any) -> Any:
    """Binary: probability; MC: list in option order; numeric: CDF list. None if hidden."""
    latest = _latest_aggregation(question)
    if not latest:
        return None
    values = latest.get("forecast_values")
    if isinstance(question, BinaryQuestion):
        centers = latest.get("centers")
        return float(centers[0]) if centers else (float(values[1]) if values else None)
    return [float(v) for v in values] if values else None


def score(question: Any, prediction: Any, community: Any) -> float:
    if isinstance(question, BinaryQuestion):
        return kl_binary(community, float(prediction))
    if isinstance(question, MultipleChoiceQuestion):
        ours = {o.option_name: o.probability for o in prediction.predicted_options}
        return kl_discrete(community, [ours.get(option, EPS) for option in question.options])
    ours_cdf = [p.percentile for p in prediction.get_cdf()]
    return kl_cdf(community, ours_cdf)


# ---------------------------------------------------------------- storage


class BenchStore:
    """JSON files in fall26-data under bench/ (or memory, for tests)."""

    def __init__(self, token: str | None = None, memory: dict | None = None) -> None:
        self._token = token
        self._memory = memory

    def _store(self, path: str):  # type: ignore[no-untyped-def]
        if self._memory is not None:
            return _MemoryPath(self._memory, path)
        return GitHubFileStore(DATA_REPO, f"bench/{path}", self._token or "")

    def load(self, path: str) -> Any:
        try:
            return self._store(path).load()
        except Exception:
            return None

    def save(self, path: str, data: Any) -> None:
        self._store(path).save(data)


class _MemoryPath(MemoryStore):
    def __init__(self, memory: dict, path: str) -> None:
        self._memory, self._path = memory, path

    def load(self) -> Any:
        return self._memory.get(self._path)

    def save(self, data: Any) -> None:
        self._memory[self._path] = data


# ---------------------------------------------------------------- the bench


@dataclass
class Scored:
    post_id: int
    question_type: str
    kl: float
    cost: float


def question_counts(size: int) -> dict[str, int]:
    counts = {t: max(1, round(size * share)) for t, share in TYPE_SHARES.items()}
    counts["binary"] += size - sum(counts.values())
    return counts


def blind(question: Any) -> Any:
    """
    A copy of the question for the bot with the community prediction removed:
    the bot must never see it (it's read with Tony's token only for scoring).
    """
    update: dict[str, Any] = {"api_json": {}}
    if isinstance(question, BinaryQuestion):
        update["community_prediction_at_access_time"] = None
    return question.model_copy(update=update)


def question_filter(question_type: str) -> ApiFilter:
    """
    Open main-site questions of one type with at least 30 forecasters. The
    library's community-prediction filter only works for binary questions, so
    for the other types the community prediction is checked after fetching.
    """
    return ApiFilter(
        allowed_statuses=["open"],
        allowed_types=[question_type],
        num_forecasters_gte=MIN_FORECASTERS,
        community_prediction_exists=True if question_type == "binary" else None,
        is_in_main_feed=True,
        group_question_mode="exclude",
    )


def select_questions(client: MetaculusClient, size: int, seed: int = 0) -> list[Any]:
    chosen = []
    for question_type, count in question_counts(size).items():
        api_filter = question_filter(question_type)
        found = asyncio.run(
            client.get_questions_matching_filter(
                api_filter, num_questions=count * 2, randomly_sample=True, error_if_question_target_missed=False
            )
        )
        # The list endpoint can leave out the aggregations: fetch each in full.
        full = [client.get_question_by_post_id(q.id_of_post) for q in found]
        with_cp = [q for q in full if not isinstance(q, list) and community_prediction(q) is not None]
        chosen += with_cp[:count]
    return chosen


def make_bot(config: str):  # type: ignore[no-untyped-def]
    """The live bot with this config's lineup, research frozen, never submitting."""
    import main
    from bot_config import get_lineup

    if config not in ALLOWED_CONFIGS:
        raise SystemExit(f"Config {config!r} not allowed on the bench (never the live Gemini quota).")
    lineup = get_lineup(config)

    class BenchBot(main.FallBot2026):
        frozen_research: dict[int, str] = {}

        async def run_research(self, question):  # type: ignore[no-untyped-def]
            return self.frozen_research[question.id_of_post]

    bot = BenchBot(
        research_reports_per_question=1,
        predictions_per_research_report=lineup.predictions_per_research_report,
        use_research_summary_to_forecast=False,
        enable_summarize_research=False,
        publish_reports_to_metaculus=False,
        skip_previously_forecasted_questions=False,
        llms=lineup.llms,
        required_successful_predictions=0 if lineup.planner else 0.5,
    )
    bot.planner = lineup.planner
    bot.submit_forecasts = False
    bot.question_log = None
    return bot


async def freeze_research(bot, questions: list[Any], store: BenchStore, batch: str) -> dict[int, str]:  # type: ignore[no-untyped-def]
    frozen = {}
    for q in questions:
        path = f"{batch}/research/{q.id_of_post}.json"
        saved = store.load(path)
        if saved is None:
            text = await bot._asknews_with_free_fallback(q, q.question_text)
            saved = {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "text": text}
            store.save(path, saved)
        frozen[q.id_of_post] = saved["text"]
    return frozen


def run_config(config: str, label: str, questions: list[Any], store: BenchStore, batch: str) -> list[Scored]:
    bot = make_bot(config)
    bot.frozen_research = asyncio.run(freeze_research(bot, questions, store, batch))
    scored = []
    for q in questions:
        path = f"{batch}/results/{label}/{q.id_of_post}.json"
        result = store.load(path)
        if result is None:
            [report] = asyncio.run(bot.forecast_questions([blind(q)], return_exceptions=True))
            if isinstance(report, BaseException):
                print(f"Question {q.id_of_post}: no forecast ({type(report).__name__})")
                continue
            result = {
                "kl": score(q, report.prediction, community_prediction(q)),
                "cost": report.price_estimate or 0.0,
                "prediction": to_jsonable(report.prediction),
            }
            store.save(path, result)
        scored.append(Scored(q.id_of_post, q.question_type, float(result["kl"]), float(result["cost"])))
    return scored


def report_markdown(a_label: str, a: list[Scored], b_label: str | None = None, b: list[Scored] | None = None) -> str:
    lines = [f"## Test bench: {a_label}" + (f" vs {b_label}" if b_label else ""), ""]
    types = ["all", "binary", "numeric", "discrete", "multiple_choice"]

    def of_type(rows: list[Scored], t: str) -> list[Scored]:
        return rows if t == "all" else [r for r in rows if r.question_type == t]

    if b is None:
        lines += ["| Type | Questions | Mean KL (lower is better) | Cost (USD) |", "|---|---|---|---|"]
        for t in types:
            rows = of_type(a, t)
            if rows:
                lines.append(f"| {t} | {len(rows)} | {statistics.fmean(r.kl for r in rows):.4f} | {sum(r.cost for r in rows):.4f} |")
        return "\n".join(lines)
    b_by_post = {r.post_id: r for r in b}
    lines += [
        f"Paired difference = KL({b_label}) - KL({a_label}); negative means {b_label} is closer to the community.",
        "",
        "| Type | Pairs | Mean KL A | Mean KL B | Mean diff (B - A) | 90% CI | Cost A | Cost B |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for t in types:
        pairs = [(r, b_by_post[r.post_id]) for r in of_type(a, t) if r.post_id in b_by_post]
        if not pairs:
            continue
        diffs = [rb.kl - ra.kl for ra, rb in pairs]
        low, high = bootstrap_ci(diffs)
        lines.append(
            f"| {t} | {len(pairs)} | {statistics.fmean(ra.kl for ra, _ in pairs):.4f} | "
            f"{statistics.fmean(rb.kl for _, rb in pairs):.4f} | {statistics.fmean(diffs):+.4f} | "
            f"[{low:+.4f}, {high:+.4f}] | {sum(ra.cost for ra, _ in pairs):.4f} | {sum(rb.cost for _, rb in pairs):.4f} |"
        )
    return "\n".join(lines)


def main() -> None:
    configure_public_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", choices=list(SIZES), default="quick")
    parser.add_argument("--config-a", default="free")
    parser.add_argument("--label-a", default=None)
    parser.add_argument("--config-b", default=None)
    parser.add_argument("--label-b", default=None)
    args = parser.parse_args()
    for config in filter(None, (args.config_a, args.config_b)):
        if config not in ALLOWED_CONFIGS:
            sys.exit(f"Config {config!r} not allowed on the bench (never the live Gemini quota).")

    store = BenchStore(token=os.getenv("DATA_REPO_TOKEN"))
    batch = args.size
    size = SIZES[args.size]
    # Community predictions are read with Tony's read-only token (the bot
    # account can't see them). It's used only here, never by the live bot.
    read_token = os.getenv("METACULUS_READ_TOKEN")
    if not read_token:
        sys.exit("METACULUS_READ_TOKEN is not set: the bench can't see community predictions.")
    client = MetaculusClient(token=read_token)
    saved = store.load(f"{batch}/questions.json")
    if saved:
        questions = [client.get_question_by_post_id(p["post_id"]) for p in saved]
    else:
        questions = select_questions(client, size)
        if questions:  # never save an empty list (it would be reused)
            store.save(f"{batch}/questions.json", [{"post_id": q.id_of_post, "type": q.question_type} for q in questions])
    questions = [q for q in questions if community_prediction(q) is not None]
    print(f"Bench {batch}: {len(questions)} questions with a visible community prediction")
    if not questions:
        sys.exit("No questions with a visible community prediction.")

    label_a = args.label_a or args.config_a
    a = run_config(args.config_a, label_a, questions, store, batch)
    b = label_b = None
    if args.config_b:
        label_b = args.label_b or args.config_b
        b = run_config(args.config_b, label_b, questions, store, batch)
    report = report_markdown(label_a, a, label_b, b)
    print(report)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report + "\n")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    store.save(f"{batch}/reports/{stamp}_{label_a}{'_vs_' + label_b if label_b else ''}.json", {"markdown": report})


if __name__ == "__main__":
    main()
