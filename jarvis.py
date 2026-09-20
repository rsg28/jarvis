"""
Jarvis — a tiny personal assistant.

On boot:
    · greets you by name with a synthesized neural voice
    · opens Spotify (desktop app if installed, otherwise web player)
    · opens Google in your default browser

Then it loops, accepting typed commands, one-shot voice commands,
or a background wake-word listener ("hey jarvis").

Usage:
    python jarvis.py                       # boot + text loop
    python jarvis.py --no-greet            # skip the morning routine
    python jarvis.py --voice               # one-shot mic input
    python jarvis.py --wake                # always-listening ("hey jarvis")

Config:  copy config.example.toml to config.toml and edit your name + apps.
"""
from __future__ import annotations

import argparse
import atexit
import logging
import os
import platform
import queue
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

LOCK_PATH = Path(tempfile.gettempdir()) / "jarvis.lock"

# Force UTF-8 on the console so accented team names / world news don't crash
# the default cp1252 encoding on Windows shells.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from commands import CommandDispatcher, CommandResult
from voice import Voice


# ─────────────────────── config ───────────────────────

DEFAULT_CONFIG = {
    "user": {"name": "Raul", "greeting_style": "morning", "address": "sir"},
    "voice": {"enabled": True, "rate": 180, "volume": 0.9},
    "startup": {
        "open_spotify": True,
        "open_google": True,
        "extra_urls": [],
    },
    "wake": {
        "enabled": False,
        "phrases": ["hey jarvis", "jarvis"],
        "ack": "Yes?",
    },
    "llm": {
        # LLM fallback (Gemini). When the regex intent table can't match a
        # request, the raw transcript is sent to Gemini which either emits
        # a canonical command (re-dispatched here) or a short chat reply.
        # Get a free API key at https://aistudio.google.com/app/apikey
        # or set the GEMINI_API_KEY env var.
        "enabled": False,
        "api_key": "",
        "model": "gemini-3.6-flash",
        "temperature": 0.3,
        "timeout_seconds": 6.0,
        "history_size": 5,
    },
    "stt": {
        # Primary Google STT language, plus fallbacks tried when the primary
        # gives no hypothesis. Keep the primary matching your wake word.
        "language": "en-US",
        "alt_languages": ["es-ES", "fr-FR"],
        # Hard floor for the recognizer's dynamic energy threshold. Higher =
        # less sensitive (good for noisy rooms). ~300 is a decent starting
        # point for a headset; try 500-700 for a laptop mic in a busy room.
        "energy_threshold": 300,
        "pause_threshold": 0.7,
        "phrase_time_limit": 6.0,
        "followup_time_limit": 10.0,
        "conversation_timeout": 12.0,
        # 0..1. Lower = more forgiving to mispronunciations, higher = stricter.
        "fuzzy_threshold": 0.78,
        # Periodic ambient noise recalibration while running (0 disables).
        "recalibrate_every_seconds": 300.0,
        # Optional: pin a specific input device index. Use None (default) for
        # the system default. Run `python -c "import speech_recognition as sr;
        # print(list(enumerate(sr.Microphone.list_microphone_names())))"`
        "mic_index": None,
    },
    "apps": {
        "spotify":    "spotify",
        "chrome":     "chrome",
        "edge":       "msedge",
        "code":       "code",
        "notepad":    "notepad",
        "calculator": "calc",
        "terminal":   "wt",
    },
}


def load_config(path: Path) -> dict:
    if not path.exists():
        return DEFAULT_CONFIG
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    merged = {**DEFAULT_CONFIG}
    for key, val in data.items():
        merged[key] = {**merged.get(key, {}), **val} if isinstance(val, dict) else val
    return merged


# ─────────────────────── boot sequence ───────────────────────

def _write_lock() -> None:
    """Persist our PID so external tools (e.g. clap_watcher) don't relaunch us."""
    try:
        LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
        atexit.register(_release_lock)
    except OSError as exc:
        logging.debug("could not write lock file: %s", exc)


def _release_lock() -> None:
    try:
        if LOCK_PATH.exists() and LOCK_PATH.read_text().strip() == str(os.getpid()):
            LOCK_PATH.unlink()
    except OSError:
        pass


def morning_greeting(voice: Voice, name: str, address: str) -> str:
    hour = datetime.now().hour
    if hour < 12:
        greet = f"Good morning, {address}" if address else f"Good morning, {name}"
    elif hour < 18:
        greet = f"Good afternoon, {address}" if address else f"Good afternoon, {name}"
    else:
        greet = f"Good evening, {address}" if address else f"Good evening, {name}"
    now = datetime.now().strftime("%A, %B %d — %I:%M %p")
    line = f"{greet}. It is {now}. At your service."
    voice.say(line)
    return line


def boot(config: dict, voice: Voice) -> None:
    name = config["user"]["name"]
    address = config["user"].get("address", "")
    print("\n=== JARVIS · online ===")
    morning_greeting(voice, name, address)

    startup = config["startup"]
    if startup.get("open_spotify", True):
        _open_spotify(config)
    if startup.get("open_google", True):
        webbrowser.open("https://www.google.com")
    for url in startup.get("extra_urls", []) or []:
        webbrowser.open(url)


def _open_spotify(config: dict) -> None:
    if platform.system() == "Windows":
        import subprocess
        try:
            subprocess.Popen(["start", "spotify:"], shell=True)
            return
        except Exception:
            pass
    webbrowser.open("https://open.spotify.com/")


# ─────────────────────── speaking helper (pauses wake listener) ───────────────────────

class GuardedVoice:
    """Wraps Voice.say() so the wake listener is silenced while speaking."""

    def __init__(self, voice: Voice, listener_pause: threading.Event) -> None:
        self._voice = voice
        self._pause = listener_pause

    def say(self, text: str) -> None:
        self._pause.set()
        try:
            self._voice.say(text)
            # Small grace period so the mic doesn't pick up the tail of the audio
            time.sleep(0.25)
        finally:
            self._pause.clear()

    def set_voice(self, name: str) -> str:
        return self._voice.set_voice(name)


# ─────────────────────── input modes ───────────────────────

def _read_text() -> str:
    try:
        return input("you › ").strip()
    except UnicodeDecodeError:
        return ""


def _make_one_shot_listener(config: dict):
    try:
        import speech_recognition as sr
    except ImportError:
        return None
    stt = config.get("stt", {})
    try:
        r = sr.Recognizer()
        r.pause_threshold = float(stt.get("pause_threshold", 0.7))
        r.dynamic_energy_threshold = True
        floor = stt.get("energy_threshold")
        if floor is not None:
            r.energy_threshold = int(floor)
        idx = stt.get("mic_index")
        try:
            mic = sr.Microphone(device_index=idx) if idx is not None else sr.Microphone()
        except Exception:
            mic = sr.Microphone()
        with mic as source:
            r.adjust_for_ambient_noise(source, duration=0.6)
        langs = [stt.get("language", "en-US")] + list(stt.get("alt_languages", []) or [])
        return (r, mic, langs)
    except Exception:
        return None


def _read_voice(listener, config: dict) -> str:
    import speech_recognition as sr
    r, mic, langs = listener
    stt = config.get("stt", {})
    phrase_limit = float(stt.get("followup_time_limit", stt.get("phrase_time_limit", 8.0)))
    print("(listening…)")
    try:
        with mic as source:
            audio = r.listen(source, timeout=6, phrase_time_limit=phrase_limit)
    except sr.WaitTimeoutError:
        return ""
    except Exception as exc:
        logging.warning("voice input failed: %s", exc)
        return ""

    # Try each language in order; take the first hypothesis we get.
    for lang in langs:
        try:
            raw = r.recognize_google(audio, language=lang, show_all=True)
        except sr.UnknownValueError:
            continue
        except sr.RequestError as exc:
            logging.warning("Google STT unreachable (%s): %s", lang, exc)
            continue
        except Exception as exc:
            logging.debug("recognize error (%s): %s", lang, exc)
            continue
        if raw and isinstance(raw, dict):
            alts = raw.get("alternative") or []
            if alts:
                text = (alts[0].get("transcript") or "").strip()
                if text:
                    print(f"you › {text}")
                    return text
    return ""


# ─────────────────────── wake listener factory ───────────────────────

def _build_wake_listener(config, phrases, on_command, on_wake, pause_flag):
    """Construct a WakeWordListener wired to the [stt] config block."""
    from wake import WakeWordListener

    stt = config.get("stt", {})
    return WakeWordListener(
        wake_phrases=phrases,
        on_command=on_command,
        on_wake=on_wake,
        pause_flag=pause_flag,
        language=stt.get("language", "en-US"),
        alt_languages=stt.get("alt_languages", []),
        energy_threshold=stt.get("energy_threshold"),
        pause_threshold=float(stt.get("pause_threshold", 0.7)),
        phrase_time_limit=float(stt.get("phrase_time_limit", 6.0)),
        followup_time_limit=float(stt.get("followup_time_limit", 10.0)),
        conversation_timeout=float(stt.get("conversation_timeout", 12.0)),
        fuzzy_threshold=float(stt.get("fuzzy_threshold", 0.78)),
        recalibrate_every_seconds=float(stt.get("recalibrate_every_seconds", 300.0)),
        mic_index=stt.get("mic_index"),
    )


# ─────────────────────── loop drivers ───────────────────────

def run_text_or_oneshot(config: dict, voice: Voice, use_voice: bool) -> int:
    dispatcher = CommandDispatcher(config=config, voice=voice)
    address = config["user"].get("address", "")

    listener = None
    if use_voice:
        listener = _make_one_shot_listener(config)
        if listener is None:
            print("[jarvis] voice input unavailable, falling back to text.", file=sys.stderr)
            use_voice = False

    while True:
        try:
            command = _read_voice(listener, config) if use_voice else _read_text()
        except (KeyboardInterrupt, EOFError):
            voice.say("Signing off. Have a productive day.")
            return 0

        if not command:
            continue

        result = dispatcher.dispatch(command)
        if result.speak:
            voice.say(result.speak)
        if result.print_out:
            print(result.print_out)
        if result.should_exit:
            return 0


def run_ui_loop(config: dict, voice: Voice) -> int:
    """Wake-word loop with a floating orb HUD. Blocks on the Qt event loop."""
    from PySide6.QtWidgets import QApplication

    from ui_bridge import JarvisBridge
    from jarvis_ui import run_hud
    from wake import WakeWordListener

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)

    bridge = JarvisBridge()

    # Guard so the wake listener silences itself while Jarvis is speaking.
    listener_pause = threading.Event()

    class GuardedUIVoice:
        def __init__(self, inner: Voice) -> None:
            self._inner = inner

        def say(self, text: str) -> None:
            bridge.push_jarvis(text)
            bridge.set_state("speaking")
            listener_pause.set()
            try:
                self._inner.say(text)
                time.sleep(0.25)
            finally:
                listener_pause.clear()
                bridge.set_state("idle")

        def set_voice(self, name: str) -> str:
            return self._inner.set_voice(name)

    guarded = GuardedUIVoice(voice)
    dispatcher = CommandDispatcher(config=config, voice=guarded)

    cmd_queue: "queue.Queue[str]" = queue.Queue()

    wake_cfg = config.get("wake", {})
    phrases = wake_cfg.get("phrases", ["hey jarvis", "jarvis"])
    address = config["user"].get("address", "")
    default_ack = f"Yes, {address}?" if address else "Yes?"
    ack = wake_cfg.get("ack", default_ack)

    def _on_wake() -> None:
        bridge.set_state("listening")
        guarded.say(ack)

    def _on_command(text: str) -> None:
        bridge.push_you(text)
        bridge.set_state("processing")
        cmd_queue.put(text)

    # Wake listener on its own daemon thread (unchanged from run_wake_loop).
    listener = _build_wake_listener(
        config,
        phrases=phrases,
        on_command=_on_command,
        on_wake=_on_wake,
        pause_flag=listener_pause,
    )
    listener.start()

    # Command dispatcher runs on a daemon thread so the Qt event loop stays
    # responsive. Voice.say() is synchronous but that's fine on this thread.
    def _dispatch_pump() -> None:
        while True:
            text = cmd_queue.get()
            if text == "__EXIT__":
                return
            result = dispatcher.dispatch(text)

            # UI-facing side-effects go out on the bridge first so the panel
            # appears before Jarvis starts speaking.
            if result.ui_action:
                bridge.ui_action.emit(result.ui_action)

            spoke = False
            if result.speak:
                guarded.say(result.speak)   # transitions state → speaking → idle
                spoke = True
            if result.print_out:
                print(result.print_out)

            # Failsafe: if the handler returned nothing spoken and nothing
            # else brought the state back down, force idle so the HUD
            # doesn't get stuck in "thinking" forever.
            if not spoke and not result.should_exit:
                bridge.set_state("idle")

            if result.should_exit:
                bridge.quit_requested.emit()
                return

    threading.Thread(target=_dispatch_pump, daemon=True).start()

    # Boot greeting on startup (unless suppressed elsewhere).
    bridge.set_state("idle")

    ret = run_hud(bridge)
    listener.stop()
    cmd_queue.put("__EXIT__")
    return ret


def run_wake_loop(config: dict, voice: Voice) -> int:
    """Always-on wake-word mode. Also accepts typed commands in parallel."""
    from wake import WakeWordListener

    pause = threading.Event()
    guarded = GuardedVoice(voice, pause)
    dispatcher = CommandDispatcher(config=config, voice=guarded)
    address = config["user"].get("address", "")

    cmd_queue: "queue.Queue[str]" = queue.Queue()

    wake_cfg = config.get("wake", {})
    phrases = wake_cfg.get("phrases", ["hey jarvis", "jarvis"])
    default_ack = f"Yes, {address}?" if address else "Yes?"
    ack = wake_cfg.get("ack", default_ack)

    def _on_wake() -> None:
        guarded.say(ack)

    def _on_command(text: str) -> None:
        cmd_queue.put(text)

    print(f"[jarvis] wake mode active. Say one of: {', '.join(phrases)}")
    print("[jarvis] you can also type a command any time. Ctrl+C to quit.\n")

    try:
        listener = _build_wake_listener(
            config,
            phrases=phrases,
            on_command=_on_command,
            on_wake=_on_wake,
            pause_flag=pause,
        )
        listener.start()
    except Exception as exc:
        print(f"[jarvis] wake listener failed to start: {exc}", file=sys.stderr)
        print("[jarvis] falling back to text mode.")
        return run_text_or_oneshot(config, voice, use_voice=False)

    # Parallel stdin reader so typed commands still work while listening.
    def _stdin_pump() -> None:
        while True:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                cmd_queue.put("__EXIT__")
                return
            if line.strip():
                cmd_queue.put(line.strip())

    threading.Thread(target=_stdin_pump, daemon=True).start()

    while True:
        try:
            text = cmd_queue.get()
        except KeyboardInterrupt:
            guarded.say("Signing off. Have a productive day.")
            listener.stop()
            return 0

        if text == "__EXIT__":
            listener.stop()
            return 0

        result = dispatcher.dispatch(text)
        if result.speak:
            guarded.say(result.speak)
        if result.print_out:
            print(result.print_out)
        if result.should_exit:
            listener.stop()
            return 0


# ─────────────────────── main ───────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Jarvis — a tiny personal assistant")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--no-greet", action="store_true", help="Skip boot routine")
    parser.add_argument("--voice", action="store_true", help="One-shot microphone input")
    parser.add_argument("--wake",  action="store_true",
                        help="Always-listening wake-word mode ('hey jarvis')")
    parser.add_argument("--ui",    action="store_true",
                        help="Wake mode with a floating orb HUD")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _write_lock()
    config = load_config(args.config)
    voice = Voice(
        enabled=config["voice"].get("enabled", True),
        rate=config["voice"].get("rate", 180),
        volume=config["voice"].get("volume", 0.9),
        engine=config["voice"].get("engine", "auto"),
        voice_name=config["voice"].get("voice_name"),
        address=config["user"].get("address", ""),
    )

    if not args.no_greet:
        boot(config, voice)

    # Config-driven default: `[wake].enabled = true` also triggers wake mode.
    wake_mode = args.wake or config.get("wake", {}).get("enabled", False)
    ui_mode = args.ui or config.get("ui", {}).get("enabled", False)

    if ui_mode:
        return run_ui_loop(config, voice)

    if wake_mode:
        return run_wake_loop(config, voice)

    print("\nType a command (help / quit).")
    return run_text_or_oneshot(config, voice, use_voice=args.voice)


if __name__ == "__main__":
    raise SystemExit(main())
