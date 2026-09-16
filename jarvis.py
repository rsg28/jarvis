"""
Jarvis — a tiny personal assistant.

On boot:
    · greets you by name with a synthesized voice
    · opens Spotify (desktop app if installed, otherwise web player)
    · opens Google in your default browser

Then it loops, accepting either typed commands (default) or spoken
commands (with --voice). Type `help` for the command list, `quit` to exit.

Usage:
    python jarvis.py                       # boot + text loop
    python jarvis.py --no-greet            # skip the morning routine
    python jarvis.py --voice               # listen through the microphone

Config:  copy config.example.toml to config.toml and edit your name + apps.
"""
from __future__ import annotations

import argparse
import logging
import platform
import shlex
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

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
    "user": {"name": "Raul", "greeting_style": "morning"},
    "voice": {"enabled": True, "rate": 180, "volume": 0.9},
    "startup": {
        "open_spotify": True,
        "open_google": True,
        "extra_urls": [],
    },
    "apps": {
        # Friendly name → command / URL. Extend as you like.
        "spotify": "spotify",
        "chrome":  "chrome",
        "edge":    "msedge",
        "code":    "code",
        "notepad": "notepad",
        "calculator": "calc",
        "terminal":  "wt",
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

def morning_greeting(voice: Voice, name: str) -> str:
    hour = datetime.now().hour
    if hour < 12:
        greet = f"Good morning, {name}"
    elif hour < 18:
        greet = f"Good afternoon, {name}"
    else:
        greet = f"Good evening, {name}"
    now = datetime.now().strftime("%A, %B %d — %I:%M %p")
    line = f"{greet}. It is {now}. Ready when you are."
    voice.say(line)
    return line


def boot(config: dict, voice: Voice) -> None:
    name = config["user"]["name"]
    print(f"\n=== JARVIS · online ===")
    morning_greeting(voice, name)

    startup = config["startup"]
    if startup.get("open_spotify", True):
        _open_spotify(config)
    if startup.get("open_google", True):
        webbrowser.open("https://www.google.com")
    for url in startup.get("extra_urls", []) or []:
        webbrowser.open(url)


def _open_spotify(config: dict) -> None:
    """Try the desktop protocol first, fall back to the web player."""
    if platform.system() == "Windows":
        import subprocess
        try:
            subprocess.Popen(["start", "spotify:"], shell=True)
            return
        except Exception:
            pass
    # Any platform: web player
    webbrowser.open("https://open.spotify.com/")


# ─────────────────────── main loop ───────────────────────

def run_loop(config: dict, voice: Voice, use_voice: bool) -> int:
    dispatcher = CommandDispatcher(config=config, voice=voice)

    if use_voice:
        listener = _make_listener()
        if listener is None:
            print("[jarvis] voice input unavailable, falling back to text.", file=sys.stderr)
            use_voice = False

    while True:
        try:
            command = _read_voice(listener) if use_voice else _read_text()
        except (KeyboardInterrupt, EOFError):
            voice.say("Signing off. Have a good one.")
            return 0

        if not command:
            continue

        result: CommandResult = dispatcher.dispatch(command)

        if result.speak:
            voice.say(result.speak)
        if result.print_out:
            print(result.print_out)

        if result.should_exit:
            return 0


def _read_text() -> str:
    try:
        return input("you › ").strip()
    except UnicodeDecodeError:
        return ""


def _make_listener():  # pragma: no cover — hardware-dependent
    try:
        import speech_recognition as sr  # type: ignore
    except ImportError:
        return None
    try:
        r = sr.Recognizer()
        mic = sr.Microphone()
        with mic as source:
            r.adjust_for_ambient_noise(source, duration=0.6)
        return (r, mic)
    except Exception:
        return None


def _read_voice(listener) -> str:  # pragma: no cover
    import speech_recognition as sr  # type: ignore
    r, mic = listener
    print("(listening…)")
    try:
        with mic as source:
            audio = r.listen(source, timeout=6, phrase_time_limit=8)
        text = r.recognize_google(audio)
        print(f"you › {text}")
        return text
    except sr.WaitTimeoutError:
        return ""
    except sr.UnknownValueError:
        return ""
    except Exception as exc:
        logging.warning("voice input failed: %s", exc)
        return ""


# ─────────────────────── main ───────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Jarvis — a tiny personal assistant")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--no-greet", action="store_true", help="Skip boot routine")
    parser.add_argument("--voice", action="store_true", help="Use microphone input")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(args.config)
    voice = Voice(
        enabled=config["voice"].get("enabled", True),
        rate=config["voice"].get("rate", 180),
        volume=config["voice"].get("volume", 0.9),
    )

    if not args.no_greet:
        boot(config, voice)

    print("\nType a command (help / quit).")
    return run_loop(config, voice, use_voice=args.voice)


if __name__ == "__main__":
    raise SystemExit(main())
