"""
Manga watchlist state + "did a new chapter drop?" check.

Config schema (config.toml):

    [manga]
    enabled = true
    language = "en"             # preferred translation language
    check_on_boot = true        # run a check when the hotkey loop arms
    boot_delay_seconds = 4      # wait a bit so the greeting plays first

    [[manga.series]]
    name = "Smoking Behind the Supermarket With You"
    # optional: pin the MangaDex ID so we never re-search
    # manga_id = "..."

State file: manga_state.json (next to config.toml). Shape:

    {
      "<manga_id>": {
        "name": "...",
        "last_seen_chapter": "65",
        "last_seen_num": 65.0,
        "checked_at": "2026-10-01T20:00:00Z"
      }
    }

The state file is created on first successful check — the FIRST run
never speaks "new chapter!" because we have no baseline to compare
against; it just records what's currently out. From then on, any
numeric increase triggers the announcement.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import manga as _mx
import comix as _cx


STATE_FILENAME = "manga_state.json"


@dataclass
class NewChapter:
    series_name: str
    previous: str   # last chapter we had on record ("" if first-ever run)
    chapter: str
    title: str      # chapter title (may be empty)
    read_url: str


# ────────────────────── state file ──────────────────────
def _state_path(config_dir: Path) -> Path:
    return config_dir / STATE_FILENAME


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logging.warning("[manga] state file unreadable, starting fresh: %s", exc)
        return {}


def _save_state(path: Path, state: dict) -> None:
    try:
        path.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    except Exception as exc:
        logging.warning("[manga] could not persist state: %s", exc)


# ────────────────────── id resolution ──────────────────────
def _resolve_manga_id(series_cfg: dict, *, language: str = "en",
                      ) -> Optional[tuple[str, str]]:
    """Return (manga_id, resolved_title). If the config pinned an id we
    trust it; otherwise we search by `name`, iterate the top candidates
    and keep the first one that actually has a translated feed (so
    "Fan Colored" / fan-TL dupes without chapters don't win)."""
    manga_id = (series_cfg.get("manga_id") or "").strip()
    name = (series_cfg.get("name") or "").strip()
    if manga_id:
        return manga_id, name or manga_id
    if not name:
        return None
    for cand in _mx.search_manga(name, limit=5):
        if cand.score < 0.3:
            continue
        info = _mx.latest_chapter(cand.manga_id, language=language)
        if info is not None and info.chapter:
            return cand.manga_id, cand.title
    return None


# ────────────────────── public API ──────────────────────
def check_all(config: dict, config_dir: Path) -> List[NewChapter]:
    """Check every configured series. Returns the list of NEW chapters
    detected on this run. State is updated (and persisted) regardless
    so a crash mid-run doesn't cause duplicate announcements next time.
    """
    manga_cfg = config.get("manga", {}) or {}
    if not manga_cfg.get("enabled", True):
        return []
    series_list = manga_cfg.get("series", []) or []
    if not series_list:
        return []
    language = str(manga_cfg.get("language", "en"))

    state_file = _state_path(config_dir)
    state = _load_state(state_file)
    new_drops: List[NewChapter] = []
    changed = False

    for series_cfg in series_list:
        source = str(series_cfg.get("source", "mangadex")).lower()

        # ─── Source: Comix.to (scrapes the page's #initial-data JSON) ───
        if source == "comix":
            url = (series_cfg.get("url") or "").strip()
            if not url:
                logging.warning("[manga] comix series %r has no `url`",
                                series_cfg.get("name"))
                continue
            info_cx = _cx.latest_chapter_from_url(url)
            if info_cx is None or not info_cx.chapter:
                logging.info("[manga] comix: no chapters found for %r",
                             series_cfg.get("name"))
                continue
            # Build a synthetic manga_id so state keys stay stable
            # even if the user renames the series in config.
            manga_id = f"comix:{_cx._normalize_title_url(url)}"
            info = _cx.ChapterInfo(
                chapter=info_cx.chapter,
                chapter_num=info_cx.chapter_num,
                title=info_cx.title,
                language=info_cx.language,
                published_at=info_cx.published_at,
                chapter_id=info_cx.chapter_id,
                read_url=info_cx.read_url,
            )
            display_name = (series_cfg.get("name") or info.title or url).strip()

        # ─── Source: MangaDex (default) ───
        else:
            series_lang = str(series_cfg.get("language", language))
            resolved = _resolve_manga_id(series_cfg, language=series_lang)
            if resolved is None:
                logging.warning("[manga] couldn't resolve %r on MangaDex",
                                series_cfg.get("name"))
                continue
            manga_id, resolved_title = resolved
            display_name = (series_cfg.get("name") or resolved_title).strip()
            info = _mx.latest_chapter(manga_id, language=series_lang)
            if info is None or not info.chapter:
                logging.info("[manga] no chapters yet for %r", display_name)
                continue

        prev_entry = state.get(manga_id, {})
        prev_num = float(prev_entry.get("last_seen_num") or 0.0)
        prev_str = str(prev_entry.get("last_seen_chapter") or "")

        is_first_run = manga_id not in state
        is_new = (not is_first_run) and info.chapter_num > prev_num

        if is_new:
            new_drops.append(NewChapter(
                series_name=display_name,
                previous=prev_str,
                chapter=info.chapter,
                title=info.title,
                read_url=info.read_url,
            ))

        state[manga_id] = {
            "name": display_name,
            "last_seen_chapter": info.chapter,
            "last_seen_num": info.chapter_num,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "last_chapter_url": info.read_url,
        }
        changed = True

    if changed:
        _save_state(state_file, state)
    return new_drops


def format_announcement(drops: List[NewChapter]) -> str:
    """Short, spoken-friendly summary of the new chapters."""
    if not drops:
        return ""
    if len(drops) == 1:
        d = drops[0]
        if d.previous:
            return (f"Heads up, chapter {d.chapter} of "
                    f"{d.series_name} is out. You were on {d.previous}.")
        return f"Heads up, chapter {d.chapter} of {d.series_name} is out."
    names = ", ".join(f"{d.series_name} ({d.chapter})" for d in drops)
    return f"Heads up, {len(drops)} of your manga have new chapters: {names}."


def summary_for_log(drops: List[NewChapter]) -> str:
    if not drops:
        return "[manga] no new chapters."
    lines = ["[manga] NEW CHAPTERS:"]
    for d in drops:
        prev = f" (was {d.previous})" if d.previous else " (first check)"
        lines.append(f"  • {d.series_name}: ch.{d.chapter}{prev}  {d.read_url}")
    return "\n".join(lines)


# ────────────────────── one-off helpers used by voice commands ──────────────────────
def latest_for_query(query: str, language: str = "en") -> Optional[dict]:
    """Used by the voice command 'latest chapter of X'. Returns a small
    dict ready to be spoken, or None if nothing matched."""
    best = _mx.find_best(query)
    if best is None:
        return None
    info = _mx.latest_chapter(best.manga_id, language=language)
    if info is None or not info.chapter:
        return {"title": best.title, "chapter": None, "read_url": ""}
    return {
        "title": best.title,
        "chapter": info.chapter,
        "chapter_title": info.title,
        "read_url": info.read_url,
        "published_at": info.published_at,
        "manga_id": best.manga_id,
    }
