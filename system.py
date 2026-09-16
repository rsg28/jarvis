"""
System — battery, cpu, ram, disk, network, volume, media keys, screenshot.

Everything Windows-first with cross-platform fallbacks where cheap.
"""
from __future__ import annotations

import ctypes
import logging
import platform
import socket
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional


# ─────────────────────── system info ───────────────────────

def battery() -> Optional[str]:
    try:
        import psutil
        b = psutil.sensors_battery()
        if not b:
            return "No battery detected (looks like a desktop)."
        state = "plugged in" if b.power_plugged else "on battery"
        eta = ""
        if b.secsleft not in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) and b.secsleft > 0:
            mins = b.secsleft // 60
            eta = f", about {mins // 60}h {mins % 60}m left"
        return f"Battery is at {int(b.percent)} percent, {state}{eta}."
    except Exception as exc:
        logging.warning("battery: %s", exc)
        return None


def cpu() -> str:
    import psutil
    pct = psutil.cpu_percent(interval=0.4)
    freq = psutil.cpu_freq()
    ghz = f", running at {freq.current/1000:.1f} gigahertz" if freq and freq.current else ""
    return f"CPU is at {pct:.0f} percent load{ghz}."


def ram() -> str:
    import psutil
    m = psutil.virtual_memory()
    used_gb = (m.total - m.available) / (1024 ** 3)
    total_gb = m.total / (1024 ** 3)
    return f"RAM: {used_gb:.1f} of {total_gb:.1f} gigabytes used ({m.percent:.0f} percent)."


def disk() -> str:
    import psutil
    d = psutil.disk_usage("C:\\" if platform.system() == "Windows" else "/")
    used_gb = d.used / (1024 ** 3)
    total_gb = d.total / (1024 ** 3)
    return f"Disk: {used_gb:.0f} of {total_gb:.0f} gigabytes used ({d.percent:.0f} percent full)."


def ip_address() -> str:
    try:
        # Local IP via a UDP socket trick (doesn't actually send anything)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("8.8.8.8", 80))
        local = s.getsockname()[0]
        s.close()
        return f"Local IP is {local}."
    except Exception:
        return "Not connected to a network."


def wifi() -> str:
    if platform.system() != "Windows":
        return "Wi-Fi info is only supported on Windows for now."
    try:
        out = subprocess.check_output(
            ["netsh", "wlan", "show", "interfaces"],
            stderr=subprocess.DEVNULL,
            timeout=4,
        ).decode("utf-8", errors="ignore")
    except Exception as exc:
        return f"Could not query Wi-Fi ({exc})."

    ssid = signal = state = None
    for line in out.splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith("state") and ":" in s:
            state = s.split(":", 1)[1].strip()
        elif low.startswith("ssid") and not low.startswith("bssid") and ":" in s:
            ssid = s.split(":", 1)[1].strip()
        elif low.startswith("signal") and ":" in s:
            signal = s.split(":", 1)[1].strip()

    if not ssid:
        return "Wi-Fi is disconnected or unavailable."
    return f"Wi-Fi: connected to {ssid}, signal {signal or '?'}, state {state or '?'}."


# ─────────────────────── volume ───────────────────────

# Windows virtual-key codes for media/volume
_VK_VOLUME_MUTE       = 0xAD
_VK_VOLUME_DOWN       = 0xAE
_VK_VOLUME_UP         = 0xAF
_VK_MEDIA_NEXT_TRACK  = 0xB0
_VK_MEDIA_PREV_TRACK  = 0xB1
_VK_MEDIA_PLAY_PAUSE  = 0xB3
_KEYEVENTF_KEYUP      = 0x0002


def _tap(vk: int) -> None:
    """Simulate a key tap via keybd_event (Windows only)."""
    if platform.system() != "Windows":
        raise RuntimeError("media/volume keys are Windows-only for now")
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)


def volume_up(steps: int = 5) -> str:
    for _ in range(max(1, steps)):
        _tap(_VK_VOLUME_UP)
    return f"Volume up by {steps} steps."


def volume_down(steps: int = 5) -> str:
    for _ in range(max(1, steps)):
        _tap(_VK_VOLUME_DOWN)
    return f"Volume down by {steps} steps."


def volume_mute() -> str:
    _tap(_VK_VOLUME_MUTE)
    return "Toggled mute."


def _endpoint_volume():
    """Return pycaw's EndpointVolume for the default speakers."""
    from pycaw.pycaw import AudioUtilities
    return AudioUtilities.GetSpeakers().EndpointVolume


def volume_set(percent: int) -> str:
    """Set master volume to a specific percentage (0-100)."""
    if platform.system() != "Windows":
        return "Volume set is Windows-only for now."
    percent = max(0, min(100, int(percent)))
    try:
        _endpoint_volume().SetMasterVolumeLevelScalar(percent / 100.0, None)
        return f"Volume set to {percent} percent."
    except Exception as exc:
        logging.warning("volume_set failed: %s", exc)
        return "Could not set volume."


def volume_get() -> str:
    if platform.system() != "Windows":
        return "Volume read is Windows-only for now."
    try:
        ev = _endpoint_volume()
        pct = int(round(ev.GetMasterVolumeLevelScalar() * 100))
        muted = bool(ev.GetMute())
        return f"Volume is at {pct} percent" + (" (muted)." if muted else ".")
    except Exception as exc:
        logging.warning("volume_get failed: %s", exc)
        return "Could not read volume."


# ─────────────────────── media control ───────────────────────

def media_play_pause() -> str:
    _tap(_VK_MEDIA_PLAY_PAUSE)
    return "Toggled playback."


def media_next() -> str:
    _tap(_VK_MEDIA_NEXT_TRACK)
    return "Next track."


def media_previous() -> str:
    _tap(_VK_MEDIA_PREV_TRACK)
    return "Previous track."


# ─────────────────────── screenshot ───────────────────────

def _desktop_dir() -> Path:
    """Return the real Desktop path, respecting OneDrive redirection."""
    if platform.system() == "Windows":
        # OneDrive-redirected Desktop takes priority on modern Windows setups.
        import os
        candidates = [
            os.environ.get("OneDrive"),
            os.environ.get("OneDriveConsumer"),
            os.environ.get("USERPROFILE"),
        ]
        for base in candidates:
            if not base:
                continue
            d = Path(base) / "Desktop"
            if d.exists():
                return d
    return Path.home() / "Desktop"


def screenshot(directory: Optional[Path] = None) -> str:
    try:
        from PIL import ImageGrab
    except ImportError:
        return "Pillow is not installed. Run: pip install Pillow"

    if directory is None:
        directory = _desktop_dir()
    directory.mkdir(parents=True, exist_ok=True)
    fname = directory / f"jarvis-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    img = ImageGrab.grab(all_screens=True)
    img.save(fname, "PNG")
    return f"Saved screenshot to {fname}."
