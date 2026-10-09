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
  write <path> content: <text>   Create or overwrite a file. Relative paths
                                 land in ~/Desktop/jarvis-workspace by default.
                                 e.g. user: "crea un archivo notas.txt con 'hola'"
                                      -> "write notas.txt content: hola"
                                      user: "guarda esto en C:\\tmp\\x.py: print(1)"
                                      -> "write C:\\tmp\\x.py content: print(1)"
  append to <path> content: <text>
                                 Append (keeps existing content, adds newline).
                                 e.g. "add a line to todo.md saying buy milk"
                                      -> "append to todo.md content: buy milk"
  delete file <path>             Delete ONE file (not a folder). Explicit path required.
                                 e.g. "borra el archivo notas.txt"
                                      -> "delete file notas.txt"
  make folder <path>             Create a directory (parents included).
                                 e.g. "crea una carpeta llamada clients"
                                      -> "make folder clients"
  remove folder <path>           Recursively remove a directory. Use sparingly.
  list <path>                    List directory contents.
                                 e.g. "qué hay en mi workspace" -> "list ."
  scaffold <stack> <name>        Create a new project. Stack is one of:
                                 python / node / static / rust. Writes a
                                 sensible starting layout and runs git init.
                                 e.g. user: "crea un proyecto python llamado tango"
                                      -> "scaffold python tango"
                                      user: "make a static site called landing"
                                      -> "scaffold static landing"
                                      user: "nuevo proyecto node api-manga"
                                      -> "scaffold node api-manga"
  click [at <x> <y>]             Left-click (at current pos or coords).
  right click [at <x> <y>]       Right-click.
  double click [at <x> <y>]      Double-click.
  move to <x> <y>                Move the cursor.
  scroll up [<n>] / scroll down [<n>]
                                 Mouse wheel. n defaults to 3.
  drag from <x1> <y1> to <x2> <y2>
                                 Click-and-drag.
  mouse position                 Speak current coords (useful to grab coords
                                 before scripting a click).
  watch screen [every <N> seconds|minutes]
                                 Start a background pixel-diff watcher. Alerts
                                 you via toast + voice when the screen changes
                                 significantly. Default interval 60s.
                                 e.g. user: "avísame cuando algo cambie en pantalla"
                                      -> "watch screen"
                                      user: "mira la pantalla cada 2 minutos"
                                      -> "watch screen every 2 minutes"
  stop watching                  Stop the screen watch daemon.
  screen activity                Report how long it's been watching, how many
                                 changes caught, and when the last was.
  release vram                   Force-unload the local LLM from VRAM so the
                                 GPU is free for games or heavy GPU work.
                                 The next voice turn will pay a 20-40 s
                                 reload cost. Aliases: "release gpu",
                                 "unload model", "voy a jugar".
  reclaim vram                   Reload the model proactively so the next
                                 voice turn is instant. Aliases: "wake up",
                                 "warm up model", "carga el modelo".
  vram status                    Report which models are resident and how
                                 many megabytes of VRAM they take.
  gaming mode on                 Arm the auto-watcher: poll processes every
                                 10 s and auto-release VRAM the moment a known
                                 game launches. Reloads the model when it
                                 exits (configurable). On by default on boot.
  gaming mode off                Stop the auto-watcher. VRAM stays loaded.
  gaming mode status             Report whether it's armed and the current
                                 detected game (if any).
  remember <fact>                Add a durable fact to long-term memory.
                                 Jarvis sees these on every future turn.
                                 e.g. "remember that my cat is named <X>",
                                      "recuerda que mi examen es el 15 de mayo"
  forget <query>                 Remove any remembered fact that matches.
                                 "forget everything" wipes the long-term store.
                                 e.g. "forget my cat", "olvida la fecha del examen"
  what do you remember [about <topic>]
                                 List long-term memories, optionally filtered.
                                 e.g. "qué recuerdas sobre mi?", "list memories"
  recent memory / history        Show the last 10 exchanges with timestamps.
  clear recent                   Wipe the recent-conversation log (keeps the
                                 long-term facts).
  run <cmd> [in <path>]          Execute a shell command. Only allow-listed
                                 tools (git, python, pip, uv, npm, npx, node,
                                 cargo, dotnet, pytest, mypy, ruff, gh, etc.)
                                 are permitted. Destructive patterns are
                                 blocked. Default cwd is the workspace root;
                                 add "in <path>" to override.
                                 e.g. user: "corre pytest en el proyecto tango"
                                      -> "run pytest in tango"
                                      user: "haz git status"
                                      -> "run git status"
                                      user: "install flask"
                                      -> "run pip install flask"
  find <name>                    List matching apps / files without opening.
  search <query>                 Google search
  play <song>                    play a specific song on Spotify (desktop app).
                                 Also matches "pon <song>", "reproduce <song>",
                                 "pon <song> en la app", "play <song> on Spotify".
  pause | play | next | previous media control keys (whatever is playing)
  what time is it | what day is it
  weather in <location>          e.g. "weather in Vancouver"
  news [<topic>]                 world | tech | sports | business | science | health | entertainment
  scores [<league or team>]      la liga | premier | champions | mls | serie a | bundesliga | ligue 1 | peru | libertadores | europa
  battery | cpu | ram | disk | ip | wifi
  volume up [<n>] | volume down [<n>] | mute | set volume <0-100> | volume
  type <text>                    type <text> into the focused window / active tab.
                                 Use this when the user says "escribe X aquí",
                                 "write X in the current tab", "pon X en la
                                 barra", "type X here". The cursor is wherever
                                 the user left it, so just send the text.
                                 Snippet form: "type my email" uses config.
  type my email | type my name | type my phone | type my address | type my github
  press <key>                    e.g. "press enter", "press tab", "press ctrl+a"
  screenshot                     save a PNG of the screen to Desktop
  read my screen | what's on my screen | describe my screen
                                 → send screen to Gemini vision, speak reply
  what does this say | translate this | explain this error
                                 → same, with the follow-up as prompt
  set a timer for <duration>     e.g. "set a timer for 25 minutes"
  pomodoro
  remind me in <duration> to <task>
  timers                         list active timers
  cancel timers
  joke | trivia
  learn <path>                   ingest a document (.pdf/.docx/.xlsx/.txt/.md/
                                 .csv) into the knowledge base. From then on,
                                 questions about its content are answered
                                 with the doc as grounding context.
                                 e.g. "learn my protocol.pdf", "study the
                                       patient worksheet", "aprende este pdf",
                                       "remember this file ..."
  what documents do you know     list ingested docs
                                 e.g. "list my documents", "qué documentos
                                       tienes", "knowledge base"
  forget <doc>                   remove a doc from the knowledge base.
                                 "forget everything" / "forget all" wipes it.
  diagnose <url>                 fetch a URL, test its referenced hosts, and
                                 explain why it isn't loading. Covers DNS,
                                 SSL, timeout, 4xx/5xx, and for comix.to
                                 chapter pages also detects a dead image CDN.
                                 e.g. "why isn't this page loading <url>",
                                      "averigua el error de <url>",
                                      "what's wrong with <url>"
  check for new chapters         scan the configured manga watchlist for new drops
  check for new chapters of <series>
                                 scope the check to one series. "episode" works too.
                                 e.g. "check if there is a new episode of X",
                                      "any update on X", "did X update",
                                      "is there a new chapter of X"
  latest chapter of <series>     one-off lookup for any manga (watched or not)
                                 e.g. "what's the latest chapter of chainsaw man",
                                      "cual es el ultimo capitulo de one piece"
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

============ Spanish/English synonyms (common pitfalls) ============
These wording pairs trip up intent routing. Treat them as equivalent:

  lee / leeme             -> read
  escribe / tipea         -> type   (only for typing into focused window)
  crea un archivo         -> write  (NOT type — write makes a new file)
  guarda esto en <path>   -> write
  agrega / añade          -> append
  borra el archivo        -> delete file   (physical file on disk)
  elimina el archivo      -> delete file
  olvida / olvídate de    -> forget         (removes from KB, NOT disk)
  crea una carpeta        -> make folder
  lista / qué hay en      -> list
  abre / ábreme           -> open
  pon / reproduce         -> play
  busca / googlea         -> search
  sube el volumen         -> volume up
  baja el volumen         -> volume down

Critical disambiguation:
- "borra el archivo X" means `delete file X` (physical file). The
  `forget` intent is ONLY for removing docs from the knowledge base
  (triggered by "olvida X", "forget X", "quítalo del KB", etc.).
- "crea un archivo X con contenido Y" means `write X content: Y`.
  Do NOT emit `type` for new-file creation — `type` is only for
  injecting keystrokes into whatever window has focus right now.

Few-shot examples (follow these EXACTLY):

  user: "crea un archivo notas.txt que diga buenos dias"
  -> {"action":"call_intent","command":"write notas.txt content: buenos dias"}

  user: "guarda hola mundo en un archivo llamado test.py"
  -> {"action":"call_intent","command":"write test.py content: hola mundo"}

  user: "borra el archivo notas.txt"
  -> {"action":"call_intent","command":"delete file notas.txt"}

  user: "elimina viejo.log"
  -> {"action":"call_intent","command":"delete file viejo.log"}

  user: "olvida el protocolo de anafilaxia"
  -> {"action":"call_intent","command":"forget protocolo de anafilaxia"}

  user: "escribe hola en la tab que estoy viendo"
  -> {"action":"call_intent","command":"type hola"}

  user: "escribe en el buscador de google: hola como estas"
  -> {"action":"call_intent","command":"type hola como estas"}

  user: "pon hola mundo en la barra de busqueda"
  -> {"action":"call_intent","command":"type hola mundo"}

  user: "escribe esto en el input: foo bar baz"
  -> {"action":"call_intent","command":"type foo bar baz"}

  CRITICAL for `type`: the command text must be EXACTLY what the user
  wants typed, with ZERO prefix like "google search" or "in the search
  bar". The `type` intent sends raw keystrokes to the focused window;
  the user already positioned their cursor where they want the text.

  user: "lee notas.txt"
  -> {"action":"call_intent","command":"read notas.txt"}

  user: "crea un proyecto python llamado tango"
  -> {"action":"call_intent","command":"scaffold python tango"}

  user: "nuevo proyecto node llamado api-manga"
  -> {"action":"call_intent","command":"scaffold node api-manga"}

  user: "haz git status"
  -> {"action":"call_intent","command":"run git status"}

  user: "corre los tests del proyecto tango"
  -> {"action":"call_intent","command":"run pytest in tango"}

  user: "instala flask"
  -> {"action":"call_intent","command":"run pip install flask"}

  user: "commit todo con el mensaje arregla el bug del login"
  -> {"action":"call_intent","command":"run git commit -am \"arregla el bug del login\""}

  user: "sube los cambios"
  -> {"action":"call_intent","command":"run git push"}

  user: "crea una nueva rama llamada feature-login"
  -> {"action":"call_intent","command":"run git checkout -b feature-login"}

  user: "abre un PR en github con el titulo listo para review"
  -> {"action":"call_intent","command":"run gh pr create --title \"listo para review\" --fill"}

  user: "muestrame el ultimo commit"
  -> {"action":"call_intent","command":"run git log --oneline -1"}

  user: "donde esta el mouse"
  -> {"action":"call_intent","command":"mouse position"}

  user: "haz click derecho en 100 200"
  -> {"action":"call_intent","command":"right click at 100 200"}

  user: "mueve el cursor a 800 600"
  -> {"action":"call_intent","command":"move to 800 600"}

  user: "haz scroll hacia arriba"
  -> {"action":"call_intent","command":"scroll up"}

  user: "arrastra de 100 100 a 500 500"
  -> {"action":"call_intent","command":"drag from 100 100 to 500 500"}

  user: "avisame si algo cambia en la pantalla"
  -> {"action":"call_intent","command":"watch screen"}

  user: "mira la pantalla cada 2 minutos"
  -> {"action":"call_intent","command":"watch screen every 2 minutes"}

  user: "deja de vigilar"
  -> {"action":"call_intent","command":"stop watching"}

  user: "voy a jugar un rato, libera la vram"
  -> {"action":"call_intent","command":"release vram"}

  user: "ya termine de jugar, carga el modelo"
  -> {"action":"call_intent","command":"reclaim vram"}

  user: "que modelos tienes cargados?"
  -> {"action":"call_intent","command":"vram status"}

  user: "activa el modo gaming automatico"
  -> {"action":"call_intent","command":"gaming mode on"}

  user: "apaga el modo gaming"
  -> {"action":"call_intent","command":"gaming mode off"}

  user: "esta activado el modo gaming?"
  -> {"action":"call_intent","command":"gaming mode status"}

  user: "recuerda que vivo en <ciudad>"
  -> {"action":"call_intent","command":"remember I live in <ciudad>"}

  user: "recuerda que mi hermano se llama <nombre>"
  -> {"action":"call_intent","command":"remember my brother is named <nombre>"}

  user: "olvida lo del trabajo"
  -> {"action":"call_intent","command":"forget work"}

  user: "que sabes de mi"
  -> {"action":"call_intent","command":"what do you remember about me"}

  user: "muestrame la conversacion reciente"
  -> {"action":"call_intent","command":"recent memory"}

Memory distinction (CRITICAL):

  * TELL = user is giving Jarvis a new fact to save
    -> emit `remember <fact>` (call_intent)

  * ASK = user is asking about a fact that might be in memory
    -> if the memory block below the prompt contains it, answer in
       `chat` using THAT fact (do NOT re-save). If the memory block
       does NOT contain the answer, say honestly in `chat` that you
       don't remember — DO NOT invent a value.

  Example of TELL:
  user: "recuerda que my favorite color is <X>"
  -> {"action":"call_intent","command":"remember my favorite color is <X>"}

  Example of ASK — when memory HAS the fact:
    memory contains: "- my favorite color is blue"
    user: "what's my favorite color?"
    -> {"action":"chat","reply":"Your favorite color is blue."}

  Example of ASK — when memory is EMPTY on the topic:
    memory contains: (no entry about color)
    user: "what's my favorite color?"
    -> {"action":"chat","reply":"I don't remember — you haven't told me."}

  user: "cual es el estado del watch?"
  -> {"action":"call_intent","command":"screen activity"}

  user: "cuantos cambios has visto?"
  -> {"action":"call_intent","command":"screen activity"}

Capability-question few-shots (these MUST be chat, not call_intent):

  user: "puedes hacer click en el boton de login?"
  -> {"action":"chat","reply":"No, todavía no puedo hacer clic por descripción. Necesito coordenadas — di 'mouse position' para obtenerlas."}

  user: "can you create a react app?"
  -> {"action":"chat","reply":"I can scaffold python, node, static, or rust projects — not a React-specific template yet. Want a plain node project?"}

  user: "puedes modificar tu propio codigo?"
  -> {"action":"chat","reply":"No, no puedo modificar mi propio código fuente. Pero sí puedo crear o editar otros archivos."}

  user: "sabes jugar ajedrez?"
  -> {"action":"chat","reply":"No, no juego ajedrez — no es una de mis habilidades."}

============ Honesty about capabilities ============
When the user asks whether you CAN do something (e.g. "¿puedes crear
proyectos?", "can you edit files?", "¿puedes modificar tu propio
código?"), answer truthfully based on the command list above.

- If the capability IS in the list, say yes and name the command.
- If it ISN'T, say "No, todavía no — solo puedo <nearest related
  thing>" (in the user's language). Do NOT invent abilities to be
  polite. Specifically, you currently CANNOT: click on things by
  description alone (you need coordinates — use "mouse position"
  to grab them), describe WHAT changed on screen while watching
  (the watcher is pixel-diff only, it detects change but not meaning),
  modify your own source code, or upload files to the internet.
- You CAN: open things, read text files, create/append/delete files
  and folders, scaffold full projects (python/node/static/rust),
  run allow-listed shell commands (git, python, pip, npm, cargo,
  pytest, gh, etc.), learn documents (RAG), type into the focused
  window, press keys, click/scroll/drag the mouse by coordinates,
  take screenshots, describe what's on screen on demand (one-shot
  vision), watch the screen in the background and alert on changes
  (pixel-diff, no description), control media/volume, check manga,
  set timers/reminders, give weather/news/scores.
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
        # Short human-readable reason for the last infer() -> None result.
        # The dispatcher reads this so it can tell the user *why* the
        # LLM didn't help, instead of a generic "I didn't catch that".
        self.last_error: Optional[str] = None
        # In-process TTL cache keyed by normalised transcript. When the
        # user says the same thing twice ("revisa si hay nuevo cap"),
        # we re-use Gemini's previous routing decision instead of
        # burning another request. Default 10 min, configurable via
        # [llm].cache_seconds. Set to 0 to disable.
        self._cache: Dict[str, Tuple[float, Dict]] = {}
        self._cache_ttl: float = 600.0

    # ────────────── public ──────────────
    def infer(self, user_text: str) -> Optional[Dict]:
        """Return a parsed {"action": ..., ...} dict, or None on failure.
        On failure, self.last_error holds a short human-readable reason.

        Transparent TTL cache: identical transcripts (case/space-normalised)
        within self._cache_ttl seconds reuse the previous decision, so
        rapid repeat phrasings don't burn RPM. Only successful parses
        are cached — errors always re-try next time."""
        self.last_error = None

        # Cache lookup (normalised: lowercase, collapsed whitespace)
        import time as _t
        cache_key = " ".join(user_text.lower().split())
        now = _t.time()
        if self._cache_ttl > 0 and cache_key in self._cache:
            ts, cached_parsed = self._cache[cache_key]
            if now - ts < self._cache_ttl:
                logging.debug("[llm] cache hit (%ds old): %r",
                              int(now - ts), cache_key[:60])
                return cached_parsed
            # expired — drop it
            del self._cache[cache_key]

        try:
            import requests
        except ImportError:
            logging.warning("requests not installed; LLM fallback disabled")
            self.last_error = "the requests library isn't installed"
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
                "maxOutputTokens": 1024,
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
                kind = type(exc).__name__.lower()
                if "timeout" in kind:
                    self.last_error = "the language model timed out"
                elif "connection" in kind or "dns" in kind:
                    self.last_error = "I can't reach the language model (no network)"
                else:
                    self.last_error = f"the language model request failed ({type(exc).__name__})"
                return None
            if resp.status_code == 200:
                break
            if resp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                _time.sleep(0.6)
                continue
            logging.warning("LLM HTTP %s: %s", resp.status_code, resp.text[:200])
            if resp.status_code in (401, 403):
                self.last_error = "the Gemini API key was rejected"
            elif resp.status_code == 429:
                self.last_error = "the Gemini API is rate-limiting us"
            elif resp.status_code in (500, 502, 503, 504):
                self.last_error = "Gemini is overloaded right now"
            else:
                self.last_error = f"Gemini returned HTTP {resp.status_code}"
            return None
        if resp is None or resp.status_code != 200:
            self.last_error = self.last_error or "the language model didn't reply"
            return None

        try:
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as exc:
            logging.warning("LLM parse failed: %s", exc)
            self.last_error = "the language model reply couldn't be parsed"
            return None
        # Debug trace so misfires are inspectable in jarvis.log later.
        logging.debug("[llm] raw reply: %s", text[:400])

        parsed = self._extract_json(text)
        if parsed and "action" in parsed:
            if self._cache_ttl > 0:
                self._cache[cache_key] = (now, parsed)
            return parsed

        # ── Graceful degradation: Gemini sometimes forgets the JSON
        # contract and replies in plain prose. Rather than dropping
        # the turn and leaving the user with silence, promote the raw
        # text to a chat reply. This is what the user expects from a
        # "talk to me naturally" assistant — if the model has something
        # intelligible to say, say it.
        stripped = (text or "").strip()
        if stripped:
            # Strip any stray code fences so we don't speak backticks.
            stripped = re.sub(r"^```[a-z]*\s*", "", stripped)
            stripped = re.sub(r"\s*```\s*$", "", stripped)
            # Truncated-JSON rescue: Gemini sometimes hits the token
            # cap mid-reply, so we get something like
            #   {"action": "chat", "reply": "In chapter 65, Sasaki and
            # with no closing quote/brace. json.loads can't parse it,
            # but we can still fish out the reply text. Same for a
            # complete-but-slightly-malformed response.
            if stripped.lstrip().startswith("{") or '"reply"' in stripped:
                m = re.search(r'"reply"\s*:\s*"((?:\\.|[^"\\])*)',
                              stripped, re.DOTALL)
                if m:
                    rescued = (m.group(1)
                               .replace('\\n', '\n')
                               .replace('\\"', '"')
                               .replace('\\\\', '\\')).strip()
                    if rescued:
                        logging.info("[llm] rescued reply from truncated "
                                     "JSON: %s", rescued[:120])
                        stripped = rescued
            # Keep it reasonable for TTS.
            if len(stripped) > 600:
                stripped = stripped[:597].rsplit(" ", 1)[0] + "…"
            logging.info("[llm] promoting non-JSON reply to chat: %s",
                         stripped[:120])
            promoted = {"action": "chat", "reply": stripped}
            if self._cache_ttl > 0:
                self._cache[cache_key] = (now, promoted)
            return promoted

        logging.debug("LLM returned nothing usable: %s", text[:200])
        self.last_error = "the language model returned an empty reply"
        return None

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


def build_from_config(config: dict):
    """Return an LLM instance if enabled in config, else None.
    Dispatches by [llm].backend:
      "gemini" (default)  → the LLM class in this file
      "ollama"            → OllamaLLM from llm_ollama.py (fully local,
                            no API key needed, zero rate limits)
    Both backends expose the same infer()/remember()/last_error shape
    so the dispatcher doesn't care which one it got."""
    llm_cfg = config.get("llm", {}) or {}
    if not llm_cfg.get("enabled", False):
        config["_llm_disabled_reason"] = "the language model is turned off in config"
        return None

    backend = str(llm_cfg.get("backend", "gemini")).lower()
    if backend == "ollama":
        from llm_ollama import build_from_config as _build_ollama
        return _build_ollama(config)
    if backend not in ("gemini", "google"):
        config["_llm_disabled_reason"] = \
            f"unknown LLM backend '{backend}' in config"
        logging.warning("unknown LLM backend: %s", backend)
        return None

    api_key = llm_cfg.get("api_key") or os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        logging.info("LLM enabled but no api_key / GEMINI_API_KEY set")
        config["_llm_disabled_reason"] = "no Gemini API key is set"
        return None
    config["_llm_disabled_reason"] = None
    inst = LLM(
        api_key=api_key,
        model=llm_cfg.get("model", "gemini-3.6-flash"),
        temperature=float(llm_cfg.get("temperature", 0.3)),
        timeout=float(llm_cfg.get("timeout_seconds", 6.0)),
        history_size=int(llm_cfg.get("history_size", 5)),
    )
    inst._cache_ttl = float(llm_cfg.get("cache_seconds", 600.0))
    return inst
