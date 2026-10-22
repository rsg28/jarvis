"""
Phrase matchers for Jarvis.

No regular expressions. Commands are recognised with plain string
checks: exact phrases, prefixes, and a few small parsers (URL, ints,
"content:" splits). Natural language still goes through Ollama; this
module is only the local fallback when the model is offline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


_ACCENT = str.maketrans({
    "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "ü": "u", "ñ": "n",
    "Á": "a", "É": "e", "Í": "i", "Ó": "o", "Ú": "u", "Ü": "u", "Ñ": "n",
    "¿": "", "¡": "",
})


def fold(s: str) -> str:
    """Lowercase, strip accents/punctuation, collapse spaces."""
    s = (s or "").lower().translate(_ACCENT)
    for ch in "'’\"?,.!":
        s = s.replace(ch, "")
    return " ".join(s.split())


def orig_rest(text: str, folded_prefix: str) -> str:
    """Original-casing remainder after a folded prefix of N words."""
    n = len(folded_prefix.split()) if folded_prefix else 0
    parts = text.strip().split()
    return " ".join(parts[n:])


@dataclass
class Match:
    """Drop-in stand-in for the old re.Match used by handlers."""
    text: str
    groups: dict = field(default_factory=dict)

    def group(self, name, default=None):
        if name == 0:
            return self.text
        if isinstance(name, int):
            return default
        val = self.groups.get(name)
        return default if val is None else val

    def groupdict(self) -> dict:
        return dict(self.groups)

    @property
    def string(self) -> str:
        return self.text


ParseFn = Callable[[str], Optional[Match]]


def exact(*phrases: str) -> ParseFn:
    want = {fold(p) for p in phrases}

    def parse(text: str) -> Optional[Match]:
        if fold(text) in want:
            return Match(text.strip(), {})
        return None

    return parse


def prefix(*prefixes: str, rest: str = "q", allow_empty: bool = False) -> ParseFn:
    items = sorted({fold(p) for p in prefixes}, key=len, reverse=True)

    def parse(text: str) -> Optional[Match]:
        n = fold(text)
        for p in items:
            if n == p:
                if allow_empty:
                    return Match(text.strip(), {rest: ""})
                return None
            if n.startswith(p + " "):
                return Match(text.strip(), {rest: orig_rest(text, p)})
        return None

    return parse


def one_of(*parsers: ParseFn) -> ParseFn:
    def parse(text: str) -> Optional[Match]:
        for p in parsers:
            m = p(text)
            if m is not None:
                return m
        return None

    return parse


def ints_in(text: str) -> list[str]:
    out: list[str] = []
    for tok in text.replace(",", " ").split():
        tok = tok.strip()
        body = tok[1:] if tok.startswith("-") else tok
        if body.isdigit():
            out.append(tok)
    return out


def extract_url(text: str) -> Optional[str]:
    raw = text or ""
    low = raw.lower()
    start = -1
    for scheme in ("https://", "http://"):
        i = low.find(scheme)
        if i >= 0:
            start = i
            break
    if start < 0:
        return None
    url = raw[start:].split()[0].rstrip(".,;:)?!")
    return url or None


def split_marker(text: str, marker: str) -> Optional[tuple[str, str]]:
    i = text.lower().find(marker.lower())
    if i < 0:
        return None
    return text[:i], text[i + len(marker):]


def slug_name(name: str) -> str:
    out: list[str] = []
    for ch in (name or "").lower():
        if ch.isalnum() or ch in "-_":
            out.append(ch)
        else:
            out.append("-")
    s = "".join(out)
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-_")


def strip_fences(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        nl = s.find("\n")
        s = s[nl + 1:] if nl != -1 else s[3:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()
            s = s[: s.rfind("```")].rstrip()
    return s


def first_json_object(text: str) -> Optional[str]:
    s = strip_fences(text)
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        return s[start:end + 1]
    return None


def rescue_json_reply(text: str) -> Optional[str]:
    """Pull the string after "reply": from truncated JSON, no regex."""
    key = '"reply"'
    i = (text or "").find(key)
    if i < 0:
        return None
    rest = text[i + len(key):].lstrip()
    if not rest.startswith(":"):
        return None
    rest = rest[1:].lstrip()
    if not rest.startswith('"'):
        return None
    rest = rest[1:]
    out: list[str] = []
    esc = False
    for ch in rest:
        if esc:
            if ch == "n":
                out.append("\n")
            elif ch == "t":
                out.append("\t")
            elif ch == '"':
                out.append('"')
            elif ch == "\\":
                out.append("\\")
            else:
                out.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == '"':
            break
        else:
            out.append(ch)
    rescued = "".join(out).strip()
    return rescued or None
