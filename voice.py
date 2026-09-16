"""
Thin wrapper around pyttsx3 (offline TTS). If pyttsx3 is missing or the
platform lacks a voice engine, it degrades to plain print — the assistant
keeps working, just quieter.
"""
from __future__ import annotations

import sys


class Voice:
    def __init__(self, *, enabled: bool = True, rate: int = 180, volume: float = 0.9) -> None:
        self.enabled = enabled
        self._engine = None
        if enabled:
            self._engine = self._make_engine(rate=rate, volume=volume)

    def _make_engine(self, *, rate: int, volume: float):
        try:
            import pyttsx3  # type: ignore
        except ImportError:
            print("[voice] pyttsx3 not installed — using text output only.", file=sys.stderr)
            return None
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", rate)
            engine.setProperty("volume", volume)
            return engine
        except Exception as exc:
            print(f"[voice] TTS engine init failed ({exc}) — falling back to print.",
                  file=sys.stderr)
            return None

    def say(self, text: str) -> None:
        # Always echo for visibility, even when the engine is speaking
        print(f"jarvis › {text}")
        if not self._engine:
            return
        try:
            self._engine.say(text)
            self._engine.runAndWait()
        except Exception as exc:
            print(f"[voice] failed to speak ({exc})", file=sys.stderr)
