"""
Command dispatcher for Jarvis.

Each intent is matched with a small regex + handler pair. Adding a new
skill is dropping a new pair into `INTENTS` — nothing else changes.
"""
from __future__ import annotations

import platform
import re
import subprocess
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
    # Optional signal to the HUD process ("help" opens the command panel).
    ui_action: Optional[str] = None


class CommandDispatcher:
    def __init__(self, *, config: dict, voice) -> None:
        self.config = config
        self.voice = voice
        self.apps = config.get("apps", {})
        # Lazy scheduler — created on first timer/reminder
        self._scheduler = None

    def _get_scheduler(self):
        if self._scheduler is None:
            from scheduler import Scheduler
            # notify callback speaks the message; runs on a daemon thread
            self._scheduler = Scheduler(notify=lambda msg: self.voice.say(msg))
        return self._scheduler

    # ────────────── public entry point ──────────────
    def dispatch(self, text: str) -> CommandResult:
        text = text.strip()
        if not text:
            return CommandResult()

        for pattern, handler in INTENTS:
            match = re.match(pattern, text, re.IGNORECASE)
            if match:
                return handler(self, match)

        return CommandResult(
            speak="I did not catch that. Say help to see what I can do.",
            print_out=f"[jarvis] not recognized: {text!r}. Try 'help'.",
        )

    # ────────────── existing skills ──────────────
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
        location = (match.groupdict().get("loc") or "Vancouver").strip()
        try:
            import urllib.request
            with urllib.request.urlopen(
                f"https://wttr.in/{urllib.parse.quote(location)}?format=3", timeout=4
            ) as resp:
                text = resp.read().decode("utf-8").strip()
            return CommandResult(speak=text, print_out=text)
        except Exception:
            return CommandResult(speak="I could not reach the weather service.",
                                 print_out="[jarvis] wttr.in unreachable")

    def _news(self, match) -> CommandResult:
        import news as news_mod
        topic = (match.groupdict().get("topic") or "world").strip()
        limit = int(self.config.get("news", {}).get("limit", 5))
        items = news_mod.headlines(topic, limit=limit)
        if not items:
            msg = f"No headlines available for {topic}."
            return CommandResult(speak=msg, print_out=f"[jarvis] {msg}")
        printable = [f"── Top {topic} headlines ──"] + [f"  {i+1}. {t}" for i, t in enumerate(items)]
        spoken_bits = [t.split(" - ")[0] for t in items[:3]]
        spoken = f"Here are the latest {topic} headlines. " + ". ".join(spoken_bits) + "."
        return CommandResult(speak=spoken, print_out="\n".join(printable))

    def _soccer(self, match) -> CommandResult:
        import soccer as soccer_mod
        query = (match.groupdict().get("q") or "").strip()
        if not query:
            query = self.config.get("soccer", {}).get("favorite_league", "la liga")
        slug, name = soccer_mod.resolve_league(query)
        games = soccer_mod.scoreboard(slug)
        spoken, printable = soccer_mod.format_scoreboard(games, name)
        return CommandResult(speak=spoken, print_out=printable)

    # ────────────── new: system info ──────────────
    def _system(self, key: str) -> CommandResult:
        import system as sys_mod
        table = {
            "battery": sys_mod.battery,
            "cpu":     sys_mod.cpu,
            "ram":     sys_mod.ram,
            "memory":  sys_mod.ram,
            "disk":    sys_mod.disk,
            "ip":      sys_mod.ip_address,
            "network": sys_mod.ip_address,
            "wifi":    sys_mod.wifi,
        }
        fn = table.get(key.lower())
        if not fn:
            return CommandResult(speak=f"I don't have a reading for {key}.")
        text = fn() or f"Could not read {key}."
        return CommandResult(speak=text, print_out=text)

    # ────────────── new: volume + media ──────────────
    def _volume(self, action: str, value: Optional[int] = None) -> CommandResult:
        import system as sys_mod
        try:
            if action == "up":
                msg = sys_mod.volume_up(steps=value or 5)
            elif action == "down":
                msg = sys_mod.volume_down(steps=value or 5)
            elif action == "mute":
                msg = sys_mod.volume_mute()
            elif action == "set":
                msg = sys_mod.volume_set(value or 50)
            elif action == "get":
                msg = sys_mod.volume_get()
            else:
                msg = "Unknown volume command."
        except Exception as exc:
            msg = f"Volume command failed: {exc}"
        return CommandResult(speak=msg, print_out=f"[jarvis] {msg}")

    def _media(self, action: str) -> CommandResult:
        import system as sys_mod
        try:
            if action in ("pause", "play", "toggle"):
                msg = sys_mod.media_play_pause()
            elif action == "next":
                msg = sys_mod.media_next()
            elif action == "prev":
                msg = sys_mod.media_previous()
            else:
                msg = "Unknown media command."
        except Exception as exc:
            msg = f"Media command failed: {exc}"
        return CommandResult(speak=msg, print_out=f"[jarvis] {msg}")

    # ────────────── new: screenshot ──────────────
    def _screenshot(self, _match) -> CommandResult:
        import system as sys_mod
        try:
            msg = sys_mod.screenshot()
        except Exception as exc:
            msg = f"Screenshot failed: {exc}"
        return CommandResult(speak="Screenshot saved to your desktop.", print_out=f"[jarvis] {msg}")

    # ────────────── new: jokes / trivia ──────────────
    def _joke(self, _match) -> CommandResult:
        import fun as fun_mod
        text = fun_mod.joke()
        return CommandResult(speak=text, print_out=text)

    def _trivia(self, _match) -> CommandResult:
        import fun as fun_mod
        text = fun_mod.trivia()
        return CommandResult(speak=text, print_out=text)

    # ────────────── new: timers / pomodoro / reminders ──────────────
    def _timer(self, match) -> CommandResult:
        from scheduler import parse_duration, humanize
        raw = match.groupdict().get("dur", "").strip()
        seconds = parse_duration(raw)
        if not seconds:
            return CommandResult(speak="Tell me how long, for example: set a timer for 5 minutes.")
        sched = self._get_scheduler()
        sched.add("timer", seconds, raw)
        return CommandResult(
            speak=f"Timer set for {humanize(seconds)}.",
            print_out=f"[jarvis] timer scheduled for {humanize(seconds)}",
        )

    def _pomodoro(self, _match) -> CommandResult:
        from scheduler import humanize
        seconds = int(self.config.get("pomodoro", {}).get("work_minutes", 25)) * 60
        sched = self._get_scheduler()
        sched.add("pomodoro", seconds, "focus block")
        return CommandResult(
            speak=f"Pomodoro started. Focus for {humanize(seconds)}. I will tell you when to break.",
            print_out=f"[jarvis] pomodoro focus block: {humanize(seconds)}",
        )

    def _remind(self, match) -> CommandResult:
        from scheduler import parse_duration, humanize
        raw = match.groupdict().get("dur", "").strip()
        text = match.groupdict().get("what", "").strip()
        seconds = parse_duration(raw)
        if not seconds or not text:
            return CommandResult(speak="Try: remind me in 30 minutes to stretch.")
        sched = self._get_scheduler()
        sched.add("reminder", seconds, text)
        return CommandResult(
            speak=f"Okay, in {humanize(seconds)} I will remind you to {text}.",
            print_out=f"[jarvis] reminder in {humanize(seconds)}: {text}",
        )

    def _list_jobs(self, _match) -> CommandResult:
        if self._scheduler is None:
            return CommandResult(speak="No timers or reminders yet.", print_out="[jarvis] no jobs")
        jobs = self._scheduler.active()
        if not jobs:
            return CommandResult(speak="No active timers or reminders.", print_out="[jarvis] no jobs")
        from scheduler import humanize
        lines = ["── Active jobs ──"]
        for j in jobs:
            lines.append(f"  #{j.id} [{j.kind}] {j.label or ''} — {humanize(j.seconds_left())} left")
        return CommandResult(speak=f"You have {len(jobs)} active jobs.", print_out="\n".join(lines))

    def _cancel_jobs(self, _match) -> CommandResult:
        if self._scheduler is None:
            return CommandResult(speak="Nothing to cancel.")
        n = self._scheduler.cancel_all()
        return CommandResult(
            speak=f"Cancelled {n} active job{'s' if n != 1 else ''}." if n else "Nothing to cancel.",
            print_out=f"[jarvis] cancelled {n} jobs",
        )

    # ────────────── new: spanish voice toggle ──────────────
    def _speak_lang(self, lang: str) -> CommandResult:
        table = {
            "english":  "en-US-JennyNeural",
            "spanish":  "es-MX-DaliaNeural",
            "french":   "fr-FR-DeniseNeural",
            "british":  "en-GB-SoniaNeural",
        }
        voice_name = table.get(lang.lower())
        if not voice_name:
            return CommandResult(speak=f"I don't have a {lang} voice configured.")
        self.voice.set_voice(voice_name)
        # Speak one confirmation line in the new voice.
        msgs = {
            "english":  "Voice switched to English.",
            "spanish":  "Voz cambiada al español, listo.",
            "french":   "Voix française activée.",
            "british":  "Switched to British English.",
        }
        line = msgs[lang.lower()]
        return CommandResult(speak=line, print_out=f"[jarvis] voice → {voice_name}")

    # ────────────── help / quit ──────────────
    def _help(self, _match) -> CommandResult:
        # In UI mode this pops open a stylised help panel next to the orb.
        # In text/wake mode the same lines print to stdout via `print_out`.
        lines = [
            "Commands:",
            "  open <app>              e.g. open spotify, open chrome, open code",
            "  search <query>          Google search in your browser",
            "  play <query>            search Spotify",
            "  play/pause | next | previous     media control keys",
            "  news [<topic>]          world | tech | sports | business | science …",
            "  scores [<league|team>]  la liga, premier, champions, mls, peru …",
            "  weather [<location>]    quick summary via wttr.in",
            "  what time is it | what day is it",
            "  battery | cpu | ram | disk | ip | wifi",
            "  volume up|down|mute     set volume <0-100>       what's the volume",
            "  screenshot              saves to your Desktop",
            "  set a timer for 25 minutes",
            "  pomodoro                25-minute focus block",
            "  remind me in 30 minutes to stretch",
            "  timers                  list active timers/reminders",
            "  cancel timers           cancel all",
            "  joke | trivia",
            "  speak spanish | speak english | speak french | speak british",
            "  stop listening          end the conversation, back to wake mode",
            "  quit / exit             shut Jarvis down",
        ]
        return CommandResult(
            speak="Opening the command panel, sir.",
            print_out="\n".join(lines),
            ui_action="help",
        )

    def _quit(self, _match) -> CommandResult:
        return CommandResult(speak="Signing off. Have a productive day.",
                             print_out="[jarvis] goodbye", should_exit=True)

    def _stop_listening(self, _match) -> CommandResult:
        # Soft stop — end the current conversation, stay resident, wait
        # for another wake word. Wake listener already returned by the
        # time this fires; we just need a spoken acknowledgement.
        return CommandResult(speak="Standing by, sir. Just say 'hey jarvis' when you need me.")


# ────────────── intent table ──────────────
# Order matters: the first match wins.
INTENTS: list[tuple[str, Callable[["CommandDispatcher", re.Match], CommandResult]]] = [
    (r"^__stop_listening__$",
     lambda d, m: d._stop_listening(m)),
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

    # News
    (r"^(?:news|headlines)(?:\s+(?:about\s+|on\s+)?(?P<topic>.+))?$",
     lambda d, m: d._news(m)),
    (r"^(?:what.?s\s+(?:new|happening)(?:\s+in\s+(?P<topic>.+))?)$",
     lambda d, m: d._news(m)),

    # Soccer
    (r"^(?:scores|score|soccer|football|fixtures|matches)(?:\s+(?P<q>.+))?$",
     lambda d, m: d._soccer(m)),
    (r"^who(?:'s| is)\s+playing(?:\s+in\s+(?P<q>.+))?(?:\s+today)?$",
     lambda d, m: d._soccer(m)),

    # System info — battery / cpu / ram / disk / ip / wifi
    (r"^(?:what.?s\s+(?:the|my)\s+)?(?P<key>battery|cpu|ram|memory|disk|storage|ip|network|wifi|wi-fi)(?:\s+.*)?$",
     lambda d, m: d._system(m.group("key").replace("wi-fi", "wifi").replace("storage", "disk").replace("network", "ip"))),

    # Volume
    (r"^(?:volume\s+up|louder|turn\s+it\s+up)(?:\s+(?P<n>\d+))?$",
     lambda d, m: d._volume("up", int(m.group("n") or 5))),
    (r"^(?:volume\s+down|quieter|turn\s+it\s+down)(?:\s+(?P<n>\d+))?$",
     lambda d, m: d._volume("down", int(m.group("n") or 5))),
    (r"^(?:mute|unmute)$",
     lambda d, m: d._volume("mute")),
    (r"^set\s+volume\s+(?:to\s+)?(?P<n>\d+)%?$",
     lambda d, m: d._volume("set", int(m.group("n")))),
    (r"^(?:what.?s\s+the\s+volume|volume|current\s+volume)$",
     lambda d, m: d._volume("get")),

    # Media control (media keys work with Spotify, YouTube, VLC, etc.)
    (r"^(?:pause|play|resume|toggle\s+playback)$",
     lambda d, m: d._media("toggle")),
    (r"^(?:next(?:\s+(?:song|track))?|skip)$",
     lambda d, m: d._media("next")),
    (r"^(?:previous(?:\s+(?:song|track))?|back|prev)$",
     lambda d, m: d._media("prev")),

    # Screenshot
    (r"^(?:screenshot|screen\s+shot|capture\s+screen|take\s+a\s+screenshot)$",
     lambda d, m: d._screenshot(m)),

    # Timers / Pomodoro / Reminders
    (r"^(?:set\s+(?:a\s+)?timer(?:\s+for)?\s+|timer\s+)(?P<dur>.+)$",
     lambda d, m: d._timer(m)),
    (r"^pomodoro$",
     lambda d, m: d._pomodoro(m)),
    (r"^remind\s+me\s+in\s+(?P<dur>.+?)\s+to\s+(?P<what>.+)$",
     lambda d, m: d._remind(m)),
    (r"^(?:list\s+timers|timers|active\s+jobs|reminders)$",
     lambda d, m: d._list_jobs(m)),
    (r"^cancel\s+(?:all\s+)?(?:timers|reminders|jobs)$",
     lambda d, m: d._cancel_jobs(m)),

    # Jokes / trivia
    (r"^(?:tell\s+me\s+a\s+)?joke$",
     lambda d, m: d._joke(m)),
    (r"^(?:trivia|random\s+fact|fun\s+fact)$",
     lambda d, m: d._trivia(m)),

    # Voice language toggle
    (r"^(?:speak|switch\s+to)\s+(?P<lang>english|spanish|french|british)$",
     lambda d, m: d._speak_lang(m.group("lang"))),

    # App launcher — MUST come after the specific commands above
    (r"^open\s+(?P<app>.+)$",
     lambda d, m: d._open_app(m.group("app").strip())),
    (r"^launch\s+(?P<app>.+)$",
     lambda d, m: d._open_app(m.group("app").strip())),

    # Search
    (r"^(?:google|search)\s+(?:for\s+)?(?P<q>.+)$",
     lambda d, m: d._search_web(m.group("q").strip())),
    (r"^(?:look\s+up|find)\s+(?P<q>.+)$",
     lambda d, m: d._search_web(m.group("q").strip())),

    # Spotify search — `play <song>` and `spotify <song>`
    (r"^(?:play|spotify)\s+(?P<q>.+)$",
     lambda d, m: d._play_spotify(m.group("q").strip())),
]
