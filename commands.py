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
        # Lazy knowledge-base (RAG). Built on first use so startup stays
        # fast even with many ingested docs. See knowledge.py.
        self._kb = None
        self._kb_built = False
        # Re-entrancy guard so an LLM-produced command that also fails to
        # match never triggers another LLM round-trip.
        self._in_llm_dispatch = False

    def _get_kb(self):
        """Lazy KnowledgeBase builder — only hits disk/API when first
        asked. Caches the (possibly None) result."""
        if not self._kb_built:
            try:
                from knowledge import build_from_config
                self._kb = build_from_config(self.config)
            except Exception as exc:
                logging.warning("[kb] builder failed: %s", exc)
                self._kb = None
            self._kb_built = True
        return self._kb

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

        # Dispatch order is config-driven. Default is `llm_first = true`
        # when the LLM is available, because natural phrasings (which
        # the user actually says) don't survive small wording changes
        # against brittle regex. Regex becomes a deterministic fallback
        # for when the LLM is rate-limited, offline, or disabled.
        llm_cfg = self.config.get("llm") or {}
        llm_first = (self._llm is not None
                     and not self._in_llm_dispatch
                     and bool(llm_cfg.get("llm_first", True)))

        # ── Safety net: a handful of commands must ALWAYS work
        # instantly and locally — "quit", "stop listening", "help",
        # media keys, volume — because they're used constantly and
        # must survive a dead LLM. We check these up front even in
        # llm-first mode so there's zero added latency on them.
        if llm_first:
            for pattern in _CRITICAL_INTENT_PATTERNS:
                if re.match(pattern, text, re.IGNORECASE):
                    # Fall through to the regex loop below, which has
                    # these in its table.
                    for pat, handler in INTENTS:
                        m = re.match(pat, text, re.IGNORECASE)
                        if m:
                            result = handler(self, m)
                            self._remember(text, result)
                            return result

        # ── LLM-first path (default when enabled) ──
        if llm_first:
            llm_result = self._try_llm(text)
            if llm_result is not None:
                self._remember(text, llm_result)
                return llm_result
            # LLM failed (rate-limited, offline, no key, etc). Fall
            # through to regex so the user still gets a reply.
            logging.info("[dispatch] LLM failed, falling back to regex "
                         "(reason: %s)", getattr(self._llm, "last_error", None))

        # ── Regex path (either regex-first mode, or LLM fallback) ──
        for pattern, handler in INTENTS:
            match = re.match(pattern, text, re.IGNORECASE)
            if match:
                result = handler(self, match)
                self._remember(text, result)
                return result

        # ── LLM try (if we were regex-first and regex missed) ──
        if not llm_first and self._llm is not None and not self._in_llm_dispatch:
            llm_result = self._try_llm(text)
            if llm_result is not None:
                self._remember(text, llm_result)
                return llm_result

        # Explain *why* we couldn't handle it, so the user knows whether
        # to rephrase, enable the LLM, check the network, etc.
        if self._llm is None:
            disabled = self.config.get("_llm_disabled_reason") \
                       or "the language model is off"
            speak = (f"I don't have a command that matches \"{text}\", "
                     f"and {disabled}, so I can't improvise. "
                     f"Say help to see what I understand natively.")
            tag = f"[jarvis] no regex match + llm off ({disabled})"
        elif self._in_llm_dispatch:
            # LLM produced a canonical command that itself didn't match —
            # unusual, but worth naming so we don't ping-pong silently.
            speak = (f"The language model suggested a command I don't "
                     f"recognize for \"{text}\". Say help to see what I can do.")
            tag = "[jarvis] llm produced unknown command"
        else:
            why = getattr(self._llm, "last_error", None) \
                  or "the language model had no useful reply"
            speak = (f"I don't have a built-in command for \"{text}\", "
                     f"and {why}. Try rephrasing, or say help.")
            tag = f"[jarvis] no regex match + llm failed ({why})"
        return CommandResult(
            speak=speak,
            print_out=f"{tag}: {text!r}",
        )

    # ────────────── LLM fallback ──────────────
    def _try_llm(self, text: str) -> Optional[CommandResult]:
        """Ask Gemini to convert the transcript into either a canonical
        command (re-dispatched here) or a short chat reply (spoken).
        If the user has ingested documents, retrieval runs transparently
        first — relevant chunks become grounding context for Gemini."""
        # ── RAG augmentation ──
        # Only augment on free-form chatty-looking input. Short/command-y
        # input ("open spotify", "pause", "volume up") won't benefit and
        # we don't want to burn embed calls on them.
        augmented = text
        used_kb = False
        is_chatty = self._looks_chatty(text)
        if is_chatty:
            kb = self._get_kb()
            if kb is not None and kb.docs:
                try:
                    hits = kb.search(text)
                except Exception as exc:
                    logging.warning("[kb] search failed: %s", exc)
                    hits = []
                if hits:
                    from knowledge import format_context
                    context = format_context(hits)
                    augmented = (
                        "You MUST respond with {\"action\":\"chat\",\"reply\":...}. "
                        "Do NOT emit call_intent in this turn. The user is "
                        "asking a question about their uploaded documents.\n\n"
                        "Use ONLY the following excerpts to answer. If the "
                        "answer isn't in them, say so plainly in the reply — "
                        "do not invent facts. Cite the source document name "
                        "in your reply.\n\n"
                        f"=== REFERENCE EXCERPTS ===\n{context}\n=== END ===\n\n"
                        f"User question: {text}"
                    )
                    used_kb = True
                    logging.info("[kb] grounding reply on %d chunks "
                                 "(top score %.2f)", len(hits), hits[0].score)
            # Clear-question inputs ("?") still get a nudge toward
            # chat mode even when no KB chunks matched, because local
            # models can be over-eager and route "puedo sentar al
            # paciente?" as `type sentar al paciente` based on surface
            # word overlap. We ONLY add this nudge when there's an
            # explicit question mark — ambiguous long inputs like
            # "revisa si hay nuevo capitulo" should still be allowed
            # to route to intents.
            if not used_kb and ("?" in text or "¿" in text):
                augmented = (
                    "The user is asking a free-form question. Respond with "
                    "{\"action\":\"chat\",\"reply\":...} — a short spoken "
                    "answer. Do NOT emit call_intent unless the request is "
                    "unambiguously an app/file/media/system command.\n\n"
                    f"User: {text}"
                )

        parsed = self._llm.infer(augmented)
        # When we grounded via RAG, strip the LLM's own prefixed label
        # like {"action":"chat"} and ONLY accept chat replies. If the
        # model still tried to route an intent, downgrade to chat with
        # whatever text it produced so the user hears SOMETHING grounded.
        if used_kb and parsed and parsed.get("action") != "chat":
            fallback_reply = (parsed.get("reply") or parsed.get("command")
                              or "").strip()
            if fallback_reply:
                parsed = {"action": "chat", "reply": fallback_reply}
            else:
                parsed = None  # force the "I couldn't answer" branch
        if not parsed:
            return None

        action = (parsed.get("action") or "").lower()

        if action == "call_intent":
            command = (parsed.get("command") or "").strip()
            if not command:
                return None
            # Normalise common Qwen misroutes before re-dispatch.
            # Observed empirically with qwen2.5:7b-instruct:
            #   * "type foo.txt content: bar" is really a write (Qwen
            #     keeps the `content:` syntax but picks the wrong verb)
            #   * "forget notas.txt" with a filename extension is
            #     really a `delete file` (Qwen conflates borrar=forget)
            # Both corrections are safe because the real intents
            # (type a snippet / forget a KB doc) never use those shapes.
            command = self._normalize_llm_command(command)
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

    @staticmethod
    def _normalize_llm_command(cmd: str) -> str:
        """Correct a handful of known Qwen 7B routing mistakes before
        we re-dispatch the LLM's canonical command. Each rewrite is
        narrow: it only fires on shapes the legitimate target intent
        would never produce, so there's no regression risk."""
        low = cmd.lower()
        # "type foo content: bar" -> "write foo content: bar"
        # The `type` intent is single-argument free text; it never
        # includes " content:" literally.
        if low.startswith("type ") and " content:" in low:
            return "write " + cmd[5:]
        # "forget <path-with-extension>" -> "delete file <path>"
        # The `forget` intent targets KB doc names, not .txt / .log /
        # .py paths. A bare dotted extension means it's a real file.
        import re as _re
        m = _re.match(r"^forget\s+(.+\.[a-zA-Z0-9]{1,6})\s*$", cmd)
        if m:
            return f"delete file {m.group(1)}"
        return cmd

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
            return CommandResult(
                speak=f"I couldn't launch that: {type(exc).__name__}.",
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
            return CommandResult(
                speak=f"I couldn't open {spoken_name}: {type(exc).__name__}.",
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
        """Play a song in the Spotify DESKTOP app.

        Preferred path (requires [spotify].client_id / client_secret):
          1. Resolve the top-matching track via Spotify Web API search.
          2. Launch `spotify:track:<id>` so the desktop app opens
             directly on that track's page.
          3. Fire the media Play/Pause key so it starts playing.

        Fallback (no credentials): open the web search page (old
        behaviour) and nudge the user to add credentials in config.
        """
        query = query.strip()
        if not query:
            return CommandResult(speak="Play what?", print_out="[spotify] empty query")

        # Allow phrases like "play X on the app" / "en la app" / "from the app"
        # to be treated as a stronger hint (they don't change the path, but
        # we strip the trailing hint so it doesn't pollute the search terms).
        for suffix in (
            " on the app", " from the app", " en la app", " desde la app",
            " desde la aplicacion", " en la aplicacion",
        ):
            if query.lower().endswith(suffix):
                query = query[: -len(suffix)].strip()
                break

        try:
            import spotify as sp_mod
        except Exception as exc:
            logging.warning("spotify module unavailable: %s", exc)
            sp_mod = None

        api = sp_mod.api_from_config(self.config) if sp_mod else None

        # Track WHY we're falling back so the spoken reply tells the
        # user something actionable — not just "I opened a search page".
        reason: Optional[str] = None

        if api is None:
            reason = "no_credentials"
        elif sp_mod is not None:
            track = api.search_track(query)
            if not track or not track.get("uri"):
                reason = "no_match"
            else:
                auto_play = bool((self.config.get("spotify") or {})
                                 .get("auto_play", True))
                ok = sp_mod.play_track(track["uri"], auto_play=auto_play)
                if ok:
                    label = f"{track['name']} by {track['artists']}".strip()
                    return CommandResult(
                        speak=f"Playing {label} on Spotify.",
                        print_out=f"[spotify] {track['uri']}  →  {label}",
                    )
                reason = "launch_failed"

        # ── Fallback: open the web search page with a spoken reason ──
        url = f"https://open.spotify.com/search/{urllib.parse.quote_plus(query)}"
        webbrowser.open(url)
        if reason == "no_credentials":
            speak = (f"I don't have Spotify credentials yet, so I can't play "
                     f"inside the app. I opened the search for {query} in your "
                     f"browser. Add your Spotify client id and secret to the "
                     f"config to play directly from the app next time.")
            print_tag = "[spotify] credentials missing"
        elif reason == "no_match":
            speak = (f"I couldn't find a track called {query} on Spotify. "
                     f"I opened the search in your browser so you can pick one.")
            print_tag = "[spotify] no track matched"
        elif reason == "launch_failed":
            speak = ("I found the track but couldn't launch the Spotify app. "
                     "I opened the web search as a fallback.")
            print_tag = "[spotify] desktop launch failed"
        else:
            speak = f"Looking up {query} on Spotify."
            print_tag = "[jarvis] opened"
        return CommandResult(
            speak=speak,
            print_out=f"{print_tag}  →  {url}",
        )

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
        except Exception as exc:
            return CommandResult(
                speak=f"I couldn't reach the weather service for {location}: "
                      f"{type(exc).__name__}.",
                print_out=f"[jarvis] wttr.in unreachable: {exc}")

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
            return CommandResult(
                speak=f"I couldn't take the screenshot: {type(exc).__name__}.",
                print_out=f"[jarvis] screenshot failed: {exc}")
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
                speak=f"I can't type right now: {type(exc).__name__}.",
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
                speak=f"I couldn't send {combo}: {type(exc).__name__}.",
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
            why = getattr(self._llm, "last_error", None) \
                  or "the vision model didn't return anything"
            return CommandResult(
                speak=f"I couldn't read the screen: {why}.",
                print_out=f"[vision] no reply: {why}",
            )
        return CommandResult(speak=reply, print_out=f"[vision] {reply}")

    # ────────────── new: knowledge base (RAG) ──────────────
    @staticmethod
    def _looks_chatty(text: str) -> bool:
        """Heuristic: is this a question / long free-form sentence that
        would benefit from doc grounding, or a short imperative?"""
        t = text.strip()
        if "?" in t or "¿" in t:
            return True
        if len(t.split()) >= 6:
            return True
        # Common question starters EN/ES/FR
        lead = t.lower().split(maxsplit=1)[0] if t else ""
        return lead in {
            "what", "why", "how", "when", "where", "who", "which",
            "qué", "que", "por", "cómo", "como", "cuándo", "cuando",
            "dónde", "donde", "quién", "quien", "cuál", "cual",
            "explain", "tell", "describe", "summarize", "summarise",
            "explica", "cuéntame", "cuentame", "resume", "describe",
        }

    def _kb_learn(self, path: str) -> CommandResult:
        """Ingest a file: extract, chunk, embed, persist."""
        path = (path or "").strip().strip('"').strip("'")
        if not path:
            return CommandResult(speak="Learn which file?",
                                 print_out="[kb] empty path")
        kb = self._get_kb()
        if kb is None:
            return CommandResult(
                speak="I can't learn files without a Gemini API key configured.",
                print_out="[kb] no api key available")
        # Resolve relative paths via the usual resolver so the user can
        # just say "learn my protocol" without a full absolute path.
        from pathlib import Path as _P
        p = _P(path).expanduser()
        if not p.exists():
            import resolver as _r
            resolved = _r.resolve_path(path)
            if resolved is None:
                extra = self.config.get("resolver", {}).get("extra_roots", []) or []
                match = _r.find_best(path, roots=extra, kinds=("file",))
                if match is not None:
                    p = _P(match.path)
            else:
                p = resolved
        if not p.exists() or not p.is_file():
            return CommandResult(
                speak=f"I couldn't find a file called {path}.",
                print_out=f"[kb] path not found: {path}")
        try:
            stats = kb.ingest(p)
        except ValueError as exc:
            return CommandResult(
                speak=str(exc), print_out=f"[kb] ingest refused: {exc}")
        except Exception as exc:
            logging.exception("[kb] ingest failed")
            return CommandResult(
                speak=f"I couldn't learn {p.name}: {type(exc).__name__}.",
                print_out=f"[kb] ingest failed: {exc}")
        return CommandResult(
            speak=(f"Got it. Learned {stats['doc']} — "
                   f"{stats['chunks']} sections indexed. "
                   f"Ask me anything about it."),
            print_out=f"[kb] ingested {stats['doc']}: "
                      f"{stats['chars']} chars, {stats['chunks']} chunks")

    def _kb_list(self, _match) -> CommandResult:
        kb = self._get_kb()
        if kb is None or not kb.docs:
            return CommandResult(
                speak="I don't have any documents in memory yet. "
                      "Say 'learn' and give me a file to start.",
                print_out="[kb] empty")
        docs = kb.list_docs()
        lines = [f"── Knowledge base ({len(docs)} docs) ──"]
        for d in docs:
            lines.append(f"  • {d['doc']}  —  {d['chunks']} chunks, "
                         f"{d['chars']} chars  (ingested {d['ingested_at']})")
        spoken = (f"I know {len(docs)} document{'s' if len(docs) != 1 else ''}: "
                  + ", ".join(d['doc'] for d in docs[:5]))
        if len(docs) > 5:
            spoken += f", and {len(docs) - 5} more"
        return CommandResult(speak=spoken + ".", print_out="\n".join(lines))

    def _kb_forget(self, doc: str) -> CommandResult:
        doc = (doc or "").strip().strip('"').strip("'")
        if not doc:
            return CommandResult(speak="Forget which document?",
                                 print_out="[kb] empty doc")
        kb = self._get_kb()
        if kb is None or not kb.docs:
            return CommandResult(speak="There's nothing in my knowledge base.",
                                 print_out="[kb] empty")
        if doc.lower() in ("all", "everything", "todo", "todos"):
            n = kb.clear()
            return CommandResult(
                speak=f"Cleared {n} document{'s' if n != 1 else ''} from memory.",
                print_out=f"[kb] cleared {n} docs")
        ok = kb.forget(doc)
        if not ok:
            return CommandResult(
                speak=f"I don't have anything matching {doc}.",
                print_out=f"[kb] no match for {doc!r}")
        return CommandResult(
            speak=f"Forgotten. {doc} is out of my memory.",
            print_out=f"[kb] removed {doc!r}")

    # ────────────── new: web diagnose ──────────────
    def _diagnose_url(self, url: str) -> CommandResult:
        """Fetch a URL, test its referenced hosts, and explain WHY it
        isn't loading. Great for the "the page just won't load" case
        where screen vision can only read 'Tap to retry'."""
        url = (url or "").strip().strip('"').strip("'")
        if not url:
            return CommandResult(
                speak="Diagnose which URL?",
                print_out="[web] empty url")
        # Accept bare domains too — add https:// so urllib works.
        if not re.match(r"^https?://", url, re.IGNORECASE):
            url = "https://" + url.lstrip("/")
        try:
            import web as _web
        except Exception as exc:
            return CommandResult(
                speak=f"The web module failed to load: {type(exc).__name__}.",
                print_out=f"[web] import failed: {exc}")
        diag = _web.diagnose(url)
        return CommandResult(speak=diag.summary,
                             print_out=diag.detail or diag.summary)

    # ────────────── new: manga watcher ──────────────
    def _manga_check(self, match_or_query) -> CommandResult:
        """Run the configured manga watcher on demand.
        Accepts either a regex Match (whose named group `q` may hold a
        specific series) or a plain string. If a series is named and
        it's NOT in the watchlist, falls through to a one-shot lookup
        so phrases like "check for a new chapter of <series I never
        configured>" still return something useful."""
        # Resolve the optional query from either call style.
        query = ""
        if isinstance(match_or_query, str):
            query = match_or_query.strip()
        elif match_or_query is not None:
            try:
                query = (match_or_query.groupdict().get("q") or "").strip()
            except Exception:
                query = ""
        # Trim trailing filler like "please", "today", "right now".
        query = re.sub(r"\s+(please|today|now|right\s+now)\s*$", "", query,
                       flags=re.IGNORECASE).strip()

        try:
            import manga_watch as mw
        except Exception as exc:
            return CommandResult(
                speak=f"The manga watcher module failed to load: {type(exc).__name__}.",
                print_out=f"[manga] import failed: {exc}")
        from pathlib import Path as _P
        config_dir = _P(self.config.get("_config_dir", ".")).resolve()

        # ── Specific series requested ──
        # Prefer the watched copy (so the user hears "no new, you're on
        # ch N"); if not watched, fall through to a one-shot lookup.
        if query:
            series_list = (self.config.get("manga", {}) or {}).get("series") or []
            q_tokens = set(re.findall(r"[a-z0-9]+", query.lower()))
            def _overlap(name: str) -> float:
                nt = set(re.findall(r"[a-z0-9]+", (name or "").lower()))
                return len(q_tokens & nt) / max(1, len(q_tokens))
            scored = [(s, _overlap(s.get("name", ""))) for s in series_list]
            scored.sort(key=lambda x: x[1], reverse=True)
            if scored and scored[0][1] >= 0.5:
                # Series is on the watchlist — run the watcher but
                # scope it to just that one series by filtering the
                # config in-flight (cheap: watcher iterates series).
                scoped = {**self.config,
                          "manga": {**(self.config.get("manga") or {}),
                                    "series": [scored[0][0]]}}
                drops = mw.check_all(scoped, config_dir)
                if drops:
                    return CommandResult(
                        speak=mw.format_announcement(drops),
                        print_out=mw.summary_for_log(drops))
                # No drops: report current baseline for that series.
                name = scored[0][0].get("name", query)
                try:
                    import json as _j
                    state = _j.loads((config_dir / "manga_state.json")
                                     .read_text(encoding="utf-8"))
                    last = next((v.get("last_seen_chapter")
                                 for v in state.values()
                                 if (v.get("name") or "").lower() == name.lower()),
                                None)
                    if last:
                        return CommandResult(
                            speak=f"No new chapter. {name} is still on {last}.",
                            print_out=f"[manga] {name} baseline: ch.{last}")
                except Exception:
                    pass
                return CommandResult(
                    speak=f"No new chapter of {name} yet.",
                    print_out=f"[manga] {name}: no update")
            # Series not on watchlist — one-shot lookup fallback.
            return self._manga_latest(query)

        # ── No series specified: check EVERYTHING ──
        drops = mw.check_all(self.config, config_dir)
        if not drops:
            # Pull last-seen numbers from the state file so we can tell
            # the user what we DID find, instead of silence.
            series = (self.config.get("manga", {}) or {}).get("series") or []
            if not series:
                return CommandResult(
                    speak="You don't have any manga on your watchlist yet. "
                          "Add one under the manga section in config.",
                    print_out="[manga] no series configured")
            try:
                import json as _j
                state = _j.loads((config_dir / "manga_state.json")
                                 .read_text(encoding="utf-8"))
                parts = [f"{v.get('name')} is at chapter {v.get('last_seen_chapter')}"
                         for v in state.values() if v.get("last_seen_chapter")]
                if parts:
                    speak = "No new chapters. " + "; ".join(parts) + "."
                else:
                    speak = "No new chapters right now."
            except Exception:
                speak = "No new chapters right now."
            return CommandResult(speak=speak, print_out=mw.summary_for_log(drops))
        return CommandResult(
            speak=mw.format_announcement(drops),
            print_out=mw.summary_for_log(drops))

    def _manga_latest(self, query: str) -> CommandResult:
        """One-shot lookup: 'latest chapter of <series>'."""
        query = (query or "").strip()
        if not query:
            return CommandResult(speak="Latest chapter of what?",
                                 print_out="[manga] empty query")
        try:
            import manga_watch as mw
        except Exception as exc:
            return CommandResult(
                speak=f"The manga module failed to load: {type(exc).__name__}.",
                print_out=f"[manga] import failed: {exc}")
        language = str((self.config.get("manga", {}) or {}).get("language", "en"))
        hit = mw.latest_for_query(query, language=language)
        if hit is None:
            return CommandResult(
                speak=f"I couldn't find anything called {query} on MangaDex.",
                print_out=f"[manga] no match for {query!r}")
        if not hit.get("chapter"):
            return CommandResult(
                speak=f"I found {hit['title']} but there are no translated "
                      f"chapters yet.",
                print_out=f"[manga] {hit['title']}: no chapters")
        return CommandResult(
            speak=f"The latest chapter of {hit['title']} is {hit['chapter']}.",
            print_out=f"[manga] {hit['title']} ch.{hit['chapter']}  →  "
                      f"{hit['read_url']}")

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

    # ────────────── file ops (Phase 1) ──────────────
    # Safety model
    # ------------
    # * Relative paths resolve inside WORKSPACE_ROOT (default
    #   ~/Desktop/jarvis-workspace, overridable via [files].workspace).
    # * Absolute paths are allowed but a short deny-list blocks
    #   writes/deletes under Windows system directories so Jarvis
    #   can never nuke the OS.
    # * Deletes are file-only. Removing directories needs a separate
    #   "remove folder" intent so a one-word slip can't take out a tree.

    def _workspace_root(self) -> Path:
        cfg = (self.config.get("files", {}) or {}).get("workspace")
        if cfg:
            root = Path(str(cfg)).expanduser()
        else:
            root = Path.home() / "Desktop" / "jarvis-workspace"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _is_system_path(path: Path) -> bool:
        """True if `path` falls under a Windows directory we refuse
        to write to. Compared on the resolved path so symlinks and
        .. tricks can't bypass it."""
        try:
            resolved = str(path.resolve()).lower()
        except Exception:
            resolved = str(path).lower()
        for bad in (r"c:\windows", r"c:\program files",
                    r"c:\program files (x86)", r"c:\programdata"):
            if resolved.startswith(bad):
                return True
        return False

    def _resolve_user_path(self, raw: str) -> Path:
        """Turn a spoken path into an absolute Path. Absolute paths
        pass through; relative paths resolve under the workspace."""
        raw = (raw or "").strip().strip('"').strip("'")
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self._workspace_root() / p
        return p

    def _write_file(self, path: str, content: str,
                    *, append: bool = False) -> CommandResult:
        target = self._resolve_user_path(path)
        if self._is_system_path(target):
            return CommandResult(
                speak="I won't write inside a Windows system folder.",
                print_out=f"[write] refused system path: {target}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append else "w"
            with target.open(mode, encoding="utf-8", newline="\n") as fh:
                fh.write(content)
                if append and not content.endswith("\n"):
                    fh.write("\n")
        except Exception as exc:
            return CommandResult(
                speak=f"I couldn't {'append to' if append else 'write'} "
                      f"{target.name}: {type(exc).__name__}.",
                print_out=f"[write] {target}: {exc}")
        verb = "Appended to" if append else "Wrote"
        return CommandResult(
            speak=f"{verb} {target.name}, {len(content)} characters.",
            print_out=f"[write] {verb.lower()} {target} ({len(content)} chars)")

    def _delete_file(self, path: str) -> CommandResult:
        target = self._resolve_user_path(path)
        if self._is_system_path(target):
            return CommandResult(
                speak="I won't delete inside a Windows system folder.",
                print_out=f"[delete] refused system path: {target}")
        if not target.exists():
            return CommandResult(
                speak=f"{target.name} doesn't exist.",
                print_out=f"[delete] not found: {target}")
        if target.is_dir():
            return CommandResult(
                speak=f"{target.name} is a folder. Say "
                      f"'remove folder {target.name}' if you really mean it.",
                print_out=f"[delete] refused directory: {target}")
        try:
            target.unlink()
        except Exception as exc:
            return CommandResult(
                speak=f"I couldn't delete {target.name}: {type(exc).__name__}.",
                print_out=f"[delete] {target}: {exc}")
        return CommandResult(
            speak=f"Deleted {target.name}.",
            print_out=f"[delete] {target}")

    def _mkdir(self, path: str) -> CommandResult:
        target = self._resolve_user_path(path)
        if self._is_system_path(target):
            return CommandResult(
                speak="I won't create folders inside a Windows system path.",
                print_out=f"[mkdir] refused system path: {target}")
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return CommandResult(
                speak=f"I couldn't create {target.name}: {type(exc).__name__}.",
                print_out=f"[mkdir] {target}: {exc}")
        return CommandResult(
            speak=f"Folder {target.name} ready.",
            print_out=f"[mkdir] {target}")

    def _remove_folder(self, path: str) -> CommandResult:
        """Recursive — explicitly separate from _delete_file so a
        voice slip ("delete whatever") can't take a whole tree."""
        import shutil
        target = self._resolve_user_path(path)
        if self._is_system_path(target):
            return CommandResult(
                speak="I won't remove a Windows system folder.",
                print_out=f"[rmdir] refused system path: {target}")
        if target.resolve() == self._workspace_root().resolve():
            return CommandResult(
                speak="I won't remove the workspace root itself.",
                print_out=f"[rmdir] refused workspace root: {target}")
        if not target.exists():
            return CommandResult(
                speak=f"{target.name} doesn't exist.",
                print_out=f"[rmdir] not found: {target}")
        if not target.is_dir():
            return CommandResult(
                speak=f"{target.name} is a file, not a folder.",
                print_out=f"[rmdir] not a dir: {target}")
        try:
            shutil.rmtree(target)
        except Exception as exc:
            return CommandResult(
                speak=f"I couldn't remove {target.name}: {type(exc).__name__}.",
                print_out=f"[rmdir] {target}: {exc}")
        return CommandResult(
            speak=f"Removed folder {target.name}.",
            print_out=f"[rmdir] {target}")

    def _list_dir(self, path: str) -> CommandResult:
        target = self._resolve_user_path(path or ".")
        if not target.exists():
            return CommandResult(
                speak=f"{target.name} doesn't exist.",
                print_out=f"[ls] not found: {target}")
        if not target.is_dir():
            return CommandResult(
                speak=f"{target.name} is a file.",
                print_out=f"[ls] not a dir: {target}")
        try:
            entries = sorted(target.iterdir(),
                             key=lambda p: (not p.is_dir(), p.name.lower()))
        except Exception as exc:
            return CommandResult(
                speak=f"I couldn't list {target.name}: {type(exc).__name__}.",
                print_out=f"[ls] {target}: {exc}")
        if not entries:
            return CommandResult(
                speak=f"{target.name} is empty.",
                print_out=f"[ls] {target} (empty)")
        # Spoken summary: counts + first few names
        n_dirs = sum(1 for e in entries if e.is_dir())
        n_files = len(entries) - n_dirs
        preview = ", ".join(e.name for e in entries[:6])
        if len(entries) > 6:
            preview += f", and {len(entries) - 6} more"
        spoken = (f"{target.name}: {n_dirs} folder"
                  f"{'s' if n_dirs != 1 else ''}, "
                  f"{n_files} file{'s' if n_files != 1 else ''}. {preview}.")
        printed = "\n".join(
            f"  {'[d] ' if e.is_dir() else '    '}{e.name}" for e in entries)
        return CommandResult(speak=spoken,
                             print_out=f"[ls] {target}\n{printed}")

    # ────────────── project scaffold (Phase 2) ──────────────
    # Supported stacks: python, node, static (html+css+js), rust.
    # Each produces a sensibly-opinionated starting layout inside
    # the workspace root (or the user-given absolute path), then
    # kicks off `git init` so the project is version-controlled
    # from minute one. Nothing is pushed anywhere.

    _SCAFFOLD_STACKS = {"python", "node", "static", "rust"}

    def _scaffold_project(self, stack: str, name: str) -> CommandResult:
        stack = (stack or "").strip().lower()
        name = (name or "").strip().strip('"').strip("'")
        if stack not in self._SCAFFOLD_STACKS:
            return CommandResult(
                speak=f"I don't know the {stack} stack. I can do "
                      f"python, node, static, or rust.",
                print_out=f"[scaffold] unsupported stack: {stack}")
        if not name:
            return CommandResult(speak="What should I call the project?",
                                 print_out="[scaffold] no name")

        # Normalise name: lower-snake for python/rust, lower-kebab elsewhere.
        import re as _re
        safe_name = _re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_")
        if not safe_name:
            return CommandResult(speak=f"'{name}' isn't a usable project name.",
                                 print_out=f"[scaffold] empty safe name")
        snake = safe_name.replace("-", "_")

        root = self._resolve_user_path(safe_name)
        if root.exists() and any(root.iterdir()):
            return CommandResult(
                speak=f"{safe_name} already exists and isn't empty. "
                      f"Pick another name or remove it first.",
                print_out=f"[scaffold] refuse non-empty dir: {root}")
        root.mkdir(parents=True, exist_ok=True)

        # ── Write stack-specific files ──
        files: dict[str, str] = {}
        if stack == "python":
            files["README.md"] = (
                f"# {safe_name}\n\n"
                f"Scaffolded by Jarvis.\n\n"
                f"## Quickstart\n\n"
                f"    python -m venv .venv\n"
                f"    .venv\\Scripts\\activate\n"
                f"    pip install -e .[dev]\n"
                f"    pytest -q\n")
            files[".gitignore"] = (
                "__pycache__/\n*.pyc\n.venv/\nvenv/\n.env\n"
                ".pytest_cache/\n.mypy_cache/\nbuild/\ndist/\n*.egg-info/\n")
            files[f"src/{snake}/__init__.py"] = (
                f'"""{safe_name} package."""\n__version__ = "0.1.0"\n')
            files[f"src/{snake}/main.py"] = (
                "def main() -> None:\n"
                f'    print("hello from {safe_name}")\n\n\n'
                'if __name__ == "__main__":\n    main()\n')
            files[f"tests/test_{snake}.py"] = (
                f"from {snake} import __version__\n\n\n"
                "def test_version():\n"
                '    assert __version__ == "0.1.0"\n')
            files["pyproject.toml"] = (
                f'[project]\n'
                f'name = "{safe_name}"\n'
                f'version = "0.1.0"\n'
                f'description = "Scaffolded by Jarvis."\n'
                f'requires-python = ">=3.10"\n'
                f'dependencies = []\n\n'
                f'[project.optional-dependencies]\n'
                f'dev = ["pytest"]\n\n'
                f'[build-system]\n'
                f'requires = ["setuptools>=61"]\n'
                f'build-backend = "setuptools.build_meta"\n\n'
                f'[tool.setuptools.packages.find]\n'
                f'where = ["src"]\n')
        elif stack == "node":
            files["README.md"] = (
                f"# {safe_name}\n\nScaffolded by Jarvis.\n\n"
                f"## Quickstart\n\n    npm install\n    npm start\n")
            files[".gitignore"] = (
                "node_modules/\ndist/\nbuild/\n.env\n*.log\n.DS_Store\n")
            files["package.json"] = (
                '{\n'
                f'  "name": "{safe_name}",\n'
                '  "version": "0.1.0",\n'
                '  "description": "Scaffolded by Jarvis.",\n'
                '  "type": "module",\n'
                '  "main": "src/index.js",\n'
                '  "scripts": {\n'
                '    "start": "node src/index.js",\n'
                '    "test": "node --test"\n'
                '  }\n'
                '}\n')
            files["src/index.js"] = (
                f'console.log("hello from {safe_name}");\n')
        elif stack == "static":
            files["README.md"] = (
                f"# {safe_name}\n\nStatic site scaffolded by Jarvis.\n\n"
                f"Open `index.html` in a browser, or serve with "
                f"`python -m http.server 8000`.\n")
            files[".gitignore"] = ".DS_Store\n*.log\n.vscode/\n"
            files["index.html"] = (
                '<!doctype html>\n<html lang="en">\n<head>\n'
                '  <meta charset="utf-8">\n'
                '  <meta name="viewport" content="width=device-width,initial-scale=1">\n'
                f'  <title>{safe_name}</title>\n'
                '  <link rel="stylesheet" href="style.css">\n'
                '</head>\n<body>\n'
                f'  <h1>{safe_name}</h1>\n'
                '  <p>Hello world.</p>\n'
                '  <script src="script.js"></script>\n'
                '</body>\n</html>\n')
            files["style.css"] = (
                ":root { color-scheme: light dark; }\n"
                "body { font-family: system-ui, sans-serif; "
                "max-width: 60ch; margin: 4rem auto; padding: 0 1rem; }\n")
            files["script.js"] = (
                f'console.log("{safe_name} loaded");\n')
        elif stack == "rust":
            # We don't scaffold Cargo.toml by hand — `cargo new` does
            # it correctly. We only need the parent dir to exist and
            # be empty. shell_exec call below handles it.
            files["README.md"] = (
                f"# {safe_name}\n\nScaffolded by Jarvis (via `cargo new`).\n\n"
                f"## Quickstart\n\n    cargo run\n    cargo test\n")

        try:
            for rel, content in files.items():
                dest = root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8", newline="\n")
        except Exception as exc:
            return CommandResult(
                speak=f"I started the project but hit an error writing files: "
                      f"{type(exc).__name__}.",
                print_out=f"[scaffold] write error: {exc}")

        # ── Rust: delegate layout to cargo ──
        if stack == "rust":
            import subprocess
            try:
                subprocess.run(
                    ["cargo", "init", "--name", safe_name, "."],
                    cwd=str(root), capture_output=True, text=True,
                    timeout=30, shell=False, encoding="utf-8", errors="replace")
            except Exception as exc:
                logging.warning("[scaffold] cargo init failed: %s", exc)

        # ── git init (best-effort; silent on failure) ──
        import subprocess
        try:
            subprocess.run(
                ["git", "init", "-q"],
                cwd=str(root), capture_output=True, text=True,
                timeout=15, shell=False, encoding="utf-8", errors="replace")
            subprocess.run(
                ["git", "add", "-A"],
                cwd=str(root), capture_output=True, text=True,
                timeout=15, shell=False, encoding="utf-8", errors="replace")
            subprocess.run(
                ["git", "commit", "-q", "-m", "Initial scaffold by Jarvis"],
                cwd=str(root), capture_output=True, text=True,
                timeout=15, shell=False, encoding="utf-8", errors="replace")
        except Exception as exc:
            logging.warning("[scaffold] git init chain failed: %s", exc)

        n_files = sum(1 for _ in root.rglob("*") if _.is_file())
        return CommandResult(
            speak=f"{stack.capitalize()} project {safe_name} is ready in "
                  f"your workspace, {n_files} files, git initialised.",
            print_out=f"[scaffold] {stack} {root} ({n_files} files)")

    # ────────────── shell exec (Phase 3) ──────────────
    # Safety model
    # ------------
    # * Allow-list of first-word tools (git, python, npm, uv, pip,
    #   cargo, node, pytest, gh, cmake, make, dotnet). Anything else
    #   is refused with a clear reason.
    # * Hard deny-list on substrings that could brick the box
    #   (rm -rf /, shutdown, format, sc delete, reg delete, Remove-Item -Recurse).
    # * Default cwd is the workspace root so projects stay contained.
    #   An explicit "in <path>" suffix lets the user target elsewhere.
    # * 60s wall-clock timeout. Stdout + stderr truncated for the
    #   spoken summary; full text goes to the printed log.

    _SHELL_ALLOW = {
        "git", "gh", "python", "python3", "py", "pip", "uv",
        "npm", "npx", "yarn", "pnpm", "node",
        "cargo", "rustc",
        "dotnet", "cmake", "make", "ninja",
        "pytest", "mypy", "ruff", "black", "flake8",
        "go", "deno", "bun",
    }

    _SHELL_DENY_SUBSTR = (
        "rm -rf /", "rm -rf ~", "rm -rf *",
        "shutdown", "reboot", "halt",
        "format ", "mkfs",
        "sc delete", "reg delete", "reg add",
        "del /s", "del /f", "rmdir /s",
        "remove-item -recurse", "remove-item -force",
        ":(){:|:&};:",            # classic fork-bomb
        "mkpart", "fdisk",
        "chmod -r 777 /",
    )

    def _run_shell(self, raw: str) -> CommandResult:
        """Execute a shell command after allow/deny-list checks.

        The caller's phrasing can be like:
          run git status
          run python -m pytest -q in C:\\code\\project
          run npm install
        """
        import subprocess, shlex
        raw = (raw or "").strip()
        if not raw:
            return CommandResult(speak="Run what?",
                                 print_out="[shell] empty command")

        # Optional "in <path>" suffix to override cwd.
        cwd = self._workspace_root()
        low = raw.lower()
        if " in " in low:
            # Split on the last " in " to keep paths with spaces intact.
            idx = low.rfind(" in ")
            maybe_cmd, maybe_cwd = raw[:idx], raw[idx + 4:].strip()
            if maybe_cwd:
                p = Path(maybe_cwd).expanduser()
                if not p.is_absolute():
                    p = self._workspace_root() / p
                if p.exists() and p.is_dir():
                    raw = maybe_cmd.strip()
                    cwd = p

        # Deny-list substring check (case-insensitive).
        low = raw.lower()
        for bad in self._SHELL_DENY_SUBSTR:
            if bad in low:
                return CommandResult(
                    speak=f"I won't run that — it contains {bad!r} "
                          f"which can destroy data.",
                    print_out=f"[shell] denied: {bad!r} in {raw!r}")

        # First-word allow-list check.
        try:
            parts = shlex.split(raw, posix=False)
        except ValueError as exc:
            return CommandResult(
                speak=f"I can't parse that command: {exc}.",
                print_out=f"[shell] parse error: {exc}")
        if not parts:
            return CommandResult(speak="Run what?",
                                 print_out="[shell] empty after parse")
        tool = parts[0].lower().replace(".exe", "")
        if tool not in self._SHELL_ALLOW:
            allowed = ", ".join(sorted(self._SHELL_ALLOW))
            return CommandResult(
                speak=f"I can only run allow-listed tools. "
                      f"{tool} isn't on the list.",
                print_out=f"[shell] denied non-allowlisted tool "
                          f"{tool!r}. Allowed: {allowed}")

        # Execute.
        try:
            proc = subprocess.run(
                parts,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=60,
                shell=False,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return CommandResult(
                speak=f"{tool} timed out after 60 seconds.",
                print_out=f"[shell] timeout: {raw}")
        except FileNotFoundError:
            return CommandResult(
                speak=f"{tool} isn't installed, or isn't on PATH.",
                print_out=f"[shell] not found: {tool}")
        except Exception as exc:
            return CommandResult(
                speak=f"I couldn't run {tool}: {type(exc).__name__}.",
                print_out=f"[shell] {raw}: {exc}")

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        ok = proc.returncode == 0
        # Spoken summary: short. Printed: full streams.
        summary_src = stdout or stderr or "(no output)"
        first_line = summary_src.splitlines()[0] if summary_src else "(no output)"
        if ok:
            spoken = f"{tool} finished. {first_line[:140]}"
        else:
            spoken = (f"{tool} exited with code {proc.returncode}. "
                      f"{(stderr.splitlines()[0] if stderr else first_line)[:140]}")
        printed_lines = [
            f"[shell] cwd={cwd}",
            f"[shell] $ {raw}",
            f"[shell] exit={proc.returncode}",
        ]
        if stdout:
            printed_lines.append("[shell] stdout:")
            printed_lines.append(stdout if len(stdout) < 4000
                                 else stdout[:4000] + "\n... (truncated)")
        if stderr:
            printed_lines.append("[shell] stderr:")
            printed_lines.append(stderr if len(stderr) < 4000
                                 else stderr[:4000] + "\n... (truncated)")
        return CommandResult(speak=spoken, print_out="\n".join(printed_lines))

    def _quit(self, _match) -> CommandResult:
        return CommandResult(speak="Signing off. Have a productive day.",
                             print_out="[jarvis] goodbye", should_exit=True)

    def _stop_listening(self, _match) -> CommandResult:
        # Soft stop — end the current conversation, stay resident, wait
        # for another wake word. Wake listener already returned by the
        # time this fires; we just need a spoken acknowledgement.
        return CommandResult(speak="Standing by, sir. Just say 'hey jarvis' when you need me.")


# ────────────── critical fast-path (always local) ──────────────
# A handful of commands MUST work in microseconds and must survive a
# dead/rate-limited LLM: process-control (quit/stop), help, media
# transport (play/pause/next/prev/volume). In LLM-first mode we match
# these up front so there's zero added latency on them. Everything
# else routes through the LLM for natural-phrasing tolerance.
_CRITICAL_INTENT_PATTERNS = [
    r"^__stop_listening__$",
    r"^(?:quit|exit|bye|goodbye|shutdown|apagate|cierrate|cerrar)$",
    r"^(?:stop\s+listening|stop)$",
    r"^(?:help|what\s+can\s+you\s+do|commands?|ayuda)$",
    # Media keys — zero-latency essential for a voice assistant.
    r"^(?:pause|play|resume|toggle\s+playback|pausa|reanuda)$",
    r"^(?:next(?:\s+(?:song|track))?|skip|siguiente)$",
    r"^(?:previous(?:\s+(?:song|track))?|back|prev|anterior)$",
    # Volume shortcuts.
    r"^(?:volume\s+up|louder|sube\s+(?:el\s+)?volumen)(?:\s+\d+)?$",
    r"^(?:volume\s+down|quieter|baja\s+(?:el\s+)?volumen)(?:\s+\d+)?$",
    r"^(?:mute|unmute|silencio)$",
]


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

    # ───── File ops (Phase 1) — MUST come before the type regex,
    # because the type regex accepts `write` as an alias and would
    # swallow `write foo content: bar` into _type_text otherwise.
    (r"^write\s+(?P<path>\S.+?)\s+content:\s*(?P<content>.*)$",
     lambda d, m: d._write_file(m.group("path").strip(), m.group("content"))),
    (r"^append(?:\s+to)?\s+(?P<path>\S.+?)\s+content:\s*(?P<content>.*)$",
     lambda d, m: d._write_file(m.group("path").strip(),
                                m.group("content"), append=True)),
    (r"^delete\s+file\s+(?P<path>.+)$",
     lambda d, m: d._delete_file(m.group("path").strip())),
    (r"^(?:make|create)\s+(?:folder|directory|dir)\s+(?P<path>.+)$",
     lambda d, m: d._mkdir(m.group("path").strip())),
    (r"^remove\s+(?:folder|directory|dir)\s+(?P<path>.+)$",
     lambda d, m: d._remove_folder(m.group("path").strip())),
    (r"^(?:list|ls)\s+(?P<path>.+)$",
     lambda d, m: d._list_dir(m.group("path").strip())),

    # Type text into the focused window. Snippet substitution:
    # `type my email` -> config[snippets][email].
    # NOTE: `write` is intentionally NOT listed here anymore — it's
    # now reserved for the file-creation intent above. `escribe`
    # stays because the LLM normally routes it to `type`, but the
    # regex fallback should still handle it for offline/direct use.
    (r"^(?:type|escribe|escribir)\s+(?P<t>.+)$",
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

    # ───── Knowledge base (RAG) ─────
    # Ingest: "learn <path>", "study <path>", "aprende de <path>",
    #         "remember this file <path>", "ingest <path>"
    (r"^(?:learn(?:\s+from)?|study|read\s+and\s+remember|remember(?:\s+this(?:\s+file)?)?|ingest"
     r"|aprende(?:\s+de)?|aprende\s+este\s+(?:archivo|documento)"
     r"|memoriza(?:\s+este)?)\s+(?P<q>.+?)\s*\??$",
     lambda d, m: d._kb_learn(m.group("q"))),
    # List: "what documents do you know", "list my documents", "what have you learned"
    (r"^(?:what\s+documents?\s+do\s+you\s+(?:know|have|remember)"
     r"|list\s+(?:my\s+)?documents?"
     r"|what\s+(?:files|docs)\s+have\s+you\s+learned"
     r"|qu[eé]\s+documentos?\s+(?:tienes|conoces|sabes)"
     r"|lista\s+(?:los\s+)?documentos?"
     r"|knowledge\s+base|kb\s+list)\s*\??$",
     lambda d, m: d._kb_list(m)),
    # Forget: "forget <doc>", "forget everything", "olvida <doc>"
    (r"^(?:forget|unlearn|remove|delete|olvida(?:te\s+de)?|borra)\s+"
     r"(?P<q>.+?)\s*\??$",
     lambda d, m: d._kb_forget(m.group("q"))),

    # Diagnose a URL / page. Catches:
    #   "diagnose https://..." / "check this url" / "why is this page broken"
    #   "why won't https://... load" / "what's wrong with https://..."
    #   "averigua el error de <url>" / "por que no carga <url>"
    (r"^(?:diagnose|debug|inspect|check|averigua(?:\s+el\s+error\s+de)?"
     r"|por\s+qu[eé]\s+no\s+carga|why\s+(?:won.?t|isn.?t|is).{0,40}?load(?:ing)?"
     r"|what(?:'s|\s+is)\s+wrong\s+with)\s+(?:this\s+url\s+|this\s+page\s+|the\s+url\s+|the\s+page\s+)?"
     r"(?P<url>https?://\S+)\s*\??$",
     lambda d, m: d._diagnose_url(m.group("url"))),
    # Short form when the URL is on its own line and the user already
    # said "diagnose this" in a prior turn: just a bare URL triggers
    # diagnose when prefixed with "why won't it load", etc.
    (r"^(?:why\s+(?:won.?t|isn.?t)\s+(?:it|this|the\s+page)\s+load"
     r"|why\s+is\s+this\s+(?:not\s+loading|broken)"
     r"|what.?s\s+wrong\s+with\s+this\s+page"
     r"|diagnose\s+(?:this|it)|debug\s+(?:this|it))\s*\??\s+"
     r"(?P<url>https?://\S+)\s*\??$",
     lambda d, m: d._diagnose_url(m.group("url"))),
    # Bare URL after an explicit diagnose intent in natural phrasing
    (r"^(?P<url>https?://\S+)\s+(?:won.?t\s+load|isn.?t\s+loading|is\s+broken|no\s+carga)\s*\.?$",
     lambda d, m: d._diagnose_url(m.group("url"))),
    # "why won't <url> load" / "why isn't <url> working" — URL in the middle
    (r"^(?:why\s+(?:won.?t|isn.?t|doesn.?t)\s+|por\s+qu[eé]\s+no\s+(?:carga|funciona)\s+)"
     r"(?P<url>https?://\S+)"
     r"(?:\s+(?:load|loading|work|working|cargar|funcionar))?\s*\??$",
     lambda d, m: d._diagnose_url(m.group("url"))),

    # ───── Manga watcher ─────
    # Broad "any new chapters?" style, with optional "of <series>".
    # Catches: "check for new chapters", "is there a new chapter of X",
    #          "check if there is a new episode of X", "any update on X",
    #          "did X update", "has X updated", "new episode of X",
    #          "manga update", "hay un nuevo capitulo de X", etc.
    # "episode" is accepted as a synonym for "chapter" because voice STT
    # often transcribes it that way (and manga/anime are near-synonyms
    # in casual speech).
    (r"^(?:"
     r"(?:please\s+)?check\s+(?:(?:if\s+there\s+(?:is|are)\s+)|(?:for\s+))?"
     r"(?:(?:a|any)\s+)?(?:new\s+)?(?:manga\s+)?(?:chapters?|episodes?|updates?)"
     r"|is\s+there\s+(?:a\s+|any\s+)?new\s+(?:chapter|episode|update)"
     r"|are\s+there\s+(?:any\s+)?new\s+(?:chapters?|episodes?|updates?)"
     r"|any\s+(?:new\s+)?(?:manga\s+)?(?:chapters?|episodes?|updates?)"
     r"|new\s+(?:chapters?|episodes?)"
     r"|(?:did|has|have)\s+(?P<q2>.+?)\s+(?:update|updated|dropped?)"
     r"|manga\s+(?:update|check)"
     r"|hay\s+(?:un\s+)?nuevo\s+(?:cap[ií]tulo|cap|episodio)"
     r"|(?:checa|revisa|mira|f[ií]jate|chequea)\s+(?:si\s+hay\s+)?(?:un\s+)?"
     r"(?:nuevo\s+)?(?:cap[ií]tulo|cap|episodio|actualizaci[oó]n)"
     r")"
     r"(?:\s+(?:of|for|on|about|de|del?)\s+(?P<q>.+?))?"
     r"\s*\??$",
     lambda d, m: d._manga_check(m.group("q") or m.group("q2") or "")),
    # One-off lookup: "latest chapter of <series>", "what chapter is X on"
    (r"^(?:what(?:'s|\s+is)\s+the\s+latest\s+(?:chapter|episode)\s+of"
     r"|latest\s+(?:chapter|episode)\s+of"
     r"|how\s+many\s+chapters?\s+of"
     r"|what\s+chapter\s+is"
     r"|cu[aá]l\s+es\s+el\s+(?:[uú]ltimo\s+)?(?:cap[ií]tulo|cap)\s+de"
     r"|en\s+qu[eé]\s+(?:cap[ií]tulo|cap)\s+(?:va|est[aá])"
     r"|qu[eé]\s+(?:cap[ií]tulo|cap)\s+(?:salio|sacaron|hay))"
     r"\s+(?P<q>.+?)\s*(?:\s+on)?\s*\??$",
     lambda d, m: d._manga_latest(m.group("q"))),

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

    # ───── Project scaffold (Phase 2) ─────
    # Canonical: `scaffold <stack> <name>` where stack is one of
    # python / node / static / rust. Creates a sensible starting
    # layout in the workspace, then runs git init.
    (r"^scaffold\s+(?P<stack>python|node|static|rust)\s+(?P<name>.+)$",
     lambda d, m: d._scaffold_project(m.group("stack"), m.group("name").strip())),

    # ───── Shell exec (Phase 3) ─────
    # Canonical: `run <cmd> [in <path>]`. Allow-listed tools only
    # (git/python/npm/uv/cargo/node/gh/...). The LLM translates
    # natural phrasings into this shape. Must come before the generic
    # `open|launch|start|run <app>` catch-all so "run pytest" doesn't
    # try to launch pytest.exe as an app.
    (r"^(?:run|exec|execute)\s+(?P<cmd>.+)$",
     lambda d, m: d._run_shell(m.group("cmd").strip())),

    # App / file / folder / URL launcher — MUST come after the specific commands above.
    (r"^(?:open|launch|start)\s+(?P<app>.+)$",
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
