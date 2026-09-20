"""
LLM fallback for Jarvis.

When the regex-based intent table doesn't recognise a request, the
dispatcher hands the raw transcript to Gemini with a system prompt
describing the available skills. Gemini returns JSON with one of:

    {"action": "call_intent", "command": "<canonical string>"}
    {"action": "chat",        "reply":   "<conversational answer>"}

`call_intent` results are re-dispatched through the same regex table,
so LLM-driven commands share the exact same execution path as typed
ones. `chat` responses are just spoken back.

No new dependency: uses `requests` (already required by the news /
soccer modules) and the free Gemini REST API. Get a key at
https://aistudio.google.com/app/apikey and put it in `config.toml`
under `[llm].api_key`, or set the `GEMINI_API_KEY` env var.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple


SYSTEM_PROMPT = """You are the language layer for Jarvis, a personal desktop assistant.

Your job is to convert a user's free-form request into ONE of two responses:

(1) A canonical command string that Jarvis's built-in skills understand,
    wrapped as {"action": "call_intent", "command": "<string>"}.

(2) A short spoken reply for anything that is not a command,
    wrapped as {"action": "chat", "reply": "<one to three sentences>"}.

Always respond with a single valid JSON object, no code fences, no prose.

============ Available canonical commands ============
Prefer these EXACT phrasings when you emit `call_intent`:

  open <anything>                Apps, files, folders, URLs — resolved by name.
                                 e.g. "open spotify", "open my resume", "open github.com",
                                       "open the downloads folder", "open C:\\path\\to\\file.pdf"
  read <file>                    Read a text file by name and speak an excerpt.
                                 e.g. "read the todo list", "read config.toml"
  find <name>                    List matching apps / files without opening.
  search <query>                 Google search
  play <query>                   search Spotify
  pause | play | next | previous media control keys (whatever is playing)
  what time is it | what day is it
  weather in <location>          e.g. "weather in Vancouver"
  news [<topic>]                 world | tech | sports | business | science | health | entertainment
  scores [<league or team>]      la liga | premier | champions | mls | serie a | bundesliga | ligue 1 | peru | libertadores | europa
  battery | cpu | ram | disk | ip | wifi
  volume up [<n>] | volume down [<n>] | mute | set volume <0-100> | volume
  screenshot
  set a timer for <duration>     e.g. "set a timer for 25 minutes"
  pomodoro
  remind me in <duration> to <task>
  timers                         list active timers
  cancel timers
  joke | trivia
  speak english | speak spanish | speak french | speak british
  stop listening                 end conversation, back to wake mode
  quit                           shut Jarvis down

============ Rules ============
- If the request clearly maps to a canonical command, emit `call_intent` with
  the closest matching phrasing. Small paraphrases are fine ("pon spotify" ->
  "open spotify", "sube el volumen" -> "volume up", "qué hora es" ->
  "what time is it").
- If the request is a chat / question / small talk / calculation / translation
  / recommendation / general knowledge, emit `chat` with a concise answer
  (1-3 sentences, spoken aloud, so keep it human).
- Never invent commands that aren't in the list above.
- Keep chat replies short. This is a voice interface — no bullet lists, no
  markdown, no code blocks in the reply.
- The user may address you in English, Spanish, or French. Detect the
  language and reply in the same one for `chat`. For `call_intent`,
  always emit the canonical English command from the list.
- If truly ambiguous, prefer `chat` and ask a one-line clarifying question.
"""


class LLM:
    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.6-flash",
        temperature: float = 0.3,
        timeout: float = 6.0,
        history_size: int = 5,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.temperature = float(temperature)
        self.timeout = float(timeout)
        self.history: Deque[Tuple[str, str]] = deque(maxlen=history_size)

    # ────────────── public ──────────────
    def infer(self, user_text: str) -> Optional[Dict]:
        """Return a parsed {"action": ..., ...} dict, or None on failure."""
        try:
            import requests
        except ImportError:
            logging.warning("requests not installed; LLM fallback disabled")
            return None

        url = (
            "https://generativelanguage.googleapis.com/v1beta/"
            f"models/{self.model}:generateContent?key={self.api_key}"
        )

        # Build the conversation: prior history + this user turn.
        contents: List[Dict] = []
        for u, j in self.history:
            contents.append({"role": "user",  "parts": [{"text": u}]})
            contents.append({"role": "model", "parts": [{"text": j}]})
        contents.append({"role": "user", "parts": [{"text": user_text}]})

        payload = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "generationConfig": {
                "temperature": self.temperature,
                "responseMimeType": "application/json",
                "maxOutputTokens": 512,
            },
        }

        # 1 retry on transient errors (503 overload, 429 rate-limit) with
        # a short back-off. Anything else is fatal for this turn.
        import time as _time
        resp = None
        for attempt in range(2):
            try:
                resp = requests.post(url, json=payload, timeout=self.timeout)
            except Exception as exc:
                logging.warning("LLM request failed: %s", exc)
                return None
            if resp.status_code == 200:
                break
            if resp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                _time.sleep(0.6)
                continue
            logging.warning("LLM HTTP %s: %s", resp.status_code, resp.text[:200])
            return None
        if resp is None or resp.status_code != 200:
            return None

        try:
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as exc:
            logging.warning("LLM parse failed: %s", exc)
            return None

        parsed = self._extract_json(text)
        if not parsed or "action" not in parsed:
            logging.debug("LLM returned no usable JSON: %s", text[:200])
            return None

        return parsed

    def remember(self, user_text: str, jarvis_reply: str) -> None:
        """Store an exchange in the rolling context window."""
        if not user_text or not jarvis_reply:
            return
        self.history.append((user_text.strip(), jarvis_reply.strip()))

    # ────────────── helpers ──────────────
    @staticmethod
    def _extract_json(text: str) -> Optional[Dict]:
        """Try hard to pull a JSON object out of the model's output.
        Gemini with responseMimeType=application/json usually just returns
        clean JSON, but we still strip fences and pick the first {...}
        block just in case."""
        if not text:
            return None
        stripped = text.strip()
        # Strip ```json ... ``` fences if present.
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
        try:
            return json.loads(stripped)
        except Exception:
            pass
        # Last resort: find the first balanced { ... } block.
        m = re.search(r"\{.*\}", stripped, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None


def build_from_config(config: dict) -> Optional["LLM"]:
    """Return an LLM instance if enabled in config, else None."""
    llm_cfg = config.get("llm", {}) or {}
    if not llm_cfg.get("enabled", False):
        return None
    api_key = llm_cfg.get("api_key") or os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        logging.info("LLM enabled but no api_key / GEMINI_API_KEY set")
        return None
    return LLM(
        api_key=api_key,
        model=llm_cfg.get("model", "gemini-3.6-flash"),
        temperature=float(llm_cfg.get("temperature", 0.3)),
        timeout=float(llm_cfg.get("timeout_seconds", 6.0)),
        history_size=int(llm_cfg.get("history_size", 5)),
    )
