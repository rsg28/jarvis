"""
News — headlines via Google News RSS (free, no API key).

Topics come from Google News' section endpoints; anything else falls
back to a keyword search. Titles are already deduped and geo-tagged.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from typing import List

TOPIC_URLS = {
    "world":     "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-US&gl=US&ceid=US:en",
    "tech":      "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=en-US&gl=US&ceid=US:en",
    "sports":    "https://news.google.com/rss/headlines/section/topic/SPORTS?hl=en-US&gl=US&ceid=US:en",
    "business":  "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en",
    "science":   "https://news.google.com/rss/headlines/section/topic/SCIENCE?hl=en-US&gl=US&ceid=US:en",
    "health":    "https://news.google.com/rss/headlines/section/topic/HEALTH?hl=en-US&gl=US&ceid=US:en",
    "entertainment": "https://news.google.com/rss/headlines/section/topic/ENTERTAINMENT?hl=en-US&gl=US&ceid=US:en",
}
TOPIC_ALIASES = {
    "technology": "tech",
    "sport":      "sports",
    "soccer":     "sports",
    "football":   "sports",
    "finance":    "business",
    "economy":    "business",
    "fun":        "entertainment",
    "movies":     "entertainment",
}

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(title: str) -> str:
    # RSS titles occasionally include the source suffix " - Publisher".
    text = _TAG_RE.sub("", title).strip()
    return text


def _fetch(url: str, limit: int) -> List[str]:
    try:
        import feedparser
    except ImportError:
        logging.warning("feedparser not installed — install with: pip install feedparser")
        return []
    feed = feedparser.parse(url)
    return [_clean(e.title) for e in feed.entries[:limit] if getattr(e, "title", "")]


def headlines(topic: str = "world", limit: int = 5) -> List[str]:
    """Return top `limit` headlines for a topic keyword or search phrase."""
    key = (topic or "world").strip().lower()
    key = TOPIC_ALIASES.get(key, key)

    if key in TOPIC_URLS:
        return _fetch(TOPIC_URLS[key], limit)

    # Free-form search fallback
    q = urllib.parse.quote_plus(topic)
    return _fetch(
        f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en",
        limit,
    )
