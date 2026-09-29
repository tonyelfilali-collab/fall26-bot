"""
Build 6a (architect, 29 Sep): background from Wikipedia. The research
planner names up to 3 entities; their Wikipedia summaries (REST API, free, no
key) go into the dossier as "Background (Wikipedia, retrieved <date>)", at
most ~800 tokens in all. A failed fetch is skipped. Switch WIKIPEDIA_ENABLED.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Callable
from urllib.parse import quote

import requests

from bot_helpers import PUBLIC_LOGGER_NAME

logger = logging.getLogger(PUBLIC_LOGGER_NAME)

WIKIPEDIA_ENABLED = True
SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
# Wikipedia asks API users to identify themselves.
USER_AGENT = "fall26-bot/1.0 (https://github.com/tonyelfilali-collab/fall26-bot)"
MAX_ENTITIES = 3
MAX_TOKENS = 800
WORDS_PER_TOKEN = 0.75
TIMEOUT_SECONDS = 15

Get = Callable[..., Any]


def fetch_summary(title: str, get: Get = requests.get) -> tuple[str, str] | None:
    """(page title, summary text) or None."""
    response = get(
        SUMMARY_URL.format(title=quote(title.strip().replace(" ", "_"), safe="")),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        return None
    data = response.json()
    if data.get("type") == "disambiguation" or not data.get("extract"):
        return None
    return data.get("title") or title, data["extract"].strip()


def background(entities: list[str], today: date | None = None, get: Get = requests.get) -> tuple[str, list[str]]:
    """(the dossier section, the page titles used); ("", []) if nothing was found."""
    if not WIKIPEDIA_ENABLED:
        return "", []
    budget = int(MAX_TOKENS * WORDS_PER_TOKEN)
    parts, titles = [], []
    for entity in [e for e in entities if e and e.strip()][:MAX_ENTITIES]:
        try:
            found = fetch_summary(entity, get)
        except Exception as e:
            logger.warning(f"Wikipedia summary failed ({type(e).__name__}); going on without it")
            continue
        if found is None:
            continue
        title, text = found
        words = text.split()
        if budget <= 0:
            break
        text = " ".join(words[:budget]) + (" [...]" if len(words) > budget else "")
        budget -= min(len(words), budget)
        parts.append(f"**{title}**: {text}")
        titles.append(title)
    if not parts:
        return "", []
    header = f"## Background (Wikipedia, retrieved {(today or date.today()).isoformat()})"
    return header + "\n" + "\n\n".join(parts), titles
