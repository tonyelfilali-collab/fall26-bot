"""
PLAN.md Step 9: market prices, LOG-ONLY for now (architect, 27 Sep 2026).

For a question: search Polymarket, Kalshi and Manifold (free, keyless APIs)
with keywords from its title; keep open markets with enough volume and a price
updated in the last 24 hours; ask a judge model (the parser model, never the
forecasting quota) whether each is the same event, with the same resolution
source and the same deadline. A market is accepted only if all three are yes.
The candidates and verdicts are SAVED (question JSON log, fall26-data) but
NOT blended into forecasts: MARKET_MODE in bot_config is "log-only".

    poetry run python markets.py --limit 12   # manual "Market matches" table
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import requests

from free_news import build_query

MAX_CANDIDATES = 5
FRESH_WITHIN = timedelta(hours=24)
MIN_VOLUME = {"polymarket": 10_000.0, "kalshi": 1_000.0, "manifold": 1_000.0}
_TIMEOUT = 20
_HEADERS = {"User-Agent": "fall26-bot market matching (log-only)"}


@dataclass
class Candidate:
    source: str
    market_id: str
    title: str
    rules: str
    deadline: str | None  # ISO date/time the market closes or resolves
    price: float | None  # probability of Yes, 0-1
    volume: float
    updated: str | None  # ISO time of the last price update, if known
    url: str
    # Filled in by the checks and the judge:
    fresh: bool | None = None
    liquid: bool | None = None
    same_event: bool | None = None
    same_source: bool | None = None
    same_deadline: bool | None = None
    accepted: bool = False
    reason: str = ""


def _iso(ms_or_text: Any) -> str | None:
    if ms_or_text in (None, ""):
        return None
    if isinstance(ms_or_text, (int, float)):
        return datetime.fromtimestamp(ms_or_text / 1000, tz=timezone.utc).isoformat()
    return str(ms_or_text)


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- sources (normalising is pure)


def manifold_candidates(data: list[dict]) -> list[Candidate]:
    found = []
    for m in data:
        if m.get("outcomeType") != "BINARY" or m.get("isResolved"):
            continue
        found.append(Candidate(
            "manifold", str(m.get("id")), m.get("question", ""), m.get("textDescription", "") or "",
            _iso(m.get("closeTime")), m.get("probability"), float(m.get("volume") or 0),
            _iso(m.get("lastUpdatedTime")), m.get("url", ""),
        ))
    return found


def polymarket_candidates(data: dict) -> list[Candidate]:
    found = []
    for event in data.get("events") or []:
        for m in event.get("markets") or []:
            if m.get("closed") or not m.get("active"):
                continue
            try:
                outcomes = json.loads(m.get("outcomes") or "[]")
                prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
                price = prices[outcomes.index("Yes")] if "Yes" in outcomes else None
            except (ValueError, TypeError):
                price = None
            found.append(Candidate(
                "polymarket", str(m.get("id")), m.get("question", ""), m.get("description", "") or event.get("description", "") or "",
                m.get("endDate"), price, float(m.get("volume") or 0), m.get("updatedAt"),
                f"https://polymarket.com/event/{event.get('slug', '')}",
            ))
    return found


def kalshi_candidates(data: dict) -> list[Candidate]:
    found = []
    for result in data.get("current_page") or []:
        for m in result.get("markets") or []:
            if m.get("result"):
                continue
            bid, ask, last = m.get("yes_bid"), m.get("yes_ask"), m.get("last_price")
            cents = (bid + ask) / 2 if bid is not None and ask is not None and ask > 0 else last
            found.append(Candidate(
                "kalshi", m.get("ticker", ""), f"{result.get('event_title', '')} {m.get('yes_subtitle') or ''}".strip(),
                "", m.get("close_ts"), cents / 100 if cents is not None else None, float(m.get("volume") or 0),
                None, f"https://kalshi.com/markets/{result.get('series_ticker', '').lower()}",
            ))
    return found


def search_markets(query: str) -> list[Candidate]:
    """All three sources; a source that fails adds nothing."""
    found: list[Candidate] = []
    sources = [
        (f"https://api.manifold.markets/v0/search-markets?term={quote(query)}&filter=open&limit=5", manifold_candidates),
        (f"https://gamma-api.polymarket.com/public-search?q={quote(query)}&limit_per_type=5&events_status=active", polymarket_candidates),
        (f"https://api.elections.kalshi.com/v1/search/series?query={quote(query)}&page_size=5", kalshi_candidates),
    ]
    for url, normalise in sources:
        try:
            response = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            response.raise_for_status()
            found += normalise(response.json())
        except Exception:
            continue
    return found


def kalshi_details(candidate: Candidate) -> None:
    """Rules and 24 h activity for a Kalshi market (its search result has neither)."""
    try:
        response = requests.get(
            f"https://api.elections.kalshi.com/trade-api/v2/markets/{candidate.market_id}", headers=_HEADERS, timeout=_TIMEOUT
        )
        response.raise_for_status()
        market = response.json()["market"]
        candidate.rules = market.get("rules_primary", "") or ""
        traded_today = float(market.get("volume_24h_fp") or market.get("volume_24h") or 0) > 0
        candidate.updated = datetime.now(timezone.utc).isoformat() if traded_today else None
    except Exception:
        pass


# ---------------------------------------------------------------- checks (pure)


def apply_checks(candidate: Candidate, now: datetime | None = None) -> Candidate:
    now = now or datetime.now(timezone.utc)
    updated = _parse_time(candidate.updated)
    candidate.fresh = updated is not None and now - updated <= FRESH_WITHIN
    candidate.liquid = candidate.volume >= MIN_VOLUME.get(candidate.source, float("inf"))
    return candidate


def judge_prompt(question: Any, candidates: list[Candidate]) -> str:
    listing = "\n\n".join(
        f"[{i}] {c.source}: {c.title}\nCloses/resolves: {c.deadline}\nRules: {c.rules[:600]}"
        for i, c in enumerate(candidates)
    )
    return (
        "You check whether prediction markets ask EXACTLY the same thing as a forecasting question.\n\n"
        f"Question: {question.question_text}\nResolution criteria: {question.resolution_criteria or ''}\n"
        f"Question closes: {question.close_time}; scheduled to resolve: {question.scheduled_resolution_time}\n\n"
        f"Markets:\n{listing}\n\n"
        "For each market answer: same_event (same outcome being predicted), same_source (resolved by the "
        "same source or data), same_deadline (the same cut-off date). Be strict: a different date or threshold is a no.\n"
        'Answer with JSON only: [{"index": 0, "same_event": true, "same_source": false, "same_deadline": true, "reason": "..."}]'
    )


def apply_verdicts(candidates: list[Candidate], judge_text: str) -> list[Candidate]:
    """Accepted only if same event AND same source AND same deadline AND fresh AND liquid."""
    match = re.search(r"\[.*\]", judge_text, re.DOTALL)
    try:
        verdicts = json.loads(match.group(0)) if match else []
    except json.JSONDecodeError:
        verdicts = []
    for verdict in verdicts:
        index = verdict.get("index")
        if not isinstance(index, int) or not 0 <= index < len(candidates):
            continue
        c = candidates[index]
        c.same_event = bool(verdict.get("same_event"))
        c.same_source = bool(verdict.get("same_source"))
        c.same_deadline = bool(verdict.get("same_deadline"))
        c.reason = str(verdict.get("reason", ""))[:300]
    for c in candidates:
        c.accepted = bool(c.same_event and c.same_source and c.same_deadline and c.fresh and c.liquid)
    return candidates


# ---------------------------------------------------------------- one question


async def match_question(
    question: Any,
    invoke_judge: Callable[[str], Awaitable[str]],
    search: Callable[[str], list[Candidate]] = search_markets,
) -> list[Candidate]:
    """Find, check and judge candidate markets for one question (log-only)."""
    import asyncio

    query = " ".join(build_query(question.question_text).split()[:5])
    if not query:
        return []
    found = await asyncio.to_thread(search, query)
    # A fair mix: at most 2 per source, up to MAX_CANDIDATES in all.
    candidates: list[Candidate] = []
    for source in ("polymarket", "kalshi", "manifold"):
        candidates += [c for c in found if c.source == source][:2]
    candidates = candidates[:MAX_CANDIDATES]
    for c in candidates:
        if c.source == "kalshi" and search is search_markets:
            await asyncio.to_thread(kalshi_details, c)
        apply_checks(c)
    if candidates:
        try:
            apply_verdicts(candidates, await invoke_judge(judge_prompt(question, candidates)))
        except Exception as e:
            for c in candidates:
                c.reason = f"judge failed ({type(e).__name__})"
    return candidates


def to_records(candidates: list[Candidate]) -> list[dict]:
    return [asdict(c) for c in candidates]


def markdown_table(rows: list[tuple[int, Candidate]]) -> str:
    def yn(v: bool | None) -> str:
        return "?" if v is None else ("yes" if v else "no")

    lines = [
        "| # | Question | Source | Market | Price | Volume | Fresh | Same event | Same source | Same deadline | Accepted | Judge's reason |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for n, (post, c) in enumerate(rows, 1):
        price = f"{c.price:.0%}" if c.price is not None else "?"
        title = c.title.replace("|", "/")[:70]
        reason = c.reason.replace("|", "/").replace("\n", " ")[:90]
        lines.append(
            f"| {n} | {post} | {c.source} | [{title}]({c.url}) | {price} | {c.volume:,.0f} | {yn(c.fresh)} | "
            f"{yn(c.same_event)} | {yn(c.same_source)} | {yn(c.same_deadline)} | {'**yes**' if c.accepted else 'no'} | {reason} |"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- manual table (Market matches workflow)


def main() -> None:
    import argparse
    import asyncio
    import os

    from bot_helpers import configure_public_logging, silence_noisy_dependencies

    silence_noisy_dependencies()
    configure_public_logging()
    from forecasting_tools import ApiFilter, MetaculusClient

    from bot_config import get_lineup
    from gemini_budget import GitHubFileStore
    from question_log import DATA_REPO

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=12, help="how many questions to try")
    parser.add_argument("--rows", type=int, default=20, help="table rows")
    args = parser.parse_args()

    # The judge is the free OpenRouter model: never the live Gemini quota.
    judge = get_lineup("free").llms["parser"]
    client = MetaculusClient()
    questions = asyncio.run(
        client.get_questions_matching_filter(
            ApiFilter(
                allowed_statuses=["open"], allowed_types=["binary"], num_forecasters_gte=30,
                is_in_main_feed=True, group_question_mode="exclude",
            ),
            num_questions=args.limit, randomly_sample=True, error_if_question_target_missed=False,
        )
    )
    rows: list[tuple[int, Candidate]] = []
    saved = []
    for q in questions:
        candidates = asyncio.run(match_question(q, judge.invoke))
        saved.append({"post_id": q.id_of_post, "question": q.question_text, "candidates": to_records(candidates)})
        rows += [(q.id_of_post, c) for c in candidates]
        print(f"Question {q.id_of_post}: {len(candidates)} candidate(s), {sum(c.accepted for c in candidates)} accepted")
        if len(rows) >= args.rows:
            break
    table = markdown_table(rows[: args.rows])
    report = f"## Market matches (log-only)\n\n{table}\n"
    print(report)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    token = os.getenv("DATA_REPO_TOKEN")
    if token:
        GitHubFileStore(DATA_REPO, f"markets/{stamp}.json", token).save({"questions": saved, "table": table})


if __name__ == "__main__":
    main()
