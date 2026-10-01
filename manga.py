"""
MangaDex client — search a manga by title and look up its latest
translated chapter.

Public API, no key required. Docs: https://api.mangadex.org/docs/
We're deliberately conservative with the surface we touch:

  - GET /manga?title=...           -> list candidates, pick the best
  - GET /manga/{id}/feed?...       -> latest chapter in preferred lang

The returned chapter number is kept as a string ("65", "65.5", "66")
because MangaDex uses decimals for side-chapters and we want to
preserve that in the state file for honest comparisons.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

import urllib.parse
import urllib.request
import json


API_BASE = "https://api.mangadex.org"
USER_AGENT = "jarvis-personal-assistant/1.0 (+https://github.com/rsg28/jarvis)"
DEFAULT_TIMEOUT = 8.0
# MangaDex serves adult content behind an opt-in flag; "smoking behind
# the supermarket with you" is suggestive-rated, so include that bucket
# by default. We stop short of 'pornographic' unless the user flips it.
DEFAULT_CONTENT_RATINGS = ("safe", "suggestive", "erotica")


@dataclass
class MangaMatch:
    manga_id: str
    title: str           # best English/romaji title we could pick
    score: float         # heuristic similarity to the search query


@dataclass
class ChapterInfo:
    chapter: str         # "66", "66.1", … (string to preserve decimals)
    chapter_num: float   # parsed numeric form for comparisons; 0 if "none"
    title: str           # chapter title or "" if the TL omitted one
    language: str        # ISO code, e.g. "en"
    published_at: str    # ISO timestamp from MangaDex
    chapter_id: str      # MangaDex chapter UUID
    read_url: str        # convenience https://mangadex.org/chapter/<id>


# ────────────────────── low-level HTTP ──────────────────────
def _get_json(path: str, params: dict, timeout: float = DEFAULT_TIMEOUT) -> Optional[dict]:
    """GET {API_BASE}{path} with query params. Returns parsed JSON or None.
    Handles list-valued params the MangaDex way: contentRating[]=safe&...
    rather than ?contentRating=safe,suggestive which the API rejects."""
    qs_parts: List[str] = []
    for key, value in params.items():
        if isinstance(value, (list, tuple)):
            for v in value:
                qs_parts.append(f"{key}={urllib.parse.quote(str(v))}")
        else:
            qs_parts.append(f"{key}={urllib.parse.quote(str(value))}")
    url = f"{API_BASE}{path}?{'&'.join(qs_parts)}" if qs_parts else f"{API_BASE}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                logging.warning("[manga] HTTP %s on %s", resp.status, path)
                return None
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logging.warning("[manga] request failed: %s", exc)
        return None


# ────────────────────── search ──────────────────────
_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _score_title(query_tokens: set[str], title: str) -> float:
    """How well does `title` match the query? Simple token overlap over
    the union — good enough to disambiguate "smoking behind the
    supermarket" from unrelated results."""
    tt = _tokens(title)
    if not tt or not query_tokens:
        return 0.0
    overlap = len(query_tokens & tt)
    return overlap / len(query_tokens | tt)


def _all_titles(attributes: dict) -> List[str]:
    """Every title variant MangaDex has on file for the entry — main
    title in all languages, plus altTitles. Used for match scoring so
    a Japanese-main-titled entry with an English altTitle still ranks."""
    out: List[str] = []
    title = attributes.get("title", {}) or {}
    out.extend(str(v) for v in title.values() if v)
    for entry in attributes.get("altTitles", []) or []:
        if isinstance(entry, dict):
            out.extend(str(v) for v in entry.values() if v)
    return out


def _pick_title(attributes: dict) -> str:
    """MangaDex titles come as {lang: text}. Prefer English, then
    romaji/japanese-latin, then whatever's first."""
    title = attributes.get("title", {}) or {}
    for lang in ("en", "ja-ro", "ja"):
        if title.get(lang):
            return title[lang]
    # Also check altTitles for an English rendering.
    for entry in attributes.get("altTitles", []) or []:
        if isinstance(entry, dict) and entry.get("en"):
            return entry["en"]
    if title:
        return next(iter(title.values()))
    return "(untitled)"


def search_manga(query: str, *, limit: int = 5,
                 content_ratings: tuple[str, ...] = DEFAULT_CONTENT_RATINGS,
                 ) -> List[MangaMatch]:
    """Return the top candidates ranked by title similarity to `query`."""
    query = (query or "").strip()
    if not query:
        return []
    params = {
        "title": query,
        "limit": limit,
        "order[relevance]": "desc",
        "contentRating[]": list(content_ratings),
    }
    data = _get_json("/manga", params)
    if not data or "data" not in data:
        return []
    q_tokens = _tokens(query)
    results: List[MangaMatch] = []
    for entry in data["data"]:
        attrs = entry.get("attributes", {}) or {}
        title = _pick_title(attrs)
        # Score against every title variant — a Japanese main title with
        # an English altTitle shouldn't be penalised for not matching
        # the user's English query.
        score = max((_score_title(q_tokens, t) for t in _all_titles(attrs)),
                    default=0.0)
        results.append(MangaMatch(
            manga_id=entry.get("id", ""),
            title=title,
            score=score,
        ))
    # MangaDex's own relevance ordering is already decent; our token
    # overlap is just the tiebreaker.
    results.sort(key=lambda m: m.score, reverse=True)
    return results


def find_best(query: str, **kwargs) -> Optional[MangaMatch]:
    """Convenience wrapper used by one-shot "latest chapter of X" calls."""
    matches = search_manga(query, **kwargs)
    return matches[0] if matches else None


# ────────────────────── chapters ──────────────────────
def _parse_chapter_num(raw: Optional[str]) -> float:
    """MangaDex sends chapter numbers as strings (or null for oneshots)."""
    if not raw:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        # Some side-chapters are tagged like "Extra" or "65.2"; the
        # second case parses, the first doesn't — treat it as 0 so a
        # genuine numbered chapter always wins the "latest" race.
        return 0.0


def latest_chapter(manga_id: str, *, language: str = "en",
                   content_ratings: tuple[str, ...] = DEFAULT_CONTENT_RATINGS,
                   ) -> Optional[ChapterInfo]:
    """Return the latest numbered chapter in `language`.
    `language="any"` skips the language filter entirely and returns the
    globally-latest chapter — useful when the user tracks the Japanese
    (or Indonesian) raws instead of the official English TL.
    Falls back to any language if the preferred one has nothing."""
    if language in ("any", "*", "all", ""):
        return _fetch_latest(manga_id, None, content_ratings)
    return (_fetch_latest(manga_id, language, content_ratings)
            or _fetch_latest(manga_id, None, content_ratings))


def _fetch_latest(manga_id: str, lang: Optional[str],
                  content_ratings: tuple[str, ...]) -> Optional[ChapterInfo]:
    def _fetch(lang: Optional[str]) -> Optional[ChapterInfo]:
        params: dict = {
            "limit": 1,
            "order[chapter]": "desc",
            "order[publishAt]": "desc",
            "includeExternalUrl": 0,
            "contentRating[]": list(content_ratings),
        }
        if lang:
            params["translatedLanguage[]"] = [lang]
        data = _get_json(f"/manga/{manga_id}/feed", params)
        if not data or not data.get("data"):
            return None
        entry = data["data"][0]
        attrs = entry.get("attributes", {}) or {}
        chap_raw = attrs.get("chapter")
        return ChapterInfo(
            chapter=str(chap_raw or ""),
            chapter_num=_parse_chapter_num(chap_raw),
            title=attrs.get("title") or "",
            language=attrs.get("translatedLanguage") or (lang or ""),
            published_at=attrs.get("publishAt") or attrs.get("readableAt") or "",
            chapter_id=entry.get("id", ""),
            read_url=f"https://mangadex.org/chapter/{entry.get('id', '')}",
        )

    return _fetch(lang)
