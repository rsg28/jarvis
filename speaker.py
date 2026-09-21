"""
Speaker verification for Jarvis.

We use Resemblyzer's VoiceEncoder — a lightweight PyTorch model
pretrained on VoxCeleb — to produce a 256-dim embedding per
utterance. Enrollment averages several embeddings from the owner's
voice into a single "voiceprint" that's stored on disk. At runtime
we compute the embedding of each incoming utterance and accept it
only when its cosine similarity to the voiceprint clears a
configurable threshold.

Design notes
────────────
· Fail-open by default: if the model isn't installed or no voiceprint
  has been enrolled yet, `is_owner()` returns True so Jarvis keeps
  working. Turn `enabled=true` in config to actually gate commands.
· Lazy encoder init — the ~40 MB torch model is only loaded the first
  time we verify, so cold-start latency for text mode stays fast.
· 16 kHz mono float32 in [-1, 1] — matches Resemblyzer's preprocessing.
· Threshold ~0.65 works well for VoxCeleb-style embeddings; tune per
  microphone. Higher = stricter.
"""
from __future__ import annotations

import io
import logging
import wave
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np


class SpeakerVerifier:
    def __init__(
        self,
        voiceprint_path: Path,
        *,
        threshold: float = 0.65,
        sample_rate: int = 16000,
    ) -> None:
        self.voiceprint_path = Path(voiceprint_path)
        self.threshold = float(threshold)
        self.sample_rate = int(sample_rate)
        self._encoder = None
        self._encoder_failed = False
        self._reference: Optional[np.ndarray] = None
        self._load_reference()

    # ────────────── public state ──────────────
    @property
    def enrolled(self) -> bool:
        return self._reference is not None

    @property
    def available(self) -> bool:
        return not self._encoder_failed

    # ────────────── enrollment ──────────────
    def enroll(self, wav_arrays: List[np.ndarray]) -> np.ndarray:
        """Compute the mean unit embedding across the enrollment clips
        and persist it to disk."""
        if not wav_arrays:
            raise ValueError("no audio clips supplied for enrollment")
        encoder = self._encoder_lazy()
        if encoder is None:
            raise RuntimeError("resemblyzer not available")

        from resemblyzer import preprocess_wav
        embs = []
        for arr in wav_arrays:
            wav = preprocess_wav(arr, source_sr=self.sample_rate)
            embs.append(encoder.embed_utterance(wav))
        mean = np.mean(embs, axis=0)
        mean /= (np.linalg.norm(mean) + 1e-9)

        self._reference = mean
        self.voiceprint_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(self.voiceprint_path, mean)
        logging.info("voiceprint saved to %s (%d dims)",
                     self.voiceprint_path, mean.shape[-1])
        return mean

    # ────────────── verification ──────────────
    def is_owner(self, wav_bytes: bytes) -> Tuple[bool, float]:
        """Return (accepted, similarity). Fail-open when not enrolled or
        when the encoder isn't installed — Jarvis stays usable while
        the user sets things up."""
        if not self.enrolled:
            return (True, 1.0)
        if self._encoder_failed:
            return (True, 1.0)
        try:
            arr = _wav_bytes_to_float32(wav_bytes)
        except Exception as exc:
            logging.debug("speaker: could not decode wav bytes: %s", exc)
            return (True, 1.0)

        sim = self._similarity(arr)
        if sim is None:
            return (True, 1.0)
        return (sim >= self.threshold, sim)

    def _similarity(self, arr: np.ndarray) -> Optional[float]:
        encoder = self._encoder_lazy()
        if encoder is None:
            return None
        try:
            from resemblyzer import preprocess_wav
            wav = preprocess_wav(arr, source_sr=self.sample_rate)
            emb = encoder.embed_utterance(wav)
            emb = emb / (np.linalg.norm(emb) + 1e-9)
            return float(np.dot(emb, self._reference))
        except Exception as exc:
            logging.debug("speaker: embed failed: %s", exc)
            return None

    # ────────────── internals ──────────────
    def _load_reference(self) -> None:
        if not self.voiceprint_path.exists():
            return
        try:
            self._reference = np.load(self.voiceprint_path)
        except Exception as exc:
            logging.warning("could not load voiceprint %s: %s",
                            self.voiceprint_path, exc)

    def _encoder_lazy(self):
        if self._encoder is not None or self._encoder_failed:
            return self._encoder
        try:
            from resemblyzer import VoiceEncoder
            self._encoder = VoiceEncoder(device="cpu")
        except ImportError as exc:
            logging.info(
                "resemblyzer not installed; speaker verification disabled "
                "(pip install resemblyzer). %s", exc,
            )
            self._encoder_failed = True
        except Exception as exc:
            logging.warning("resemblyzer init failed: %s", exc)
            self._encoder_failed = True
        return self._encoder


# ────────────── audio helpers ──────────────
def _wav_bytes_to_float32(wav_bytes: bytes) -> np.ndarray:
    """Decode a WAV blob (as produced by speech_recognition's
    audio.get_wav_data()) into a mono float32 array in [-1, 1]."""
    with io.BytesIO(wav_bytes) as buf:
        with wave.open(buf, "rb") as wf:
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            frames = wf.readframes(wf.getnframes())

    if sampwidth == 2:
        arr = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 4:
        arr = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif sampwidth == 1:
        arr = np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0
        arr /= 128.0
    else:
        raise ValueError(f"unsupported sample width: {sampwidth}")

    if n_channels > 1:
        arr = arr.reshape(-1, n_channels).mean(axis=1)
    return arr


# ────────────── config-driven factory ──────────────
def build_from_config(config: dict, base_dir: Path) -> Optional[SpeakerVerifier]:
    """Return a SpeakerVerifier if [speaker].enabled is true, else None.
    `base_dir` is used to resolve a relative voiceprint_path (defaults
    to the folder that owns config.toml)."""
    sp = (config.get("speaker") or {})
    if not sp.get("enabled", False):
        return None
    raw_path = sp.get("voiceprint_path", "voiceprint.npy")
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path(base_dir) / path
    return SpeakerVerifier(
        voiceprint_path=path,
        threshold=float(sp.get("threshold", 0.65)),
        sample_rate=int(sp.get("sample_rate", 16000)),
    )
