"""
UI bridge — a QObject that lets background threads (wake listener, voice
engine, command dispatcher) push state updates into the Qt main thread
via signals (queued connections are automatically thread-safe).
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class JarvisBridge(QObject):
    # High-level state: "idle" | "listening" | "processing" | "speaking"
    state_changed = Signal(str)

    # (kind, text) — kind is "you" (heard command) or "jarvis" (spoken response)
    transcript = Signal(str, str)

    # Emitted when the user right-clicks Close on the HUD.
    quit_requested = Signal()

    # Emitted when a command needs the UI to do something (e.g. "help" pops
    # open the command panel). Payload is a short action key.
    ui_action = Signal(str)

    def set_state(self, state: str) -> None:
        self.state_changed.emit(state)

    def push_you(self, text: str) -> None:
        self.transcript.emit("you", text)

    def push_jarvis(self, text: str) -> None:
        self.transcript.emit("jarvis", text)
