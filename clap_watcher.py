"""
Clap Watcher — a tiny background service that launches Jarvis when it
hears two claps in quick succession.

How it works
------------
A clap has three defining properties: a fast attack, a peak that clears
a loud threshold, and a very short decay. This script streams the
microphone through sounddevice, computes a short-time RMS + peak for
each block, and looks for two spike-shaped events whose peaks are
separated by 150 ms to 1200 ms — the natural cadence of a double clap.

To avoid false positives from speech and sustained noise the detector
demands:

    · peak above `--threshold`   (default 0.35 in float32)
    · block RMS below `--noise-floor` between claps
    · a 3 second cooldown after firing

When two claps are detected the script spawns Jarvis by running
`launch.bat --wake`. If Jarvis is already running (detected via a lock
file in the user's temp dir) the trigger is ignored.

Usage
-----
    python clap_watcher.py                        # default settings
    python clap_watcher.py --threshold 0.28       # more sensitive
    python clap_watcher.py --dry-run              # print only, don't launch

Hook it into Windows startup with `install_startup.ps1` (see repo).
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd

HERE = Path(__file__).resolve().parent
LOCK_PATH = Path(tempfile.gettempdir()) / "jarvis.lock"


# ─────────────────────── clap state machine ───────────────────────

class ClapDetector:
    """Detects double claps in a streaming audio callback."""

    def __init__(
        self,
        threshold: float = 0.35,
        noise_floor: float = 0.05,
        min_gap: float = 0.15,
        max_gap: float = 1.2,
        cooldown: float = 3.0,
    ) -> None:
        self.threshold = threshold
        self.noise_floor = noise_floor
        self.min_gap = min_gap
        self.max_gap = max_gap
        self.cooldown = cooldown

        self.state: str = "idle"          # idle | one_clap | cool
        self.first_clap_at: float = 0.0
        self.last_peak_at: float = 0.0
        self.cool_until: float = 0.0

    def feed(self, peak: float, rms: float, now: float) -> bool:
        """Return True exactly when a double clap has just been recognised."""
        if now < self.cool_until:
            return False

        is_spike = peak >= self.threshold and rms < self.noise_floor * 6

        # Also ignore very rapid re-fires from the same clap tail
        if is_spike and (now - self.last_peak_at) < 0.05:
            return False

        if is_spike:
            self.last_peak_at = now
            if self.state == "idle":
                self.state = "one_clap"
                self.first_clap_at = now
            elif self.state == "one_clap":
                gap = now - self.first_clap_at
                if self.min_gap <= gap <= self.max_gap:
                    # Double clap!
                    self.state = "cool"
                    self.cool_until = now + self.cooldown
                    return True
                else:
                    # Too fast or too slow — treat this as the new first clap
                    self.first_clap_at = now
        else:
            # If we've been waiting too long for the second clap, reset.
            if self.state == "one_clap" and (now - self.first_clap_at) > self.max_gap:
                self.state = "idle"

        return False


# ─────────────────────── launcher ───────────────────────

def _jarvis_is_running() -> bool:
    if not LOCK_PATH.exists():
        return False
    try:
        pid = int(LOCK_PATH.read_text().strip())
    except (ValueError, OSError):
        return False
    # On Windows, check whether the PID is still alive
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


def launch_jarvis(dry_run: bool = False) -> None:
    if _jarvis_is_running():
        print(f"[clap] Jarvis already running (pid in {LOCK_PATH.name}); ignoring double clap")
        return
    print("[clap] double clap detected — launching Jarvis")
    if dry_run:
        return

    bat = HERE / "launch.bat"
    if not bat.exists():
        print(f"[clap] launch.bat not found at {bat}", file=sys.stderr)
        return

    # `start` with a fresh window so the watcher's own console (if any) stays.
    # DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP so it survives when we exit.
    creationflags = 0
    if sys.platform == "win32":
        creationflags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    subprocess.Popen(
        ["cmd.exe", "/c", "start", "", str(bat), "--wake"],
        cwd=str(HERE),
        creationflags=creationflags,
    )


# ─────────────────────── main loop ───────────────────────

def run(
    threshold: float,
    noise_floor: float,
    device: Optional[int],
    dry_run: bool,
) -> int:
    samplerate = 16000
    blocksize = 512  # ~32 ms per block

    detector = ClapDetector(threshold=threshold, noise_floor=noise_floor)

    print(f"[clap] listening on device {device if device is not None else 'default'}")
    print(f"[clap] threshold={threshold:.2f}  noise_floor={noise_floor:.2f}")
    print("[clap] two quick claps → launch Jarvis. Ctrl+C to stop.")

    def callback(indata, frames, time_info, status):
        if status:
            logging.debug("stream status: %s", status)
        samples = indata[:, 0] if indata.ndim > 1 else indata
        peak = float(np.max(np.abs(samples)))
        rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
        now = time.monotonic()
        if detector.feed(peak, rms, now):
            launch_jarvis(dry_run=dry_run)

    try:
        with sd.InputStream(
            samplerate=samplerate,
            blocksize=blocksize,
            channels=1,
            dtype="float32",
            device=device,
            callback=callback,
        ):
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[clap] stopping")
        return 0
    except Exception as exc:
        print(f"[clap] audio stream failed: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Jarvis clap watcher")
    parser.add_argument("--threshold", type=float, default=0.35,
                        help="Peak amplitude that counts as a clap (0-1). "
                             "Lower = more sensitive. Default 0.35.")
    parser.add_argument("--noise-floor", type=float, default=0.05,
                        help="Ambient RMS ceiling to reject sustained noise.")
    parser.add_argument("--device", type=int, default=None,
                        help="sounddevice input device index. Default: system default.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print detections without launching Jarvis.")
    parser.add_argument("--list-devices", action="store_true",
                        help="Print available audio input devices and exit.")
    args = parser.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return run(args.threshold, args.noise_floor, args.device, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
