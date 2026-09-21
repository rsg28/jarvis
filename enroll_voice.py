"""
One-time voice enrollment for Jarvis.

Records N short clips of the owner's voice, averages the embeddings
into a single voiceprint, and saves it to disk (default:
`voiceprint.npy` next to this script).

Usage
─────
    python enroll_voice.py                 # 5 clips at 4 s each
    python enroll_voice.py --clips 7 --seconds 5
    python enroll_voice.py --out D:/vault/raul.npy

After enrollment, set `[speaker].enabled = true` in config.toml and
Jarvis will only respond to your voice.
"""
from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from pathlib import Path
from typing import List

import numpy as np


PROMPTS = [
    "The quick brown fox jumps over the lazy dog.",
    "Hey Jarvis, open Spotify and play something focused.",
    "Two claps in any room should launch the assistant.",
    "Set a timer for twenty five minutes, and remind me to stretch.",
    "The weather in Vancouver today is honestly pretty grey.",
    "Trilingual — English, French, Spanish — take your pick.",
    "Compile the delivery metrics and email the summary by Friday.",
]

# 4 seconds at 16 kHz mono
SAMPLE_RATE = 16000


def record(seconds: float, sample_rate: int) -> np.ndarray:
    """Record `seconds` of audio from the default input device."""
    import sounddevice as sd

    frames = int(seconds * sample_rate)
    print(f"    recording {seconds:.0f}s at {sample_rate} Hz…", end="", flush=True)
    audio = sd.rec(frames, samplerate=sample_rate, channels=1, dtype="float32")
    sd.wait()
    print(" done.")
    # Squeeze channel dim.
    return audio.reshape(-1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Enroll your voice for Jarvis.")
    parser.add_argument("--clips", type=int, default=5,
                        help="How many enrollment clips to record (default: 5)")
    parser.add_argument("--seconds", type=float, default=4.0,
                        help="Length of each clip in seconds (default: 4.0)")
    parser.add_argument("--out", type=Path, default=None,
                        help="Where to save the voiceprint (.npy). "
                             "Default: voiceprint.npy next to this script.")
    parser.add_argument("--auto", action="store_true",
                        help="Skip 'press Enter' prompts — count down 3-2-1 "
                             "before each clip. Useful for hands-free runs.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        import sounddevice  # noqa: F401
    except ImportError:
        print("sounddevice is required. Install with: pip install sounddevice",
              file=sys.stderr)
        return 2

    try:
        from speaker import SpeakerVerifier
    except ImportError as exc:
        print(f"could not import speaker.py: {exc}", file=sys.stderr)
        return 2

    out = args.out or (Path(__file__).parent / "voiceprint.npy")

    verifier = SpeakerVerifier(voiceprint_path=out, sample_rate=SAMPLE_RATE)
    if verifier._encoder_lazy() is None:  # noqa: SLF001
        print(
            "resemblyzer isn't installed. Install it (and PyTorch):\n"
            "    pip install torch resemblyzer\n"
            "then rerun this script.",
            file=sys.stderr,
        )
        return 2

    print()
    print("=== Jarvis voice enrollment ===")
    print(f"I'll record {args.clips} short clips of your voice ({args.seconds:.0f}s each).")
    print("Speak naturally, at the distance you normally use.")
    print(f"Voiceprint will be saved to: {out}")
    print()

    import time as _time
    clips: List[np.ndarray] = []
    for i in range(args.clips):
        prompt = PROMPTS[i % len(PROMPTS)]
        print(f"\n[{i + 1}/{args.clips}] Say: \"{prompt}\"")
        if args.auto:
            for n in (3, 2, 1):
                print(f"    starting in {n}…", end="\r", flush=True)
                _time.sleep(1)
            print("    speak now!            ")
        else:
            input("    press Enter to start recording…")
        arr = record(args.seconds, SAMPLE_RATE)
        # Quick sanity: warn if the clip is basically silent.
        rms = float(np.sqrt(np.mean(arr ** 2)))
        if rms < 0.005:
            print("    ⚠  very quiet clip (RMS %.4f). Consider re-recording." % rms)
        else:
            print("    clip captured (RMS %.3f)." % rms)
        clips.append(arr)

    print()
    print("Computing voiceprint from the enrollment clips…")
    verifier.enroll(clips)
    print(f"✓ Saved voiceprint to {out}")
    print()
    print("Next: set [speaker].enabled = true in config.toml and restart Jarvis.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
