"""
Jarvis HUD — an always-on-top circular orb widget with reactive
animations for each assistant state (idle / listening / processing /
speaking). Frameless, transparent, draggable, Iron-Man-esque.

Runs on the Qt main thread. Background threads (wake listener, voice
engine) push state changes through a JarvisBridge (see ui_bridge.py)
which fires QueuedConnection signals — thread-safe by construction.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QEvent, QPoint, QPointF, QRectF, QSize, QTimer, Qt, Signal,
)
from PySide6.QtGui import (
    QAction, QBrush, QColor, QConicalGradient, QCursor, QFont, QFontMetrics,
    QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMenu, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)


# ─────────────────────── palette per state ───────────────────────

@dataclass(frozen=True)
class Palette:
    core:  QColor
    glow:  QColor
    ring:  QColor
    accent: QColor
    label: str


PALETTES: dict[str, Palette] = {
    "idle": Palette(
        core=QColor(30, 60, 90),
        glow=QColor(0, 200, 255, 160),
        ring=QColor(0, 180, 220, 90),
        accent=QColor(120, 220, 255),
        label="STANDBY",
    ),
    "listening": Palette(
        core=QColor(0, 130, 200),
        glow=QColor(0, 240, 255, 220),
        ring=QColor(120, 240, 255, 200),
        accent=QColor(220, 250, 255),
        label="LISTENING",
    ),
    "processing": Palette(
        core=QColor(160, 90, 15),
        glow=QColor(255, 200, 60, 220),
        ring=QColor(255, 210, 90, 200),
        accent=QColor(255, 230, 130),
        label="THINKING",
    ),
    "speaking": Palette(
        core=QColor(200, 30, 130),
        glow=QColor(255, 80, 180, 230),
        ring=QColor(255, 120, 200, 210),
        accent=QColor(255, 200, 240),
        label="SPEAKING",
    ),
}


# ─────────────────────── orb widget ───────────────────────

class OrbWidget(QWidget):
    """The reactive circular orb. Everything is painted from scratch."""

    ORB_SIZE = 120

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(self.ORB_SIZE + 28, self.ORB_SIZE + 28)  # margin for glow
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        self._state = "idle"
        self._phase = 0.0           # radians, drives pulse
        self._spin  = 0.0           # degrees, drives ring rotation
        self._ripples: list[tuple[float, float]] = []   # (phase, amplitude)
        self._last_ripple_at = 0.0

        # 60 fps animation loop
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ────────── public API ──────────
    def set_state(self, state: str) -> None:
        if state not in PALETTES:
            return
        if state == self._state:
            return
        self._state = state
        if state == "listening":
            self._ripples.append((0.0, 1.0))     # kick off a fresh ripple
        self.update()

    # ────────── animation tick ──────────
    def _tick(self) -> None:
        # Pulse speed depends on state — faster when active.
        speed = {
            "idle": 0.02,
            "listening": 0.09,
            "processing": 0.12,
            "speaking": 0.14,
        }.get(self._state, 0.04)
        self._phase = (self._phase + speed) % (2 * math.pi)
        self._spin  = (self._spin  + 0.6) % 360.0

        # Spawn a ripple every ~0.6 seconds while listening.
        if self._state == "listening":
            self._last_ripple_at += 0.016
            if self._last_ripple_at > 0.55:
                self._ripples.append((0.0, 1.0))
                self._last_ripple_at = 0.0

        # Age existing ripples
        new_ripples: list[tuple[float, float]] = []
        for r, a in self._ripples:
            r += 0.025
            if r < 1.0:
                new_ripples.append((r, a * (1.0 - r)))
        self._ripples = new_ripples

        self.update()

    # ────────── painting ──────────
    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)

        pal = PALETTES[self._state]
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        r = self.ORB_SIZE / 2

        pulse = 0.5 + 0.5 * math.sin(self._phase)   # 0..1

        # 1. Outer atmospheric glow (large, feathered)
        glow_r = r * (1.35 + 0.10 * pulse)
        g = QRadialGradient(QPointF(cx, cy), glow_r)
        c = QColor(pal.glow)
        c.setAlpha(int(180 * (0.55 + 0.45 * pulse)))
        g.setColorAt(0.55, c)
        g.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(QBrush(g))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(cx, cy), glow_r, glow_r)

        # 2. Sonar ripples (listening state)
        for phase, amp in self._ripples:
            rr = r * (1.0 + phase * 0.6)
            pen_color = QColor(pal.ring)
            pen_color.setAlpha(int(180 * amp))
            pen = QPen(pen_color, 2.2)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(cx, cy), rr, rr)

        # 3. Rotating outer ring (dashed arcs)
        rect = QRectF(cx - r * 1.05, cy - r * 1.05, r * 2.10, r * 2.10)
        pen = QPen(QColor(pal.accent.red(), pal.accent.green(), pal.accent.blue(), 200), 2)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        for start_deg in (0, 120, 240):
            p.drawArc(rect,
                      int((start_deg + self._spin) * 16),
                      int(55 * 16))

        # 4. Counter-rotating inner ring
        rect2 = QRectF(cx - r * 0.92, cy - r * 0.92, r * 1.84, r * 1.84)
        pen2 = QPen(QColor(pal.ring.red(), pal.ring.green(), pal.ring.blue(), 160), 1.4)
        p.setPen(pen2)
        for start_deg in (30, 210):
            p.drawArc(rect2,
                      int((start_deg - self._spin * 1.4) * 16),
                      int(90 * 16))

        # 5. Base orb — radial gradient dark → core color
        og = QRadialGradient(QPointF(cx, cy - r * 0.2), r)
        core = QColor(pal.core)
        og.setColorAt(0.0, core.lighter(160))
        og.setColorAt(0.55, core)
        og.setColorAt(1.0, QColor(6, 10, 20))
        p.setBrush(QBrush(og))
        p.setPen(QPen(QColor(pal.accent.red(), pal.accent.green(), pal.accent.blue(), 220), 1.6))
        p.drawEllipse(QPointF(cx, cy), r, r)

        # 6. State-specific inner overlay
        if self._state == "speaking":
            # Concentric expanding rings
            for i in range(3):
                sr = r * (0.20 + 0.24 * ((self._phase / (2 * math.pi) + i / 3.0) % 1.0))
                alpha = int(200 * (1.0 - sr / r))
                if alpha <= 0:
                    continue
                pen = QPen(QColor(pal.accent.red(), pal.accent.green(), pal.accent.blue(), alpha), 2)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(QPointF(cx, cy), sr, sr)
        elif self._state == "processing":
            # Spinning conic gradient arc as a "loader"
            cg = QConicalGradient(QPointF(cx, cy), -self._spin * 4)
            cg.setColorAt(0.0,  QColor(pal.accent.red(), pal.accent.green(), pal.accent.blue(), 230))
            cg.setColorAt(0.35, QColor(pal.accent.red(), pal.accent.green(), pal.accent.blue(), 0))
            cg.setColorAt(1.0,  QColor(pal.accent.red(), pal.accent.green(), pal.accent.blue(), 0))
            path = QPainterPath()
            path.addEllipse(QPointF(cx, cy), r * 0.55, r * 0.55)
            inner = QPainterPath()
            inner.addEllipse(QPointF(cx, cy), r * 0.38, r * 0.38)
            path = path.subtracted(inner)
            p.setBrush(QBrush(cg))
            p.setPen(Qt.NoPen)
            p.drawPath(path)
        elif self._state == "listening":
            # Central mic-dot with pulse
            dot_r = r * (0.12 + 0.03 * pulse)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(pal.accent))
            p.drawEllipse(QPointF(cx, cy), dot_r, dot_r)

        # 7. Bright inner core
        core_r = r * 0.14
        cg = QRadialGradient(QPointF(cx, cy), core_r * 2.2)
        cg.setColorAt(0.0, QColor(255, 255, 255, 230))
        cg.setColorAt(0.4, QColor(pal.accent.red(), pal.accent.green(), pal.accent.blue(), 180))
        cg.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(QBrush(cg))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(cx, cy), core_r * 2.2, core_r * 2.2)


# ─────────────────────── HUD window ───────────────────────

class JarvisHUD(QWidget):
    def __init__(self, bridge) -> None:
        super().__init__(None)
        self._bridge = bridge
        self._drag_pos: Optional[QPoint] = None

        # Frameless, transparent, always-on-top.
        self.setWindowFlags(
            Qt.Window
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("Jarvis HUD")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.orb = OrbWidget(self)
        layout.addWidget(self.orb, alignment=Qt.AlignHCenter)

        self.state_label = self._chip("STANDBY")
        layout.addWidget(self.state_label, alignment=Qt.AlignHCenter)

        self.transcript_label = self._chip("", small=True)
        self.transcript_label.setMinimumWidth(210)
        self.transcript_label.setMaximumWidth(240)
        layout.addWidget(self.transcript_label, alignment=Qt.AlignHCenter)

        # Help panel (created lazily on first `help` command).
        self._help_panel: Optional[HelpPanel] = None

        self.adjustSize()
        self._dock_bottom_right()

        # Wire up bridge signals (queued = thread-safe)
        bridge.state_changed.connect(self._on_state, Qt.QueuedConnection)
        bridge.transcript.connect(self._on_transcript, Qt.QueuedConnection)
        bridge.ui_action.connect(self._on_ui_action, Qt.QueuedConnection)
        bridge.quit_requested.connect(QApplication.instance().quit)

    # ────────── chip label style ──────────
    def _chip(self, text: str, small: bool = False) -> QLabel:
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setWordWrap(True)
        pad = "4px 12px" if not small else "3px 10px"
        size = 11 if not small else 10
        lbl.setStyleSheet(f"""
            QLabel {{
                color: #dff7ff;
                background: rgba(8, 16, 28, 190);
                border: 1px solid rgba(0, 220, 255, 120);
                border-radius: 10px;
                padding: {pad};
                font-family: "Segoe UI", "SF Pro Display", sans-serif;
                font-size: {size}pt;
                font-weight: 600;
                letter-spacing: 1px;
            }}
        """)
        return lbl

    def _dock_bottom_right(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        w = self.sizeHint().width()
        h = self.sizeHint().height()
        x = screen.right() - w - 24
        y = screen.bottom() - h - 24
        self.setGeometry(x, y, w, h)

    # ────────── bridge slots ──────────
    def _on_state(self, state: str) -> None:
        self.orb.set_state(state)
        pal = PALETTES.get(state)
        if pal:
            self.state_label.setText(pal.label)

    def _on_transcript(self, kind: str, text: str) -> None:
        prefix = "you" if kind == "you" else "jarvis"
        short = text.strip()
        if len(short) > 90:
            short = short[:87] + "…"
        self.transcript_label.setText(f"{prefix}  ›  {short}")

    def _on_ui_action(self, action: str) -> None:
        if action == "help":
            if self._help_panel is None:
                self._help_panel = HelpPanel(anchor=self)
            self._help_panel.show()
            self._help_panel.raise_()
            self._help_panel.activateWindow()

    # ────────── drag + context menu ──────────
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        elif event.button() == Qt.RightButton:
            self._show_menu(event.globalPosition().toPoint())
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and (event.buttons() & Qt.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    def _show_menu(self, at: QPoint) -> None:
        m = QMenu(self)
        m.setStyleSheet("""
            QMenu { background: #0b1622; color: #dff7ff;
                    border: 1px solid rgba(0, 220, 255, 140);
                    padding: 4px; }
            QMenu::item { padding: 6px 18px; }
            QMenu::item:selected { background: rgba(0, 220, 255, 60); }
        """)
        act_reset = QAction("Reset position", self)
        act_reset.triggered.connect(self._dock_right_edge)
        m.addAction(act_reset)

        act_top = QAction("Always on top", self, checkable=True)
        act_top.setChecked(bool(self.windowFlags() & Qt.WindowStaysOnTopHint))
        act_top.triggered.connect(self._toggle_top)
        m.addAction(act_top)

        m.addSeparator()
        act_quit = QAction("Quit Jarvis", self)
        act_quit.triggered.connect(self._bridge.quit_requested.emit)
        m.addAction(act_quit)
        m.exec(at)

    def _toggle_top(self) -> None:
        flags = self.windowFlags() ^ Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()


# ─────────────────────── help panel ───────────────────────

# Grouped command reference — categories keep the panel scannable.
HELP_SECTIONS = [
    ("Apps & Web", [
        ("open <app>",              "spotify, chrome, code, notepad, calc…"),
        ("search <query>",          "Google in your default browser"),
        ("play <query>",            "search Spotify"),
        ("play / pause / next / previous", "media keys"),
    ]),
    ("News & Live", [
        ("news [<topic>]",          "world · tech · sports · business · science"),
        ("scores [<league|team>]",  "la liga · premier · champions · mls · peru"),
        ("weather [<location>]",    "wttr.in one-liner"),
    ]),
    ("System", [
        ("what time is it · what day is it", "clock / date"),
        ("battery · cpu · ram · disk · ip · wifi", "psutil readings"),
        ("volume up · down · mute · set volume <0-100>", "pycaw master"),
        ("screenshot",              "PNG to your Desktop"),
    ]),
    ("Timers & Focus", [
        ("set a timer for 25 minutes", "one-off timer"),
        ("pomodoro",                 "25-minute focus block"),
        ("remind me in 30 minutes to stretch", "voice reminder"),
        ("timers · cancel timers",   "list / cancel jobs"),
    ]),
    ("Fun & Voice", [
        ("joke · trivia",            "JokeAPI + OpenTDB"),
        ("speak spanish · english · french · british", "swap the neural voice"),
    ]),
    ("Session", [
        ("stop listening",           "end this conversation, wait for wake"),
        ("quit / exit",              "shut Jarvis down"),
    ]),
]


class HelpPanel(QWidget):
    """A gamer-styled terminal-looking window that lists every command."""

    def __init__(self, anchor: Optional[QWidget] = None) -> None:
        super().__init__(None)
        self._anchor = anchor
        self._drag_pos: Optional[QPoint] = None

        self.setWindowFlags(
            Qt.Window
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("Jarvis · Commands")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # Card container
        card = QWidget(self)
        card.setObjectName("helpCard")
        card.setStyleSheet("""
            QWidget#helpCard {
                background: rgba(6, 14, 24, 235);
                border: 1px solid rgba(0, 220, 255, 140);
                border-radius: 14px;
            }
            QLabel {
                color: #dff7ff;
                font-family: "JetBrains Mono", "Cascadia Mono", "Consolas", monospace;
            }
            QLabel[role="title"] {
                color: #dff7ff;
                font-size: 12pt;
                font-weight: 700;
                letter-spacing: 3px;
            }
            QLabel[role="section"] {
                color: #80e4ff;
                font-size: 9.5pt;
                font-weight: 700;
                letter-spacing: 2px;
                margin-top: 4px;
            }
            QLabel[role="cmd"] {
                color: #b6f2ff;
                font-size: 9.5pt;
                font-weight: 600;
            }
            QLabel[role="desc"] {
                color: rgba(210, 235, 250, 180);
                font-size: 9.5pt;
            }
            QLabel[role="prompt"] {
                color: rgba(255, 158, 214, 220);
                font-size: 9pt;
                font-weight: 600;
                letter-spacing: 1px;
            }
            QPushButton {
                color: #dff7ff;
                background: rgba(0, 220, 255, 40);
                border: 1px solid rgba(0, 220, 255, 130);
                border-radius: 6px;
                padding: 4px 10px;
                font-family: "Segoe UI", sans-serif;
                font-size: 9pt;
                font-weight: 600;
            }
            QPushButton:hover {
                background: rgba(0, 220, 255, 80);
            }
        """)
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(18, 14, 18, 16)
        card_lay.setSpacing(4)

        # Header row: title chip + close button
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 6)
        title = QLabel("JARVIS · COMMAND PANEL")
        title.setProperty("role", "title")
        prompt = QLabel(">_")
        prompt.setProperty("role", "prompt")
        header.addWidget(prompt)
        header.addSpacing(6)
        header.addWidget(title)
        header.addStretch(1)
        close_btn = QPushButton("close")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.hide)
        header.addWidget(close_btn)
        card_lay.addLayout(header)

        # Sections
        for section, entries in HELP_SECTIONS:
            sec_lbl = QLabel(f"── {section.upper()} ──")
            sec_lbl.setProperty("role", "section")
            card_lay.addWidget(sec_lbl)
            for cmd, desc in entries:
                row = QHBoxLayout()
                row.setContentsMargins(8, 0, 0, 0)
                cmd_lbl = QLabel(cmd)
                cmd_lbl.setProperty("role", "cmd")
                cmd_lbl.setMinimumWidth(230)
                desc_lbl = QLabel(desc)
                desc_lbl.setProperty("role", "desc")
                desc_lbl.setWordWrap(True)
                row.addWidget(cmd_lbl, 0)
                row.addWidget(desc_lbl, 1)
                card_lay.addLayout(row)

        # Footer hint
        foot = QLabel("Tip: say 'hey jarvis' once, then keep talking. 'stop listening' ends the conversation without closing me.")
        foot.setProperty("role", "desc")
        foot.setWordWrap(True)
        foot.setContentsMargins(0, 10, 0, 0)
        card_lay.addWidget(foot)

        outer.addWidget(card)
        self.adjustSize()
        self._reposition()

    def _reposition(self) -> None:
        """Sit above-left of the orb, clamped to the screen."""
        screen = QApplication.primaryScreen().availableGeometry()
        w = self.sizeHint().width()
        h = self.sizeHint().height()
        if self._anchor and self._anchor.isVisible():
            ax = self._anchor.geometry().x()
            ay = self._anchor.geometry().y()
            x = max(screen.left() + 20, ax - w - 20)
            y = max(screen.top() + 20, ay + self._anchor.height() - h)
        else:
            x = screen.right() - w - 200
            y = screen.bottom() - h - 40
        self.setGeometry(x, y, w, h)

    # Drag to reposition
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and (event.buttons() & Qt.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_pos)
    def mouseReleaseEvent(self, event):
        self._drag_pos = None


# ─────────────────────── entry point ───────────────────────

def run_hud(bridge) -> int:
    """Create the QApplication and show the HUD. Blocks until user quits."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)

    hud = JarvisHUD(bridge)
    hud.show()
    hud.raise_()
    hud.activateWindow()

    return app.exec()


if __name__ == "__main__":
    # Standalone preview mode — cycles through states so you can see the animations.
    from ui_bridge import JarvisBridge
    b = JarvisBridge()
    app = QApplication(sys.argv)
    hud = JarvisHUD(b)
    hud.show()

    states = ["idle", "listening", "processing", "speaking"]
    idx = [0]
    def cycle():
        s = states[idx[0] % len(states)]
        b.set_state(s)
        b.push_you("what's the cpu load")
        b.push_jarvis(f"State demo: {s}")
        idx[0] += 1
    t = QTimer()
    t.timeout.connect(cycle)
    t.start(2500)
    cycle()
    sys.exit(app.exec())
