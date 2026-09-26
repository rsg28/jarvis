"""
Tiny always-on-top status orb for Jarvis.

A frameless, transparent, click-through widget that sits in a corner
of the screen and shows what Jarvis is doing:

    idle       - dim grey dot (barely visible)
    listening  - green pulse
    thinking   - amber pulse (STT / LLM / vision roundtrip)
    speaking   - blue pulse (voice.say playing)

Click-through means it never steals the mouse — you can click right
through it as if it wasn't there.

Uses PySide6, which is already in requirements.txt for the
Jarvis --ui mode. Runs the Qt event loop on the main thread; the
hotkey/audio work happens on background threads and communicates
state via a Qt signal (thread-safe by design).
"""
from __future__ import annotations

import logging
import math
from typing import Optional


class _IndicatorImpl:
    """Deferred import wrapper so the module can be imported even when
    PySide6 isn't installed. `create()` returns None on failure."""

    @staticmethod
    def create(size: int = 24, corner: str = "bottom-right",
               margin: int = 24) -> Optional["Indicator"]:
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            logging.warning("PySide6 not installed — indicator disabled")
            return None
        # Ensure a QApplication exists (safe if one is already running).
        app = QApplication.instance() or QApplication([])
        w = Indicator(size=size, corner=corner, margin=margin)
        w.show()
        return w


try:
    from PySide6.QtCore import Qt, QTimer, Signal, QObject, QPoint
    from PySide6.QtGui import QColor, QPainter, QBrush
    from PySide6.QtWidgets import QApplication, QWidget

    class _Signals(QObject):
        state_changed = Signal(str)

    class Indicator(QWidget):
        STATE_COLORS = {
            "idle":      (120, 120, 120, 90),
            "listening": (60,  220, 100, 240),
            "thinking":  (240, 190, 60,  240),
            "speaking":  (80,  170, 240, 240),
            "error":     (230, 80,  80,  240),
        }

        def __init__(self, size: int = 24, corner: str = "bottom-right",
                     margin: int = 24, parent=None) -> None:
            super().__init__(parent,
                             Qt.FramelessWindowHint |
                             Qt.WindowStaysOnTopHint |
                             Qt.Tool |
                             Qt.WindowTransparentForInput)
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setAttribute(Qt.WA_ShowWithoutActivating)
            self._size = int(size)
            self._pad = max(6, self._size // 3)
            box = self._size + self._pad * 2
            self.resize(box, box)

            # Position in the requested corner.
            screen = QApplication.primaryScreen().availableGeometry()
            positions = {
                "bottom-right": (screen.right() - box - margin,
                                 screen.bottom() - box - margin),
                "bottom-left":  (screen.left()  + margin,
                                 screen.bottom() - box - margin),
                "top-right":    (screen.right() - box - margin,
                                 screen.top()    + margin),
                "top-left":     (screen.left()  + margin,
                                 screen.top()    + margin),
            }
            x, y = positions.get(corner, positions["bottom-right"])
            self.move(int(x), int(y))

            self._state = "idle"
            self._pulse_phase = 0.0
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._tick)
            self._timer.start(50)  # 20 fps

            # Signals let us change state safely from any thread.
            self._signals = _Signals()
            self._signals.state_changed.connect(self._on_state)

        # ── thread-safe public API ──
        def set_state(self, state: str) -> None:
            self._signals.state_changed.emit(state)

        # ── internals ──
        def _on_state(self, state: str) -> None:
            if state != self._state:
                logging.debug("[indicator] %s -> %s", self._state, state)
            self._state = state
            self.update()

        def _tick(self) -> None:
            # Only animate when we're actively doing something.
            if self._state in ("listening", "thinking", "speaking"):
                self._pulse_phase = (self._pulse_phase + 0.09) % (2 * math.pi)
                self.update()

        def paintEvent(self, _ev) -> None:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            r, g, b, a = self.STATE_COLORS.get(self._state,
                                               self.STATE_COLORS["idle"])
            core = QColor(r, g, b, a)
            cx = self.width() / 2
            cy = self.height() / 2
            core_r = self._size / 2

            # Halo pulse when active.
            if self._state in ("listening", "thinking", "speaking"):
                pulse = 0.5 + 0.5 * math.sin(self._pulse_phase)
                halo = QColor(r, g, b, int(90 * pulse))
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(halo))
                halo_r = core_r + self._pad * (0.4 + 0.6 * pulse)
                p.drawEllipse(QPoint(int(cx), int(cy)),
                              int(halo_r), int(halo_r))

            # Core dot.
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(core))
            p.drawEllipse(QPoint(int(cx), int(cy)),
                          int(core_r), int(core_r))

    def create_indicator(size: int = 24, corner: str = "bottom-right",
                         margin: int = 24) -> Optional[Indicator]:
        return _IndicatorImpl.create(size=size, corner=corner, margin=margin)

except ImportError:
    # PySide6 missing — expose no-ops so imports don't blow up.
    Indicator = None  # type: ignore[assignment]

    def create_indicator(*_args, **_kwargs):  # type: ignore[misc]
        logging.warning("PySide6 not installed — indicator disabled")
        return None
