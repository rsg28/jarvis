"""
Command dispatcher for Jarvis.

Each intent is matched with a small regex + handler pair. Adding a new
skill is dropping a new pair into `INTENTS` — nothing else changes.
"""
from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
import urllib.parse
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
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
        # Lazy LLM fallback — only built if [llm].enabled = true and a key
        # is available. `None` means "regex-only mode".
        from llm import build_from_config
        self._llm = build_from_config(config)
        # Re-entrancy guard so an LLM-produced command that also fails to
        # match never triggers another LLM round-trip.
        self._in_llm_dispatch = False

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
                result = handler(self, match)
                self._remember(text, result)
                return result

        # Nothing matched — try the LLM before giving up.
        if self._llm is not None and not self._in_llm_dispatch:
            llm_result = self._try_llm(text)
            if llm_result is not None:
                self._remember(text, llm_result)
                return llm_result

        return CommandResult(
            speak="I did not catch that. Say help to see what I can do.",
            print_out=f"[jarvis] not recognized: {text!r}. Try 'help'.",
        )

    # ────────────── LLM fallback ──────────────
    def _try_llm(self, text: str) -> Optional[CommandResult]:
        """Ask Gemini to convert the transcript into either a canonical
        command (re-dispatched here) or a short chat reply (spoken)."""
        parsed = self._llm.infer(text)
        if not parsed:
            return None

        action = (parsed.get("action") or "").lower()

        if action == "call_intent":
            command = (parsed.get("command") or "").strip()
            if not command:
                return None
            # Re-dispatch the LLM's canonical command through the same
            # regex table so the execution path is identical to a typed
            # command. The re-entrancy guard prevents an LLM ping-pong.
            self._in_llm_dispatch = True
            try:
                inner = self.dispatch(command)
            finally:
                self._in_llm_dispatch = False
            # If even the canonical command failed to match, treat the
            # LLM's own reply (if any) as a chat fallback.
            if inner.speak and not inner.speak.startswith("I did not catch that"):
                return inner
            reply = (parsed.get("reply") or "").strip()
            if reply:
                return CommandResult(speak=reply, print_out=reply)
            return inner

        if action == "chat":
            reply = (parsed.get("reply") or "").strip()
            if not reply:
                return None
            return CommandResult(speak=reply, print_out=reply)

        return None

    def _remember(self, user_text: str, result: CommandResult) -> None:
        """Feed successful exchanges back into the LLM's context window
        so follow-up questions like 'and tomorrow?' have anchor."""
        if self._llm is None:
            return
        reply = result.speak or result.print_out or ""
        if reply:
            self._llm.remember(user_text, reply)

    # ────────────── existing skills ──────────────
    def _open_app(self, name: str) -> CommandResult:
        """Open anything by name. Resolution order:

        1. Explicit `apps` mapping in config (fastest, always wins).
        2. Direct URL / path if the target already looks like one.
        3. Fuzzy resolver against Desktop / Documents / Downloads / Start
           Menu shortcuts, plus any extra roots configured in `[resolver]`.
        4. Windows shell fallback (`start "" name`) which works for things
           on PATH like `notepad`, `calc`, `wt`, `code`.
        """
        name = (name or "").strip().strip('"').strip("'")
        if not name:
            return CommandResult(speak="Open what, sir?", print_out="[jarvis] empty open target")

        # 1. Explicit mapping — user's curated shortcuts.
        cmd = self.apps.get(name.lower())
        if cmd:
            return self._launch(cmd, spoken=f"Opening {name}.",
                                print_line=f"[jarvis] launched mapping {cmd!r}")

        # 2. URL?
        import resolver as _r
        if _r.looks_like_url(name):
            url = _r.normalize_url(name)
            webbrowser.open(url)
            return CommandResult(speak=f"Opening {name} in the browser.",
                                 print_out=f"[jarvis] opened {url}")

        # 3. Absolute / relative path?
        if _r.looks_like_path(name):
            path = _r.resolve_path(name)
            if path is not None:
                return self._open_resolved(path, name)
            return CommandResult(speak=f"I couldn't find {name}.",
                                 print_out=f"[jarvis] path not found: {name}")

        # 4. Fuzzy search across common roots.
        extra = self.config.get("resolver", {}).get("extra_roots", []) or []
        matches = _r.find_targets(
            name,
            roots=extra,
            kinds=("app", "file", "folder"),
            limit=3,
        )
        if matches:
            top = matches[0]
            return self._open_resolved(Path(top.path), top.name,
                                       extra_hint=self._hint_others(matches[1:]))

        # 5. Windows shell fallback — lets `open notepad` / `open wt` work.
        if platform.system() == "Windows":
            try:
                subprocess.Popen(f'start "" {name}', shell=True)
                return CommandResult(speak=f"Opening {name}.",
                                     print_out=f"[jarvis] shell start {name}")
            except Exception as exc:
                logging.debug("shell start failed for %s: %s", name, exc)

        return CommandResult(speak=f"I could not find {name} anywhere.",
                             print_out=f"[jarvis] no resolver hit for {name!r}")

    def _launch(self, cmd: str, *, spoken: str, print_line: str) -> CommandResult:
        try:
            if platform.system() == "Windows":
                subprocess.Popen(f"start {cmd}", shell=True)
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", "-a", cmd])
            else:
                subprocess.Popen([cmd])
            return CommandResult(speak=spoken, print_out=print_line)
        except Exception as exc:
            return CommandResult(speak="I could not open that.",
                                 print_out=f"[jarvis] launch failed: {exc}")

    def _open_resolved(self, path: Path, spoken_name: str,
                       *, extra_hint: str = "") -> CommandResult:
        """Open a filesystem target we've already resolved. Uses the OS
        default association (double-click equivalent) — safe for apps,
        docs, folders, images, media."""
        try:
            if platform.system() == "Windows":
                os.startfile(str(path))  # nosec — user-initiated
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
            spoken = f"Opening {spoken_name}."
            if extra_hint:
                spoken += " " + extra_hint
            return CommandResult(speak=spoken,
                                 print_out=f"[jarvis] opened {path}")
        except Exception as exc:
            return CommandResult(speak=f"I couldn't open {spoken_name}.",
                                 print_out=f"[jarvis] open failed: {exc}")

    @staticmethod
    def _hint_others(others) -> str:
        if not others:
            return ""
        names = ", ".join(m.name for m in others[:2])
        return f"I also saw {names} — say 'find <name>' to list all matches."

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

    # ────────────── new: type text on the user's behalf ──────────────
    def _type_text(self, text: str) -> CommandResult:
        """Type `text` into whatever window currently has focus.
        Supports snippet substitution: `type my email` uses
        config[snippets][email] if present. `text` is used literally
        otherwise. Adds a small delay before typing so the user has
        time to release the hotkey combo (otherwise the trailing
        modifier keys would be pressed WHILE we type)."""
        text = (text or "").strip()
        if not text:
            return CommandResult(speak="Type what?", print_out="[type] no text given")

        # Snippet substitution: `type my email`, `type email` -> config value.
        snippets = self.config.get("snippets", {}) or {}
        key = text.lower().strip()
        for candidate in (key, key.removeprefix("my ").strip()):
            if candidate in snippets:
                text = str(snippets[candidate])
                logging.info("[type] snippet %r -> %r", candidate, text[:60])
                break

        try:
            import keyboard
            import time as _time
            # Small delay so the Ctrl+Shift+Space modifiers we're
            # holding at the moment of activation are released before
            # we start injecting keystrokes.
            _time.sleep(0.35)
            keyboard.write(text, delay=0.01)
        except Exception as exc:
            return CommandResult(
                speak="I can't type right now.",
                print_out=f"[type] failed: {exc}",
            )
        preview = text if len(text) <= 60 else text[:57] + "..."
        return CommandResult(print_out=f"[type] wrote {len(text)} chars: {preview}")

    def _press_key(self, combo: str) -> CommandResult:
        """Press a single key or combo like 'enter', 'tab', 'ctrl+a'."""
        combo = (combo or "").strip().lower()
        if not combo:
            return CommandResult(speak="Press what?", print_out="[press] no key")
        # Normalise common voice-transcribed variants.
        combo = (combo
                 .replace(" plus ", "+")
                 .replace(" and ", "+")
                 .replace(" ", "+"))
        try:
            import keyboard
            import time as _time
            _time.sleep(0.25)
            keyboard.press_and_release(combo)
        except Exception as exc:
            return CommandResult(
                speak="I can't send that key.",
                print_out=f"[press] {combo!r} failed: {exc}",
            )
        return CommandResult(print_out=f"[press] {combo}")

    # ────────────── new: see / read the screen (Gemini vision) ──────────────
    def _see_screen(self, prompt: str) -> CommandResult:
        """Capture the primary display and ask Gemini about it.
        Works for: describing what's visible, reading on-screen text,
        translating foreign UIs, explaining code/errors, summarising
        articles, etc."""
        if self._llm is None:
            return CommandResult(
                speak="Vision needs the language model enabled. "
                      "Turn on the L L M section in config first.",
                print_out="[vision] LLM not configured — set [llm].enabled=true",
            )
        try:
            from vision import describe_screen
        except Exception as exc:
            return CommandResult(
                speak="Vision module could not load.",
                print_out=f"[vision] import failed: {exc}",
            )
        # A blank prompt just asks "what's on screen"; anything else
        # (translate this, what does this error mean, summarise the
        # article, etc.) is passed through verbatim.
        prompt = (prompt or "").strip() or "Describe what is on my screen right now."
        reply = describe_screen(prompt, self._llm)
        if not reply:
            return CommandResult(
                speak="I couldn't read the screen. Check the log for details.",
                print_out="[vision] no reply from Gemini",
            )
        return CommandResult(speak=reply, print_out=f"[vision] {reply}")

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

    # ────────────── read / find files ──────────────
    def _read_file(self, target: str) -> CommandResult:
        """Read a text file by name and speak an excerpt. Full contents
        go to the terminal / HUD transcript."""
        target = (target or "").strip().strip('"').strip("'")
        if not target:
            return CommandResult(speak="Read what, sir?",
                                 print_out="[jarvis] empty read target")

        import resolver as _r

        # Explicit path first, then fuzzy search restricted to text files.
        if _r.looks_like_path(target):
            path = _r.resolve_path(target)
            if path is None or not path.is_file():
                return CommandResult(speak=f"I couldn't find {target}.",
                                     print_out=f"[jarvis] read: not found: {target}")
        else:
            extra = self.config.get("resolver", {}).get("extra_roots", []) or []
            match = _r.find_best(target, roots=extra, kinds=("file",), text_only=True)
            if match is None:
                return CommandResult(
                    speak=f"I couldn't find a text file matching {target}.",
                    print_out=f"[jarvis] read: no match for {target!r}",
                )
            path = Path(match.path)

        max_bytes = int(self.config.get("resolver", {}).get("read_max_bytes", 200_000))
        try:
            text = _r.read_text_file(path, max_bytes=max_bytes)
        except ValueError as exc:
            return CommandResult(speak=str(exc),
                                 print_out=f"[jarvis] read refused: {exc}")
        except Exception as exc:
            return CommandResult(speak="I couldn't read that file.",
                                 print_out=f"[jarvis] read failed: {exc}")

        excerpt = self._speech_excerpt(text)
        header = f"── {path.name} ──"
        return CommandResult(
            speak=f"{path.name}. {excerpt}",
            print_out=f"{header}\n{text}",
        )

    @staticmethod
    def _speech_excerpt(text: str, limit: int = 600) -> str:
        """Trim a file to something reasonable to speak out loud."""
        t = " ".join(text.split())
        if len(t) <= limit:
            return t
        return t[:limit].rsplit(" ", 1)[0] + "…"

    def _find(self, target: str) -> CommandResult:
        """List matching apps / files / folders without opening anything."""
        target = (target or "").strip()
        if not target:
            return CommandResult(speak="Find what, sir?",
                                 print_out="[jarvis] empty find target")
        import resolver as _r
        extra = self.config.get("resolver", {}).get("extra_roots", []) or []
        matches = _r.find_targets(target, roots=extra, limit=8)
        if not matches:
            return CommandResult(speak=f"No matches for {target}.",
                                 print_out=f"[jarvis] find: no match")
        lines = [f"── Matches for {target!r} ──"]
        for i, m in enumerate(matches, 1):
            lines.append(f"  {i}. [{m.kind}] {m.name}   ({m.score:.2f})   {m.path}")
        spoken = f"I found {len(matches)} match{'es' if len(matches) != 1 else ''}: "
        spoken += ", ".join(m.name for m in matches[:3])
        return CommandResult(speak=spoken, print_out="\n".join(lines))

    # ────────────── help / quit ──────────────
    def _help(self, _match) -> CommandResult:
        # In UI mode this pops open a stylised help panel next to the orb.
        # In text/wake mode the same lines print to stdout via `print_out`.
        lines = [
            "Commands:",
            "  open <anything>         apps, files, folders, URLs — resolved by name",
            "                          e.g. open spotify, open my resume, open OneDrive",
            "                                open github.com, open C:\\Users\\...\\file.pdf",
            "  read <file>             speaks an excerpt of a text file, prints the rest",
            "                          e.g. read the todo list, read config.toml",
            "  find <name>             list matching apps / files / folders (no launch)",
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

    # Type text into the focused window. Snippet substitution:
    # `type my email` -> config[snippets][email].
    (r"^(?:type|write|escribe|escribir)\s+(?P<t>.+)$",
     lambda d, m: d._type_text(m.group("t"))),

    # Press a single key or combo: `press enter`, `press tab`, `press ctrl a`.
    (r"^(?:press|hit|pulsa|presiona)\s+(?P<k>[a-z0-9 +]+?)\s*(?:key)?$",
     lambda d, m: d._press_key(m.group("k"))),
    # Shorthand: bare key words.
    (r"^(?:enter|return|tab|escape|esc|backspace|delete|space|home|end|"
     r"page\s+up|page\s+down|up|down|left|right)$",
     lambda d, m: d._press_key(m.group(0))),

    # Screen vision — "what's on my screen", "read my screen",
    # "describe the screen", "que hay en la pantalla",
    # "what does the error say", etc. The optional `<prompt>` capture
    # forwards anything after the trigger phrase as extra context.
    (r"^(?:what.?s\s+on\s+(?:my\s+|the\s+)?(?:screen|monitor|pantalla)"
     r"|what\s+do\s+i\s+see"
     r"|describe\s+(?:my\s+|the\s+)?(?:screen|monitor|display|pantalla)"
     r"|read\s+(?:my\s+|the\s+)?(?:screen|monitor|pantalla)"
     r"|look\s+at\s+(?:my\s+|the\s+)?(?:screen|monitor|pantalla)"
     r"|see\s+(?:my\s+|the\s+)?(?:screen|monitor|pantalla)"
     r"|qu[eé]\s+(?:hay|se\s+ve|dice)\s+en\s+(?:mi\s+|la\s+)?pantalla"
     r"|mira\s+(?:mi\s+|la\s+)?pantalla)"
     r"(?:\s+(?P<prompt>.+))?\s*\??$",
     lambda d, m: d._see_screen(m.group("prompt") or "")),

    # Follow-up style: "what does this say", "translate this",
    # "explain this error" — all read the current screen with the
    # question as the prompt.
    (r"^(?:what\s+does\s+this\s+(?:say|mean|show)"
     r"|translate\s+this"
     r"|explain\s+this(?:\s+(?:error|code|screen))?"
     r"|summarize\s+this"
     r"|summarise\s+this)\s*\??$",
     lambda d, m: d._see_screen(m.group(0))),

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

    # Read a file by name or path — speaks an excerpt, prints the whole thing.
    (r"^(?:read|open\s+and\s+read|show(?:\s+me)?)\s+(?:the\s+(?:file\s+)?|file\s+)?(?P<q>.+)$",
     lambda d, m: d._read_file(m.group("q").strip())),
    (r"^what.?s\s+in\s+(?:the\s+file\s+|file\s+|my\s+)?(?P<q>.+?)\s*\??$",
     lambda d, m: d._read_file(m.group("q").strip())),

    # Find a target without opening it — great for disambiguation.
    (r"^(?:find|locate|where\s+is)\s+(?P<q>.+?)\s*\??$",
     lambda d, m: d._find(m.group("q").strip())),

    # App / file / folder / URL launcher — MUST come after the specific commands above.
    (r"^(?:open|launch|start|run)\s+(?P<app>.+)$",
     lambda d, m: d._open_app(m.group("app").strip())),

    # Search
    (r"^(?:google|search)\s+(?:for\s+)?(?P<q>.+)$",
     lambda d, m: d._search_web(m.group("q").strip())),
    (r"^look\s+up\s+(?P<q>.+)$",
     lambda d, m: d._search_web(m.group("q").strip())),

    # Spotify search — `play <song>` and `spotify <song>`
    (r"^(?:play|spotify)\s+(?P<q>.+)$",
     lambda d, m: d._play_spotify(m.group("q").strip())),
]
