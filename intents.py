"""
Local command parsers for Jarvis. No regex.

Ollama handles free-form speech. These parsers cover canonical phrases
so the assistant still works if the model is unloaded.
"""
from __future__ import annotations

from matchers import (
    Match,
    exact,
    extract_url,
    fold,
    ints_in,
    orig_rest,
    prefix,
    split_marker,
)


def parse_weather(text: str):
    n = fold(text)
    if n == "weather":
        return Match(text.strip(), {})
    for p in ("weather in", "weather at"):
        if n.startswith(p + " "):
            return Match(text.strip(), {"loc": orig_rest(text, p)})
    return None


def parse_news(text: str):
    n = fold(text)
    if n in ("news", "headlines"):
        return Match(text.strip(), {})
    for p in ("news about", "news on", "whats happening in", "whats new in",
              "headlines about", "headlines on"):
        if n.startswith(p + " "):
            return Match(text.strip(), {"topic": orig_rest(text, p)})
    if n in ("whats new", "whats happening"):
        return Match(text.strip(), {})
    for p in ("news", "headlines"):
        if n.startswith(p + " "):
            return Match(text.strip(), {"topic": orig_rest(text, p)})
    return None


def parse_soccer(text: str):
    n = fold(text)
    if n in ("scores", "score", "soccer", "football", "fixtures", "matches",
             "whos playing", "who is playing", "whos playing today",
             "who is playing today"):
        return Match(text.strip(), {"q": ""})
    for p in ("scores", "score", "soccer", "football", "fixtures", "matches",
              "whos playing in", "who is playing in"):
        if n.startswith(p + " "):
            rest = orig_rest(text, p)
            if fold(rest) == "today":
                rest = ""
            elif fold(rest).endswith(" today"):
                rest = rest.rsplit(" ", 1)[0]
            return Match(text.strip(), {"q": rest})
    return None


_SYS = {
    "battery": "battery", "cpu": "cpu", "ram": "ram", "memory": "ram",
    "disk": "disk", "storage": "disk", "ip": "ip", "network": "ip",
    "wifi": "wifi", "wi-fi": "wifi",
}


def parse_system(text: str):
    n = fold(text)
    for lead in ("whats the ", "whats my ", "what is the ", "what is my "):
        if n.startswith(lead):
            n = n[len(lead):]
            break
    if n.startswith("wi fi") or n.startswith("wifi") or n.startswith("wi-fi"):
        return Match(text.strip(), {"key": "wifi"})
    key = n.split()[0] if n else ""
    mapped = _SYS.get(key)
    if mapped:
        return Match(text.strip(), {"key": mapped})
    return None


def _volume_dir(text: str, phrases: tuple[str, ...], default_n: str = "5"):
    n = fold(text)
    for p in phrases:
        p = fold(p)
        if n == p:
            return Match(text.strip(), {"n": default_n})
        if n.startswith(p + " "):
            rest = n[len(p) + 1:]
            if rest.isdigit():
                return Match(text.strip(), {"n": rest})
    return None


def parse_volume_up(text: str):
    return _volume_dir(text, (
        "volume up", "louder", "turn it up",
        "sube el volumen", "sube volumen",
    ))


def parse_volume_down(text: str):
    return _volume_dir(text, (
        "volume down", "quieter", "turn it down",
        "baja el volumen", "baja volumen",
    ))


def parse_set_volume(text: str):
    n = fold(text)
    for p in ("set volume to", "set volume"):
        if n.startswith(p + " "):
            rest = n[len(p) + 1:].rstrip("% ")
            if rest.startswith("to "):
                rest = rest[3:]
            if rest.isdigit():
                return Match(text.strip(), {"n": rest})
    return None


def parse_write(text: str):
    n = fold(text)
    if not n.startswith("write "):
        return None
    split = split_marker(text, " content:")
    if split is None:
        return None
    before, content = split
    path = orig_rest(before, "write").strip()
    if not path:
        return None
    return Match(text.strip(), {"path": path, "content": content.lstrip()})


def parse_append(text: str):
    n = fold(text)
    if not (n.startswith("append to ") or n.startswith("append ")):
        return None
    split = split_marker(text, " content:")
    if split is None:
        return None
    before, content = split
    fn = fold(before)
    lead = "append to" if fn.startswith("append to") else "append"
    path = orig_rest(before, lead).strip()
    if not path:
        return None
    return Match(text.strip(), {"path": path, "content": content.lstrip()})


def parse_see_screen(text: str):
    n = fold(text)
    leads = (
        "whats on my screen", "whats on the screen", "whats on screen",
        "whats on my monitor", "whats on the monitor",
        "whats on my pantalla", "whats on the pantalla",
        "what do i see",
        "describe my screen", "describe the screen", "describe screen",
        "describe my monitor", "describe the monitor", "describe the display",
        "describe my pantalla", "describe the pantalla",
        "read my screen", "read the screen", "read screen",
        "read my monitor", "read the monitor",
        "look at my screen", "look at the screen", "look at screen",
        "see my screen", "see the screen",
        "que hay en mi pantalla", "que hay en la pantalla", "que hay en pantalla",
        "que se ve en la pantalla", "que dice en la pantalla",
        "mira mi pantalla", "mira la pantalla",
    )
    for p in sorted(leads, key=len, reverse=True):
        if n == p:
            return Match(text.strip(), {"prompt": ""})
        if n.startswith(p + " "):
            return Match(text.strip(), {"prompt": orig_rest(text, p)})
    return None


def parse_see_this(text: str):
    n = fold(text)
    if n in (
        "what does this say", "what does this mean", "what does this show",
        "translate this", "explain this", "explain this error",
        "explain this code", "explain this screen",
        "summarize this", "summarise this",
    ):
        return Match(text.strip(), {})
    return None


def parse_timer(text: str):
    n = fold(text)
    for p in ("set a timer for", "set timer for", "set a timer", "set timer", "timer"):
        if n.startswith(p + " "):
            return Match(text.strip(), {"dur": orig_rest(text, p)})
    return None


def parse_remind(text: str):
    n = fold(text)
    if not n.startswith("remind me in "):
        return None
    rest = orig_rest(text, "remind me in")
    i = rest.lower().find(" to ")
    if i < 0:
        return None
    return Match(text.strip(), {
        "dur": rest[:i].strip(),
        "what": rest[i + 4:].strip(),
    })


def parse_kb_learn(text: str):
    n = fold(text)
    leads = (
        "remember this file", "remember this",
        "read and remember", "learn from", "learn",
        "aprende este archivo", "aprende este documento", "aprende de",
        "aprende", "study", "ingest", "memoriza este",
    )
    for p in leads:
        if n.startswith(p + " "):
            return Match(text.strip(), {"q": orig_rest(text, p)})
    return None


def parse_kb_list(text: str):
    return exact(
        "what documents do you know", "what documents do you have",
        "what documents do you remember", "list my documents", "list documents",
        "what files have you learned", "what docs have you learned",
        "que documentos tienes", "que documentos conoces", "que documentos sabes",
        "lista los documentos", "lista documentos",
        "knowledge base", "kb list",
    )(text)


def parse_kb_forget_doc(text: str):
    n = fold(text)
    for p in (
        "unlearn",
        "forget the document", "forget document", "forget the doc", "forget doc",
        "forget the file", "forget file",
        "forget the archivo", "forget archivo",
        "forget the documento", "forget documento",
    ):
        if n.startswith(p + " "):
            return Match(text.strip(), {"q": orig_rest(text, p)})
    return None


def parse_diagnose(text: str):
    url = extract_url(text)
    if not url:
        return None
    n = fold(text)
    triggers = (
        "diagnose", "debug", "inspect", "check", "averigua",
        "por que no carga", "why wont", "why isnt", "why is",
        "whats wrong with", "wont load", "isnt loading", "is broken", "no carga",
    )
    if any(t in n for t in triggers) or n.startswith("http"):
        return Match(text.strip(), {"url": url})
    return None


def parse_manga_check(text: str):
    n = fold(text)
    leads = (
        "check for new chapters", "check for new chapter",
        "check for new episodes", "check for new episode",
        "check for new updates", "check for updates",
        "check if there is a new chapter", "check if there is a new episode",
        "is there a new chapter", "is there a new episode", "is there a new update",
        "are there new chapters", "any new chapters", "any new chapter",
        "new chapters", "new chapter", "new episodes", "new episode",
        "manga update", "manga check",
        "hay un nuevo capitulo", "hay nuevo capitulo",
        "checa si hay un nuevo capitulo", "revisa si hay un nuevo capitulo",
        "mira si hay un nuevo capitulo",
    )
    for p in sorted(leads, key=len, reverse=True):
        if n == p:
            return Match(text.strip(), {"q": ""})
        for of in (" of ", " for ", " on ", " about ", " de ", " del "):
            if n.startswith(p + of.strip() + " ") or n.startswith(p + of):
                # orig rest after p
                rest = orig_rest(text, p)
                low = fold(rest)
                for prep in ("of", "for", "on", "about", "de", "del"):
                    if low.startswith(prep + " "):
                        rest = orig_rest(rest, prep)
                        break
                return Match(text.strip(), {"q": rest})
    # "did X update"
    for verb in (" did ",):
        pass
    if n.startswith("did ") and n.endswith(" update"):
        mid = orig_rest(text, "did")
        if fold(mid).endswith(" update"):
            mid = mid[: mid.lower().rfind(" update")].strip()
            return Match(text.strip(), {"q": mid})
    if n.startswith("has ") and (n.endswith(" updated") or n.endswith(" update")):
        mid = orig_rest(text, "has")
        for tail in (" updated", " update"):
            if fold(mid).endswith(tail.strip()):
                mid = mid[: len(mid) - len(tail) + (0 if mid.endswith(tail) else 0)]
                # simpler:
                break
        words = mid.split()
        if words and fold(words[-1]) in ("updated", "update", "dropped"):
            mid = " ".join(words[:-1])
        return Match(text.strip(), {"q": mid})
    return None


def parse_manga_latest(text: str):
    n = fold(text)
    leads = (
        "whats the latest chapter of", "what is the latest chapter of",
        "whats the latest episode of", "what is the latest episode of",
        "latest chapter of", "latest episode of",
        "how many chapters of", "what chapter is",
        "cual es el ultimo capitulo de", "cual es el capitulo de",
        "en que capitulo va", "en que capitulo esta",
        "que capitulo salio", "que capitulo sacaron", "que capitulo hay",
    )
    for p in leads:
        if n.startswith(p + " "):
            return Match(text.strip(), {"q": orig_rest(text, p)})
    return None


def parse_mem_know(text: str):
    n = fold(text)
    if n in (
        "what do you remember", "what do you know",
        "what memories do you have", "list your memory", "list your memories",
        "list memory", "list memories", "show your memory", "show your memories",
    ):
        return Match(text.strip(), {"q": ""})
    for p in ("what do you remember about", "what do you know about"):
        if n.startswith(p + " "):
            return Match(text.strip(), {"q": orig_rest(text, p)})
    return None


def parse_watch_start(text: str):
    n = fold(text)
    if n == "watch screen":
        return Match(text.strip(), {})
    if n.startswith("watch screen every "):
        rest = n[len("watch screen every "):].split()
        if not rest or not rest[0].isdigit():
            return None
        unit = rest[1] if len(rest) > 1 else "s"
        return Match(text.strip(), {"n": rest[0], "unit": unit})
    return None


def parse_mouse_move(text: str):
    n = fold(text)
    for p in ("move mouse to", "move cursor to", "move to", "move mouse"):
        if n.startswith(p + " "):
            nums = ints_in(orig_rest(text, p))
            if len(nums) >= 2:
                return Match(text.strip(), {"x": nums[0], "y": nums[1]})
    return None


def parse_click(text: str, btn: str = "left", clicks: int = 1):
    n = fold(text)
    leads = {
        1: {
            "left": ("left click", "mouse click", "click"),
            "right": ("right click",),
            "middle": ("middle click",),
        },
        2: {
            "left": ("double click", "dbl click", "doubleclick"),
        },
    }[clicks][btn]
    for p in leads:
        if n == p:
            return Match(text.strip(), {"btn": btn, "x": "", "y": "", "clicks": str(clicks)})
        if n.startswith(p + " at "):
            nums = ints_in(orig_rest(text, p + " at"))
            if len(nums) >= 2:
                return Match(text.strip(), {
                    "btn": btn, "x": nums[0], "y": nums[1], "clicks": str(clicks),
                })
        if n.startswith(p + " "):
            nums = ints_in(orig_rest(text, p))
            if len(nums) >= 2:
                return Match(text.strip(), {
                    "btn": btn, "x": nums[0], "y": nums[1], "clicks": str(clicks),
                })
    return None


def parse_scroll(text: str):
    n = fold(text)
    for d in ("up", "down"):
        p = "scroll " + d
        if n == p:
            return Match(text.strip(), {"dir": d, "n": "3"})
        if n.startswith(p + " "):
            rest = n[len(p) + 1:]
            if rest.isdigit():
                return Match(text.strip(), {"dir": d, "n": rest})
    return None


def parse_drag(text: str):
    n = fold(text)
    if not n.startswith("drag "):
        return None
    nums = ints_in(orig_rest(text, "drag"))
    if len(nums) >= 4:
        return Match(text.strip(), {
            "x1": nums[0], "y1": nums[1], "x2": nums[2], "y2": nums[3],
        })
    return None


def parse_scaffold(text: str):
    n = fold(text)
    if not n.startswith("scaffold "):
        return None
    parts = orig_rest(text, "scaffold").split()
    if len(parts) < 2:
        return None
    stack = parts[0].lower()
    if stack not in ("python", "node", "static", "rust"):
        return None
    return Match(text.strip(), {"stack": stack, "name": " ".join(parts[1:])})


def parse_run(text: str):
    return prefix("run", "exec", "execute", rest="cmd")(text)


# Re-export helpers used by commands.py
exact_quit = exact(
    "quit", "exit", "bye", "goodbye", "shutdown",
    "apagate", "cierrate", "cerrar",
)
exact_help = exact("help", "what can you do", "command", "commands", "ayuda")
exact_stop = exact("__stop_listening__", "stop listening", "stop")
exact_phone = exact(
    "phone link", "link del celular", "enlace del celular",
    "jarvis en el celular", "abrir en el celular",
)
exact_pause = exact("pause", "play", "resume", "toggle playback", "pausa", "reanuda")
exact_next = exact("next", "next song", "next track", "skip", "siguiente")
exact_prev = exact("previous", "previous song", "previous track", "back", "prev", "anterior")
exact_mute = exact("mute", "unmute", "silencio")
exact_vram_off = exact(
    "release vram", "release gpu", "unload model", "unload models",
    "free vram", "free gpu", "voy a jugar",
)
exact_vram_on = exact(
    "reclaim vram", "warm up", "warm up model", "wake up",
    "preload model", "carga el modelo",
)
exact_vram_stat = exact(
    "vram status", "gpu status", "whats in vram", "what is in vram",
    "que hay en la vram", "que hay en vram",
)
exact_game_on = exact(
    "gaming mode on", "arm gaming mode", "modo gaming on", "activa modo gaming",
)
exact_game_off = exact(
    "gaming mode off", "disarm gaming mode", "modo gaming off", "apaga modo gaming",
)
exact_game_stat = exact(
    "gaming mode status", "modo gaming status", "estado del modo gaming",
    "estado modo gaming",
)
