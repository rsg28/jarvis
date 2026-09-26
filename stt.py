"""
Local Whisper STT backend for Jarvis.

Wraps `faster-whisper` (an efficient CTranslate2 re-implementation of
OpenAI's Whisper). It runs entirely offline, no API keys, and beats
Google's free STT badly on:

  - accented English
  - mixed languages (ES/EN/FR within the same utterance)
  - noisy environments
  - short utterances / one-word commands

Model sizes vs quality (all fine on CPU for command-style speech):

  tiny    ~ 40 M   fastest,  worst-of-good accuracy
  base    ~ 75 M   ~2x tiny, noticeably better  ← default
  small   ~ 245 M  much better, 2-4s per utterance on CPU
  medium  ~ 770 M  near-cloud quality, needs a decent CPU

First run downloads the model (~150 MB for `base`) and caches it under
`%USERPROFILE%\\.cache\\huggingface\\`.

We accept `speech_recognition.AudioData` objects on input for a
drop-in replacement in `_read_voice`.
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional


class WhisperBackend:
    def __init__(
        self,
        model_size: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
        language: Optional[str] = None,
        beam_size: int = 1,
    ) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        # `language=None` means Whisper auto-detects per utterance.
        # That's what lets it seamlessly switch between EN/ES/FR.
        self.language = (language or "").strip() or None
        self.beam_size = int(beam_size)
        self._model = None
        self._failed = False

    # ────────────── model lifecycle ──────────────
    def load(self) -> bool:
        """Force the model to load now (downloads on first run)."""
        return self._ensure_model() is not None

    def _ensure_model(self):
        if self._model is not None or self._failed:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            logging.warning(
                "faster-whisper not installed — Whisper STT disabled. "
                "Install with: pip install faster-whisper"
            )
            self._failed = True
            return None
        try:
            t0 = time.monotonic()
            self._model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
            )
            logging.info(
                "[whisper] loaded model=%s device=%s compute=%s in %.2fs",
                self.model_size, self.device, self.compute_type,
                time.monotonic() - t0,
            )
        except Exception as exc:
            logging.warning("faster-whisper init failed: %s", exc)
            self._failed = True
        return self._model

    @property
    def available(self) -> bool:
        return not self._failed

    # ────────────── transcription ──────────────
    def transcribe_audio(self, audio) -> Optional[dict]:
        """Transcribe a speech_recognition.AudioData.
        Returns {"text": str, "language": str, "duration": float} or None."""
        model = self._ensure_model()
        if model is None:
            return None

        import numpy as np
        # Whisper expects float32 mono @ 16 kHz in [-1, 1].
        try:
            wav_bytes = audio.get_wav_data(convert_rate=16000, convert_width=2)
        except Exception as exc:
            logging.warning("[whisper] could not export audio: %s", exc)
            return None

        arr = _wav_bytes_to_float32(wav_bytes)
        if arr is None or arr.size == 0:
            return None

        t0 = time.monotonic()
        try:
            segments, info = model.transcribe(
                arr,
                language=self.language,
                beam_size=self.beam_size,
                vad_filter=True,       # drop leading/trailing silence
                vad_parameters={"min_silence_duration_ms": 300},
                condition_on_previous_text=False,
                temperature=0.0,       # deterministic
                no_speech_threshold=0.5,
            )
            text = "".join(seg.text for seg in segments).strip()
        except Exception as exc:
            logging.warning("[whisper] transcribe failed: %s", exc)
            return None

        elapsed = time.monotonic() - t0
        detected = getattr(info, "language", None) or "?"
        logging.info(
            "[whisper] %.2fs → lang=%s (p=%.2f) text=%r",
            elapsed, detected,
            getattr(info, "language_probability", 0.0) or 0.0,
            text[:200],
        )
        return {
            "text": text,
            "language": detected,
            "duration": elapsed,
        }


def _wav_bytes_to_float32(wav_bytes: bytes):
    """Convert a mono 16-bit WAV blob to a float32 numpy array."""
    try:
        import io
        import wave
        import numpy as np
    except ImportError:
        return None
    with io.BytesIO(wav_bytes) as buf:
        with wave.open(buf, "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            n_channels = wf.getnchannels()
    arr = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if n_channels > 1:
        arr = arr.reshape(-1, n_channels).mean(axis=1)
    return arr


def build_from_config(config: dict) -> Optional[WhisperBackend]:
    """Return a WhisperBackend if [stt].engine = 'whisper', else None."""
    stt = config.get("stt", {}) or {}
    engine = (stt.get("engine") or "google").lower()
    if engine != "whisper":
        return None
    return WhisperBackend(
        model_size=stt.get("whisper_model", "base"),
        device=stt.get("whisper_device", "cpu"),
        compute_type=stt.get("whisper_compute_type", "int8"),
        language=stt.get("whisper_language", ""),
        beam_size=int(stt.get("whisper_beam_size", 1)),
    )
