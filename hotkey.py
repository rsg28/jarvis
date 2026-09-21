"""
Global hotkey listener for Jarvis.

Registers a system-wide keyboard shortcut (e.g. Ctrl+Alt+J) that fires
a callback from any application on Windows. Uses the `keyboard`
package, which installs a low-level raw HID hook and does not require
admin rights on typical Windows setups.

Rationale: wake-word listening is convenient but noisy — Jarvis can
miss you, mishear you, or drain CPU running STT on every ambient
sound. A single hotkey is deterministic, silent, and works even when
the mic is muted for calls.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional


class HotkeyListener:
    def __init__(self, combo: str, on_trigger: Callable[[], None]) -> None:
        self.combo = combo
        self.on_trigger = on_trigger
        self._registered = False
        self._lock = threading.Lock()
        self._firing = False
        self._hotkey_handle = None

    def start(self) -> bool:
        """Register the combo with the OS. Returns True on success."""
        try:
            import keyboard  # noqa: F401
        except ImportError:
            logging.warning(
                "`keyboard` package not installed — hotkey mode disabled. "
                "Install with: pip install keyboard"
            )
            return False

        import keyboard
        try:
            self._hotkey_handle = keyboard.add_hotkey(
                self.combo, self._on_press, suppress=False, trigger_on_release=False
            )
            self._registered = True
            logging.info("hotkey registered: %s", self.combo)
            return True
        except ValueError as exc:
            logging.error("bad hotkey combo %r: %s", self.combo, exc)
            return False
        except Exception as exc:
            logging.error("could not register hotkey %s: %s", self.combo, exc)
            return False

    def stop(self) -> None:
        if not self._registered:
            return
        try:
            import keyboard
            if self._hotkey_handle is not None:
                keyboard.remove_hotkey(self._hotkey_handle)
        except Exception as exc:
            logging.debug("hotkey remove failed: %s", exc)
        self._registered = False

    def wait_forever(self) -> None:
        """Block the current thread until the process exits."""
        try:
            import keyboard
            keyboard.wait()
        except KeyboardInterrupt:
            pass

    def _on_press(self) -> None:
        # Re-entrancy guard: if the user machine-guns the combo, drop
        # the extras instead of stacking a queue of listens.
        with self._lock:
            if self._firing:
                logging.debug("hotkey: already handling a press, ignoring")
                return
            self._firing = True

        try:
            self.on_trigger()
        except Exception:
            logging.exception("hotkey callback crashed")
        finally:
            with self._lock:
                self._firing = False
