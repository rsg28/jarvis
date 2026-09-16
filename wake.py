"""
Wake-word listener — "hey jarvis" (or any configured phrase).

Runs on a daemon thread. Continuously chunks the microphone stream,
sends each chunk to Google's free STT (via SpeechRecognition), and
watches for the configured wake phrases. When it hears one, it:

  1. plays a short acknowledgement beep + speaks "yes?"
  2. optionally captures the rest of that same utterance as a command
     ("hey jarvis play blinding lights" → direct dispatch)
  3. otherwise records a follow-up phrase and dispatches that instead.

While Jarvis is speaking, the listener pauses so it doesn't try to
transcribe its own voice.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable, Iterable, List, Optional

_STOP_ACK = re.compile(r"^\s*[,.!?]*\s*", re.UNICODE)


class WakeWordListener:
    def __init__(
        self,
        wake_phrases: Iterable[str],
        on_command: Callable[[str], None],
        on_wake: Optional[Callable[[], None]] = None,
        pause_flag: Optional[threading.Event] = None,
    ) -> None:
        self.wake_phrases: List[str] = [p.lower().strip() for p in wake_phrases if p.strip()]
        self.on_command = on_command
        self.on_wake = on_wake or (lambda: None)
        self.pause_flag = pause_flag or threading.Event()

        import speech_recognition as sr
        self._sr = sr
        self.recognizer = sr.Recognizer()
        self.recognizer.pause_threshold = 0.7
        self.recognizer.dynamic_energy_threshold = True
        self.mic = sr.Microphone()

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ────────────── lifecycle ──────────────
    def start(self) -> None:
        try:
            with self.mic as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=0.8)
        except Exception as exc:
            logging.warning("wake calibration failed: %s", exc)

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="jarvis-wake")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ────────────── inner loop ──────────────
    def _run(self) -> None:
        sr = self._sr
        while not self._stop.is_set():
            # Silence while Jarvis is speaking (or any explicit pause).
            if self.pause_flag.is_set():
                time.sleep(0.15)
                continue

            try:
                with self.mic as source:
                    audio = self.recognizer.listen(source, timeout=1.5, phrase_time_limit=6)
            except sr.WaitTimeoutError:
                continue
            except OSError as exc:
                logging.debug("mic read error: %s", exc)
                time.sleep(0.3)
                continue
            except Exception as exc:
                logging.debug("listen error: %s", exc)
                time.sleep(0.3)
                continue

            # Skip if paused mid-listen (Jarvis started talking)
            if self.pause_flag.is_set():
                continue

            try:
                text = self.recognizer.recognize_google(audio).lower().strip()
            except sr.UnknownValueError:
                continue
            except sr.RequestError as exc:
                logging.warning("Google STT unreachable: %s", exc)
                time.sleep(1.0)
                continue
            except Exception as exc:
                logging.debug("recognize error: %s", exc)
                continue

            trailing = self._match_wake(text)
            if trailing is None:
                continue

            print(f"[wake] heard → {text!r}")
            self.on_wake()

            if trailing:
                # "hey jarvis play blinding lights" → dispatch inline
                self.on_command(trailing)
            else:
                # Bare wake — listen for the follow-up phrase
                self._capture_followup()

    def _match_wake(self, text: str) -> Optional[str]:
        """Return the trailing command if a wake phrase was heard, else None."""
        for phrase in self.wake_phrases:
            # Word-boundary match to avoid false positives like "generous"
            m = re.search(rf"\b{re.escape(phrase)}\b", text)
            if m:
                trailing = text[m.end():]
                # Strip leading punctuation and connective words
                trailing = _STOP_ACK.sub("", trailing).strip()
                for filler in ("please ", "can you ", "could you "):
                    if trailing.startswith(filler):
                        trailing = trailing[len(filler):]
                return trailing
        return None

    def _capture_followup(self) -> None:
        sr = self._sr
        try:
            with self.mic as source:
                audio = self.recognizer.listen(source, timeout=4.5, phrase_time_limit=8)
        except sr.WaitTimeoutError:
            print("[wake] no follow-up caught, going back to listening")
            return
        except Exception as exc:
            logging.debug("follow-up listen error: %s", exc)
            return

        try:
            text = self.recognizer.recognize_google(audio).strip()
        except (sr.UnknownValueError, sr.RequestError):
            return
        except Exception as exc:
            logging.debug("follow-up recognize error: %s", exc)
            return

        if text:
            print(f"[wake] command → {text!r}")
            self.on_command(text)
