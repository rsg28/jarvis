"""
Local LLM backend via Ollama.

Mirrors the public shape of llm.LLM so the dispatcher can swap
backends without any changes:

    inst.infer(user_text) -> Optional[Dict]  (same action/command/reply schema)
    inst.remember(user_text, reply)
    inst.last_error  (short human reason when infer returns None)
    inst._cache_ttl  (seconds; TTL cache keyed by normalised transcript)

Ollama exposes an HTTP API on localhost:11434. We use:
  POST /api/chat    with format="json" for strict JSON contracts
                    (Ollama enforces the output is parseable JSON)
  POST /api/embed   for embeddings (used by knowledge.py)

No external dependencies — pure urllib. If the Ollama service isn't
running or the model isn't pulled, last_error reports exactly that so
the dispatcher's "explain why" fallback is actionable.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

# We import the system prompt from the Gemini module so both backends
# describe the same canonical skill set — any change (e.g. adding a new
# command) propagates to both without duplication.
from llm import SYSTEM_PROMPT


DEFAULT_HOST  = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:7b-instruct"


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
        # Persistent memory layer (long-term facts + recent exchanges).
        # Set from the factory; None means memory is disabled.
        self.memory = None
        # keep_alive controls how long Ollama keeps the model warm in
        # VRAM after each request. Values Ollama accepts:
        #   "5m"  -> keep loaded 5 minutes after last use
        #   "0"   -> unload immediately after each request
        #   "-1m" -> keep loaded forever
        # Default matches Ollama's own 5-min idle unload so VRAM gets
        # freed automatically between bursts without constant reloads
        # (which cost ~40s each). Set via [llm].ollama_keep_alive.
        self.keep_alive: str = "5m"

    # ─────────────────── public ───────────────────
    def infer(self, user_text: str) -> Optional[Dict]:
        """Return a parsed {"action": ..., ...} dict, or None on failure."""
        self.last_error = None

        # Normalised-key TTL cache so repeat phrasings don't re-run
        # inference. The LLM itself is deterministic enough at temp
        # 0.3 that this is a safe optimisation.
        cache_key = " ".join(user_text.lower().split())
        now = time.time()
        if self._cache_ttl > 0 and cache_key in self._cache:
            ts, cached = self._cache[cache_key]
            if now - ts < self._cache_ttl:
                logging.debug("[ollama] cache hit (%ds): %r",
                              int(now - ts), cache_key[:60])
                return cached
            del self._cache[cache_key]

        # Build chat history (OpenAI-style messages array).
        # Memory layer: inject long-term facts into the system prompt,
        # and seed recent turns from the persistent log so Jarvis has
        # continuity across restarts.
        system_text = SYSTEM_PROMPT
        if self.memory is not None:
            block = self.memory.context_block()
            if block:
                system_text = system_text + "\n\n" + block
        messages: List[Dict] = [{"role": "system", "content": system_text}]
        # Seed from persistent recent log first (older), then in-process
        # history (newer) wins for duplicates. In-process history has
        # the fresh unmemoried turns from the current session.
        seeded = set()
        if self.memory is not None:
            for u, j, _ts in self.memory.tail_recent():
                messages.append({"role": "user",      "content": u})
                messages.append({"role": "assistant", "content": j})
                seeded.add(u)
        for u, j in self.history:
            if u in seeded:
                continue
            messages.append({"role": "user",      "content": u})
            messages.append({"role": "assistant", "content": j})
        messages.append({"role": "user", "content": user_text})

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            # Ollama's "format": "json" guarantees the reply parses as
            # JSON — no need for our regex-based rescue layer.
            "format": "json",
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": self.temperature,
                "num_predict": 1024,
            },
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.host}/api/chat", data=data, method="POST",
            headers={"Content-Type": "application/json"})

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read())
        except urllib.error.URLError as exc:
            reason = str(getattr(exc, "reason", exc)).lower()
            if "refused" in reason or "actively refused" in reason:
                self.last_error = ("the Ollama service isn't running "
                                   "(start it with `ollama serve`)")
            elif "name or service" in reason or "getaddrinfo" in reason:
                self.last_error = f"I can't reach the Ollama host at {self.host}"
            elif "timed out" in reason or "timeout" in reason:
                self.last_error = ("the local model timed out — it may be "
                                   "loading for the first time, try again")
            else:
                self.last_error = f"Ollama request failed: {type(exc).__name__}"
            logging.warning("[ollama] %s", self.last_error)
            return None
        except Exception as exc:
            self.last_error = f"Ollama request failed: {type(exc).__name__}"
            logging.warning("[ollama] %s", exc)
            return None

        # Ollama error field — most commonly "model '...' not found".
        if isinstance(body, dict) and body.get("error"):
            err = str(body["error"])
            if "not found" in err.lower() or "model" in err.lower():
                self.last_error = (
                    f"the model `{self.model}` isn't pulled yet. "
                    f"Run `ollama pull {self.model}` to download it.")
            else:
                self.last_error = f"Ollama error: {err[:140]}"
            logging.warning("[ollama] %s", err)
            return None

        content = ((body.get("message") or {}).get("content") or "").strip()
        if not content:
            self.last_error = "the local model returned an empty reply"
            return None

        logging.debug("[ollama] raw reply: %s", content[:400])

        parsed = self._extract_json(content)
        if parsed and "action" in parsed:
            if self._cache_ttl > 0:
                self._cache[cache_key] = (now, parsed)
            return parsed

        # format=json should guarantee JSON but models occasionally
        # still emit prose. Promote to chat so the user hears something.
        # Guard: a reply that's just "{}" / "{...}" / "[]" is garbage
        # (Qwen does this during cold-start after an unload). Treat
        # as failure so the dispatcher falls back to regex instead of
        # speaking "{}" out loud.
        stripped = content.strip()
        if stripped in ("{}", "[]", "null", "{\"}") or len(stripped) < 3:
            self.last_error = ("the local model returned garbage — it may "
                               "still be warming up after an unload")
            return None
        promoted = {"action": "chat", "reply": content[:600]}
        if self._cache_ttl > 0:
            self._cache[cache_key] = (now, promoted)
        logging.info("[ollama] promoting non-schema reply to chat: %s",
                     content[:120])
        return promoted

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

        # Mem-plumbing turns are NOT conversations — don't persist them
        # or let them colour the in-process history. In particular, a
        # "Forgotten: <fact>" reply would otherwise leak the forgotten
        # fact back into Qwen's seeded history on the very next turn.
        if is_mem_cmd:
            # drop TTL cache so the next question re-queries with the
            # fresh fact set injected into the system prompt
            self._cache.clear()
            if any(k in low for k in ("forget", "olvida", "olvidate")):
                # extra: clear in-memory history too — any prior turn
                # may mention the forgotten fact
                self.history.clear()
            return

        self.history.append((user_text.strip(), jarvis_reply.strip()))
        # Persist non-plumbing turns so continuity survives restarts.
        if self.memory is not None:
            try:
                self.memory.append_recent(user_text, jarvis_reply)
            except Exception as exc:
                logging.debug("[ollama] recent persist failed: %s", exc)

    # ─────────────────── helpers ───────────────────
    @staticmethod
    def _extract_json(text: str) -> Optional[Dict]:
        """Strip fences and parse. format=json makes this almost always
        a one-shot `json.loads`, but we keep the regex fallback for
        the odd model that still wraps its output."""
        s = text.strip()
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
        try:
            return json.loads(s)
        except Exception:
            pass
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
        return None


# ─────────────────── VRAM management ───────────────────
def list_loaded(host: str = DEFAULT_HOST,
                timeout: float = 5.0) -> List[Dict]:
    """Return the list of currently-loaded models from /api/ps.
    Each entry has at least: name, size, size_vram, expires_at."""
    host = host.rstrip("/")
    req = urllib.request.Request(f"{host}/api/ps", method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read())
    return body.get("models") or []


def unload(model: str, host: str = DEFAULT_HOST,
           timeout: float = 10.0) -> bool:
    """Force Ollama to unload `model` from VRAM NOW.
    Done by posting an empty generate request with keep_alive=0:
    Ollama interprets that as "flush this model after responding
    (which is instantly because there's no prompt to generate)".
    Returns True if the model is gone from /api/ps afterwards."""
    host = host.rstrip("/")
    payload = {"model": model, "prompt": "", "keep_alive": 0, "stream": False}
    req = urllib.request.Request(
        f"{host}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=timeout).read()
    except Exception as exc:
        logging.warning("[ollama] unload request failed: %s", exc)
        return False
    # Verify: it may take a split second for /api/ps to reflect it.
    try:
        loaded = list_loaded(host=host, timeout=2.0)
    except Exception:
        return True  # assume success if we can't verify
    return not any(m.get("name", "").split(":")[0] == model.split(":")[0]
                   for m in loaded)


def warmup(model: str, host: str = DEFAULT_HOST,
           keep_alive: str = "5m", timeout: float = 60.0) -> bool:
    """Load `model` into VRAM proactively with a trivial request so
    the first real inference doesn't eat the 20-40 s cold-start."""
    host = host.rstrip("/")
    payload = {"model": model, "prompt": "", "keep_alive": keep_alive,
               "stream": False}
    req = urllib.request.Request(
        f"{host}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=timeout).read()
        return True
    except Exception as exc:
        logging.warning("[ollama] warmup failed: %s", exc)
        return False


# ─────────────────── embeddings (used by knowledge.py) ───────────────────
def embed(texts: List[str], *, model: str = "nomic-embed-text",
          host: str = DEFAULT_HOST, timeout: float = 30.0) -> List[List[float]]:
    """Batch-embed via Ollama's /api/embed. Returns one vector per input
    string. Raises on failure (knowledge.py already wraps embed calls
    with its own error UX)."""
    host = host.rstrip("/")
    payload = {"model": model, "input": texts}
    req = urllib.request.Request(
        f"{host}/api/embed",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read())
    if body.get("error"):
        raise RuntimeError(f"Ollama embed error: {body['error']}")
    vecs = body.get("embeddings") or []
    if len(vecs) != len(texts):
        raise RuntimeError(
            f"Ollama embed returned {len(vecs)} vectors for {len(texts)} inputs")
    return vecs


# ─────────────────── factory ───────────────────
def build_from_config(config: dict) -> Optional[OllamaLLM]:
    """Return an OllamaLLM iff [llm].backend = 'ollama'. Also stashes a
    human-readable reason in config['_llm_disabled_reason'] when it
    returns None, matching the Gemini factory's convention."""
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
    # Persistent memory (long-term facts + recent exchanges log). The
    # directory lives next to config.toml so it's easy to inspect /
    # hand-edit / git-ignore. See memory.py.
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
    return inst
