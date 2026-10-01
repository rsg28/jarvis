"""
Multi-channel notifications for Jarvis events the user shouldn't miss.

Currently used for new-manga-chapter alerts, but intentionally generic
so other watchers (news, scores, reminders) can reuse it later.

Channels, in order of persistence:

  1. toast   — Windows Action Center notification. Clickable, stays
               in the notification history until dismissed. Zero setup.
  2. browser — auto-opens the URL in the default browser. Impossible
               to ignore but noisy; opt-in.
  3. email   — SMTP send. Opt-in; requires config.

Each channel fails independently and never raises back to the caller —
a bad SMTP config shouldn't block a toast, and a missing WinRT runtime
shouldn't block an email.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
import webbrowser
from dataclasses import dataclass
from email.mime.text import MIMEText
from typing import Optional


@dataclass
class Notification:
    title: str
    body: str
    url: Optional[str] = None   # if set: toast button + browser-open target


# ────────────────────── toast ──────────────────────
# WindowsToaster is cheap to construct but holds a WinRT reference,
# so we cache it. The import is lazy so non-Windows platforms (or
# venvs without windows-toasts installed) just skip this channel.
_toaster = None
_toaster_failed = False


def _get_toaster(interactive: bool):
    """Return a cached toaster. InteractableWindowsToaster supports
    action buttons; the plain WindowsToaster is lighter and used when
    no URL is attached. Both drop their toasts into the Action Center."""
    global _toaster, _toaster_failed
    if _toaster_failed:
        return None
    if _toaster is not None and _toaster[0] is interactive:
        return _toaster[1]
    try:
        if interactive:
            from windows_toasts import InteractableWindowsToaster  # type: ignore
            inst = InteractableWindowsToaster("Jarvis")
        else:
            from windows_toasts import WindowsToaster  # type: ignore
            inst = WindowsToaster("Jarvis")
        _toaster = (interactive, inst)
        return inst
    except Exception as exc:
        logging.warning("[notify] toast backend unavailable: %s", exc)
        _toaster_failed = True
        return None


def send_toast(n: Notification) -> bool:
    """Fire a Windows Action Center toast. Clickable row + optional
    'Open chapter' button when a URL is attached."""
    toaster = _get_toaster(interactive=bool(n.url))
    if toaster is None:
        return False
    try:
        from windows_toasts import Toast, ToastButton  # type: ignore
        toast = Toast()
        toast.text_fields = [n.title, n.body]
        if n.url:
            # Clicking the toast row opens the URL (default browser).
            toast.launch_action = n.url
            toast.AddAction(ToastButton("Open chapter", n.url))
        toaster.show_toast(toast)
        return True
    except Exception as exc:
        logging.warning("[notify] toast send failed: %s", exc)
        return False


# ────────────────────── browser ──────────────────────
def send_browser(n: Notification) -> bool:
    if not n.url:
        return False
    try:
        webbrowser.open(n.url)
        return True
    except Exception as exc:
        logging.warning("[notify] browser open failed: %s", exc)
        return False


# ────────────────────── email ──────────────────────
def send_email(n: Notification, cfg: dict) -> bool:
    """SMTP send. `cfg` is config['email']:

        host      = "smtp.gmail.com"
        port      = 465               # 465=SSL, 587=STARTTLS
        username  = "you@gmail.com"
        password  = "app-password"    # Gmail: use an app password
        from      = "you@gmail.com"
        to        = "you@gmail.com"
    """
    if not cfg:
        return False
    host = (cfg.get("host") or "").strip()
    user = (cfg.get("username") or "").strip()
    pw   = (cfg.get("password") or "").strip()
    to   = (cfg.get("to") or user).strip()
    sender = (cfg.get("from") or user).strip()
    if not all([host, user, pw, to, sender]):
        logging.info("[notify] email channel enabled but config incomplete")
        return False
    port = int(cfg.get("port") or 465)

    body = n.body + (f"\n\n{n.url}" if n.url else "")
    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = n.title
    msg["From"] = sender
    msg["To"] = to

    try:
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=15) as s:
                s.login(user, pw)
                s.sendmail(sender, [to], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.ehlo(); s.starttls(context=context); s.ehlo()
                s.login(user, pw)
                s.sendmail(sender, [to], msg.as_string())
        return True
    except Exception as exc:
        logging.warning("[notify] email send failed: %s", exc)
        return False


# ────────────────────── fan-out ──────────────────────
def notify(n: Notification, config: dict) -> dict:
    """Fire every channel enabled in config['notifications'] and return
    a per-channel success dict so callers can log what actually worked.

        [notifications]
        toast   = true
        browser = false
        email   = false
    """
    nc = (config.get("notifications") or {})
    results: dict = {}
    if nc.get("toast", True):
        results["toast"] = send_toast(n)
    if nc.get("browser", False):
        results["browser"] = send_browser(n)
    if nc.get("email", False):
        results["email"] = send_email(n, config.get("email") or {})
    return results
