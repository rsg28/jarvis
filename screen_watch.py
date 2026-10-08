"""
Continuous screen watch for Jarvis (Phase 6, pixel-diff backend).

A background daemon that samples the primary display at a configurable
interval, compares each sample to the previous one, and fires a
callback when the mean per-pixel difference crosses a threshold. The
watcher knows SOMETHING changed, not what — that's the trade-off for
zero rate-limits, zero model downloads, and ~2 ms per sample.

Design:
    * Capture the screen (full res) with PIL.ImageGrab.
    * Downscale to a tiny 64x64 grayscale thumbnail. All comparison
      happens on the thumbnail — it's enough to detect window
      switches, dialog pops, video playing, etc., and keeps CPU
      cost in the single-digit milliseconds.
    * mean(abs(now - prev)) in [0, 255]. We call it "activity".
    * Threshold defaults to 15 — fires on any meaningful UI change
      but ignores anti-aliasing jitter and video thumbnail cycling.
    * Fires `on_change(activity, change_count, ts)` on a daemon
      thread. The caller decides whether to speak, toast, log,
      increment a counter, whatever.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


THUMB_SIZE = 64
DEFAULT_INTERVAL_S = 60.0
DEFAULT_THRESHOLD = 15.0


@dataclass
class WatchState:
    running: bool = False
    change_count: int = 0
    last_change_ts: float = 0.0
    last_activity: float = 0.0
    started_at: float = field(default_factory=time.time)
    interval_s: float = DEFAULT_INTERVAL_S
    threshold: float = DEFAULT_THRESHOLD


class ScreenWatcher:
    """Thread-based pixel-diff screen watcher. One at a time."""

    def __init__(self,
                 on_change: Callable[[float, int, float], None],
                 interval_s: float = DEFAULT_INTERVAL_S,
                 threshold: float = DEFAULT_THRESHOLD) -> None:
        self._on_change = on_change
        self._state = WatchState(interval_s=interval_s, threshold=threshold)
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

    # ────────────── public API ──────────────
    @property
    def state(self) -> WatchState:
        return self._state

    def start(self) -> bool:
        if self._state.running:
            return False
        self._stop_evt.clear()
        self._state = WatchState(
            running=True,
            interval_s=self._state.interval_s,
            threshold=self._state.threshold,
        )
        self._thread = threading.Thread(
            target=self._loop, name="jarvis-screen-watch", daemon=True)
        self._thread.start()
        logging.info("[watch] started (interval=%.1fs threshold=%.1f)",
                     self._state.interval_s, self._state.threshold)
        return True

    def stop(self) -> bool:
        if not self._state.running:
            return False
        self._stop_evt.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._state.running = False
        logging.info("[watch] stopped after %d changes",
                     self._state.change_count)
        return True

    # ────────────── internals ──────────────
    def _loop(self) -> None:
        try:
            from PIL import ImageGrab, ImageChops
        except Exception as exc:
            logging.error("[watch] PIL unavailable, aborting: %s", exc)
            self._state.running = False
            return
        prev_thumb = None
        while not self._stop_evt.is_set():
            try:
                img = ImageGrab.grab(all_screens=False).convert("L")
                thumb = img.resize((THUMB_SIZE, THUMB_SIZE))
                if prev_thumb is not None:
                    diff = ImageChops.difference(thumb, prev_thumb)
                    # mean per-pixel absolute diff in 0..255
                    # getdata() avoids a numpy dependency
                    data = list(diff.getdata())
                    activity = sum(data) / len(data) if data else 0.0
                    self._state.last_activity = activity
                    if activity >= self._state.threshold:
                        self._state.change_count += 1
                        self._state.last_change_ts = time.time()
                        try:
                            self._on_change(activity,
                                            self._state.change_count,
                                            self._state.last_change_ts)
                        except Exception as exc:
                            logging.warning("[watch] on_change raised: %s", exc)
                prev_thumb = thumb
            except Exception as exc:
                logging.warning("[watch] sample failed: %s", exc)
            # Sleep in small slices so stop() returns quickly
            waited = 0.0
            step = 0.5
            while waited < self._state.interval_s and not self._stop_evt.is_set():
                time.sleep(step)
                waited += step
