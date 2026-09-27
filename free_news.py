"""
Free, keyless news search used when AskNews fails or finds too little:
Google News RSS search and the GDELT DOC API.

collect_free_news() builds a keyword query from the question title, fetches
both sources, keeps articles from the last 60 days, drops duplicates, and
returns up to 10, newest first. format_articles() turns them into dated
research text of at most about 2,000 words.
"""
from __future__ import annotations

import html
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import requests

MAX_ARTICLES = 10
MAX_AGE_DAYS = 60
MAX_WORDS = 2000
MAX_SNIPPET_WORDS = 80
_TIMEOUT_SECONDS = 20
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; fall26-bot news research)"}
# GDELT allows one request every 5 seconds.
_GDELT_MIN_INTERVAL_SECONDS = 6.0
_last_gdelt_call = 0.0

_STOPWORDS = {
    "a", "about", "above", "after", "all", "an", "and", "any", "are", "as", "at", "be",
    "been", "before", "being", "between", "by", "did", "do", "does", "during", "each",
    "for", "from", "had", "has", "have", "how", "if", "in", "into", "is", "it", "its",
    "least", "less", "more", "most", "no", "not", "of", "on", "or", "other", "over",
    "than", "that", "the", "their", "there", "these", "this", "those", "through", "to",
    "under", "until", "up", "was", "what", "when", "which", "who", "will", "with",
    "within", "would", "yes", "per", "end", "date", "according",
}


@dataclass(frozen=True)
class Article:
    published: datetime
    source: str
    headline: str
    snippet: str = ""


def build_query(question_title: str, max_words: int = 6) -> str:
    """Keywords from the question title, in order, without stopwords or duplicates."""
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9.'-]*", question_title)
    keywords: list[str] = []
    for word in words:
        cleaned = word.strip(".'-")
        # Keep short acronyms (EU, UK, AI) and numbers (Article 50).
        is_short_keeper = cleaned.isupper() or cleaned.isdigit()
        if (len(cleaned) < 3 and not is_short_keeper) or len(cleaned) < 2:
            continue
        if cleaned.lower() in _STOPWORDS:
            continue
        if cleaned.lower() not in (k.lower() for k in keywords):
            keywords.append(cleaned)
    return " ".join(keywords[:max_words])


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()


def parse_google_news_rss(xml_text: str) -> list[Article]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    articles = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
        published_text = item.findtext("pubDate")
        if not title or not published_text:
            continue
        try:
            published = parsedate_to_datetime(published_text).astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        # Google News titles end with " - Source".
        if source and title.endswith(f" - {source}"):
            title = title[: -len(f" - {source}")]
        snippet = _strip_html(item.findtext("description") or "")
        # The description usually just repeats the headline and source.
        if snippet.startswith(title) or not snippet:
            snippet = ""
        articles.append(Article(published, source or "Google News", title, snippet))
    return articles


def parse_gdelt_json(text: str) -> list[Article]:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []  # e.g. GDELT's plain-text rate-limit answer
    articles = []
    for item in data.get("articles", []) if isinstance(data, dict) else []:
        title = (item.get("title") or "").strip()
        try:
            published = datetime.strptime(item.get("seendate", ""), "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if title:
            articles.append(Article(published, item.get("domain") or "GDELT", title))
    return articles


def fetch_google_news(query: str) -> list[Article]:
    url = (
        "https://news.google.com/rss/search?q="
        + quote_plus(f"{query} when:{MAX_AGE_DAYS}d")
        + "&hl=en-US&gl=US&ceid=US:en"
    )
    response = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT_SECONDS)
    response.raise_for_status()
    return parse_google_news_rss(response.text)


def fetch_gdelt(query: str) -> list[Article]:
    global _last_gdelt_call
    # GDELT rejects words under 3 letters and treats spaces as AND, so use
    # fewer, longer terms.
    gdelt_words = [w for w in query.split() if len(w) >= 3][:4]
    if not gdelt_words:
        return []
    gdelt_query = " ".join(gdelt_words) + " sourcelang:english"
    url = (
        "https://api.gdeltproject.org/api/v2/doc/doc?query="
        + quote_plus(gdelt_query)
        + f"&mode=artlist&format=json&maxrecords=25&timespan={MAX_AGE_DAYS}d&sort=datedesc"
    )
    for attempt in range(2):
        wait = _last_gdelt_call + _GDELT_MIN_INTERVAL_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_gdelt_call = time.monotonic()
        response = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT_SECONDS)
        if response.status_code == 429 and attempt == 0:
            continue
        response.raise_for_status()
        return parse_gdelt_json(response.text)
    return []


def select_articles(
    articles: list[Article], now: datetime | None = None
) -> list[Article]:
    """Last 60 days, no duplicate headlines, newest first, at most 10."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    seen: set[str] = set()
    kept = []
    for article in sorted(articles, key=lambda a: a.published, reverse=True):
        key = re.sub(r"[^a-z0-9]", "", article.headline.lower())
        if article.published < cutoff or article.published > now + timedelta(days=1) or key in seen:
            continue
        seen.add(key)
        kept.append(article)
    return kept[:MAX_ARTICLES]


def collect_free_news(question_title: str, now: datetime | None = None) -> list[Article]:
    """Both free sources; a source that fails just contributes nothing."""
    query = build_query(question_title)
    if not query:
        return []
    found: list[Article] = []
    for fetch in (fetch_google_news, fetch_gdelt):
        try:
            found += fetch(query)
        except Exception:
            continue
    # Too narrow: search again with just the first 3 keywords.
    shorter = " ".join(query.split()[:3])
    if len(select_articles(found, now)) < 3 and shorter != query:
        try:
            found += fetch_google_news(shorter)
        except Exception:
            pass
    return select_articles(found, now)


def format_articles(articles: list[Article], max_words: int = MAX_WORDS) -> str:
    """Dated research text, newest first, cut off at about max_words words."""
    if not articles:
        return ""
    lines = ["Recent news articles (free sources: Google News, GDELT), newest first:"]
    words = len(lines[0].split())
    for article in articles:
        snippet = " ".join(article.snippet.split()[:MAX_SNIPPET_WORDS])
        line = f"- {article.published:%Y-%m-%d} | {article.source} | {article.headline}"
        if snippet:
            line += f" — {snippet}"
        line_words = len(line.split())
        if words + line_words > max_words:
            break
        lines.append(line)
        words += line_words
    return "\n".join(lines)


def count_asknews_articles(research: str) -> int:
    """AskNews research text has one 'Publish date:' line per article."""
    return research.count("Publish date:")
