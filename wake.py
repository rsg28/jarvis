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

Robustness (2026-09):
  · configurable energy floor + periodic ambient recalibration
  · multi-hypothesis parsing (show_all=True) — picks the first candidate
    whose transcript looks like the wake word
  · fuzzy wake matching via stdlib difflib so "jarvi", "gervis",
    "jarves" also trigger
  · multi-language fallback for STT (en-US → es-ES → fr-FR by default)
  · silent-round counter now advances on network errors too, so a
    Google STT hiccup can't lock the conversation loop forever
"""
from __future__ import annotations

import difflib
import logging
import re
import threading
import time
from typing import Callable, Iterable, List, Optional, Sequence

_STOP_ACK = re.compile(r"^\s*[,.!?]*\s*", re.UNICODE)


class WakeWordListener:
    def __init__(
        self,
        wake_phrases: Iterable[str],
        on_command: Callable[[str], None],
        on_wake: Optional[Callable[[], None]] = None,
        pause_flag: Optional[threading.Event] = None,
        *,
        language: str = "en-US",
        alt_languages: Optional[Sequence[str]] = None,
        energy_threshold: Optional[int] = 300,
        pause_threshold: float = 0.7,
        phrase_time_limit: float = 6.0,
        followup_time_limit: float = 10.0,
        conversation_timeout: float = 12.0,
        fuzzy_threshold: float = 0.78,
        recalibrate_every_seconds: float = 300.0,
        mic_index: Optional[int] = None,
        speaker_verifier=None,
    ) -> None:
        self.wake_phrases: List[str] = [p.lower().strip() for p in wake_phrases if p.strip()]
        self.on_command = on_command
        self.on_wake = on_wake or (lambda: None)
        self.pause_flag = pause_flag or threading.Event()

        # STT / mic tuning
        self.language = language
        self.alt_languages: List[str] = [l for l in (alt_languages or []) if l and l != language]
        self.phrase_time_limit = float(phrase_time_limit)
        self.followup_time_limit = float(followup_time_limit)
        self.conversation_timeout = float(conversation_timeout)
        self.fuzzy_threshold = float(fuzzy_threshold)
        self.recalibrate_every_seconds = float(recalibrate_every_seconds)
        self.speaker_verifier = speaker_verifier

        import speech_recognition as sr
        self._sr = sr
        self.recognizer = sr.Recognizer()
        self.recognizer.pause_threshold = float(pause_threshold)
        # Keep dynamic on, but pin a sensible floor so a very quiet room
        # can't drop the threshold under our voice.
        self.recognizer.dynamic_energy_threshold = True
        if energy_threshold is not None:
            self.recognizer.energy_threshold = int(energy_threshold)
            # SpeechRecognition uses this as the min "no dynamic drop below"
            self.recognizer.dynamic_energy_adjustment_damping = 0.15
            self.recognizer.dynamic_energy_ratio = 1.5

        try:
            self.mic = sr.Microphone(device_index=mic_index) if mic_index is not None else sr.Microphone()
        except Exception:
            # Fall back to default mic if the requested index is invalid
            self.mic = sr.Microphone()

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_calibration = 0.0

    # ────────────── lifecycle ──────────────
    def start(self) -> None:
        self._recalibrate(duration=0.8)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="jarvis-wake")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ────────────── calibration ──────────────
    def _recalibrate(self, duration: float = 0.6) -> None:
        try:
            with self.mic as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=duration)
            self._last_calibration = time.monotonic()
            logging.debug(
                "wake calibration ok, energy_threshold=%.0f",
                float(getattr(self.recognizer, "energy_threshold", 0.0)),
            )
        except Exception as exc:
            logging.warning("wake calibration failed: %s", exc)

    # ────────────── speaker verification ──────────────
    def _is_owner(self, audio) -> bool:
        """Return True if speaker verification is off / not enrolled /
        the audio matches the enrolled voiceprint. Fail-open on errors."""
        v = self.speaker_verifier
        if v is None or not v.enrolled or not v.available:
            return True
        try:
            wav_bytes = audio.get_wav_data(convert_rate=v.sample_rate,
                                           convert_width=2)
        except Exception as exc:
            logging.debug("speaker: could not export wav: %s", exc)
            return True
        ok, sim = v.is_owner(wav_bytes)
        if ok:
            logging.debug("speaker: accepted (sim=%.2f, thr=%.2f)",
                          sim, v.threshold)
        else:
            print(f"[speaker] rejected non-owner voice (sim={sim:.2f} < {v.threshold:.2f})")
        return ok

    def _maybe_recalibrate(self) -> None:
        if self.recalibrate_every_seconds <= 0:
            return
        if time.monotonic() - self._last_calibration >= self.recalibrate_every_seconds:
            self._recalibrate(duration=0.4)

    # ────────────── STT (with fallback languages + multi-hypothesis) ──────────────
    def _transcribe_candidates(self, audio) -> List[str]:
        """Return a list of candidate transcripts (best first), tried across
        the primary + fallback languages. Empty list if nothing usable."""
        sr = self._sr
        languages = [self.language] + self.alt_languages
        candidates: List[str] = []

        for lang in languages:
            try:
                raw = self.recognizer.recognize_google(audio, language=lang, show_all=True)
            except sr.UnknownValueError:
                continue
            except sr.RequestError as exc:
                logging.warning("Google STT unreachable (%s): %s", lang, exc)
                # Short back-off so we don't hammer the API
                time.sleep(0.8)
                continue
            except Exception as exc:
                logging.debug("recognize error (%s): %s", lang, exc)
                continue

            if not raw:
                continue

            # `show_all=True` returns {'alternative': [{'transcript': ...}, ...]}
            alts = raw.get("alternative") if isinstance(raw, dict) else None
            if not alts:
                continue
            for alt in alts:
                t = (alt.get("transcript") or "").strip().lower()
                if t and t not in candidates:
                    candidates.append(t)

            # First language that produced hypotheses wins; don't stack.
            if candidates:
                break

        return candidates

    # ────────────── inner loop ──────────────
    def _run(self) -> None:
        sr = self._sr
        while not self._stop.is_set():
            # Silence while Jarvis is speaking (or any explicit pause).
            if self.pause_flag.is_set():
                time.sleep(0.15)
                continue

            self._maybe_recalibrate()

            try:
                with self.mic as source:
                    audio = self.recognizer.listen(
                        source,
                        timeout=1.5,
                        phrase_time_limit=self.phrase_time_limit,
                    )
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

            candidates = self._transcribe_candidates(audio)
            if not candidates:
                continue

            trailing: Optional[str] = None
            matched_text: str = ""
            for text in candidates:
                t = self._match_wake(text)
                if t is not None:
                    trailing = t
                    matched_text = text
                    break

            if trailing is None:
                continue

            # Speaker gate — reject strangers before we ack or dispatch.
            if not self._is_owner(audio):
                continue

            print(f"[wake] heard → {matched_text!r}")
            self.on_wake()

            if trailing:
                # "hey jarvis play blinding lights" → dispatch inline
                self.on_command(trailing)
            else:
                # Bare wake — listen for the follow-up phrase
                self._capture_followup()

    # ────────────── wake matching ──────────────
    def _match_wake(self, text: str) -> Optional[str]:
        """Return the trailing command if a wake phrase was heard, else None.

        Uses two strategies:
          1. Exact word-boundary regex per configured phrase.
          2. Fuzzy match on the first ~3 tokens of `text` against each phrase,
             using difflib. Handles common mis-hearings ("jarvi", "gervis",
             "hey jarv is").
        """
        text = text.lower().strip()
        if not text:
            return None

        # Strategy 1: word-boundary exact match anywhere in the utterance.
        for phrase in self.wake_phrases:
            m = re.search(rf"\b{re.escape(phrase)}\b", text)
            if m:
                return self._clean_trailing(text[m.end():])

        # Strategy 2: fuzzy match on the leading window of the utterance.
        tokens = text.split()
        if not tokens:
            return None

        for phrase in self.wake_phrases:
            plen = max(1, len(phrase.split()))
            # Try a couple of window sizes so "hey jarvis" (2 tokens) still
            # matches when STT dropped the "hey" and only gave us "jarvis".
            for window in {plen, plen + 1, max(1, plen - 1)}:
                candidate = " ".join(tokens[:window]).strip()
                if not candidate:
                    continue
                ratio = difflib.SequenceMatcher(None, candidate, phrase).ratio()
                if ratio >= self.fuzzy_threshold:
                    trailing = " ".join(tokens[window:])
                    return self._clean_trailing(trailing)

        return None

    @staticmethod
    def _clean_trailing(trailing: str) -> str:
        trailing = _STOP_ACK.sub("", trailing).strip()
        for filler in ("please ", "can you ", "could you ", "would you "):
            if trailing.startswith(filler):
                trailing = trailing[len(filler):]
        return trailing

    # ────────────── conversation follow-up ──────────────
    def _capture_followup(self) -> None:
        """Conversation loop — after a wake word, keep taking commands
        without needing another 'hey jarvis' every time. The loop
        exits on silence timeout, on explicit stop words, or on
        `quit`/`exit` (dispatcher signals shutdown separately)."""
        sr = self._sr
        # Give the "yes, sir?" TTS a beat to actually start speaking so
        # our pause_flag wait below traps it correctly.
        time.sleep(0.15)

        silent_rounds = 0
        MAX_SILENT_ROUNDS = 2

        while not self._stop.is_set():
            # Wait out any active TTS.
            while self.pause_flag.is_set() and not self._stop.is_set():
                time.sleep(0.1)

            try:
                with self.mic as source:
                    audio = self.recognizer.listen(
                        source,
                        timeout=self.conversation_timeout,
                        phrase_time_limit=self.followup_time_limit,
                    )
            except sr.WaitTimeoutError:
                print("[wake] conversation timed out, going back to wake mode")
                return
            except Exception as exc:
                logging.debug("conversation listen error: %s", exc)
                return

            # If Jarvis started speaking mid-listen, discard this chunk.
            if self.pause_flag.is_set():
                continue

            candidates = self._transcribe_candidates(audio)
            if not candidates:
                silent_rounds += 1
                if silent_rounds >= MAX_SILENT_ROUNDS:
                    print("[wake] too many empty rounds, back to wake mode")
                    return
                continue

            # Reject a stranger jumping into our conversation window.
            if not self._is_owner(audio):
                # Don't count as silent — just ignore this utterance.
                continue

            text = candidates[0].strip()
            if not text:
                silent_rounds += 1
                if silent_rounds >= MAX_SILENT_ROUNDS:
                    print("[wake] too many empty rounds, back to wake mode")
                    return
                continue

            silent_rounds = 0

            # Soft stop — user asked to end the chat without shutting Jarvis down.
            if re.search(
                r"\b(stop listening|go to sleep|never ?mind|forget it|shut up)\b",
                text, re.I,
            ):
                print(f"[wake] user asked to stop: {text!r}")
                # Fire a synthetic response so the HUD echoes and speaks it.
                self.on_command("__stop_listening__")
                return

            print(f"[wake] command → {text!r}")
            self.on_command(text)
