"""
Voice — text-to-speech for Jarvis.

Prefers Microsoft Edge's neural voices (edge-tts, free, no API key) with
pygame for playback. Gracefully falls back to SAPI5 (pyttsx3) and finally
to silent mode if nothing is available.

Recommended neural voices (all sound very natural):
    en-US-JennyNeural       (default, warm female US)
    en-US-AriaNeural        (crisp female US, news-anchor style)
    en-US-MichelleNeural    (younger female US)
    en-GB-SoniaNeural       (female UK)
    es-ES-ElviraNeural      (female Spain)
    es-MX-DaliaNeural       (female Mexico)
    fr-FR-DeniseNeural      (female France)
    en-US-GuyNeural         (male, if you ever want it)

List them all:  edge-tts --list-voices
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Optional


class Voice:
    NEURAL_DEFAULT = "en-US-JennyNeural"

    def __init__(
        self,
        enabled: bool = True,
        rate: int = 180,
        volume: float = 0.9,
        engine: str = "auto",
        voice_name: Optional[str] = None,
    ) -> None:
        self.enabled = enabled
        self.rate = rate
        self.volume = max(0.0, min(1.0, float(volume)))
        self.engine = engine  # "auto" | "edge" | "sapi"
        self.voice_name = voice_name
        self._edge_ready = False
        self._sapi = None

        if not enabled:
            return

        if engine in ("auto", "edge"):
            self._edge_ready = self._init_edge()

        if not self._edge_ready:
            self._init_sapi()

    # ────────────── engine setup ──────────────
    def _init_edge(self) -> bool:
        try:
            import edge_tts  # noqa: F401
            import pygame

            # Init mixer once. Silent init — no window, no console spam.
            os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
            pygame.mixer.init()
            if not self.voice_name:
                self.voice_name = self.NEURAL_DEFAULT
            return True
        except Exception as exc:
            logging.info("edge-tts unavailable (%s); falling back to SAPI5", exc)
            return False

    def _init_sapi(self) -> None:
        try:
            import pyttsx3

            self._sapi = pyttsx3.init()
            self._sapi.setProperty("rate", self.rate)
            self._sapi.setProperty("volume", self.volume)
            # Prefer a female voice (Zira on default Windows install)
            for v in self._sapi.getProperty("voices"):
                gender = (v.gender or "").lower()
                if gender == "female" or any(
                    key in v.name.lower() for key in ("zira", "hazel", "helena", "sabina", "eva")
                ):
                    self._sapi.setProperty("voice", v.id)
                    break
        except Exception as exc:
            logging.warning("SAPI5 init failed: %s", exc)
            self._sapi = None

    # ────────────── public API ──────────────
    def say(self, text: str) -> None:
        if not self.enabled or not text:
            return

        # Trim excess whitespace but keep punctuation for natural prosody.
        text = " ".join(text.split())
        print(f"jarvis \u203a {text}")

        if self._edge_ready:
            try:
                self._say_edge(text)
                return
            except Exception as exc:
                logging.warning("edge-tts speak failed (%s); falling back to SAPI5", exc)
                self._edge_ready = False
                if self._sapi is None:
                    self._init_sapi()

        if self._sapi:
            try:
                self._sapi.say(text)
                self._sapi.runAndWait()
            except Exception as exc:
                logging.warning("SAPI5 speak failed: %s", exc)

    # ────────────── edge-tts implementation ──────────────
    def _say_edge(self, text: str) -> None:
        import edge_tts
        import pygame

        # edge-tts rate is a percentage relative to normal, e.g. "+10%".
        # We map our SAPI rate (default 180 WPM ~ normal) to a small delta.
        rate_pct = int(round((self.rate - 180) / 180 * 100))
        rate_str = f"{'+' if rate_pct >= 0 else ''}{rate_pct}%"
        vol_pct = int(round((self.volume - 1.0) * 100))
        vol_str = f"{'+' if vol_pct >= 0 else ''}{vol_pct}%"

        async def _synth(path: str) -> None:
            comm = edge_tts.Communicate(text, self.voice_name, rate=rate_str, volume=vol_str)
            await comm.save(path)

        fd, path = tempfile.mkstemp(suffix=".mp3", prefix="jarvis_")
        os.close(fd)
        try:
            asyncio.run(_synth(path))
            pygame.mixer.music.load(path)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                pygame.time.wait(60)
            pygame.mixer.music.unload()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
