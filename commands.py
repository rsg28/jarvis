"""
Command dispatcher for Jarvis.

Each intent is matched with a small regex + handler pair. Adding a new
skill is dropping a new pair into `INTENTS` — nothing else changes.
"""
from __future__ import annotations

import platform
import re
import shlex
import subprocess
import sys
import urllib.parse
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional


@dataclass
class CommandResult:
    speak: Optional[str] = None
    print_out: Optional[str] = None
    should_exit: bool = False


class CommandDispatcher:
    def __init__(self, *, config: dict, voice) -> None:
        self.config = config
        self.voice = voice
        self.apps = config.get("apps", {})

    # ────────────── public entry point ──────────────
    def dispatch(self, text: str) -> CommandResult:
        text = text.strip()
        if not text:
            return CommandResult()

        for pattern, handler in INTENTS:
            match = re.match(pattern, text, re.IGNORECASE)
            if match:
                return handler(self, match)

        # Nothing matched — echo back as a small note
        return CommandResult(
            speak=f"I did not catch that. Say help to see what I can do.",
            print_out=f"[jarvis] not recognized: {text!r}. Try 'help'.",
        )

    # ────────────── skills ──────────────
    def _open_app(self, name: str) -> CommandResult:
        cmd = self.apps.get(name.lower())
        if not cmd:
            return CommandResult(speak=f"I don't know how to open {name} yet.",
                                 print_out=f"[jarvis] no app mapping for {name!r}")
        try:
            if platform.system() == "Windows":
                subprocess.Popen(f"start {cmd}", shell=True)
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", "-a", cmd])
            else:
                subprocess.Popen([cmd])
            return CommandResult(speak=f"Opening {name}.", print_out=f"[jarvis] launched {cmd}")
        except Exception as exc:
            return CommandResult(speak=f"I could not open {name}.",
                                 print_out=f"[jarvis] launch failed: {exc}")

    def _search_web(self, query: str) -> CommandResult:
        url = f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}"
        webbrowser.open(url)
        return CommandResult(speak=f"Searching for {query}.", print_out=f"[jarvis] opened {url}")

    def _play_spotify(self, query: str) -> CommandResult:
        url = f"https://open.spotify.com/search/{urllib.parse.quote_plus(query)}"
        webbrowser.open(url)
        return CommandResult(speak=f"Looking up {query} on Spotify.",
                             print_out=f"[jarvis] opened {url}")

    def _time_now(self, _match) -> CommandResult:
        now = datetime.now().strftime("%I:%M %p")
        return CommandResult(speak=f"It is {now}.", print_out=now)

    def _date_today(self, _match) -> CommandResult:
        today = datetime.now().strftime("%A, %B %d, %Y")
        return CommandResult(speak=f"Today is {today}.", print_out=today)

    def _weather(self, match) -> CommandResult:
        """Uses wttr.in — no API key required. Falls back gracefully."""
        location = (match.groupdict().get("loc") or "Vancouver").strip()
        try:
            import urllib.request
            with urllib.request.urlopen(f"https://wttr.in/{urllib.parse.quote(location)}?format=3", timeout=4) as resp:
                text = resp.read().decode("utf-8").strip()
            return CommandResult(speak=text, print_out=text)
        except Exception:
            return CommandResult(speak=f"I could not reach the weather service.",
                                 print_out="[jarvis] wttr.in unreachable")

    def _news(self, match) -> CommandResult:
        """Read the latest headlines. `news [topic|search phrase]`."""
        import news as news_mod

        topic = (match.groupdict().get("topic") or "world").strip()
        limit = int(self.config.get("news", {}).get("limit", 5))
        items = news_mod.headlines(topic, limit=limit)
        if not items:
            msg = f"No headlines available for {topic}."
            return CommandResult(speak=msg, print_out=f"[jarvis] {msg}")

        printable = [f"── Top {topic} headlines ──"] + [f"  {i+1}. {t}" for i, t in enumerate(items)]
        # Read only the first 3 aloud to keep it snappy
        spoken_bits = [t.split(" - ")[0] for t in items[:3]]
        spoken = f"Here are the latest {topic} headlines. " + ". ".join(spoken_bits) + "."
        return CommandResult(speak=spoken, print_out="\n".join(printable))

    def _soccer(self, match) -> CommandResult:
        """Live scores + fixtures. `scores [league|team]`."""
        import soccer as soccer_mod

        query = (match.groupdict().get("q") or "").strip()
        if not query:
            query = self.config.get("soccer", {}).get("favorite_league", "la liga")
        slug, name = soccer_mod.resolve_league(query)
        games = soccer_mod.scoreboard(slug)
        spoken, printable = soccer_mod.format_scoreboard(games, name)
        return CommandResult(speak=spoken, print_out=printable)

    def _help(self, _match) -> CommandResult:
        lines = [
            "Commands:",
            "  open <app>              e.g. open spotify, open chrome, open code",
            "  search <query>          opens Google search",
            "  play <query>            searches Spotify",
            "  news [<topic>]          world | tech | sports | business | science …",
            "  scores [<league|team>]  la liga, premier, champions, mls, peru …",
            "  what time is it         reads the clock",
            "  what day is it          reads today's date",
            "  weather [<location>]    quick summary via wttr.in",
            "  quit / exit             bye",
        ]
        return CommandResult(print_out="\n".join(lines))

    def _quit(self, _match) -> CommandResult:
        return CommandResult(speak="Signing off. Have a good one.",
                             print_out="[jarvis] goodbye", should_exit=True)


# ────────────── intent table ──────────────
# Order matters: the first match wins.
INTENTS: list[tuple[str, Callable[["CommandDispatcher", re.Match], CommandResult]]] = [
    (r"^(help|what can you do|commands)$",
     lambda d, m: d._help(m)),
    (r"^(quit|exit|bye|goodbye|shutdown)$",
     lambda d, m: d._quit(m)),

    (r"^(what.?s the time|what time is it|current time|time)$",
     lambda d, m: d._time_now(m)),
    (r"^(what day is it|what.?s the date|today.?s date|date)$",
     lambda d, m: d._date_today(m)),

    (r"^weather(?:\s+(?:in|at)\s+(?P<loc>.+))?$",
     lambda d, m: d._weather(m)),

    # News: `news`, `news tech`, `news about ai`, `headlines`, `what's happening`
    (r"^(?:news|headlines)(?:\s+(?:about\s+|on\s+)?(?P<topic>.+))?$",
     lambda d, m: d._news(m)),
    (r"^(?:what.?s\s+(?:new|happening)(?:\s+in\s+(?P<topic>.+))?)$",
     lambda d, m: d._news(m)),

    # Soccer / football scores: `scores`, `scores premier`, `soccer champions`,
    # `football la liga`, `who is playing today`
    (r"^(?:scores|score|soccer|football|fixtures|matches)(?:\s+(?P<q>.+))?$",
     lambda d, m: d._soccer(m)),
    (r"^who(?:'s| is)\s+playing(?:\s+in\s+(?P<q>.+))?(?:\s+today)?$",
     lambda d, m: d._soccer(m)),

    (r"^open\s+(?P<app>.+)$",
     lambda d, m: d._open_app(m.group("app").strip())),
    (r"^launch\s+(?P<app>.+)$",
     lambda d, m: d._open_app(m.group("app").strip())),

    (r"^(?:google|search)\s+(?:for\s+)?(?P<q>.+)$",
     lambda d, m: d._search_web(m.group("q").strip())),
    (r"^(?:look\s+up|find)\s+(?P<q>.+)$",
     lambda d, m: d._search_web(m.group("q").strip())),

    (r"^(?:play|spotify)\s+(?P<q>.+)$",
     lambda d, m: d._play_spotify(m.group("q").strip())),
]
