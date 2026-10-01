"""
Spotify integration for Jarvis.

Two layers:

1. Web API via Client Credentials Flow
   - No OAuth user auth required (no browser round-trip, no refresh tokens).
   - Lets us resolve "i write sins not tragedies" -> exact track URI.
   - Needs a free Spotify Developer app (client_id + client_secret),
     set in [spotify] config. 5-minute setup at
     https://developer.spotify.com/dashboard

2. Desktop app control via `spotify:track:<id>` URI + media keys
   - os.startfile("spotify:track:XYZ") opens the Spotify desktop app
     directly on that track's page (no browser involved).
   - A short wait + a MEDIA_PLAY_PAUSE keystroke starts playback.
   - Works without Premium: free users just hear ads in between.

If credentials aren't configured we fall back to opening the web
search page (what Jarvis did before).
"""
from __future__ import annotations

import base64
import logging
import os
import platform
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Optional, Tuple


# ─────────────────── Web API: resolve a track ───────────────────
class SpotifyAPI:
    TOKEN_URL  = "https://accounts.spotify.com/api/token"
    SEARCH_URL = "https://api.spotify.com/v1/search"

    def __init__(self, client_id: str, client_secret: str,
                 timeout: float = 6.0) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = float(timeout)
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._lock = threading.Lock()

    def _token_valid(self) -> bool:
        return bool(self._token) and time.monotonic() < self._token_expires_at - 30

    def _refresh_token(self) -> bool:
        creds = f"{self.client_id}:{self.client_secret}".encode()
        header = base64.b64encode(creds).decode()
        data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        req = urllib.request.Request(
            self.TOKEN_URL, data=data,
            headers={
                "Authorization": f"Basic {header}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                import json
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logging.warning("[spotify] token request failed: %s", exc)
            return False
        tok = payload.get("access_token")
        if not tok:
            logging.warning("[spotify] token response had no access_token: %s", payload)
            return False
        self._token = tok
        self._token_expires_at = time.monotonic() + int(payload.get("expires_in", 3600))
        logging.info("[spotify] got client-credentials token (expires in %ss)",
                     payload.get("expires_in"))
        return True

    def _ensure_token(self) -> bool:
        with self._lock:
            if self._token_valid():
                return True
            return self._refresh_token()

    def search_track(self, query: str) -> Optional[dict]:
        """Return the top matching track as a small dict, or None."""
        if not self._ensure_token():
            return None
        qs = urllib.parse.urlencode({"q": query, "type": "track", "limit": 1})
        req = urllib.request.Request(
            f"{self.SEARCH_URL}?{qs}",
            headers={"Authorization": f"Bearer {self._token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                import json
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logging.warning("[spotify] search failed: %s", exc)
            return None
        tracks = (data.get("tracks") or {}).get("items") or []
        if not tracks:
            return None
        t = tracks[0]
        artists = ", ".join(a.get("name", "") for a in t.get("artists", []))
        return {
            "uri":     t.get("uri"),
            "id":      t.get("id"),
            "name":    t.get("name"),
            "artists": artists,
            "web_url": f"https://open.spotify.com/track/{t.get('id')}",
        }


# ─────────────────── Desktop control ───────────────────
def _launch_uri(uri: str) -> bool:
    """Open a `spotify:` URI with the system handler so the Spotify
    desktop app comes to the foreground on the right page."""
    try:
        if platform.system() == "Windows":
            os.startfile(uri)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", uri])
        else:
            subprocess.Popen(["xdg-open", uri])
        return True
    except Exception as exc:
        logging.warning("[spotify] could not launch URI %s: %s", uri, exc)
        return False


def _send_media_play() -> None:
    """Press the OS media play/pause key (works in any OS mixer-aware
    player). On Windows we use keybd_event / VK_MEDIA_PLAY_PAUSE
    (0xB3) via ctypes — same mechanism we use for the voice `play`
    command."""
    try:
        if platform.system() == "Windows":
            import ctypes
            VK_MEDIA_PLAY_PAUSE = 0xB3
            KEYEVENTF_KEYUP = 0x0002
            ctypes.windll.user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, 0, 0)
            ctypes.windll.user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, KEYEVENTF_KEYUP, 0)
        else:
            import keyboard  # cross-platform fallback
            keyboard.send("play/pause media")
    except Exception as exc:
        logging.warning("[spotify] media key send failed: %s", exc)


def play_track(uri: str, settle_seconds: float = 1.6,
               auto_play: bool = True) -> bool:
    """Open `uri` in the Spotify desktop app and (optionally) press
    play. The settle delay gives the app time to focus the track page
    before we send the media key — otherwise the keystroke can land
    on whatever app had focus a moment ago."""
    if not _launch_uri(uri):
        return False
    if not auto_play:
        return True

    # Spotify desktop takes a beat to focus the new track; fire the
    # play key on a background thread so we don't block the caller.
    def _after_settle():
        time.sleep(settle_seconds)
        _send_media_play()
    threading.Thread(target=_after_settle, daemon=True,
                     name="SpotifyPlay").start()
    return True


# ─────────────────── config helpers ───────────────────
def api_from_config(config: dict) -> Optional[SpotifyAPI]:
    sp = config.get("spotify", {}) or {}
    cid = (sp.get("client_id") or os.environ.get("SPOTIFY_CLIENT_ID", "")).strip()
    csec = (sp.get("client_secret") or os.environ.get("SPOTIFY_CLIENT_SECRET", "")).strip()
    if not cid or not csec:
        return None
    return SpotifyAPI(client_id=cid, client_secret=csec,
                      timeout=float(sp.get("timeout_seconds", 6.0)))
