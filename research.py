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
5. Current-value check (architect, 28 Oct): when the question itself states a
   dated current value and the dossier's CURRENT VALUE disagrees by more than
   20%, one "Note:" line is added and the unit check uses the more recent of
   the two (neither unless both are dated).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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
    # The research time limit was reached in this stage (None = it wasn't).
    time_limit_hit: str | None = None
    # Current-value check: the question's and the dossier's values and which one
    # the unit check uses ("question" / "research" / "neither"); None = they agree.
    value_check: dict | None = None

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


# ---------------------------------------------------------------- current-value check

VALUE_DISAGREE_RATIO = 1.2  # "differs by more than 20%": the larger is over 1.2x the smaller
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DATE = (
    rf"(?:(?P<y1>\d{{4}})-(?P<m1>\d{{2}})-(?P<d1>\d{{2}})"
    rf"|(?P<mon2>{_MONTH})\s+(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<y2>\d{{4}}))?"
    rf"|(?P<d3>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<mon3>{_MONTH})(?:,?\s+(?P<y3>\d{{4}}))?)"
)
_NUMBER = r"(?P<num>[-+]?\$?\d[\d,]*(?:\.\d+)?)\s*%?"
_VALUE_WORDS = (r"(?:current(?:ly)?|latest|settled|closed|stood|stands|was|is|traded|trading|"
                r"reached|level|value|price)")
# "<value word> ... <number> ... on / as of / ( <date>", or "As of / On <date>, ... <value word> ... <number>".
_STATED_AFTER = re.compile(
    rf"\b{_VALUE_WORDS}\b[^.;\n]{{0,40}}?{_NUMBER}[^.;\n\d]{{0,25}}?(?:\bon\b|\bas of\b|\()\s*{_DATE}",
    re.IGNORECASE,
)
_STATED_BEFORE = re.compile(
    rf"\b(?:as of|on)\s+{_DATE},?\s[^.;\n]{{0,60}}?\b{_VALUE_WORDS}\b[^.;\n\d]{{0,25}}?{_NUMBER}",
    re.IGNORECASE,
)
# A threshold, not a current value: "will the price be above $100 on Oct 15".
_THRESHOLD_WORDS = re.compile(
    r"\b(?:above|below|exceed\w*|over|under|more|less|least|most|than|between|reach|hit|surpass\w*|fall|rise|will|be)\b",
    re.IGNORECASE,
)
# The same value in another unit right after it: "683 cents ($6.83)".
_ALTERNATIVE = re.compile(r"\s*[a-z/ ]{0,15}\(\s*\$?(\d[\d,]*(?:\.\d+)?)", re.IGNORECASE)


@dataclass
class StatedValue:
    value: float
    text: str  # the number as written
    day: date
    alternatives: tuple[str, ...] = ()  # the same value written in another unit


def _month(name: str) -> int:
    return _MONTHS[name.lower().rstrip(".")[:3]]


def _date_of(match: re.Match, year: int) -> date | None:
    g = match.groupdict()
    try:
        if g.get("y1"):
            return date(int(g["y1"]), int(g["m1"]), int(g["d1"]))
        if g.get("mon2"):
            return date(int(g["y2"] or year), _month(g["mon2"]), int(g["d2"]))
        if g.get("mon3"):
            return date(int(g["y3"] or year), _month(g["mon3"]), int(g["d3"]))
    except (ValueError, KeyError):
        return None
    return None


def stated_value(texts: list[str], year: int, today: date | None = None) -> StatedValue | None:
    """The latest dated current value the question's own text states, if any.
    Conservative: a number next to a value word with a past date beside it (a
    value with no date, a future date or a threshold word is ignored); bare
    years (1900-2099) are never taken as values."""
    today = today or datetime.now(timezone.utc).date()
    found: list[StatedValue] = []
    for text in texts:
        for pattern in (_STATED_AFTER, _STATED_BEFORE):
            for match in pattern.finditer(text or ""):
                raw = match.group("num")
                if re.fullmatch(r"[-+]?(19|20)\d\d", raw):
                    continue
                if _THRESHOLD_WORDS.search(text[match.start():match.start("num")]):
                    continue
                try:
                    value = float(raw.replace("$", "").replace(",", ""))
                except ValueError:
                    continue
                day = _date_of(match, year)
                if day is not None and day <= today:
                    other = _ALTERNATIVE.match(text, match.end("num"))
                    found.append(StatedValue(value, raw.lstrip("+$"), day, (other.group(1),) if other else ()))
    return max(found, key=lambda v: v.day) if found else None


def research_day(text: str) -> date | None:
    """The dossier's DATE: YYYY-MM-DD (or YYYY-MM: the 1st), else None."""
    match = re.match(r"(\d{4})-(\d{2})(?:-(\d{2}))?$", (text or "").strip())
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3) or 1))
    except ValueError:
        return None


def disagree(a: float, b: float) -> bool:
    return a != b and _ratio(a, b) > VALUE_DISAGREE_RATIO


def _ratio(a: float, b: float) -> float:
    return max(abs(a), abs(b)) / min(abs(a), abs(b)) if a and b and (a > 0) == (b > 0) else float("inf")


def _number_text(value: float) -> str:
    return f"{value:,.6f}".rstrip("0").rstrip(".")


def check_current_value(
    research: CurrentValue | None, stated: StatedValue | None
) -> tuple[CurrentValue | None, str | None, dict | None]:
    """(the value the unit check uses, the dossier note line, the record) for the
    question's stated value vs the dossier's CURRENT VALUE. No disagreement: unchanged."""
    if research is None or stated is None:
        return research, None, None
    # Of the ways the question writes it, the one closest to our research's unit.
    for text in stated.alternatives:
        value = float(text.replace(",", ""))
        if _ratio(value, research.value) < _ratio(stated.value, research.value):
            stated = StatedValue(value, text, stated.day)
    if not disagree(stated.value, research.value):
        return research, None, None
    ours = research_day(research.date)
    note = (f"Note: the question says {stated.text} ({stated.day.isoformat()}); "
            f"our research says {_number_text(research.value)} ({ours.isoformat() if ours else 'no date'}). "
            "Check which is more recent.")
    use = "neither" if ours is None or ours == stated.day else "question" if stated.day > ours else "research"
    chosen = {"question": CurrentValue(stated.value, research.unit, stated.day.isoformat()),
              "research": research, "neither": None}[use]
    record = {"question": {"value": stated.value, "date": stated.day.isoformat()},
              "research": {"value": research.value, "date": research.date}, "used": use}
    return chosen, note, record


def question_year(question: Any) -> int:
    for name in ("open_time", "published_time", "close_time"):
        when = getattr(question, name, None)
        if isinstance(when, datetime):
            return when.year
    return datetime.now(timezone.utc).year


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


class ResearchTimeUp(Exception):
    """The research time limit was reached: what was gathered is used."""


async def run_planned_research(
    question: Any,
    dates: str,
    invoke_helper: Callable[[str], Awaitable[str]],
    search_asknews: Callable[[str], Awaitable[list[Any]]] = asknews_search,
    search_free: Callable[[str], list[Any]] = collect_free_news,
    fetch_background: Callable[[list[str]], tuple[str, list[str]]] | None = None,
    deadline: float | None = None,
) -> ResearchResult:
    """deadline: event-loop time (loop.time()) when research must stop; the
    dossier is then built from what was gathered (result.time_limit_hit)."""
    text = question.question_text
    criteria = question.resolution_criteria or ""
    loop = asyncio.get_running_loop()

    async def within(awaitable: Awaitable[Any]) -> Any:
        if deadline is None:
            return await awaitable
        left = deadline - loop.time()
        if left <= 0:
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise ResearchTimeUp
        try:
            return await asyncio.wait_for(awaitable, left)
        except asyncio.TimeoutError:
            if loop.time() >= deadline:
                raise ResearchTimeUp from None
            raise

    result = ResearchResult(dossier="", queries=[text])
    asknews_blocks: list[str] = []
    free_articles: list[Any] = []
    asknews_down = False
    plan = ResearchPlan(queries=[text])
    dossier: str | None = None  # the written dossier (None = not written)
    extra_search = ""
    section = ""

    def articles_text() -> str:
        return "\n\n".join(
            part for part in ("\n".join(asknews_blocks), format_articles(_dedupe(free_articles))) if part
        ) or "No recent news articles were found."

    async def asknews(query: str) -> None:
        nonlocal asknews_down
        if asknews_down or result.asknews_calls >= MAX_ASKNEWS_CALLS:
            return
        result.asknews_calls += 1
        try:
            found = await within(search_asknews(query))
        except ResearchTimeUp:
            raise
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

    stage = "plan"
    try:
        # 1. Plan.
        try:
            plan = parse_plan(await within(invoke_helper(planner_prompt(text, criteria, dates))), text)
        except ResearchTimeUp:
            raise
        except Exception as e:
            logger.warning(f"Question {question.id_of_post}: research planner failed ({type(e).__name__})")
        result.queries = plan.queries

        # 2. Search: keep one AskNews call back for the gap-fill.
        stage = "search"
        for query in plan.queries[: MAX_ASKNEWS_CALLS - 1]:
            await asknews(query)
        if len(plan.queries) == MAX_QUERIES and result.asknews_articles < MIN_ARTICLES:
            await asknews(plan.queries[-1])
        if asknews_down or result.asknews_articles < MIN_ARTICLES:
            for query in plan.queries:
                free_articles += await within(asyncio.to_thread(search_free, query))

        # 3. Dossier.
        stage = "dossier"
        missing = None
        try:
            unit = getattr(question, "unit_of_measure", None)
            if getattr(question, "question_type", None) in ("numeric", "discrete"):
                unit = unit or "(the question's unit)"
            else:
                unit = None
            dossier_text = await within(
                invoke_helper(dossier_prompt(text, criteria, dates, plan.key_facts, articles_text(), unit))
            )
            written, missing = split_missing(dossier_text)
            if written.strip():
                dossier = written
        except ResearchTimeUp:
            raise
        except Exception as e:
            logger.warning(f"Question {question.id_of_post}: dossier writer failed ({type(e).__name__})")

        # 4. Gap-fill: one extra search for a missing key fact.
        stage = "gap-fill"
        if missing:
            before = result.asknews_articles
            await asknews(missing)
            extra = asknews_blocks[-1] if result.asknews_articles > before else ""
            if not extra:
                found = await within(asyncio.to_thread(search_free, missing))
                result.free_articles += len(found)
                extra = format_articles(found)
            if extra:
                result.gap_filled = True
                extra_search = f"\n\n## Extra search: {missing}\n{extra}"

        # 5. Build 6a: Wikipedia background for the planner's entities (a
        # failure only means no background); the dossier is cut to make room.
        stage = "wikipedia"
        if plan.entities:
            try:
                from wikipedia import background

                section, result.wikipedia = await within(
                    asyncio.to_thread(fetch_background or background, plan.entities)
                )
            except ResearchTimeUp:
                raise
            except Exception as e:
                logger.warning(f"Question {question.id_of_post}: Wikipedia background failed ({type(e).__name__})")
    except ResearchTimeUp:
        result.time_limit_hit = stage

    # Build the dossier from what was gathered.
    result.free_articles += len(_dedupe(free_articles))
    result.dossier_written = dossier is not None
    body = (dossier if dossier is not None else articles_text()) + extra_search
    body = remove_market_prices(body)
    result.base_dossier, result.wikipedia_section = body, section
    if section:
        room = int(MAX_DOSSIER_TOKENS * WORDS_PER_TOKEN) - len(section.split()) - 1
        words = body.split()
        cut = body if len(words) <= room else " ".join(words[:room]) + " [...]"
        result.dossier = f"{cut}\n\n{section}"
    else:
        result.dossier = cap_tokens(body)
    result.current_value = parse_current_value(result.dossier)
    texts = [text, getattr(question, "background_info", None) or "", criteria, getattr(question, "fine_print", None) or ""]
    result.current_value, note, result.value_check = check_current_value(
        result.current_value, stated_value(texts, question_year(question))
    )
    if note:
        result.dossier = f"{result.dossier}\n\n{note}"
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
