"""
Floating text-chat window for Jarvis.

A small, borderless, always-on-top conversation panel that lives next
to the status orb. The user clicks the orb to open it, types a
command (or question for the LLM), and sees the reply inline — no
microphone involved. Pressing Esc or clicking the orb again closes
it.

Design goals:
- Minimal: no window chrome, dark translucent background, rounded corners.
- Non-blocking: dispatch runs on a worker thread so the UI never freezes.
- Thread-safe reply delivery: workers emit via a Qt signal.
- Keyboard-first: input field has focus on open, Enter submits.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

try:
    from PySide6.QtCore import Qt, QObject, Signal, QPoint
    from PySide6.QtGui import QKeySequence, QShortcut, QTextCursor
    from PySide6.QtWidgets import (
        QApplication, QWidget, QVBoxLayout, QHBoxLayout,
        QTextEdit, QLineEdit, QLabel, QPushButton,
    )
    _QT_OK = True
except ImportError:
    _QT_OK = False


if _QT_OK:

    class _Signals(QObject):
        reply_ready = Signal(str, bool)   # (text, is_user)
        close_requested = Signal()

    class ChatWindow(QWidget):
        """Minimal chat panel. `handler(text) -> str` is called on a
        background thread for each submission; the returned string is
        appended to the transcript."""

        def __init__(self,
                     handler: Callable[[str], str],
                     anchor_corner: str = "bottom-right",
                     anchor_margin: int = 24,
                     width: int = 420,
                     height: int = 340,
                     parent=None) -> None:
            super().__init__(parent,
                             Qt.FramelessWindowHint |
                             Qt.WindowStaysOnTopHint |
                             Qt.Tool)
            self.setAttribute(Qt.WA_TranslucentBackground)
            self._handler = handler
            self._anchor_corner = anchor_corner
            self._anchor_margin = anchor_margin
            self.resize(width, height)
            self._build_ui()
            self._reposition()

            self._signals = _Signals()
            self._signals.reply_ready.connect(self._append_message)
            self._signals.close_requested.connect(self.hide)

            # Esc closes.
            QShortcut(QKeySequence("Esc"), self, activated=self.hide)

        # ────────────── UI ──────────────
        def _build_ui(self) -> None:
            self.setStyleSheet("""
                QWidget#chatRoot {
                    background: rgba(22, 24, 32, 235);
                    border: 1px solid rgba(90, 110, 150, 160);
                    border-radius: 10px;
                }
                QLabel#title {
                    color: rgba(220, 230, 255, 220);
                    font: 600 11px 'Segoe UI';
                    padding: 2px 4px;
                }
                QPushButton#closeBtn {
                    background: transparent;
                    color: rgba(220, 230, 255, 180);
                    font: 600 14px 'Segoe UI';
                    border: none;
                    padding: 0 6px;
                }
                QPushButton#closeBtn:hover { color: rgba(255, 120, 120, 230); }
                QTextEdit#history {
                    background: rgba(16, 18, 24, 180);
                    color: rgba(230, 235, 245, 230);
                    border: 1px solid rgba(60, 70, 95, 140);
                    border-radius: 6px;
                    padding: 6px 8px;
                    font: 11px 'Consolas';
                }
                QLineEdit#input {
                    background: rgba(28, 32, 42, 220);
                    color: rgba(240, 245, 255, 240);
                    border: 1px solid rgba(80, 160, 240, 180);
                    border-radius: 6px;
                    padding: 6px 10px;
                    font: 12px 'Segoe UI';
                    selection-background-color: rgba(80, 160, 240, 160);
                }
                QLineEdit#input:focus {
                    border: 1px solid rgba(120, 190, 255, 220);
                }
            """)
            root = QWidget(self)
            root.setObjectName("chatRoot")
            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.addWidget(root)

            lay = QVBoxLayout(root)
            lay.setContentsMargins(10, 8, 10, 10)
            lay.setSpacing(6)

            # Header: title + close button.
            head = QHBoxLayout()
            head.setSpacing(4)
            title = QLabel("JARVIS · chat", root)
            title.setObjectName("title")
            close_btn = QPushButton("×", root)
            close_btn.setObjectName("closeBtn")
            close_btn.setFixedSize(22, 22)
            close_btn.clicked.connect(self.hide)
            head.addWidget(title)
            head.addStretch(1)
            head.addWidget(close_btn)
            lay.addLayout(head)

            self.history = QTextEdit(root)
            self.history.setObjectName("history")
            self.history.setReadOnly(True)
            self.history.setPlaceholderText(
                "Type a command — e.g. 'what time is it', 'open spotify', "
                "'read my screen', 'type my email'."
            )
            lay.addWidget(self.history, stretch=1)

            self.input = QLineEdit(root)
            self.input.setObjectName("input")
            self.input.setPlaceholderText("your message… (Enter to send, Esc to close)")
            self.input.returnPressed.connect(self._on_submit)
            lay.addWidget(self.input)

        def _reposition(self) -> None:
            """Place the chat near the requested screen corner, offset
            so it doesn't overlap the status orb exactly."""
            screen = QApplication.primaryScreen().availableGeometry()
            w, h = self.width(), self.height()
            m = self._anchor_margin + 36   # +36 to clear the orb
            positions = {
                "bottom-right": (screen.right() - w - self._anchor_margin,
                                 screen.bottom() - h - m),
                "bottom-left":  (screen.left()  + self._anchor_margin,
                                 screen.bottom() - h - m),
                "top-right":    (screen.right() - w - self._anchor_margin,
                                 screen.top()    + m),
                "top-left":     (screen.left()  + self._anchor_margin,
                                 screen.top()    + m),
            }
            x, y = positions.get(self._anchor_corner, positions["bottom-right"])
            self.move(int(x), int(y))

        # ────────────── events ──────────────
        def showEvent(self, ev) -> None:
            super().showEvent(ev)
            self._reposition()
            self.input.setFocus(Qt.OtherFocusReason)
            self.activateWindow()
            self.raise_()

        def _on_submit(self) -> None:
            text = self.input.text().strip()
            if not text:
                return
            self.input.clear()
            self._append_message(text, True)
            # Dispatch on a worker thread so the UI stays responsive
            # even if the LLM / vision roundtrip takes a few seconds.
            def _work():
                try:
                    reply = self._handler(text) or ""
                except Exception as exc:
                    logging.exception("chat handler failed")
                    reply = f"[error] {exc}"
                self._signals.reply_ready.emit(reply, False)
            threading.Thread(target=_work, daemon=True,
                             name="ChatWorker").start()

        def _append_message(self, text: str, is_user: bool) -> None:
            text = (text or "").strip()
            if not text:
                return
            color = "#9ad7ff" if is_user else "#f1c266"
            prefix = "you" if is_user else "jarvis"
            # QTextEdit append preserves scroll, so we jump to end.
            self.history.append(
                f'<span style="color:{color}">{prefix}</span> '
                f'<span style="color:rgba(230,235,245,230)">{self._html_escape(text)}</span>'
            )
            self.history.moveCursor(QTextCursor.End)

        @staticmethod
        def _html_escape(s: str) -> str:
            return (s.replace("&", "&amp;")
                     .replace("<", "&lt;")
                     .replace(">", "&gt;"))


    def create_chat(handler: Callable[[str], str],
                    anchor_corner: str = "bottom-right",
                    anchor_margin: int = 24,
                    width: int = 420,
                    height: int = 340) -> Optional["ChatWindow"]:
        try:
            app = QApplication.instance() or QApplication([])
        except Exception as exc:
            logging.warning("Qt not available for chat window: %s", exc)
            return None
        _ = app  # keep reference
        return ChatWindow(handler, anchor_corner=anchor_corner,
                          anchor_margin=anchor_margin,
                          width=width, height=height)

else:
    ChatWindow = None  # type: ignore[assignment]

    def create_chat(*_args, **_kwargs):  # type: ignore[misc]
        logging.warning("PySide6 not installed — chat window disabled")
        return None
