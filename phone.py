"""
Phone remote for Jarvis.

A small HTTP server on the LAN. The phone (same Wi-Fi) opens the page,
types or dictates a command, and the desktop Jarvis runs it — same
dispatcher, same memory, same PC-side actions (Spotify, files, volume).

No extra dependencies: stdlib HTTP server + one HTML page.

Auth is a 6-digit PIN. Generated once into phone_pin.txt next to
config.toml (gitignored) unless [phone].pin is set.
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse


@dataclass
class PhoneState:
    running: bool = False
    url: str = ""
    pin: str = ""
    port: int = 0
    urls: list = field(default_factory=list)


STATE = PhoneState()

_CALLBACK: dict = {"fn": None}


def _lan_ips() -> list[str]:
    found: list[str] = []

    def _add(ip: str) -> None:
        if ip and ip not in found and not ip.startswith("127."):
            found.append(ip)

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.4)
        s.connect(("8.8.8.8", 80))
        _add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            _add(info[4][0])
    except Exception:
        pass
    return found or ["127.0.0.1"]


def _load_pin(config: dict, config_dir: Path) -> str:
    configured = str((config.get("phone") or {}).get("pin") or "").strip()
    if configured:
        return configured
    pin_file = config_dir / "phone_pin.txt"
    if pin_file.exists():
        existing = pin_file.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    pin = f"{secrets.randbelow(900000) + 100000}"
    try:
        pin_file.write_text(pin + "\n", encoding="utf-8")
    except Exception as exc:
        logging.warning("[phone] could not save pin file: %s", exc)
    return pin


PAGE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#0c0d10">
<title>Jarvis</title>
<style>
  :root {
    --bg: #0c0d10;
    --card: #16181d;
    --line: #2a2d34;
    --text: #f2f0ea;
    --muted: #9a958a;
    --accent: #e8a54b;
    --user: #243044;
    --jarvis: #1c1a16;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; background: var(--bg); color: var(--text);
    font-family: "Segoe UI", system-ui, sans-serif; }
  body { display: flex; flex-direction: column; min-height: 100dvh; }
  header {
    padding: 14px 16px 10px;
    display: flex; align-items: baseline; justify-content: space-between;
    border-bottom: 1px solid var(--line);
  }
  header h1 { margin: 0; font-size: 15px; letter-spacing: 0.18em; font-weight: 650; }
  header p { margin: 0; color: var(--muted); font-size: 12px; }
  #log {
    flex: 1; overflow-y: auto; padding: 16px;
    display: flex; flex-direction: column; gap: 10px;
  }
  .bubble {
    max-width: 88%; padding: 10px 12px; border-radius: 14px;
    line-height: 1.4; font-size: 16px; white-space: pre-wrap; word-break: break-word;
  }
  .me { align-self: flex-end; background: var(--user); border-bottom-right-radius: 4px; }
  .jv { align-self: flex-start; background: var(--jarvis); border: 1px solid var(--line);
        border-bottom-left-radius: 4px; }
  .jv .who { color: var(--accent); font-size: 11px; letter-spacing: 0.12em; margin-bottom: 4px; }
  form.composer {
    display: flex; gap: 8px; padding: 10px 12px calc(12px + env(safe-area-inset-bottom));
    border-top: 1px solid var(--line); background: #101114;
  }
  input[type=text] {
    flex: 1; background: var(--card); color: var(--text); border: 1px solid var(--line);
    border-radius: 12px; padding: 12px 14px; font-size: 16px; outline: none;
  }
  input[type=text]:focus { border-color: var(--accent); }
  button.send {
    background: var(--accent); color: #1a1206; border: 0; border-radius: 12px;
    padding: 0 16px; font-weight: 700; font-size: 15px;
  }
  .chips { display: flex; gap: 8px; overflow-x: auto; padding: 8px 12px 0; }
  .chips button {
    flex: 0 0 auto; background: transparent; color: var(--muted);
    border: 1px solid var(--line); border-radius: 999px; padding: 6px 10px; font-size: 13px;
  }
  #gate {
    position: fixed; inset: 0; background: var(--bg);
    display: flex; align-items: center; justify-content: center; padding: 24px;
  }
  #gate .card { width: min(420px, 100%); }
  #gate h2 { margin: 0 0 8px; letter-spacing: 0.16em; font-size: 14px; }
  #gate p { color: var(--muted); margin: 0 0 16px; line-height: 1.45; }
  #gate input {
    width: 100%; background: var(--card); color: var(--text);
    border: 1px solid var(--line); border-radius: 12px; padding: 14px;
    font-size: 22px; letter-spacing: 0.3em; text-align: center;
  }
  #gate button {
    width: 100%; margin-top: 12px; background: var(--accent); color: #1a1206;
    border: 0; border-radius: 12px; padding: 14px; font-weight: 700; font-size: 16px;
  }
  #err { color: #e07a6a; min-height: 1.2em; font-size: 14px; margin-top: 8px; }
  .row { display: flex; align-items: center; gap: 8px; padding: 0 16px 8px; color: var(--muted); font-size: 13px; }
  .hidden { display: none !important; }
</style>
</head>
<body>
  <div id="gate">
    <div class="card">
      <h2>JARVIS</h2>
      <p>Misma red wifi que la PC. El pin lo dice Jarvis si le pides “link del celular”.</p>
      <input id="pin" inputmode="numeric" autocomplete="one-time-code" maxlength="8" placeholder="PIN">
      <button id="unlock" type="button">Entrar</button>
      <div id="err"></div>
    </div>
  </div>
  <header>
    <h1>JARVIS</h1>
    <p id="status">en la PC</p>
  </header>
  <div id="log"></div>
  <div class="chips">
    <button type="button" data-q="qué hora es">hora</button>
    <button type="button" data-q="qué recuerdas de mí">memoria</button>
    <button type="button" data-q="noticias">noticias</button>
    <button type="button" data-q="vram status">vram</button>
  </div>
  <label class="row"><input type="checkbox" id="pc"> también hablar en la PC</label>
  <form class="composer" id="form">
    <input id="text" type="text" placeholder="Escribe o usa el micrófono del teclado" autocomplete="off" enterkeyhint="send">
    <button class="send" type="submit">Enviar</button>
  </form>
<script>
const TOKEN_KEY = "jarvis_phone_token";
const gate = document.getElementById("gate");
const err = document.getElementById("err");
const log = document.getElementById("log");
const form = document.getElementById("form");
const text = document.getElementById("text");
const pc = document.getElementById("pc");

function token() { return localStorage.getItem(TOKEN_KEY) || ""; }

function add(role, body) {
  const el = document.createElement("div");
  el.className = "bubble " + (role === "me" ? "me" : "jv");
  if (role !== "me") {
    const who = document.createElement("div");
    who.className = "who";
    who.textContent = "JARVIS";
    el.appendChild(who);
  }
  const span = document.createElement("div");
  span.textContent = body;
  el.appendChild(span);
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

function speak(s) {
  if (!window.speechSynthesis || !s) return;
  const u = new SpeechSynthesisUtterance(s);
  u.lang = "es-MX";
  speechSynthesis.cancel();
  speechSynthesis.speak(u);
}

async function unlock() {
  err.textContent = "";
  const pin = document.getElementById("pin").value.trim();
  const res = await fetch("/api/auth", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({pin})
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { err.textContent = data.error || "Pin incorrecto"; return; }
  localStorage.setItem(TOKEN_KEY, data.token);
  gate.classList.add("hidden");
  text.focus();
}

async function send(q) {
  q = (q || "").trim();
  if (!q) return;
  text.value = "";
  add("me", q);
  const pending = add("jv", "…");
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token()
      },
      body: JSON.stringify({text: q, speak_on_pc: pc.checked})
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) {
      localStorage.removeItem(TOKEN_KEY);
      gate.classList.remove("hidden");
      pending.remove();
      return;
    }
    const reply = data.reply || data.error || "(sin respuesta)";
    pending.querySelector("div:last-child").textContent = reply;
    speak(data.speak || reply);
  } catch (e) {
    pending.querySelector("div:last-child").textContent = "No llegué a la PC. ¿Sigue Jarvis abierto y en esta wifi?";
  }
}

document.getElementById("unlock").onclick = unlock;
document.getElementById("pin").addEventListener("keydown", (e) => {
  if (e.key === "Enter") unlock();
});
form.addEventListener("submit", (e) => { e.preventDefault(); send(text.value); });
document.querySelectorAll(".chips button").forEach((b) => {
  b.onclick = () => send(b.dataset.q);
});
if (token()) gate.classList.add("hidden");
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    pin: str = ""
    sessions: set = set()
    fails: dict = {}
    state_lock = threading.Lock()

    def log_message(self, fmt: str, *args) -> None:
        logging.info("[phone] " + fmt, *args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, raw, "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 32_000:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _authed(self) -> bool:
        header = self.headers.get("Authorization") or ""
        token = header.removeprefix("Bearer ").strip()
        if not token:
            return False
        with self.state_lock:
            return token in self.sessions

    def _locked_out(self) -> bool:
        ip = self.client_address[0]
        rec = self.fails.get(ip)
        if not rec:
            return False
        count, until = rec
        if time.time() < until and count >= 8:
            return True
        return False

    def _note_fail(self) -> None:
        ip = self.client_address[0]
        count, until = self.fails.get(ip, (0, 0.0))
        if time.time() > until:
            count = 0
        count += 1
        lock_for = 60 if count >= 8 else 0
        self.fails[ip] = (count, time.time() + lock_for)

    def _note_ok(self) -> None:
        self.fails.pop(self.client_address[0], None)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/health":
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/auth":
            if self._locked_out():
                self._json(429, {"error": "demasiados intentos, espera un minuto"})
                return
            data = self._read_json()
            given = str(data.get("pin") or "")
            if not hmac.compare_digest(given, self.pin):
                self._note_fail()
                self._json(401, {"error": "pin incorrecto"})
                return
            self._note_ok()
            token = secrets.token_urlsafe(24)
            with self.state_lock:
                self.sessions.add(token)
            self._json(200, {"token": token})
            return

        if path == "/api/chat":
            if not self._authed():
                self._json(401, {"error": "pin requerido"})
                return
            data = self._read_json()
            text = str(data.get("text") or "").strip()
            if not text:
                self._json(400, {"error": "vacío"})
                return
            speak_on_pc = bool(data.get("speak_on_pc"))
            try:
                fn = _CALLBACK.get("fn")
                reply = fn(text, speak_on_pc) if fn else ""
            except Exception as exc:
                logging.exception("[phone] dispatch failed")
                self._json(500, {"error": type(exc).__name__})
                return
            reply = (reply or "(sin respuesta)").strip()
            # The phone speaks the short line. print_out-only diagnostics
            # stay on screen; spoken text is the part before a blank line
            # when both exist, which is how the dispatcher joins them.
            spoken = reply.split("\n", 1)[0][:500]
            self._json(200, {"reply": reply, "speak": spoken})
            return

        self._json(404, {"error": "not found"})


def start(config: dict, on_command: Callable[[str, bool], str]) -> PhoneState:
    """Bind the phone server. Returns immediately; serving happens
    on a daemon thread. Safe to call once per process."""
    if STATE.running:
        return STATE
    phone_cfg = config.get("phone") or {}
    if not bool(phone_cfg.get("enabled", True)):
        logging.info("[phone] disabled in config")
        return STATE

    config_dir = Path(config.get("_config_dir") or ".")
    pin = _load_pin(config, config_dir)
    port = int(phone_cfg.get("port", 8765))

    _CALLBACK["fn"] = on_command
    _Handler.pin = pin
    _Handler.sessions = set()
    _Handler.fails = {}

    server = None
    bound = port
    last_exc: Optional[Exception] = None
    for candidate in range(port, port + 5):
        try:
            server = ThreadingHTTPServer(("0.0.0.0", candidate), _Handler)
            bound = candidate
            break
        except OSError as exc:
            last_exc = exc
            server = None
    if server is None:
        logging.warning("[phone] could not bind port %s: %s", port, last_exc)
        return STATE

    thread = threading.Thread(target=server.serve_forever, name="jarvis-phone",
                              daemon=True)
    thread.start()

    ips = _lan_ips()
    urls = [f"http://{ip}:{bound}" for ip in ips]
    STATE.running = True
    STATE.pin = pin
    STATE.port = bound
    STATE.urls = urls
    STATE.url = urls[0]
    logging.info("[phone] listening %s  pin=%s", ", ".join(urls), pin)

    link_file = config_dir / "phone_link.txt"
    try:
        link_file.write_text(
            "Abre esto en el celular (misma wifi):\n"
            + "\n".join(urls)
            + f"\n\nPIN: {pin}\n",
            encoding="utf-8")
    except Exception as exc:
        logging.debug("[phone] link file skipped: %s", exc)
    return STATE
