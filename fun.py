"""
Fun — jokes and trivia from free public APIs.

    JokeAPI  (https://v2.jokeapi.dev/) — safe-mode filtered
    OpenTDB  (https://opentdb.com/)   — trivia
"""
from __future__ import annotations

import html
import logging
import random
from typing import Optional


def joke() -> str:
    try:
        import requests
        r = requests.get(
            "https://v2.jokeapi.dev/joke/Any",
            params={"safe-mode": "true", "type": "twopart,single"},
            timeout=6,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("type") == "twopart":
            return f"{d['setup']} … {d['delivery']}"
        return d.get("joke") or _fallback_joke()
    except Exception as exc:
        logging.info("jokeapi failed: %s (using fallback)", exc)
        return _fallback_joke()


def trivia() -> str:
    try:
        import requests
        r = requests.get(
            "https://opentdb.com/api.php",
            params={"amount": 1, "type": "multiple"},
            timeout=6,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return "Couldn't fetch a trivia question right now."
        q = results[0]
        question = html.unescape(q["question"])
        answer = html.unescape(q["correct_answer"])
        return f"{question}  (Answer: {answer})"
    except Exception as exc:
        logging.info("opentdb failed: %s", exc)
        return "Couldn't reach the trivia service."


_FALLBACKS = [
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "I told my computer I needed a break, and it said: no problem, I will go to sleep.",
    "Debugging: being the detective in a crime movie where you are also the murderer.",
    "There are 10 types of people in the world. Those who understand binary, and those who don't.",
]


def _fallback_joke() -> str:
    return random.choice(_FALLBACKS)
