"""
Build 5 (architect, 29 Sep): disagreement follow-up search.

When round 1 disagrees - binary: spread over 15 points; numeric/discrete: the
models' medians differ by more than 25% of the question's range; multiple
choice: the models' top option differs - the Flash-Lite planner reads each
model's two-line reason and writes up to 2 search queries aimed at the
disagreement. They are searched (AskNews while a call is left under the
3-per-question cap, otherwise free news), and a "Follow-up findings" section of
at most 1,500 tokens is added to the dossier, trimmed so the whole stays within
~6,000 tokens. Round 2 then uses the updated dossier. Skipped when the question
closes within 25 minutes. Switch FOLLOWUP_ENABLED (default ON).
"""
from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable

from forecast_safety import median_of, question_range
from free_news import format_articles
from research import MAX_DOSSIER_TOKENS, WORDS_PER_TOKEN, _format_asknews

FOLLOWUP_ENABLED = True
BINARY_SPREAD = 0.15
NUMERIC_SPREAD = 0.25  # of the question's range
MAX_QUERIES = 2
MAX_SECTION_TOKENS = 1500
MIN_MINUTES_TO_CLOSE = 25
SECTION_TITLE = "## Follow-up findings (searched because the forecasters disagreed)"

_ANSWER_LINE = re.compile(r"^(probability|percentile)\b|:\s*[-+$]?\d[\d,.]*\s*%?\s*$", re.IGNORECASE)


def disagreement(question: Any, values: list[Any]) -> str | None:
    """Why round 1 disagrees (counts only, no forecast values), or None."""
    if len(values) < 2:
        return None
    kind = getattr(question, "question_type", None)
    if kind == "binary":
        spread = max(values) - min(values)
        return f"binary spread over {BINARY_SPREAD:.0%}" if spread > BINARY_SPREAD + 1e-9 else None
    if kind in ("numeric", "discrete"):
        medians = [median_of(v.declared_percentiles) for v in values]
        medians = [m for m in medians if m is not None]
        lower, upper = question_range(question)
        if len(medians) < 2 or lower is None or upper is None or upper <= lower:
            return None
        if (max(medians) - min(medians)) / (upper - lower) > NUMERIC_SPREAD:
            return f"medians differ by over {NUMERIC_SPREAD:.0%} of the range"
        return None
    if kind == "multiple_choice":
        tops = {max(v.predicted_options, key=lambda o: o.probability).option_name for v in values}
        return "top option differs" if len(tops) > 1 else None
    return None


def reason_of(reasoning: str) -> str:
    """The last two lines of a model's reasoning before its answer lines."""
    lines = [line.strip() for line in (reasoning or "").splitlines() if line.strip()]
    while lines and _ANSWER_LINE.search(lines[-1]):
        lines.pop()
    return " ".join(lines[-2:])[:400]


def followup_prompt(question_text: str, reasons: list[str]) -> str:
    listing = "\n".join(f"- Forecaster {i + 1}: {r}" for i, r in enumerate(reasons))
    return (
        "Forecasters disagree on this question. Read their short reasons and write up to 2 news "
        "search queries that would settle the disagreement (the facts they weigh differently).\n\n"
        f"Question: {question_text}\n\nReasons:\n{listing}\n\n"
        'Answer only with JSON: {"queries": ["...", "..."]}'
    )


def parse_queries(text: str) -> list[str]:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return []
    try:
        queries = json.loads(match.group(0)).get("queries", [])
    except (ValueError, AttributeError):
        return []
    return [q.strip() for q in queries if isinstance(q, str) and q.strip()][:MAX_QUERIES]


def cap_words(text: str, max_words: int) -> str:
    words = text.split()
    return text if len(words) <= max_words else " ".join(words[:max_words]) + " [...]"


def with_followup(dossier: str, findings: str) -> str:
    """The dossier (trimmed) plus the follow-up section (at most 1,500 tokens),
    within ~6,000 tokens in all."""
    section = f"{SECTION_TITLE}\n{cap_words(findings, int(MAX_SECTION_TOKENS * WORDS_PER_TOKEN))}"
    room = int(MAX_DOSSIER_TOKENS * WORDS_PER_TOKEN) - len(section.split()) - 1
    return f"{cap_words(dossier, room)}\n\n{section}"


async def run_followup(
    question_text: str,
    reasons: list[str],
    invoke_helper: Callable[[str], Awaitable[str]],
    asknews_left: int,
    search_asknews: Callable[[str], Awaitable[list[Any]]],
    search_free: Callable[[str], list[Any]],
) -> tuple[str, dict]:
    """(findings text, detail for the question log: counts only)."""
    queries = parse_queries(await invoke_helper(followup_prompt(question_text, reasons)))
    detail: dict = {"queries": queries, "asknews_calls": 0, "asknews_articles": 0, "free_articles": 0}
    blocks = []
    for query in queries:
        found: list[Any] = []
        if asknews_left > 0:
            asknews_left -= 1
            detail["asknews_calls"] += 1
            try:
                found = await search_asknews(query)
            except Exception:
                found = []
                asknews_left = 0  # down (e.g. 402): free news for the rest
            if found:
                detail["asknews_articles"] += len(found)
                blocks.append(f"### {query}\n{_format_asknews(found)}")
                continue
        articles = search_free(query)
        if articles:
            detail["free_articles"] += len(articles)
            blocks.append(f"### {query}\n{format_articles(articles)}")
    return "\n\n".join(blocks), detail
