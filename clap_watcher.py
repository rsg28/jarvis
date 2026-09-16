"""
Clap Watcher -- a tiny background service that launches Jarvis when it
hears two claps in quick succession.

How it works
------------
A clap has three defining properties: a fast attack, a peak that clears
a loud threshold, and a very short decay. This script streams the
microphone through sounddevice, computes a short-time RMS + peak for
each block, and looks for two spike-shaped events whose peaks are
separated by 150 ms to 1200 ms -- the natural cadence of a double clap.

To avoid false positives from speech and sustained noise the detector
demands:

    - peak above `--threshold`   (default 0.22 in float32)
    - block RMS below `--noise-floor` between claps
    - a 3 second cooldown after firing

By default the watcher runs in **one-shot** mode: the first successful
double clap launches Jarvis via `launch_ui.vbs` (silent HUD mode) and
then the watcher exits. From that point on the "hey jarvis" wake word
inside Jarvis handles all further commands. On next boot the Startup
folder re-arms the watcher automatically.

Pass `--continuous` to keep listening and relaunch Jarvis on every
double clap (useful for testing).

Usage
-----
    python clap_watcher.py                        # default: one-shot
    python clap_watcher.py --continuous           # keep firing forever
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
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd

HERE = Path(__file__).resolve().parent
LOCK_PATH = Path(tempfile.gettempdir()) / "jarvis.lock"
LOG_PATH  = Path(tempfile.gettempdir()) / "jarvis-clap.log"

# One-shot mode: after the very first successful double-clap launches
# Jarvis, the watcher stops listening. From that point on the user talks
# to Jarvis with the "hey jarvis" wake word. Next reboot re-arms clap.
_stop_event = threading.Event()


def _log(msg: str) -> None:
    """Best-effort append to the clap log so we can debug missed claps."""
    try:
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{stamp}  {msg}\n")
    except OSError:
        pass


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ clap state machine â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class ClapDetector:
    """Detects double claps in a streaming audio callback."""

    def __init__(
        self,
        threshold: float = 0.22,
        noise_floor: float = 0.05,
        min_gap: float = 0.12,
        max_gap: float = 1.4,
        cooldown: float = 2.5,
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

        # Cooldown just expired â€” reset the state so future claps are heard.
        if self.state == "cool":
            _log("cooldown expired, resetting state to idle")
            self.state = "idle"
            self.first_clap_at = 0.0

        is_spike = peak >= self.threshold and rms < self.noise_floor * 6

        # Also ignore very rapid re-fires from the same clap tail
        if is_spike and (now - self.last_peak_at) < 0.05:
            return False

        if is_spike:
            _log(f"spike peak={peak:.3f} rms={rms:.3f} state={self.state}")
            self.last_peak_at = now
            if self.state == "idle":
                self.state = "one_clap"
                self.first_clap_at = now
            elif self.state == "one_clap":
                gap = now - self.first_clap_at
                if self.min_gap <= gap <= self.max_gap:
                    # Double clap!
                    _log(f"DOUBLE CLAP fired (gap={gap*1000:.0f} ms)")
                    self.state = "cool"
                    self.cool_until = now + self.cooldown
                    return True
                else:
                    _log(f"spike rejected: gap={gap*1000:.0f} ms outside [{self.min_gap*1000:.0f}, {self.max_gap*1000:.0f}]")
                    # Too fast or too slow â€” treat this as the new first clap
                    self.first_clap_at = now
        else:
            # If we've been waiting too long for the second clap, reset.
            if self.state == "one_clap" and (now - self.first_clap_at) > self.max_gap:
                self.state = "idle"

        return False


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ launcher â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _jarvis_is_running() -> bool:
    if not LOCK_PATH.exists():
        return False
    try:
        pid = int(LOCK_PATH.read_text().strip())
    except (ValueError, OSError):
        _clean_stale_lock("unreadable")
        return False
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            _clean_stale_lock(f"pid {pid} dead")
            return False
        # Extra check: process might be zombie/exited but still handleable.
        exit_code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(handle)
        if exit_code.value != STILL_ACTIVE:
            _clean_stale_lock(f"pid {pid} exited (code {exit_code.value})")
            return False
        return True
    except Exception as exc:
        _log(f"lock check failed: {exc}")
        return False


def _clean_stale_lock(reason: str) -> None:
    _log(f"cleaning stale lock ({reason})")
    try:
        LOCK_PATH.unlink()
    except OSError:
        pass


def launch_jarvis(dry_run: bool = False, one_shot: bool = True) -> None:
    if _jarvis_is_running():
        _log("launch skipped: Jarvis already running")
        print(f"[clap] Jarvis already running (pid in {LOCK_PATH.name}); ignoring double clap")
        # Still one-shot even if Jarvis was already up: hand control over
        # to the wake word.
        if one_shot:
            _stop_event.set()
        return

    _log("launching Jarvis via launch_ui.vbs (--ui, silent)")
    print("[clap] double clap detected -- launching Jarvis")

    if dry_run:
        if one_shot:
            _stop_event.set()
        return

    # Preferred: launch_ui.vbs runs jarvis.py --ui via pythonw so NO
    # terminal window ever appears. The experience is HUD-only.
    vbs = HERE / "launch_ui.vbs"
    launched = False
    if vbs.exists():
        try:
            subprocess.Popen(
                ["wscript.exe", str(vbs)],
                cwd=str(HERE),
                creationflags=(0x08000000 if sys.platform == "win32" else 0),  # CREATE_NO_WINDOW
            )
            launched = True
        except OSError as exc:
            _log(f"launch_ui.vbs failed: {exc}")

    # Fallback: pythonw.exe directly, still without a console window.
    if not launched:
        pythonw = HERE / ".venv" / "Scripts" / "pythonw.exe"
        if not pythonw.exists():
            pythonw = Path("pythonw.exe")
        jarvis_py = HERE / "jarvis.py"
        try:
            subprocess.Popen(
                [str(pythonw), str(jarvis_py), "--ui"],
                cwd=str(HERE),
                creationflags=(0x08000000 if sys.platform == "win32" else 0),  # CREATE_NO_WINDOW
            )
            launched = True
            _log("launched via pythonw fallback (--ui)")
        except OSError as exc:
            _log(f"pythonw fallback failed: {exc}")
            print(f"[clap] could not launch Jarvis: {exc}", file=sys.stderr)

    if one_shot and launched:
        _log("one-shot mode: stopping clap watcher, wake word takes over")
        print("[clap] handing over to 'hey jarvis' wake word; stopping watcher")
        _stop_event.set()


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ main loop â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def run(
    threshold: float,
    noise_floor: float,
    device: Optional[int],
    dry_run: bool,
    one_shot: bool,
) -> int:
    samplerate = 16000
    blocksize = 512  # ~32 ms per block

    detector = ClapDetector(threshold=threshold, noise_floor=noise_floor)

    print(f"[clap] listening on device {device if device is not None else 'default'}")
    print(f"[clap] threshold={threshold:.2f}  noise_floor={noise_floor:.2f}")
    print(f"[clap] one-shot={one_shot} (default: exits after first launch)")
    print(f"[clap] log: {LOG_PATH}")
    print("[clap] two quick claps -> launch Jarvis. Ctrl+C to stop.")
    _log(f"startup: threshold={threshold} noise_floor={noise_floor} device={device} one_shot={one_shot}")

    def callback(indata, frames, time_info, status):
        if status:
            _log(f"stream status: {status}")
        samples = indata[:, 0] if indata.ndim > 1 else indata
        peak = float(np.max(np.abs(samples)))
        rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
        now = time.monotonic()
        if detector.feed(peak, rms, now):
            launch_jarvis(dry_run=dry_run, one_shot=one_shot)

    try:
        with sd.InputStream(
            samplerate=samplerate,
            blocksize=blocksize,
            channels=1,
            dtype="float32",
            device=device,
            callback=callback,
        ):
            while not _stop_event.is_set():
                time.sleep(0.25)
        _log("clap watcher done (one-shot fired)")
        return 0
    except KeyboardInterrupt:
        print("\n[clap] stopping")
        return 0
    except Exception as exc:
        print(f"[clap] audio stream failed: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Jarvis clap watcher")
    parser.add_argument("--threshold", type=float, default=0.22,
                        help="Peak amplitude that counts as a clap (0-1). "
                             "Lower = more sensitive. Default 0.22.")
    parser.add_argument("--noise-floor", type=float, default=0.05,
                        help="Ambient RMS ceiling to reject sustained noise.")
    parser.add_argument("--device", type=int, default=None,
                        help="sounddevice input device index. Default: system default.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print detections without launching Jarvis.")
    parser.add_argument("--list-devices", action="store_true",
                        help="Print available audio input devices and exit.")
    parser.add_argument("--continuous", action="store_true",
                        help="Keep listening after firing (relaunches on every "
                             "double clap). Default is one-shot: fire once, "
                             "then hand control to the 'hey jarvis' wake word.")
    args = parser.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return run(
        args.threshold,
        args.noise_floor,
        args.device,
        args.dry_run,
        one_shot=not args.continuous,
    )


if __name__ == "__main__":
    raise SystemExit(main())

