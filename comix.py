"""
Comix.to backend for the manga watcher.

Comix is a Next.js app that embeds its full page state as JSON inside
a `<script id="initial-data">` tag — exactly the data its React
components hydrate from. We piggy-back on that: fetch the title page,
parse the JSON blob, read `latestChapter` / `latestChapterUrl` /
`chapterUpdatedAtFormatted` directly from the manga detail entry.

No HTML scraping, no headless browser, no API key.

Public surface mirrors manga.py so manga_watch.py can treat the two
sources interchangeably:

    info = comix.latest_chapter_from_url("https://comix.to/title/...")
    # -> ChapterInfo(chapter, chapter_num, title, published_at, read_url)
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) jarvis-personal-assistant/1.0"
DEFAULT_TIMEOUT = 10.0


@dataclass
class ChapterInfo:
    """Compatible-ish with manga.ChapterInfo (same field names) so the
    watcher's formatting helpers work for both sources."""
    chapter: str          # "65", "65.5", … (string, preserves decimals)
    chapter_num: float    # parsed numeric form; 0.0 if unparseable
    title: str            # series title (comix doesn't expose per-chapter titles here)
    language: str         # we don't know; comix aggregates all TLs
    published_at: str     # human-readable like "6d ago" (comix only gives that)
    chapter_id: str       # URL-derived id, best-effort
    read_url: str         # absolute URL to the latest chapter


# ────────────────────── helpers ──────────────────────
def _normalize_title_url(url: str) -> str:
    """Accept any Comix URL (title page, chapter page, trailing slash
    variants) and return the clean title-page URL."""
    url = (url or "").strip()
    if not url:
        return url
    # If the user pasted a chapter URL, trim down to the title segment.
    #   https://comix.to/title/<slug>/<chapter-id>-chapter-N  →
    #   https://comix.to/title/<slug>
    m = re.match(r"(https?://[^/]+/title/[^/]+)(?:/.*)?$", url)
    if m:
        return m.group(1).rstrip("/")
    return url.rstrip("/")


def _fetch_initial_data(url: str, timeout: float = DEFAULT_TIMEOUT
                        ) -> Optional[dict]:
    """GET the title page, extract `<script id="initial-data">` and
    return its parsed JSON. Returns None on any failure."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                logging.warning("[comix] HTTP %s on %s", resp.status, url)
                return None
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logging.warning("[comix] request failed: %s", exc)
        return None

    m = re.search(
        r'<script[^>]*id=["\']initial-data["\'][^>]*>(.+?)</script>',
        html, re.DOTALL)
    if not m:
        logging.warning("[comix] initial-data script not found on %s", url)
        return None
    try:
        return json.loads(m.group(1))
    except Exception as exc:
        logging.warning("[comix] initial-data JSON parse failed: %s", exc)
        return None


def _extract_detail(data: dict) -> Optional[dict]:
    """Pull the manga-detail entry out of the queries dict. Comix keys
    its React Query cache by a JSON-string tuple like
    `["manga","detail","<hid>"]`, so we iterate and pick the one that
    looks like a detail payload (has latestChapter + title)."""
    queries = data.get("queries", {})
    if not isinstance(queries, dict):
        return None
    for key, value in queries.items():
        if not isinstance(value, dict):
            continue
        if "latestChapter" in value and "title" in value:
            return value
    # Fallback: some pages stash the entry at data["manga"] directly.
    manga = data.get("manga")
    if isinstance(manga, dict) and "latestChapter" in manga:
        return manga
    return None


def _parse_num(raw) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


# ────────────────────── public API ──────────────────────
def latest_chapter_from_url(title_url: str,
                            timeout: float = DEFAULT_TIMEOUT,
                            ) -> Optional[ChapterInfo]:
    """Fetch the comix title page and return the latest chapter info."""
    title_url = _normalize_title_url(title_url)
    if not title_url:
        return None
    data = _fetch_initial_data(title_url, timeout=timeout)
    if data is None:
        return None
    detail = _extract_detail(data)
    if detail is None:
        logging.warning("[comix] no detail entry found in page JSON")
        return None

    chap_raw = detail.get("latestChapter")
    if chap_raw in (None, 0, "0"):
        return None

    latest_url = detail.get("latestChapterUrl") or ""
    # URLs on the page are relative; prefix with origin.
    if latest_url.startswith("/"):
        origin = _origin_of(title_url)
        latest_url = f"{origin}{latest_url}"

    # Best-effort chapter id = numeric prefix of the url slug segment
    chap_id = ""
    m = re.search(r"/(\d+)-chapter-", latest_url)
    if m:
        chap_id = m.group(1)

    return ChapterInfo(
        chapter=str(chap_raw),
        chapter_num=_parse_num(chap_raw),
        title=str(detail.get("title") or ""),
        language="any",
        published_at=str(detail.get("chapterUpdatedAtFormatted") or ""),
        chapter_id=chap_id,
        read_url=latest_url,
    )


def _origin_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def resolve_title(title_url: str) -> Optional[str]:
    """Convenience: return just the series title for display."""
    info = latest_chapter_from_url(title_url)
    return info.title if info else None
