"""
Persistent memory for Jarvis.

Two tiers, both plain files next to config.toml so you can inspect or
hand-edit them any time:

  memory_long.json   — explicit, durable facts about the user, their
                       preferences, people, projects. Injected into the
                       LLM's system prompt on EVERY inference so Jarvis
                       always "knows" them. Rotation: capped at
                       MAX_LONG_FACTS entries to keep context small.

  memory_recent.jsonl — append-only log of recent user turns + Jarvis
                        replies with timestamps. Last RECENT_IN_CONTEXT
                        entries are injected as prior turns so follow-up
                        questions ("and tomorrow?", "same but longer")
                        have anchor across restarts. Rotation: whole file
                        trimmed to the last MAX_RECENT_LINES on each
                        append.

Design notes:
  * JSON for long-term because facts are structured and must survive
    hand-editing; JSONL for recent because every turn appends O(1).
  * No locking: single-process by design. If you ever run two Jarvis
    instances, the recent log is still safe (append-only), but the
    long-term file could race. Not fixing that until it matters.
  * No embeddings here — this is simple keyword/substring memory. The
    RAG layer in knowledge.py is for documents; this is for identity.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


MAX_LONG_FACTS      = 100
MAX_RECENT_LINES    = 500
RECENT_IN_CONTEXT   = 10


@dataclass
class LongFact:
    text: str
    added_ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {"text": self.text, "added_ts": self.added_ts}

    @classmethod
    def from_dict(cls, d: Dict) -> "LongFact":
        return cls(text=str(d.get("text", "")),
                   added_ts=float(d.get("added_ts", time.time())))


class Memory:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.long_path   = self.root / "memory_long.json"
        self.recent_path = self.root / "memory_recent.jsonl"
        self._long: List[LongFact] = []
        self._load_long()

    # ────────────── long-term ──────────────
    def _load_long(self) -> None:
        if not self.long_path.exists():
            self._long = []
            return
        try:
            data = json.loads(self.long_path.read_text(encoding="utf-8"))
            raw = data if isinstance(data, list) else data.get("facts", [])
            self._long = [LongFact.from_dict(d) for d in raw if d.get("text")]
        except Exception as exc:
            logging.warning("[memory] long load failed: %s", exc)
            self._long = []

    def _save_long(self) -> None:
        try:
            payload = {"facts": [f.to_dict() for f in self._long]}
            self.long_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8")
        except Exception as exc:
            logging.warning("[memory] long save failed: %s", exc)

    def remember(self, text: str) -> Tuple[bool, str]:
        """Add a fact. Returns (added, reason-if-not)."""
        text = (text or "").strip()
        if not text:
            return False, "empty fact"
        if len(text) > 400:
            return False, "fact too long (max 400 chars)"
        # Dedupe: case-insensitive match on text
        low = text.lower()
        for f in self._long:
            if f.text.lower() == low:
                return False, "already remembered"
        self._long.append(LongFact(text=text))
        # Rotate oldest
        if len(self._long) > MAX_LONG_FACTS:
            self._long = self._long[-MAX_LONG_FACTS:]
        self._save_long()
        return True, "added"

    def forget(self, query: str, *, extra_needles: Optional[List[str]] = None) -> List[str]:
        """Remove any facts whose text contains `query` (case-insensitive).
        Returns the list of removed fact texts."""
        q = (query or "").strip().lower()
        if not q:
            return []
        if q in ("everything", "all", "todo", "todos"):
            removed = [f.text for f in self._long]
            self._long = []
            self._save_long()
            return removed
        removed, keep = [], []
        for f in self._long:
            if q in f.text.lower():
                removed.append(f.text)
            else:
                keep.append(f)
        if removed:
            self._long = keep
            self._save_long()
            # Also prune the recent-exchanges log of any lines mentioning
            # the forgotten text — otherwise the LLM can parrot its own
            # previous answer from the seeded chat history even though
            # the long-term fact is gone.
            # Build needles: the removed fact texts PLUS any
            # non-stopword tokens of the query (len>=4) so Spanish
            # paraphrases like "lo del cumple" still match jarvis log
            # lines containing "cumpleaños".
            STOP = {"this", "that", "about", "lo", "la", "el", "las",
                    "los", "del", "the", "and", "mi", "tu", "su"}
            tokens = [t for t in re.findall(r"[a-záéíóúñ]{4,}", q)
                      if t not in STOP]
            # Also tokenize any extra needles (e.g. the raw user_text
            # which may be in a different language than the stored fact).
            for extra in (extra_needles or []):
                if not extra:
                    continue
                elc = extra.lower()
                extra_tokens = [t for t in re.findall(r"[a-záéíóúñ]{4,}", elc)
                                if t not in STOP]
                tokens.extend(extra_tokens)
            needles = list(removed) + [q] + tokens
            try:
                self._prune_recent_matching(needles)
            except Exception as exc:
                logging.debug("[memory] recent prune failed: %s", exc)
        return removed

    def search_long(self, query: str) -> List[str]:
        q = (query or "").strip().lower()
        if not q:
            return [f.text for f in self._long]
        return [f.text for f in self._long if q in f.text.lower()]

    def list_long(self) -> List[str]:
        return [f.text for f in self._long]

    # ────────────── recent (JSONL append-only) ──────────────
    def append_recent(self, user_text: str, jarvis_reply: str) -> None:
        user_text   = (user_text or "").strip()
        jarvis_reply = (jarvis_reply or "").strip()
        if not user_text or not jarvis_reply:
            return
        line = json.dumps(
            {"ts": time.time(), "u": user_text[:600], "j": jarvis_reply[:600]},
            ensure_ascii=False) + "\n"
        try:
            with self.recent_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
            # Rotate: keep last MAX_RECENT_LINES only. Rewrites the
            # whole file but only when we exceed; cheap for 500 lines.
            self._maybe_rotate_recent()
        except Exception as exc:
            logging.warning("[memory] recent append failed: %s", exc)

    def _maybe_rotate_recent(self) -> None:
        try:
            if not self.recent_path.exists():
                return
            lines = self.recent_path.read_text(encoding="utf-8").splitlines()
            if len(lines) <= MAX_RECENT_LINES:
                return
            trimmed = lines[-MAX_RECENT_LINES:]
            self.recent_path.write_text("\n".join(trimmed) + "\n",
                                        encoding="utf-8")
        except Exception as exc:
            logging.debug("[memory] rotate skipped: %s", exc)

    def tail_recent(self, n: int = RECENT_IN_CONTEXT) -> List[Tuple[str, str, float]]:
        """Return the last n (user, jarvis, ts) tuples."""
        if not self.recent_path.exists():
            return []
        try:
            lines = self.recent_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
        out: List[Tuple[str, str, float]] = []
        for line in lines[-n:]:
            try:
                d = json.loads(line)
                out.append((str(d.get("u", "")), str(d.get("j", "")),
                            float(d.get("ts", 0))))
            except Exception:
                continue
        return out

    def _prune_recent_matching(self, needles: List[str]) -> int:
        """Drop any JSONL lines whose user OR jarvis text contains ANY of
        the given needles (case-insensitive substring). Returns # removed."""
        if not self.recent_path.exists():
            return 0
        needles_lc = [n.lower() for n in needles if n and n.strip()]
        if not needles_lc:
            return 0
        try:
            lines = self.recent_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return 0
        kept: List[str] = []
        dropped = 0
        for line in lines:
            try:
                d = json.loads(line)
                blob = (str(d.get("u", "")) + " " + str(d.get("j", ""))).lower()
            except Exception:
                kept.append(line)
                continue
            if any(n in blob for n in needles_lc):
                dropped += 1
                continue
            kept.append(line)
        if dropped:
            self.recent_path.write_text(
                ("\n".join(kept) + "\n") if kept else "",
                encoding="utf-8")
        return dropped

    def clear_recent(self) -> int:
        if not self.recent_path.exists():
            return 0
        try:
            n = sum(1 for _ in self.recent_path.open("r", encoding="utf-8"))
            self.recent_path.unlink()
            return n
        except Exception:
            return 0

    # ────────────── LLM context block ──────────────
    def context_block(self) -> str:
        """Format long-term facts as a system-prompt fragment."""
        if not self._long:
            # Empty-memory marker: tells the LLM to answer "I don't
            # remember" instead of inventing details when asked about
            # personal facts (name, birthday, pet, address, job, etc.).
            return (
                "============ What you remember about this user ============\n"
                "You currently have NO stored facts about this user.\n"
                "If the user asks a personal question (name, birthday,\n"
                "pet's name, address, favorite anything, etc.), do NOT\n"
                "invent a value. Reply in `chat` saying honestly that\n"
                "you don't remember and they haven't told you yet.\n"
            )
        bullets = "\n".join(f"- {f.text}" for f in self._long)
        return (
            "============ What you remember about this user ============\n"
            "These are durable facts the user has previously asked you to\n"
            "remember. Use them to answer questions ABOUT the user.\n\n"
            "CRITICAL ROUTING RULES for these facts:\n"
            "  * If the user ASKS about one of them (\"what's my cat's\n"
            "    name?\", \"cuando es mi cumple?\"), ANSWER directly with\n"
            "    chat using the fact. DO NOT emit `remember` — that would\n"
            "    try to re-save a fact you already have.\n"
            "  * Only emit `remember <new fact>` when the user TELLS you\n"
            "    something new (\"recuerda que X\", \"remember that Y\").\n"
            "  * Only emit `forget <query>` when the user tells you to\n"
            "    drop a memory (\"olvida X\", \"forget X\").\n\n"
            f"{bullets}\n"
        )
