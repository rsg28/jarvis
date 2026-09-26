"""
Screen vision for Jarvis (Gemini multimodal).

Captures a screenshot of the primary display and sends it to Gemini
along with a text prompt. Gemini can read on-screen text, describe
UIs, explain images, answer questions about diagrams, translate
foreign-language windows, etc.

We piggy-back on the existing `[llm]` config: same Gemini API key,
same model (any Gemini 1.5+ / 3.x model supports images). No extra
API keys, no OCR install, no local ML dependencies.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import time
from pathlib import Path
from typing import Optional


SYSTEM_PROMPT_SCREEN = """You are the visual layer of Jarvis, a personal desktop assistant.

The user has just pressed a hotkey to have you look at their screen.
You'll receive one screenshot and a short question or instruction.

Rules:
- Reply concisely — the response is spoken aloud, so 1–4 short sentences.
- Never use markdown, bullet points, or code blocks.
- If the user asks 'what's on my screen' with no further detail,
  give a 1–2 sentence summary of the most relevant window/content.
- If they ask you to read something specific, quote it faithfully
  (keep punctuation and language) but skip UI chrome.
- If the screen is empty / desktop only, say so plainly.
- The user may speak English, Spanish, or French — reply in the same
  language they used.
- If asked to explain code, error messages, or diagrams, be pragmatic
  and concrete: what it means, what to do about it.
"""


def capture_screen_png(max_side: int = 1600) -> Optional[bytes]:
    """Grab the primary display and return a PNG blob. Downscales to
    `max_side` pixels on the longest edge to keep the Gemini payload
    small (large screenshots occasionally trigger 400s on the API and
    always slow down the round-trip)."""
    try:
        from PIL import ImageGrab
    except ImportError:
        logging.warning("Pillow not installed — vision disabled")
        return None
    try:
        img = ImageGrab.grab(all_screens=False)
    except Exception as exc:
        logging.warning("screen capture failed: %s", exc)
        return None

    # Downscale to keep the payload sane.
    w, h = img.size
    long_edge = max(w, h)
    if long_edge > max_side:
        scale = max_side / float(long_edge)
        img = img.resize((int(w * scale), int(h * scale)))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def describe_screen(prompt: str, llm) -> Optional[str]:
    """Send `prompt` + a fresh screenshot to Gemini and return the
    spoken reply. `llm` is a jarvis.llm.LLM instance (we reuse its
    api_key / model / timeout)."""
    if llm is None:
        return None
    try:
        import requests
    except ImportError:
        logging.warning("requests missing; vision disabled")
        return None

    png = capture_screen_png()
    if not png:
        return None

    b64 = base64.b64encode(png).decode("ascii")
    logging.info("[vision] captured screen (%d KB); asking Gemini: %r",
                 len(png) // 1024, prompt[:80])

    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{llm.model}:generateContent?key={llm.api_key}"
    )
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": prompt or "What is on my screen right now?"},
                {"inline_data": {"mime_type": "image/png", "data": b64}},
            ],
        }],
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT_SCREEN}]},
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 400,
        },
    }

    # Vision requests are heavier than text — allow a longer timeout
    # than the base LLM roundtrip.
    timeout = max(20.0, float(getattr(llm, "timeout", 6.0)) * 2)

    resp = None
    for attempt in range(2):
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
        except Exception as exc:
            logging.warning("vision request failed: %s", exc)
            return None
        if resp.status_code == 200:
            break
        if resp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
            time.sleep(0.8)
            continue
        logging.warning("vision HTTP %s: %s",
                        resp.status_code, resp.text[:200])
        return None
    if resp is None or resp.status_code != 200:
        return None

    try:
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as exc:
        logging.warning("vision parse failed: %s", exc)
        return None

    reply = (text or "").strip()
    if not reply:
        return None
    logging.info("[vision] reply (%d chars): %s",
                 len(reply), reply[:200].replace("\n", " "))
    return reply


def save_debug_screenshot(png: bytes) -> Optional[Path]:
    """Write the captured PNG next to the Jarvis log for inspection.
    Overwrites the previous one so we don't fill up the disk."""
    try:
        out = Path(__file__).parent / "last_screen.png"
        out.write_bytes(png)
        return out
    except Exception as exc:
        logging.debug("could not save debug screenshot: %s", exc)
        return None
