"""
Settings page for Jarvis.

Served at /settings on the same LAN server as the phone remote.
Reads/writes config.toml without wiping comments or [[manga.series]],
and applies VRAM / voice / gaming changes live when it can.
"""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Any, Optional


PAGE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0c0d10">
<title>Jarvis · Ajustes</title>
<style>
  :root {
    --bg: #0c0d10; --card: #16181d; --line: #2a2d34; --text: #f2f0ea;
    --muted: #9a958a; --accent: #e8a54b; --danger: #e07a6a; --ok: #7dba7a;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--text);
    font-family: "Segoe UI", system-ui, sans-serif; }
  body { max-width: 720px; margin: 0 auto; padding: 20px 16px 80px; }
  header { display: flex; align-items: baseline; justify-content: space-between;
    margin-bottom: 18px; }
  header h1 { margin: 0; font-size: 15px; letter-spacing: 0.18em; }
  header a { color: var(--accent); text-decoration: none; font-size: 14px; }
  section {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 16px; padding: 16px 16px 8px; margin-bottom: 14px;
  }
  section h2 { margin: 0 0 4px; font-size: 13px; letter-spacing: 0.14em;
    color: var(--accent); }
  .hint { color: var(--muted); font-size: 13px; margin: 0 0 12px; line-height: 1.4; }
  .row { display: flex; align-items: center; justify-content: space-between;
    gap: 12px; padding: 10px 0; border-top: 1px solid var(--line); }
  .row:first-of-type { border-top: 0; }
  label.lbl { font-size: 15px; }
  label.lbl small { display: block; color: var(--muted); font-size: 12px; font-weight: 400; }
  input[type=text], input[type=number], select {
    background: #101114; color: var(--text); border: 1px solid var(--line);
    border-radius: 10px; padding: 8px 10px; font-size: 15px; min-width: 140px;
  }
  .toggle { position: relative; width: 48px; height: 28px; flex: 0 0 48px; }
  .toggle input { opacity: 0; width: 0; height: 0; }
  .toggle span {
    position: absolute; inset: 0; background: #2a2d34; border-radius: 999px;
    cursor: pointer; transition: .15s;
  }
  .toggle span:before {
    content: ""; position: absolute; height: 22px; width: 22px; left: 3px; top: 3px;
    background: #f2f0ea; border-radius: 50%; transition: .15s;
  }
  .toggle input:checked + span { background: var(--accent); }
  .toggle input:checked + span:before { transform: translateX(20px); }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; margin: 8px 0 12px; }
  button {
    background: var(--accent); color: #1a1206; border: 0; border-radius: 10px;
    padding: 10px 14px; font-weight: 700; font-size: 14px; cursor: pointer;
  }
  button.ghost { background: transparent; color: var(--text); border: 1px solid var(--line); }
  button.danger { background: var(--danger); color: #1a0c0c; }
  #status { min-height: 1.3em; color: var(--muted); font-size: 13px; margin-top: 8px; }
  #status.ok { color: var(--ok); }
  #status.err { color: var(--danger); }
  .pill { font-size: 12px; border: 1px solid var(--line); border-radius: 999px;
    padding: 4px 10px; color: var(--muted); }
  .pill.hot { border-color: var(--accent); color: var(--accent); }
  #gate {
    position: fixed; inset: 0; background: #0c0d10; z-index: 50;
    display: flex; align-items: center; justify-content: center; padding: 24px;
  }
  #gate .card { width: min(420px, 100%); }
  #gate h2 { color: var(--text); letter-spacing: 0.16em; }
  #gate p { color: var(--muted); }
  #gate input { width: 100%; text-align: center; letter-spacing: 0.3em; font-size: 22px;
    padding: 14px; background: #16181d; color: #f2f0ea; border: 1px solid #2a2d34; }
  #gate button { width: 100%; margin-top: 12px; padding: 14px; }
  .hidden { display: none !important; }
  #vram-bar { height: 8px; background: #101114; border-radius: 99px; overflow: hidden;
    margin: 8px 0 4px; }
  #vram-bar > i { display: block; height: 100%; width: 0; background: var(--accent);
    transition: width .3s; }
</style>
</head>
<body>
  <div id="gate">
    <div class="card">
      <h2>AJUSTES</h2>
      <p>Mismo pin que el chat del celular.</p>
      <input id="pin" inputmode="numeric" maxlength="8" placeholder="PIN">
      <button type="button" id="unlock">Entrar</button>
      <div id="gerr" style="color:#e07a6a;margin-top:8px;min-height:1.2em"></div>
    </div>
  </div>
  <header>
    <h1>AJUSTES</h1>
    <a href="/">← chat</a>
  </header>

  <section>
    <h2>VRAM</h2>
    <p class="hint">Qwen en la 5060 Ti ocupa unos 4.5 GB. Apágalo cuando vayas a jugar; Jarvis sigue con comandos locales (hora, volumen, abrir apps).</p>
    <div class="row">
      <div><span class="pill" id="vram-pill">…</span></div>
      <div id="vram-mb" class="pill">—</div>
    </div>
    <div id="vram-bar"><i id="vram-fill"></i></div>
    <div class="row">
      <label class="lbl">Usar VRAM
        <small>Si está apagado, el modelo se descarga y no vuelve a cargar solo</small>
      </label>
      <label class="toggle"><input type="checkbox" data-k="llm.hold_vram"><span></span></label>
    </div>
    <div class="actions">
      <button type="button" class="danger" id="btn-release">Liberar ahora</button>
      <button type="button" class="ghost" id="btn-load">Cargar ahora</button>
    </div>
    <div class="row">
      <label class="lbl">Keep-alive
        <small>Cuánto tiempo queda el modelo en VRAM después de hablarte</small>
      </label>
      <select data-k="llm.ollama_keep_alive">
        <option value="0">0 — soltar al terminar</option>
        <option value="1m">1 minuto</option>
        <option value="5m">5 minutos</option>
        <option value="-1m">siempre (hasta que lo sueltes)</option>
      </select>
    </div>
    <div class="row">
      <label class="lbl">Cargar al arrancar Jarvis</label>
      <label class="toggle"><input type="checkbox" data-k="llm.ollama_warmup_on_boot"><span></span></label>
    </div>
    <div class="row">
      <label class="lbl">Mantener caliente
        <small>Ping periódico para que no se descargue entre conversaciones</small>
      </label>
      <label class="toggle"><input type="checkbox" data-k="llm.ollama_keepalive_ping"><span></span></label>
    </div>
    <div class="row">
      <label class="lbl">Soltar VRAM al abrir un juego</label>
      <label class="toggle"><input type="checkbox" data-k="gaming.enabled"><span></span></label>
    </div>
    <div class="row">
      <label class="lbl">Recargar el modelo al cerrar el juego</label>
      <label class="toggle"><input type="checkbox" data-k="gaming.auto_reclaim_on_exit"><span></span></label>
    </div>
  </section>

  <section>
    <h2>VOZ</h2>
    <div class="row">
      <label class="lbl">Hablar en la PC</label>
      <label class="toggle"><input type="checkbox" data-k="voice.enabled"><span></span></label>
    </div>
    <div class="row">
      <label class="lbl">Decir “Yes?” al hotkey</label>
      <label class="toggle"><input type="checkbox" data-k="hotkey.ack"><span></span></label>
    </div>
    <div class="row">
      <label class="lbl">Voz</label>
      <select data-k="voice.voice_name">
        <option value="en-US-JennyNeural">Jenny (EN)</option>
        <option value="en-US-AriaNeural">Aria (EN)</option>
        <option value="es-MX-DaliaNeural">Dalia (ES MX)</option>
        <option value="es-ES-ElviraNeural">Elvira (ES)</option>
      </select>
    </div>
    <div class="row">
      <label class="lbl">Velocidad</label>
      <input type="number" data-k="voice.rate" min="140" max="260" step="10">
    </div>
    <div class="row">
      <label class="lbl">Volumen</label>
      <input type="number" data-k="voice.volume" min="0" max="1" step="0.1">
    </div>
  </section>

  <section>
    <h2>TÚ</h2>
    <div class="row">
      <label class="lbl">Nombre</label>
      <input type="text" data-k="user.name">
    </div>
    <div class="row">
      <label class="lbl">Trato
        <small>Vacío = no dice sir / boss</small>
      </label>
      <input type="text" data-k="user.address" placeholder="sir, boss…">
    </div>
  </section>

  <section>
    <h2>ASISTENTE</h2>
    <div class="row">
      <label class="lbl">LLM primero
        <small>Frases naturales van al modelo. Si VRAM está off, usa solo comandos locales</small>
      </label>
      <label class="toggle"><input type="checkbox" data-k="llm.llm_first"><span></span></label>
    </div>
    <div class="row">
      <label class="lbl">Cancelar voz si estás tecleando</label>
      <label class="toggle"><input type="checkbox" data-k="voice.cancel_on_keyboard"><span></span></label>
    </div>
    <div class="row">
      <label class="lbl">Hotkey <small>Reinicia Jarvis para aplicar un combo nuevo</small></label>
      <input type="text" data-k="hotkey.combo">
    </div>
    <div class="row">
      <label class="lbl">Orbe en pantalla</label>
      <label class="toggle"><input type="checkbox" data-k="indicator.enabled"><span></span></label>
    </div>
    <div class="row">
      <label class="lbl">Esquina del orbe</label>
      <select data-k="indicator.corner">
        <option value="bottom-right">abajo derecha</option>
        <option value="bottom-left">abajo izquierda</option>
        <option value="top-right">arriba derecha</option>
        <option value="top-left">arriba izquierda</option>
      </select>
    </div>
  </section>

  <section>
    <h2>ALERTAS</h2>
    <div class="row">
      <label class="lbl">Toast de Windows</label>
      <label class="toggle"><input type="checkbox" data-k="notifications.toast"><span></span></label>
    </div>
    <div class="row">
      <label class="lbl">Avisar capítulos de manga</label>
      <label class="toggle"><input type="checkbox" data-k="manga.enabled"><span></span></label>
    </div>
  </section>

  <section>
    <h2>CELULAR</h2>
    <p class="hint" id="phone-info">—</p>
  </section>

  <div class="actions">
    <button type="button" id="btn-save">Guardar</button>
    <button type="button" class="ghost" id="btn-reload">Recargar</button>
  </div>
  <div id="status"></div>

<script>
const TOKEN_KEY = "jarvis_phone_token";
const gate = document.getElementById("gate");
function token() { return localStorage.getItem(TOKEN_KEY) || ""; }
function setStatus(msg, cls) {
  const el = document.getElementById("status");
  el.className = cls || "";
  el.textContent = msg || "";
}
async function api(path, opts={}) {
  const res = await fetch(path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      "Authorization": "Bearer " + token(),
      ...(opts.headers || {})
    }
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY);
    gate.classList.remove("hidden");
    throw new Error("pin");
  }
  if (!res.ok) throw new Error(data.error || "error");
  return data;
}
function valOf(el) {
  if (el.type === "checkbox") return el.checked;
  if (el.type === "number") return Number(el.value);
  return el.value;
}
function setEl(el, v) {
  if (el.type === "checkbox") el.checked = !!v;
  else el.value = v ?? "";
}
function collect() {
  const out = {};
  document.querySelectorAll("[data-k]").forEach((el) => { out[el.dataset.k] = valOf(el); });
  return out;
}
function paint(data) {
  const v = data.values || {};
  document.querySelectorAll("[data-k]").forEach((el) => {
    if (el.dataset.k in v) setEl(el, v[el.dataset.k]);
  });
  const vr = data.vram || {};
  const pill = document.getElementById("vram-pill");
  pill.textContent = vr.loaded && vr.loaded.length
    ? vr.loaded.join(", ")
    : "GPU libre";
  pill.className = "pill" + (vr.loaded && vr.loaded.length ? " hot" : "");
  document.getElementById("vram-mb").textContent = (vr.mb || 0) + " MB";
  document.getElementById("vram-fill").style.width = Math.min(100, ((vr.mb || 0) / 8192) * 100) + "%";
  const ph = data.phone || {};
  document.getElementById("phone-info").textContent =
    (ph.url || "") + (ph.pin ? "   PIN " + ph.pin : "");
}
async function load() {
  const data = await api("/api/settings");
  paint(data);
  setStatus("");
}
async function save() {
  setStatus("guardando…");
  try {
    const data = await api("/api/settings", { method: "POST", body: JSON.stringify({values: collect()}) });
    paint(data);
    setStatus(data.note || "guardado", "ok");
  } catch (e) { setStatus(e.message, "err"); }
}
async function vram(action) {
  setStatus(action === "release" ? "soltando VRAM…" : "cargando modelo…");
  try {
    const data = await api("/api/vram", { method: "POST", body: JSON.stringify({action}) });
    paint(data);
    setStatus(data.note || "listo", "ok");
  } catch (e) { setStatus(e.message, "err"); }
}
document.getElementById("unlock").onclick = async () => {
  document.getElementById("gerr").textContent = "";
  const pin = document.getElementById("pin").value.trim();
  const res = await fetch("/api/auth", {
    method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({pin})
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { document.getElementById("gerr").textContent = data.error || "pin incorrecto"; return; }
  localStorage.setItem(TOKEN_KEY, data.token);
  gate.classList.add("hidden");
  load().catch((e) => setStatus(e.message, "err"));
};
document.getElementById("pin").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("unlock").click();
});
document.getElementById("btn-save").onclick = save;
document.getElementById("btn-reload").onclick = () => load().catch((e) => setStatus(e.message, "err"));
document.getElementById("btn-release").onclick = () => vram("release");
document.getElementById("btn-load").onclick = () => vram("reclaim");
if (token()) { gate.classList.add("hidden"); load().catch(() => gate.classList.remove("hidden")); }
</script>
</body>
</html>
"""


KEYS = (
    ("llm", "hold_vram", True),
    ("llm", "ollama_keep_alive", "5m"),
    ("llm", "ollama_warmup_on_boot", True),
    ("llm", "ollama_keepalive_ping", True),
    ("llm", "llm_first", True),
    ("gaming", "enabled", True),
    ("gaming", "auto_reclaim_on_exit", True),
    ("voice", "enabled", True),
    ("voice", "voice_name", "en-US-JennyNeural"),
    ("voice", "rate", 180),
    ("voice", "volume", 0.9),
    ("voice", "cancel_on_keyboard", True),
    ("hotkey", "ack", True),
    ("hotkey", "combo", "ctrl+shift+space"),
    ("user", "name", ""),
    ("user", "address", ""),
    ("indicator", "enabled", True),
    ("indicator", "corner", "bottom-right"),
    ("notifications", "toast", True),
    ("manga", "enabled", True),
)


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int) and not isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _coerce(sample: Any, raw: Any) -> Any:
    if isinstance(sample, bool):
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(sample, int) and not isinstance(sample, bool):
        return int(raw)
    if isinstance(sample, float):
        return float(raw)
    return str(raw)


def patch_toml(path: Path, changes: list[tuple[str, str, Any]]) -> None:
    """Upsert keys under [section] without touching comments or
    array-of-tables like [[manga.series]]."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if not text.endswith("\n"):
        text += "\n"
    for section, key, value in changes:
        text = _upsert(text, section, key, value)
    path.write_text(text, encoding="utf-8")


def _upsert(text: str, section: str, key: str, value: Any) -> str:
    header = re.compile(rf"^\[{re.escape(section)}\]\s*$", re.M)
    m = header.search(text)
    formatted = f"{key} = {_fmt(value)}"
    if not m:
        return text.rstrip() + f"\n\n[{section}]\n{formatted}\n"
    # Slice until the next [section] that is NOT an array table of this section.
    start = m.end()
    nxt = re.search(r"^\[", text[start:], re.M)
    end = start + nxt.start() if nxt else len(text)
    body = text[start:end]
    key_re = re.compile(rf"^({re.escape(key)}\s*=\s*)[^\n]*$", re.M)
    km = key_re.search(body)
    if km:
        # Keep a trailing inline comment if there is one after the value.
        rest = body[km.end():km.end()]  # noqa: F841  (placeholder)
        line = body[km.start():km.end()]
        comment = ""
        hash_at = line.find(" #")
        if hash_at != -1:
            comment = line[hash_at:]
        new_body = key_re.sub(formatted + comment, body, count=1)
    else:
        insert = formatted + "\n"
        new_body = insert + body if body.startswith("\n") else "\n" + insert + body
    return text[:start] + new_body + text[end:]


class SettingsController:
    def __init__(self, dispatcher, voice, config: dict) -> None:
        self.dispatcher = dispatcher
        self.voice = voice
        self.config = config

    def snapshot(self) -> dict:
        values = {}
        for sec, key, default in KEYS:
            values[f"{sec}.{key}"] = (self.config.get(sec) or {}).get(key, default)
        return {
            "values": values,
            "vram": self._vram_info(),
            "phone": self._phone_info(),
        }

    def apply(self, incoming: dict) -> dict:
        changes: list[tuple[str, str, Any]] = []
        for sec, key, default in KEYS:
            dotted = f"{sec}.{key}"
            if dotted not in incoming:
                continue
            try:
                val = _coerce(default, incoming[dotted])
            except Exception:
                continue
            (self.config.setdefault(sec, {}))[key] = val
            changes.append((sec, key, val))
        cfg_dir = self.config.get("_config_dir")
        if cfg_dir and changes:
            patch_toml(Path(cfg_dir) / "config.toml", changes)
        note = self._live_apply(changes)
        snap = self.snapshot()
        snap["note"] = note
        return snap

    def vram_action(self, action: str) -> dict:
        d = self.dispatcher
        action = (action or "").strip().lower()
        if action == "release":
            self._set_hold(False)
            r = d._vram_release()
            note = r.speak or "VRAM liberada"
        elif action in ("reclaim", "load"):
            self._set_hold(True)
            r = d._vram_reclaim()
            note = r.speak or "cargando modelo"
        else:
            raise ValueError("action must be release or reclaim")
        snap = self.snapshot()
        snap["note"] = note
        return snap

    def _set_hold(self, on: bool) -> None:
        llm_cfg = self.config.setdefault("llm", {})
        llm_cfg["hold_vram"] = on
        llm = getattr(self.dispatcher, "_llm", None)
        if llm is not None and hasattr(llm, "use_vram"):
            llm.use_vram = on
        cfg_dir = self.config.get("_config_dir")
        if cfg_dir:
            extra = [("llm", "hold_vram", on)]
            if not on:
                llm_cfg["ollama_warmup_on_boot"] = False
                llm_cfg["ollama_keepalive_ping"] = False
                extra += [
                    ("llm", "ollama_warmup_on_boot", False),
                    ("llm", "ollama_keepalive_ping", False),
                ]
            patch_toml(Path(cfg_dir) / "config.toml", extra)

    def _live_apply(self, changes: list[tuple[str, str, Any]]) -> str:
        notes = []
        llm = getattr(self.dispatcher, "_llm", None)
        voice = self.voice
        for sec, key, val in changes:
            if sec == "voice" and voice is not None:
                if key == "enabled":
                    voice.enabled = bool(val)
                elif key == "rate":
                    voice.rate = int(val)
                elif key == "volume":
                    voice.volume = float(val)
                elif key == "voice_name":
                    voice.set_voice(str(val))
                elif key == "address" or key == "name":
                    pass
            if sec == "user" and voice is not None and key == "address":
                voice.address = str(val or "")
            if sec == "llm" and llm is not None:
                if key == "hold_vram":
                    prev = bool(getattr(llm, "use_vram", True))
                    llm.use_vram = bool(val)
                    if prev and not val:
                        try:
                            llm.pause_keeper()
                            self.dispatcher._vram_release()
                            notes.append("modelo descargado")
                        except Exception as exc:
                            logging.warning("[settings] vram off failed: %s", exc)
                    elif val and not prev:
                        try:
                            llm.resume_keeper()
                            threading.Thread(
                                target=llm.prefetch, daemon=True,
                                name="vram-on").start()
                            notes.append("cargando modelo")
                        except Exception as exc:
                            logging.warning("[settings] vram on failed: %s", exc)
                elif key == "ollama_keep_alive":
                    llm.keep_alive = str(val)
                elif key == "ollama_warmup_on_boot":
                    llm.warmup_on_boot = bool(val)
                elif key == "ollama_keepalive_ping":
                    llm.keepalive_ping = bool(val)
                    if not val:
                        llm.pause_keeper()
                    elif getattr(llm, "use_vram", True):
                        llm.resume_keeper()
            if sec == "gaming" and key == "enabled":
                try:
                    w = self.dispatcher._get_game_watcher()
                    if val:
                        w.start()
                    else:
                        w.stop()
                except Exception as exc:
                    logging.warning("[settings] gaming toggle: %s", exc)
        if any(s == "hotkey" and k == "combo" for s, k, _ in changes):
            notes.append("reinicia Jarvis para el nuevo hotkey")
        if any(s == "indicator" for s, k, _ in changes):
            notes.append("el orbe se actualiza al reiniciar")
        return "; ".join(notes) if notes else "guardado"

    def _vram_info(self) -> dict:
        loaded, mb = [], 0
        try:
            from llm_ollama import list_loaded
            host = (self.config.get("llm") or {}).get(
                "ollama_host", "http://localhost:11434")
            models = list_loaded(host=host)
            loaded = [m.get("name", "?") for m in models]
            mb = sum(int(m.get("size_vram", 0)) for m in models) // (1024 * 1024)
        except Exception:
            pass
        llm = getattr(self.dispatcher, "_llm", None)
        use = True
        if llm is not None:
            use = bool(getattr(llm, "use_vram", True))
        else:
            use = bool((self.config.get("llm") or {}).get("hold_vram", True))
        return {"loaded": loaded, "mb": mb, "hold": use}

    @staticmethod
    def _phone_info() -> dict:
        try:
            import phone as phone_mod
            return {
                "url": phone_mod.STATE.url,
                "pin": phone_mod.STATE.pin,
                "port": phone_mod.STATE.port,
            }
        except Exception:
            return {}
