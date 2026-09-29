"""
PLAN.md Step 7: better research.

1. A planner (the parser model: Gemini Flash-Lite on the free key, never the
   forecasting quota) writes up to 3 search queries and lists the facts that
   decide the question.
2. AskNews: one search per query, at most 3 AskNews calls per question (the
   free tier allows 1 call per 10 s, so calls are spaced). One call is kept
   back for the gap-fill. If AskNews fails (e.g. 402) or finds fewer than 3
   articles, the free sources (Google News RSS + GDELT) search the same
   queries instead.
3. A dossier writer (the parser model again) turns the articles into a dossier
   of at most ~6,000 tokens: current status/value with dates, what must happen
   to resolve, base rates, key uncertainties. No market prices.
4. Gap-fill: one extra search only if the dossier writer says a key fact is
   missing (and it's appended to the dossier).
If the planner or dossier writer fails, the articles themselves are used.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from bot_helpers import PUBLIC_LOGGER_NAME
from free_news import collect_free_news, format_articles
from llm_throttle import RequestPacer

logger = logging.getLogger(PUBLIC_LOGGER_NAME)

MAX_QUERIES = 3
MAX_ASKNEWS_CALLS = 3
MIN_ARTICLES = 3
ASKNEWS_ARTICLES_PER_CALL = 8
MAX_DOSSIER_TOKENS = 6000
WORDS_PER_TOKEN = 0.75  # rough: 6,000 tokens ~ 4,500 words
# Lines mentioning market prices or crowd forecasts are removed from the dossier.
_MARKET_WORDS = re.compile(
    r"polymarket|kalshi|manifold|predictit|betting odds|prediction market|"
    r"community prediction|metaculus (community|forecast)",
    re.IGNORECASE,
)
# AskNews free tier: 1 call every 10 seconds.
_asknews_pacer = RequestPacer(requests_per_minute=5)


@dataclass
class ResearchPlan:
    queries: list[str]
    key_facts: list[str] = field(default_factory=list)
    # Build 6a: up to 3 entities for Wikipedia background.
    entities: list[str] = field(default_factory=list)


@dataclass
class ResearchResult:
    dossier: str
    queries: list[str]
    asknews_calls: int = 0
    asknews_articles: int = 0
    free_articles: int = 0
    gap_filled: bool = False
    dossier_written: bool = False
    current_value: CurrentValue | None = None
    wikipedia: list[str] = field(default_factory=list)  # page titles used
    # Replay lab: the dossier's parts, so a variant can drop one.
    base_dossier: str = ""
    wikipedia_section: str = ""

    @property
    def articles(self) -> int:
        return self.asknews_articles + self.free_articles


# ---------------------------------------------------------------- planner


def _json_from_text(text: str) -> Any:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(match.group(0)) if match else None


def parse_plan(text: str, fallback_query: str) -> ResearchPlan:
    """Read the planner's JSON answer; fall back to the question itself."""
    try:
        data = _json_from_text(text) or {}
    except (json.JSONDecodeError, TypeError):
        data = {}
    queries = [str(q).strip() for q in data.get("queries", []) if str(q).strip()][:MAX_QUERIES]
    facts = [str(f).strip() for f in data.get("key_facts", []) if str(f).strip()]
    entities = [str(e).strip() for e in data.get("entities", []) if str(e).strip()][:3]
    return ResearchPlan(queries=queries or [fallback_query], key_facts=facts, entities=entities)


def planner_prompt(question_text: str, resolution_criteria: str, dates: str) -> str:
    return (
        "You plan news research for a forecaster. Do not forecast.\n\n"
        f"Question: {question_text}\n\nResolution criteria: {resolution_criteria}\n\n{dates}\n\n"
        f"Write up to {MAX_QUERIES} short news search queries (keywords, not sentences) that would "
        "find the facts deciding this question, and list those key facts. Also name up to 3 "
        "people, organisations, places or things central to the question, as English Wikipedia "
        "article titles.\n"
        'Answer with JSON only: {"queries": ["...", "..."], "key_facts": ["...", "..."], '
        '"entities": ["...", "..."]}'
    )


# ---------------------------------------------------------------- dossier


CURRENT_VALUE_LINE = "CURRENT VALUE:"


def dossier_prompt(
    question_text: str,
    resolution_criteria: str,
    dates: str,
    key_facts: list[str],
    articles: str,
    unit: str | None = None,
) -> str:
    facts = "\n".join(f"- {f}" for f in key_facts) or "- (none listed)"
    # Numeric/discrete questions: a structured current-value line for the unit check.
    value_line = (
        f"Also include, on its own line, the latest known value of the quantity in the "
        f"question's unit ({unit}): '{CURRENT_VALUE_LINE} <number> | UNIT: {unit} | DATE: <YYYY-MM-DD>' "
        f"(a plain number, no words like 'million'), or '{CURRENT_VALUE_LINE} unknown'.\n"
        if unit is not None
        else ""
    )
    return (
        "You write a research dossier for a forecaster. Do not forecast and do not give probabilities.\n\n"
        f"Question: {question_text}\n\nResolution criteria: {resolution_criteria}\n\n{dates}\n\n"
        f"Key facts to establish:\n{facts}\n\nArticles:\n{articles}\n\n"
        "Write the dossier with these sections, citing dates:\n"
        "## Current status (latest value or state, with dates)\n"
        "## What must happen for the question to resolve Yes / a given value\n"
        "## Base rates and history\n"
        "## Key uncertainties\n"
        "Do NOT mention prediction markets, betting odds or crowd forecasts.\n"
        + value_line
        + f"Keep it under {int(MAX_DOSSIER_TOKENS * WORDS_PER_TOKEN * 0.8)} words.\n"
        "Last line: 'MISSING: <one search query for the most important fact the articles don't cover>' "
        "or 'MISSING: none'."
    )


@dataclass
class CurrentValue:
    value: float
    unit: str
    date: str


def parse_current_value(text: str) -> CurrentValue | None:
    """The dossier's 'CURRENT VALUE: <number> | UNIT: <unit> | DATE: <date>' line, if any."""
    match = re.search(
        r"CURRENT VALUE:\s*\$?\s*([-+]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*\|\s*UNIT:\s*([^|\n]*?)\s*\|\s*DATE:\s*([^\s|]+)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return CurrentValue(value=value, unit=match.group(2).strip(), date=match.group(3).strip())


def split_missing(dossier: str) -> tuple[str, str | None]:
    """Separate the dossier from its final 'MISSING:' line."""
    match = re.search(r"^\s*MISSING:\s*(.+?)\s*$", dossier, re.IGNORECASE | re.MULTILINE)
    if not match:
        return dossier.strip(), None
    missing = match.group(1).strip().strip("'\"<>")
    body = (dossier[: match.start()] + dossier[match.end():]).strip()
    return body, None if missing.lower() in {"none", "n/a", ""} else missing


def remove_market_prices(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not _MARKET_WORDS.search(line))


def cap_tokens(text: str, max_tokens: int = MAX_DOSSIER_TOKENS) -> str:
    max_words = int(max_tokens * WORDS_PER_TOKEN)
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + " [...]"


def with_official_line(dossier: str, line: str, max_tokens: int = MAX_DOSSIER_TOKENS) -> str:
    """The official-data line first, then the dossier cut so both together
    stay within the ~6,000-token limit (same words-per-token estimate)."""
    max_words = int(max_tokens * WORDS_PER_TOKEN) - len(line.split()) - 1  # 1: the "[...]"
    words = dossier.split()
    body = dossier if len(words) <= max_words else " ".join(words[:max_words]) + " [...]"
    return f"{line}\n\n{body}"


# ---------------------------------------------------------------- AskNews


def _format_asknews(articles: list[Any]) -> str:
    lines = []
    for a in sorted(articles, key=lambda a: a.pub_date, reverse=True):
        lines.append(
            f"**{a.eng_title}**\n{a.summary}\nPublish date: {a.pub_date:%Y-%m-%d}\nSource: {a.source_id}\n"
        )
    return "\n".join(lines)


async def asknews_search(query: str) -> list[Any]:
    """One AskNews call (latest and older news together)."""
    from asknews_sdk import AsyncAskNewsSDK

    client_id, secret = os.getenv("ASKNEWS_CLIENT_ID"), os.getenv("ASKNEWS_SECRET")
    oauth = bool(client_id and secret)
    await _asknews_pacer.wait_turn()
    async with AsyncAskNewsSDK(
        client_id=client_id if oauth else None,
        client_secret=secret if oauth else None,
        api_key=None if oauth else os.getenv("ASKNEWS_API_KEY"),
        scopes={"news"},
    ) as ask:
        response = await ask.news.search_news(
            query=query,
            n_articles=ASKNEWS_ARTICLES_PER_CALL,
            return_type="dicts",
            strategy="default",
        )
    return list(response.as_dicts or [])


# ---------------------------------------------------------------- the whole step


async def run_planned_research(
    question: Any,
    dates: str,
    invoke_helper: Callable[[str], Awaitable[str]],
    search_asknews: Callable[[str], Awaitable[list[Any]]] = asknews_search,
    search_free: Callable[[str], list[Any]] = collect_free_news,
    fetch_background: Callable[[list[str]], tuple[str, list[str]]] | None = None,
) -> ResearchResult:
    text = question.question_text
    criteria = question.resolution_criteria or ""

    # 1. Plan.
    try:
        plan = parse_plan(await invoke_helper(planner_prompt(text, criteria, dates)), text)
    except Exception as e:
        logger.warning(f"Question {question.id_of_post}: research planner failed ({type(e).__name__})")
        plan = ResearchPlan(queries=[text])

    result = ResearchResult(dossier="", queries=plan.queries)
    asknews_blocks: list[str] = []
    free_articles: list[Any] = []
    asknews_down = False

    async def asknews(query: str) -> None:
        nonlocal asknews_down
        if asknews_down or result.asknews_calls >= MAX_ASKNEWS_CALLS:
            return
        result.asknews_calls += 1
        try:
            found = await search_asknews(query)
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status is None and e.__cause__ is not None:
                status = getattr(getattr(e.__cause__, "response", None), "status_code", None)
            logger.warning(
                f"Question {question.id_of_post}: AskNews failed"
                f"{f' (HTTP {status})' if status else ''}, using free news sources"
            )
            asknews_down = True
            return
        result.asknews_articles += len(found)
        if found:
            asknews_blocks.append(_format_asknews(found))

    # 2. Search: keep one AskNews call back for the gap-fill.
    for query in plan.queries[: MAX_ASKNEWS_CALLS - 1]:
        await asknews(query)
    if len(plan.queries) == MAX_QUERIES and result.asknews_articles < MIN_ARTICLES:
        await asknews(plan.queries[-1])
    if asknews_down or result.asknews_articles < MIN_ARTICLES:
        for query in plan.queries:
            free_articles += await asyncio.to_thread(search_free, query)
        free_articles = _dedupe(free_articles)
        result.free_articles = len(free_articles)

    articles_text = "\n\n".join(
        part for part in ("\n".join(asknews_blocks), format_articles(free_articles)) if part
    ) or "No recent news articles were found."

    # 3. Dossier.
    missing = None
    try:
        unit = getattr(question, "unit_of_measure", None)
        if getattr(question, "question_type", None) in ("numeric", "discrete"):
            unit = unit or "(the question's unit)"
        else:
            unit = None
        dossier_text = await invoke_helper(dossier_prompt(text, criteria, dates, plan.key_facts, articles_text, unit))
        dossier, missing = split_missing(dossier_text)
        result.dossier_written = bool(dossier.strip())
    except Exception as e:
        logger.warning(f"Question {question.id_of_post}: dossier writer failed ({type(e).__name__})")
        dossier = ""
    if not result.dossier_written:
        dossier = articles_text

    # 4. Gap-fill: one extra search for a missing key fact.
    if missing:
        before = result.asknews_articles
        await asknews(missing)
        extra = asknews_blocks[-1] if result.asknews_articles > before else ""
        if not extra:
            found = await asyncio.to_thread(search_free, missing)
            result.free_articles += len(found)
            extra = format_articles(found)
        if extra:
            result.gap_filled = True
            dossier += f"\n\n## Extra search: {missing}\n{extra}"

    # 5. Build 6a: Wikipedia background for the planner's entities (a
    # failure only means no background); the dossier is cut to make room.
    section = ""
    if plan.entities:
        try:
            from wikipedia import background

            section, result.wikipedia = await asyncio.to_thread(fetch_background or background, plan.entities)
        except Exception as e:
            logger.warning(f"Question {question.id_of_post}: Wikipedia background failed ({type(e).__name__})")
    dossier = remove_market_prices(dossier)
    result.base_dossier, result.wikipedia_section = dossier, section
    if section:
        room = int(MAX_DOSSIER_TOKENS * WORDS_PER_TOKEN) - len(section.split()) - 1
        words = dossier.split()
        body = dossier if len(words) <= room else " ".join(words[:room]) + " [...]"
        result.dossier = f"{body}\n\n{section}"
    else:
        result.dossier = cap_tokens(dossier)
    result.current_value = parse_current_value(result.dossier)
    return result


def _dedupe(articles: list[Any]) -> list[Any]:
    seen: set[str] = set()
    kept = []
    for a in sorted(articles, key=lambda a: a.published, reverse=True):
        key = re.sub(r"[^a-z0-9]", "", a.headline.lower())
        if key not in seen:
            seen.add(key)
            kept.append(a)
    return kept[:10]
