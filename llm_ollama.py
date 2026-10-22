"""
Local LLM backend via Ollama.

Mirrors the public shape of llm.LLM so the dispatcher can swap
backends without any changes:

    inst.infer(user_text) -> Optional[Dict]  (same action/command/reply schema)
    inst.remember(user_text, reply)
    inst.last_error  (short human reason when infer returns None)
    inst._cache_ttl  (seconds; TTL cache keyed by normalised transcript)

Ollama HTTP API (localhost:11434):
  POST /api/chat     JSON-schema replies for routing
  POST /api/generate keep_alive / unload / warmup
  POST /api/embed    embeddings for knowledge.py
  GET  /api/ps       what's resident in VRAM
  GET  /api/version  liveness

No extra deps — urllib only.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from llm import SYSTEM_PROMPT
from matchers import first_json_object, strip_fences


DEFAULT_HOST  = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:7b-instruct"

# Force the model into the same contract the dispatcher already
# understands. `format: "json"` only guaranteed *some* JSON; a schema
# stops Qwen emitting `{}` / stray keys on cold start. If this Ollama
# build doesn't support schemas we fall back to `"json"` after one 400.
REPLY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["chat", "call_intent"],
        },
        "command": {"type": "string"},
        "reply": {"type": "string"},
        "fact_to_remember": {"type": "string"},
    },
    "required": ["action"],
}

# How many persistent recent turns to seed into the prompt. The system
# prompt is already ~6k tokens; 6 turns leave room inside num_ctx=8192.
RECENT_IN_INFER = 6

_GARBAGE = ("{}", "[]", "null", "{\"}", '{"action":}', "")


def _parse_duration(value: str) -> float:
    """Ollama keep_alive strings → seconds. 0 = unload, -1 = forever."""
    s = str(value or "5m").strip().lower()
    if s.startswith("-"):
        return -1.0
    if s in ("0", "0s", "0m", "0h"):
        return 0.0
    unit = s[-1] if s and s[-1] in "smh" else "s"
    try:
        n = float(s[:-1] if s[-1] in "smh" else s)
    except ValueError:
        return 300.0
    return n * {"s": 1.0, "m": 60.0, "h": 3600.0}[unit]


def _post_json(url: str, payload: Optional[dict], *,
               timeout: float, method: str = "POST") -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if not raw:
        return {}
    return json.loads(raw)


class OllamaLLM:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host:  str = DEFAULT_HOST,
        temperature: float = 0.3,
        timeout: float = 60.0,
        history_size: int = 5,
    ) -> None:
        self.model = model
        self.host  = host.rstrip("/")
        self.temperature = float(temperature)
        self.timeout = float(timeout)
        self.history: Deque[Tuple[str, str]] = deque(maxlen=history_size)
        self.last_error: Optional[str] = None
        self._cache: Dict[str, Tuple[float, Dict]] = {}
        self._cache_ttl: float = 600.0
        self.memory = None
        self.keep_alive: str = "5m"
        self.num_ctx: int = 8192
        self.num_predict: int = 256
        self.autostart: bool = True
        self.warmup_on_boot: bool = True
        self.keepalive_ping: bool = True
        # When False, infer() is a no-op so the model is never pulled
        # back into VRAM. Set from [llm].hold_vram / the settings page.
        self.use_vram: bool = True

        self._format: Any = REPLY_SCHEMA
        self._infer_lock = threading.Lock()
        self._pause = threading.Event()          # set = gaming / user unloaded
        self._stop = threading.Event()
        self._keeper: Optional[threading.Thread] = None
        self._last_stats: Dict[str, float] = {}

    # ─────────────────── public ───────────────────
    def infer(self, user_text: str) -> Optional[Dict]:
        """Return a parsed {"action": ..., ...} dict, or None on failure."""
        self.last_error = None
        if not self.use_vram:
            self.last_error = ("VRAM is released — I am using local "
                               "commands only until you turn the model back on")
            return None
        cache_key = " ".join(user_text.lower().split())
        now = time.time()
        if self._cache_ttl > 0 and cache_key in self._cache:
            ts, cached = self._cache[cache_key]
            if now - ts < self._cache_ttl:
                logging.debug("[ollama] cache hit (%ds): %r",
                              int(now - ts), cache_key[:60])
                return cached
            del self._cache[cache_key]

        messages = self._messages(user_text)
        loaded = self.is_loaded()
        # Cold start: weights into VRAM is 20-40s on a 7B. Give it room,
        # then retry once if Qwen still returns garbage.
        timeout = self.timeout if loaded else max(self.timeout, 120.0)
        attempts = 1 if loaded else 2

        parsed: Optional[Dict] = None
        for attempt in range(1, attempts + 1):
            parsed = self._chat(messages, timeout=timeout)
            if parsed is not None:
                break
            if attempt < attempts:
                logging.info("[ollama] retry %d/%d (%s)",
                             attempt + 1, attempts, self.last_error)
                timeout = max(timeout, 120.0)

        if parsed is None:
            return None

        auto_fact = (parsed.get("fact_to_remember") or "").strip()
        if auto_fact and self.memory is not None:
            try:
                added = self.memory.remember(auto_fact)
                if added:
                    logging.info("[memory] auto-remembered: %s",
                                 auto_fact[:120])
            except Exception as exc:
                logging.debug("[memory] auto-remember failed: %s", exc)

        # Only cache routing decisions. Free-form chat goes stale
        # (time, weather, "what did I just say") and must not stick
        # for 10 minutes.
        if self._cache_ttl > 0 and parsed.get("action") == "call_intent":
            if (parsed.get("command") or "").strip():
                self._cache[cache_key] = (now, parsed)
        return parsed

    def remember(self, user_text: str, jarvis_reply: str) -> None:
        if not user_text or not jarvis_reply:
            return
        low = user_text.lower()
        is_mem_cmd = any(k in low for k in (
            "remember", "recuerda", "memoriza",
            "forget",   "olvida",   "olvidate",
            "what do you remember", "que recuerdas",
            "recent memory", "clear recent",
        ))
        if is_mem_cmd:
            self._cache.clear()
            if any(k in low for k in ("forget", "olvida", "olvidate")):
                self.history.clear()
            return
        self.history.append((user_text.strip(), jarvis_reply.strip()))
        if self.memory is not None:
            try:
                self.memory.append_recent(user_text, jarvis_reply)
            except Exception as exc:
                logging.debug("[ollama] recent persist failed: %s", exc)

    def is_reachable(self, timeout: float = 2.0) -> bool:
        try:
            _post_json(f"{self.host}/api/version", None,
                       timeout=timeout, method="GET")
            return True
        except Exception:
            return False

    def is_loaded(self) -> bool:
        try:
            loaded = list_loaded(host=self.host, timeout=2.0)
        except Exception:
            return False
        target = self.model.split(":")[0]
        return any((m.get("name") or "").split(":")[0] == target
                   for m in loaded)

    def pause_keeper(self) -> None:
        """Stop the idle ping so a VRAM release actually sticks."""
        self._pause.set()
        logging.info("[ollama] keepalive paused")

    def resume_keeper(self) -> None:
        """Allow the idle ping again (after reclaim / game exit)."""
        self._pause.clear()
        logging.info("[ollama] keepalive resumed")

    def prefetch(self) -> bool:
        """Load weights AND the system-prompt KV cache so the first
        real turn doesn't spend 15s prompting 5k tokens."""
        if not self._ensure_server():
            return False
        if not self.is_loaded():
            if not warmup(self.model, host=self.host,
                          keep_alive=self.keep_alive, timeout=120.0):
                return False
        dummy = self._messages("ok")
        # Drop the dummy user turn's result; we only care that Ollama
        # hashed the system prefix into its prompt cache.
        return self._chat(dummy, timeout=120.0) is not None

    def start_background(self) -> None:
        if self._keeper and self._keeper.is_alive():
            return
        self._stop.clear()
        self._keeper = threading.Thread(
            target=self._keeper_loop, name="ollama-keeper", daemon=True)
        self._keeper.start()

    # ─────────────────── chat ───────────────────
    def _messages(self, user_text: str) -> List[Dict]:
        system_text = SYSTEM_PROMPT
        if self.memory is not None:
            block = self.memory.context_block()
            if block:
                system_text = system_text + "\n\n" + block
        messages: List[Dict] = [{"role": "system", "content": system_text}]
        seeded = set()
        if self.memory is not None:
            for u, j, _ts in self.memory.tail_recent(RECENT_IN_INFER):
                messages.append({"role": "user",      "content": u})
                messages.append({"role": "assistant", "content": j})
                seeded.add(u)
        for u, j in self.history:
            if u in seeded:
                continue
            messages.append({"role": "user",      "content": u})
            messages.append({"role": "assistant", "content": j})
        messages.append({"role": "user", "content": user_text})
        return messages

    def _chat(self, messages: List[Dict], *, timeout: float) -> Optional[Dict]:
        with self._infer_lock:
            return self._chat_locked(messages, timeout=timeout)

    def _chat_locked(self, messages: List[Dict], *, timeout: float) -> Optional[Dict]:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": self._format,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.num_predict,
                "num_ctx": self.num_ctx,
            },
        }
        t0 = time.perf_counter()
        try:
            body = _post_json(f"{self.host}/api/chat", payload, timeout=timeout)
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                pass
            # Schema support landed mid-2024; older Ollama returns 400.
            if exc.code == 400 and self._format != "json":
                logging.info("[ollama] schema rejected, falling back to format=json")
                self._format = "json"
                return self._chat_locked(messages, timeout=timeout)
            self.last_error = self._explain_http(exc, err_body)
            logging.warning("[ollama] %s", self.last_error)
            return None
        except urllib.error.URLError as exc:
            self.last_error = self._explain_url(exc)
            logging.warning("[ollama] %s", self.last_error)
            return None
        except Exception as exc:
            self.last_error = f"Ollama request failed: {type(exc).__name__}"
            logging.warning("[ollama] %s", exc)
            return None

        if isinstance(body, dict) and body.get("error"):
            err = str(body["error"])
            if "format" in err.lower() and self._format != "json":
                logging.info("[ollama] schema rejected (%s), using format=json",
                             err[:80])
                self._format = "json"
                return self._chat_locked(messages, timeout=timeout)
            if "not found" in err.lower() or "model" in err.lower():
                self.last_error = (
                    f"the model `{self.model}` isn't pulled yet. "
                    f"Run `ollama pull {self.model}` to download it.")
            else:
                self.last_error = f"Ollama error: {err[:140]}"
            logging.warning("[ollama] %s", err)
            return None

        self._log_stats(body, time.perf_counter() - t0)
        content = ((body.get("message") or {}).get("content") or "").strip()
        if not content:
            self.last_error = "the local model returned an empty reply"
            return None
        logging.debug("[ollama] raw reply: %s", content[:400])

        parsed = self._extract_json(content)
        if parsed and parsed.get("action") in ("chat", "call_intent"):
            return parsed

        stripped = content.strip()
        if stripped in _GARBAGE or len(stripped) < 3:
            self.last_error = ("the local model returned garbage — it may "
                               "still be warming up after an unload")
            return None
        logging.info("[ollama] promoting non-schema reply to chat: %s",
                     content[:120])
        return {"action": "chat", "reply": content[:600]}

    def _log_stats(self, body: dict, wall: float) -> None:
        def _ns(key: str) -> float:
            try:
                return float(body.get(key) or 0) / 1e9
            except Exception:
                return 0.0
        load = _ns("load_duration")
        prompt = _ns("prompt_eval_duration")
        gen = _ns("eval_duration")
        tokens = int(body.get("eval_count") or 0)
        prompt_tok = int(body.get("prompt_eval_count") or 0)
        tps = (tokens / gen) if gen > 0 else 0.0
        self._last_stats = {
            "wall": wall, "load": load, "prompt": prompt, "gen": gen,
            "tokens": float(tokens), "prompt_tok": float(prompt_tok), "tps": tps,
        }
        logging.info(
            "[ollama] %.2fs wall  load=%.2fs  prompt=%d tok/%.2fs  "
            "gen=%d tok/%.2fs (%.1f t/s)",
            wall, load, prompt_tok, prompt, tokens, gen, tps)

    def _explain_url(self, exc: urllib.error.URLError) -> str:
        reason = str(getattr(exc, "reason", exc)).lower()
        if "refused" in reason or "actively refused" in reason:
            return ("the Ollama service isn't running "
                    "(start it with `ollama serve`)")
        if "name or service" in reason or "getaddrinfo" in reason:
            return f"I can't reach the Ollama host at {self.host}"
        if "timed out" in reason or "timeout" in reason:
            return ("the local model timed out — it may be "
                    "loading for the first time, try again")
        return f"Ollama request failed: {type(exc).__name__}"

    @staticmethod
    def _explain_http(exc: urllib.error.HTTPError, body: str) -> str:
        if exc.code == 404:
            return "Ollama returned 404 — is the model pulled?"
        snippet = body[:120] if body else exc.reason
        return f"Ollama HTTP {exc.code}: {snippet}"

    @staticmethod
    def _extract_json(text: str) -> Optional[Dict]:
        s = strip_fences(text)
        try:
            return json.loads(s)
        except Exception:
            pass
        blob = first_json_object(s)
        if blob:
            try:
                return json.loads(blob)
            except Exception:
                return None
        return None

    # ─────────────────── keeper ───────────────────
    def _keeper_loop(self) -> None:
        if not self._ensure_server():
            logging.warning("[ollama] keeper: server never came up")
            return
        ka = _parse_duration(self.keep_alive)
        if self.warmup_on_boot and ka != 0 and not self._pause.is_set():
            logging.info("[ollama] prefetching %s (weights + prompt cache)…",
                         self.model)
            ok = self.prefetch()
            logging.info("[ollama] prefetch %s", "ok" if ok else "failed")

        if not self.keepalive_ping or ka <= 0:
            return
        # Ping a bit before Ollama would unload, so conversational
        # idle never pays the 20-40s reload. Gaming mode pauses this.
        interval = max(30.0, ka * 0.75)
        logging.info("[ollama] keepalive every %.0fs (keep_alive=%s)",
                     interval, self.keep_alive)
        while not self._stop.wait(interval):
            if self._pause.is_set():
                continue
            try:
                with self._infer_lock:
                    warmup(self.model, host=self.host,
                           keep_alive=self.keep_alive, timeout=15.0)
            except Exception as exc:
                logging.debug("[ollama] keepalive ping failed: %s", exc)

    def _ensure_server(self) -> bool:
        if self.is_reachable():
            return True
        if not self.autostart:
            return False
        logging.info("[ollama] not running — trying `ollama serve`")
        try:
            flags = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                flags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        except Exception as exc:
            logging.warning("[ollama] autostart failed: %s", exc)
            return False
        for _ in range(20):
            time.sleep(0.5)
            if self.is_reachable():
                logging.info("[ollama] server is up")
                return True
        return False


# ─────────────────── VRAM management ───────────────────
def list_loaded(host: str = DEFAULT_HOST,
                timeout: float = 5.0) -> List[Dict]:
    """Return currently-loaded models from /api/ps."""
    host = host.rstrip("/")
    body = _post_json(f"{host}/api/ps", None, timeout=timeout, method="GET")
    return body.get("models") or []


def unload(model: str, host: str = DEFAULT_HOST,
           timeout: float = 10.0) -> bool:
    """Force-unload `model` via generate + keep_alive=0."""
    host = host.rstrip("/")
    payload = {"model": model, "prompt": "", "keep_alive": 0, "stream": False}
    try:
        _post_json(f"{host}/api/generate", payload, timeout=timeout)
    except Exception as exc:
        logging.warning("[ollama] unload request failed: %s", exc)
        return False
    try:
        loaded = list_loaded(host=host, timeout=2.0)
    except Exception:
        return True
    return not any((m.get("name") or "").split(":")[0] == model.split(":")[0]
                   for m in loaded)


def warmup(model: str, host: str = DEFAULT_HOST,
           keep_alive: str = "5m", timeout: float = 60.0) -> bool:
    """Load `model` with an empty generate so the first real turn
    doesn't eat a 20-40s cold start."""
    host = host.rstrip("/")
    payload = {"model": model, "prompt": "", "keep_alive": keep_alive,
               "stream": False}
    try:
        _post_json(f"{host}/api/generate", payload, timeout=timeout)
        return True
    except Exception as exc:
        logging.warning("[ollama] warmup failed: %s", exc)
        return False


# ─────────────────── embeddings (used by knowledge.py) ───────────────────
def embed(texts: List[str], *, model: str = "nomic-embed-text",
          host: str = DEFAULT_HOST, timeout: float = 30.0) -> List[List[float]]:
    """Batch-embed via /api/embed. One vector per input string."""
    host = host.rstrip("/")
    payload = {"model": model, "input": texts, "keep_alive": "10m"}
    body = _post_json(f"{host}/api/embed", payload, timeout=timeout)
    if body.get("error"):
        raise RuntimeError(f"Ollama embed error: {body['error']}")
    vecs = body.get("embeddings") or []
    if len(vecs) != len(texts):
        raise RuntimeError(
            f"Ollama embed returned {len(vecs)} vectors for {len(texts)} inputs")
    return vecs


# ─────────────────── factory ───────────────────
def build_from_config(config: dict) -> Optional[OllamaLLM]:
    """Return an OllamaLLM iff [llm].backend = 'ollama'."""
    llm_cfg = config.get("llm", {}) or {}
    if not llm_cfg.get("enabled", False):
        config["_llm_disabled_reason"] = \
            "the language model is turned off in config"
        return None
    inst = OllamaLLM(
        model=llm_cfg.get("ollama_model", DEFAULT_MODEL),
        host=llm_cfg.get("ollama_host", DEFAULT_HOST),
        temperature=float(llm_cfg.get("temperature", 0.3)),
        timeout=float(llm_cfg.get("timeout_seconds", 60.0)),
        history_size=int(llm_cfg.get("history_size", 5)),
    )
    inst._cache_ttl = float(llm_cfg.get("cache_seconds", 600.0))
    inst.keep_alive = str(llm_cfg.get("ollama_keep_alive", "5m"))
    inst.num_ctx = int(llm_cfg.get("ollama_num_ctx", 8192))
    inst.num_predict = int(llm_cfg.get("ollama_num_predict", 256))
    inst.autostart = bool(llm_cfg.get("ollama_autostart", True))
    inst.warmup_on_boot = bool(llm_cfg.get("ollama_warmup_on_boot", True))
    inst.keepalive_ping = bool(llm_cfg.get("ollama_keepalive_ping", True))
    inst.use_vram = bool(llm_cfg.get("hold_vram", True))
    if not inst.use_vram:
        inst.warmup_on_boot = False
        inst.keepalive_ping = False
    try:
        from pathlib import Path as _P
        from memory import Memory as _Memory
        root = _P(config.get("_config_dir", _P(__file__).parent))
        inst.memory = _Memory(root)
        n = len(inst.memory.list_long())
        logging.info("[memory] loaded %d long-term fact%s from %s",
                     n, "s" if n != 1 else "", inst.memory.long_path)
    except Exception as exc:
        logging.warning("[memory] disabled (init failed): %s", exc)
    config["_llm_disabled_reason"] = None
    try:
        inst.start_background()
        if not inst.use_vram:
            inst.pause_keeper()
    except Exception as exc:
        logging.warning("[ollama] keeper not started: %s", exc)
    return inst
