"""
Global hotkey listener for Jarvis.

Two backends, tried in order:

1. **Win32 `RegisterHotKey`** (Windows only, preferred). This is the
   same API Discord, OBS, and most game overlays use for push-to-talk.
   It's a first-class OS hotkey — it doesn't need a low-level keyboard
   hook, so it isn't affected by AV / Defender hook-blocking or by
   apps that swallow raw key events. Downside: needs a dedicated
   thread with a Win32 message loop.

2. **`keyboard` package** (cross-platform fallback). Uses low-level
   HID hooks via `SetWindowsHookEx`. Works on Linux/macOS too, but on
   Windows can silently fail to receive events if a security product
   blocks the hook.

Combo syntax matches the `keyboard` package for compatibility:
    "ctrl+alt+j"   "win+space"   "shift+f9"   "ctrl+shift+space"
"""
from __future__ import annotations

import logging
import platform
import threading
from typing import Callable, Optional, Tuple

# Callback signature is `on_trigger(cancel: threading.Event) -> None`.
# The callback should check `cancel.is_set()` between long phases and
# bail early when the user has pressed the hotkey again.
CancelCallback = Callable[[threading.Event], None]


# ─────────────────────── combo parser ───────────────────────
# Windows virtual-key codes for common non-alphanumeric keys.
_VK_MAP = {
    "space": 0x20, "backspace": 0x08, "tab": 0x09, "enter": 0x0D,
    "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "pause": 0x13,
    "capslock": 0x14, "insert": 0x2D, "delete": 0x2E, "home": 0x24,
    "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "up": 0x26,
    "down": 0x28, "left": 0x25, "right": 0x27, "printscreen": 0x2C,
    "numlock": 0x90, "scrolllock": 0x91,
}
for _i in range(1, 25):
    _VK_MAP[f"f{_i}"] = 0x6F + _i  # F1=0x70..F24=0x87

# Windows RegisterHotKey modifier bitmask.
_MOD_ALT      = 0x0001
_MOD_CONTROL  = 0x0002
_MOD_SHIFT    = 0x0004
_MOD_WIN      = 0x0008
_MOD_NOREPEAT = 0x4000  # Windows 7+: don't repeat while held

_MOD_ALIASES = {
    "ctrl": _MOD_CONTROL, "control": _MOD_CONTROL,
    "alt": _MOD_ALT,
    "shift": _MOD_SHIFT,
    "win": _MOD_WIN, "meta": _MOD_WIN, "super": _MOD_WIN, "cmd": _MOD_WIN,
}


def _parse_combo_win32(combo: str) -> Optional[Tuple[int, int]]:
    """Parse "ctrl+alt+j" into (modifiers, vk_code). Returns None if
    the combo can't be represented as a single-key + modifiers hotkey
    (which is all Win32 RegisterHotKey supports)."""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        return None

    mods = 0
    key_parts = []
    for p in parts:
        if p in _MOD_ALIASES:
            mods |= _MOD_ALIASES[p]
        else:
            key_parts.append(p)

    if len(key_parts) != 1:
        # Multiple non-modifier keys — Win32 can't do chords like "ctrl+j,k".
        return None
    key = key_parts[0]

    # Single letter A-Z or digit 0-9.
    if len(key) == 1 and key.isalpha():
        vk = ord(key.upper())
    elif len(key) == 1 and key.isdigit():
        vk = ord(key)
    elif key in _VK_MAP:
        vk = _VK_MAP[key]
    else:
        return None

    return (mods | _MOD_NOREPEAT, vk)


# ─────────────────────── Win32 backend ───────────────────────
class _Win32HotkeyThread(threading.Thread):
    """A worker thread that owns a hidden message loop and receives
    WM_HOTKEY messages. RegisterHotKey associates hotkeys with the
    calling thread's message queue, so registration must happen INSIDE
    the run() method."""

    WM_HOTKEY = 0x0312
    WM_QUIT   = 0x0012

    def __init__(self, combo: str, callback: CancelCallback,
                 interrupt_hook: Optional[Callable[[], None]] = None) -> None:
        super().__init__(name=f"Win32Hotkey-{combo}", daemon=True)
        self.combo = combo
        self.callback = callback
        # Called from the hotkey thread when a fresh press arrives while
        # an activation is still running. Use it to abort audio playback,
        # cancel a network request, etc. Must return quickly.
        self.interrupt_hook = interrupt_hook
        self._registered = threading.Event()
        self._register_ok = False
        self._register_error: Optional[str] = None
        self._thread_id: Optional[int] = None
        self._hotkey_id = 1
        # State for the interrupt-and-restart flow.
        self._state_lock = threading.Lock()
        self._current_worker: Optional[threading.Thread] = None
        self._current_cancel: Optional[threading.Event] = None

    def run(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32   = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        parsed = _parse_combo_win32(self.combo)
        if parsed is None:
            self._register_error = f"could not parse combo {self.combo!r}"
            self._registered.set()
            return
        mods, vk = parsed

        self._thread_id = kernel32.GetCurrentThreadId()

        # RegisterHotKey(hWnd=NULL means "post WM_HOTKEY to this thread")
        if not user32.RegisterHotKey(None, self._hotkey_id, mods, vk):
            err = ctypes.get_last_error() or ctypes.GetLastError()
            self._register_error = (
                f"RegisterHotKey failed (error {err}). Combo may be in "
                f"use by another app or reserved by the system."
            )
            self._registered.set()
            return

        self._register_ok = True
        logging.info("hotkey registered via Win32 RegisterHotKey: %s "
                     "(mods=0x%04x, vk=0x%02x)", self.combo, mods & 0xFF, vk)
        self._registered.set()

        # Message pump. We do NOT run the callback inline — that would
        # block the pump and Windows would spool up subsequent
        # WM_HOTKEY messages while we're busy, causing a flood of
        # activations when the callback finally returns.
        # Instead: hand each press to a worker thread through a
        # single-slot "active" flag. Presses while active are dropped.
        msg = wintypes.MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0 or ret == -1:
                break  # WM_QUIT or error
            if msg.message == self.WM_HOTKEY:
                self._fire_async()
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _fire_async(self) -> None:
        """Called from the message pump thread when WM_HOTKEY arrives.
        Semantics: a press ALWAYS wins — if an activation is currently
        running, we interrupt it (stop audio, signal cancel, brief join)
        and start a fresh one. The user gets to override Jarvis mid-
        sentence just by pressing again."""
        prev_worker: Optional[threading.Thread] = None
        with self._state_lock:
            if (self._current_worker is not None
                    and self._current_worker.is_alive()):
                prev_worker = self._current_worker
                if self._current_cancel is not None:
                    self._current_cancel.set()
                logging.info("hotkey pressed: %s (interrupting previous run)",
                             self.combo)
            else:
                logging.info("hotkey pressed: %s", self.combo)

        # Interrupt hook lives outside the lock — it may call back into
        # audio libraries that take their own locks.
        if prev_worker is not None and self.interrupt_hook is not None:
            try:
                self.interrupt_hook()
            except Exception:
                logging.exception("interrupt hook failed")

        # Give the previous worker a window to unwind cleanly. The
        # speaking-phase interrupt returns almost instantly once
        # voice.stop() halts pygame; the listening-phase needs longer
        # because r.listen() is uncancellable and holds the mic until
        # silence detection or phrase_time_limit fires.
        if prev_worker is not None:
            prev_worker.join(timeout=1.5)

        # Fresh cancel event for the new run.
        new_cancel = threading.Event()

        def _worker():
            try:
                self.callback(new_cancel)
            except Exception:
                logging.exception("hotkey callback crashed")
            finally:
                # Drain any WM_HOTKEY presses that queued during the
                # callback so we don't rapid-fire on pump resume.
                self._drain_pending_hotkeys()

        t = threading.Thread(target=_worker, daemon=True, name="HotkeyWorker")
        with self._state_lock:
            self._current_worker = t
            self._current_cancel = new_cancel
        t.start()

    def _drain_pending_hotkeys(self) -> None:
        """Remove any WM_HOTKEY messages that queued up while the
        worker was busy, so the pump doesn't re-fire immediately."""
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return
        user32 = ctypes.windll.user32
        PM_REMOVE = 0x0001
        msg = wintypes.MSG()
        drained = 0
        # PeekMessage(hWnd=NULL, filterMin=WM_HOTKEY, filterMax=WM_HOTKEY)
        while user32.PeekMessageW(ctypes.byref(msg), None,
                                   self.WM_HOTKEY, self.WM_HOTKEY, PM_REMOVE):
            drained += 1
        if drained:
            logging.info("hotkey drained %d queued press(es)", drained)

        try:
            user32.UnregisterHotKey(None, self._hotkey_id)
        except Exception:
            pass

    def wait_registered(self, timeout: float = 3.0) -> bool:
        self._registered.wait(timeout=timeout)
        return self._register_ok

    def request_stop(self) -> None:
        if self._thread_id is None:
            return
        import ctypes
        ctypes.windll.user32.PostThreadMessageW(
            self._thread_id, self.WM_QUIT, 0, 0
        )


# ─────────────────────── keyboard-package fallback ───────────────────────
class _KeyboardBackend:
    def __init__(self, combo: str, callback: Callable[[], None]) -> None:
        self.combo = combo
        self.callback = callback
        self._handle = None
        self._wait_thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        try:
            import keyboard  # noqa: F401
        except ImportError:
            logging.warning("`keyboard` not installed and no Win32 backend available")
            return False
        import keyboard
        try:
            self._handle = keyboard.add_hotkey(
                self.combo, self._on_press, suppress=False, trigger_on_release=False
            )
        except Exception as exc:
            logging.error("keyboard.add_hotkey failed: %s", exc)
            return False

        # `keyboard` needs its worker thread to stay alive; we spin one
        # in the background so the main thread can also block on wait().
        def _pump():
            try:
                keyboard.wait()
            except KeyboardInterrupt:
                pass
        self._wait_thread = threading.Thread(target=_pump, daemon=True)
        self._wait_thread.start()
        logging.info("hotkey registered via `keyboard` package: %s", self.combo)
        return True

    def _on_press(self) -> None:
        logging.info("hotkey pressed: %s", self.combo)
        try:
            self.callback()
        except Exception:
            logging.exception("hotkey callback crashed")

    def stop(self) -> None:
        try:
            import keyboard
            if self._handle is not None:
                keyboard.remove_hotkey(self._handle)
        except Exception:
            pass


# ─────────────────────── public facade ───────────────────────
class HotkeyListener:
    def __init__(self, combo: str, on_trigger: CancelCallback,
                 interrupt_hook: Optional[Callable[[], None]] = None) -> None:
        self.combo = combo
        # Callback receives a threading.Event that gets set when the
        # next press arrives, so long-running work can bail early.
        self._callback = on_trigger
        self._interrupt_hook = interrupt_hook
        self._backend = None            # "win32" or "keyboard"
        self._win32_thread: Optional[_Win32HotkeyThread] = None
        self._kb_backend: Optional[_KeyboardBackend] = None
        # Interrupt-and-restart state for the keyboard-package fallback.
        self._kb_state_lock = threading.Lock()
        self._kb_current_worker: Optional[threading.Thread] = None
        self._kb_current_cancel: Optional[threading.Event] = None

    def _kb_guarded_callback(self) -> Callable[[], None]:
        """Interrupt-and-restart wrapper for the keyboard-package
        fallback backend. Same semantics as the Win32 backend: a fresh
        press cancels the previous run and starts a new one."""
        def _fire() -> None:
            prev_worker = None
            with self._kb_state_lock:
                if (self._kb_current_worker is not None
                        and self._kb_current_worker.is_alive()):
                    prev_worker = self._kb_current_worker
                    if self._kb_current_cancel is not None:
                        self._kb_current_cancel.set()
                    logging.info("hotkey pressed (interrupting previous)")
                else:
                    logging.info("hotkey pressed")
            if prev_worker is not None and self._interrupt_hook is not None:
                try:
                    self._interrupt_hook()
                except Exception:
                    logging.exception("interrupt hook failed")
            if prev_worker is not None:
                prev_worker.join(timeout=1.5)
            new_cancel = threading.Event()

            def _worker():
                try:
                    self._callback(new_cancel)
                except Exception:
                    logging.exception("hotkey callback crashed")

            t = threading.Thread(target=_worker, daemon=True, name="HotkeyWorker-kb")
            with self._kb_state_lock:
                self._kb_current_worker = t
                self._kb_current_cancel = new_cancel
            t.start()
        return _fire

    def start(self) -> bool:
        # Prefer Win32 on Windows — it's the OS-level, AV-friendly path.
        if platform.system() == "Windows":
            t = _Win32HotkeyThread(self.combo, self._callback,
                                   interrupt_hook=self._interrupt_hook)
            t.start()
            if t.wait_registered(timeout=3.0):
                self._win32_thread = t
                self._backend = "win32"
                return True
            # Registration failed — log and fall through to `keyboard`.
            logging.warning("Win32 hotkey failed: %s. Falling back to `keyboard`.",
                            t._register_error)  # noqa: SLF001

        kb = _KeyboardBackend(self.combo, self._kb_guarded_callback())
        if kb.start():
            self._kb_backend = kb
            self._backend = "keyboard"
            return True
        return False

    def stop(self) -> None:
        if self._win32_thread is not None:
            self._win32_thread.request_stop()
            self._win32_thread = None
        if self._kb_backend is not None:
            self._kb_backend.stop()
            self._kb_backend = None

    def wait_forever(self) -> None:
        """Block the current thread until the process exits."""
        try:
            if self._backend == "win32" and self._win32_thread is not None:
                # The Win32 message pump runs on its own thread; we
                # just park the main thread on an Event that never
                # sets, but do it interruptibly so Ctrl+C works.
                while self._win32_thread.is_alive():
                    self._win32_thread.join(timeout=0.5)
            elif self._backend == "keyboard":
                # `keyboard.wait()` blocks the calling thread indefinitely.
                import keyboard
                keyboard.wait()
            else:
                # No backend — nothing to wait on.
                threading.Event().wait()
        except KeyboardInterrupt:
            pass
