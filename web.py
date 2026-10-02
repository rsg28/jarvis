"""
Lightweight web diagnostics for Jarvis.

Lets the assistant answer questions like:
  "why is this page not loading"
  "diagnose this url"
  "check what's wrong with https://..."

Without a headless browser. The strategy is layered:

  1. Fetch the URL via urllib and record status + response time +
     final URL (follows redirects).
  2. If HTML, extract referenced resources (<img>, <script>, <link
     rel="stylesheet">) and HEAD-test their hosts in parallel.
  3. Classify failures: DNS, SSL, timeout, 4xx, 5xx, connection
     reset. These are the symptoms a user usually describes as
     "it just won't load" / "tap to retry does nothing".
  4. For comix.to chapter URLs, add a comix-aware layer that reads
     the embedded JSON state so the assistant can say things like
     "the image CDN is unreachable" instead of generic errors.

Everything returns a Diagnosis dataclass that commands.py converts
to a short, voice-friendly sentence.
"""
from __future__ import annotations

import logging
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import List, Optional


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) jarvis/1.0"
DEFAULT_TIMEOUT = 8.0


@dataclass
class ResourceCheck:
    url: str
    host: str
    status: Optional[int]   # HTTP status if we got that far
    error: Optional[str]    # short error class name if we didn't
    ok: bool                # True iff status in 200..399

    @property
    def failure_reason(self) -> str:
        if self.ok:
            return ""
        if self.error:
            return self.error
        if self.status is not None:
            return f"HTTP {self.status}"
        return "unknown"


@dataclass
class Diagnosis:
    url: str
    final_url: str
    status: Optional[int]
    elapsed_ms: int
    error: Optional[str] = None
    resources: List[ResourceCheck] = field(default_factory=list)
    # Human-targeted summary line (voice-friendly).
    summary: str = ""
    # Longer, printable detail (goes to the chat / log).
    detail: str = ""


# ────────────────────── low-level fetch ──────────────────────
def _classify_exception(exc: BaseException) -> str:
    """Compact human label for the common failure modes."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, ssl.SSLError):
        return "SSL error"
    if isinstance(exc, socket.timeout):
        return "timeout"
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        name = type(reason).__name__
        text = str(reason).lower()
        if "name or service" in text or "getaddrinfo" in text \
           or "nodename nor servname" in text:
            return "DNS failure"
        if "refused" in text:
            return "connection refused"
        if "reset" in text:
            return "connection reset"
        if "ssl" in name.lower() or "certificate" in text:
            return "SSL error"
        if "timed out" in text:
            return "timeout"
        return name
    if isinstance(exc, socket.gaierror):
        return "DNS failure"
    return type(exc).__name__


def fetch(url: str, *, timeout: float = DEFAULT_TIMEOUT,
          method: str = "GET") -> tuple[Optional[int], Optional[str],
                                        str, bytes, int, Optional[str]]:
    """Return (status, content_type, final_url, body, elapsed_ms, error)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT},
                                 method=method)
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read() if method == "GET" else b""
            elapsed = int((time.perf_counter() - start) * 1000)
            return (resp.status,
                    resp.headers.get("Content-Type", ""),
                    resp.geturl(), body, elapsed, None)
    except urllib.error.HTTPError as exc:
        elapsed = int((time.perf_counter() - start) * 1000)
        return (exc.code, exc.headers.get("Content-Type", "") if exc.headers else "",
                url, b"", elapsed, _classify_exception(exc))
    except Exception as exc:
        elapsed = int((time.perf_counter() - start) * 1000)
        return (None, None, url, b"", elapsed, _classify_exception(exc))


# ────────────────────── resource testing ──────────────────────
_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
_SCRIPT_RE = re.compile(r'<script[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
_LINK_RE = re.compile(
    r'<link[^>]+rel=["\']stylesheet["\'][^>]+href=["\']([^"\']+)["\']',
    re.IGNORECASE)


def _absolute(url: str, base: str) -> str:
    return urllib.parse.urljoin(base, url)


def extract_resources(html: str, base_url: str) -> List[str]:
    urls: list[str] = []
    for pat in (_IMG_RE, _SCRIPT_RE, _LINK_RE):
        urls.extend(_absolute(m, base_url) for m in pat.findall(html))
    # De-dup preserving order
    seen: set = set()
    out: list[str] = []
    for u in urls:
        if u not in seen and u.startswith(("http://", "https://")):
            seen.add(u); out.append(u)
    return out


def check_resource(url: str, timeout: float = 5.0) -> ResourceCheck:
    """HEAD the resource; fall back to a tiny GET if HEAD is blocked."""
    host = urllib.parse.urlparse(url).hostname or ""
    status, _ct, _final, _body, _ms, err = fetch(url, timeout=timeout,
                                                 method="HEAD")
    if err and status is None:
        # Some servers 405 HEAD; retry with GET.
        status, _ct, _final, _body, _ms, err = fetch(url, timeout=timeout,
                                                     method="GET")
    ok = (status is not None and 200 <= status < 400 and err is None)
    return ResourceCheck(url=url, host=host, status=status, error=err, ok=ok)


def check_hosts(urls: List[str], *, max_per_host: int = 1,
                workers: int = 8, timeout: float = 5.0) -> List[ResourceCheck]:
    """Test a representative subset: one URL per unique host (avoids
    hammering a dead CDN with 50 identical failures)."""
    picked: dict = {}
    for u in urls:
        host = urllib.parse.urlparse(u).hostname or ""
        if host not in picked:
            picked[host] = u
        elif len(picked[host] if isinstance(picked[host], list) else [picked[host]]) < max_per_host:
            pass
    sample = list(picked.values())
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(lambda u: check_resource(u, timeout), sample))


# ────────────────────── comix-aware layer ──────────────────────
def _is_comix_chapter(url: str) -> bool:
    try:
        p = urllib.parse.urlparse(url)
        return "comix.to" in (p.hostname or "") and "chapter" in p.path.lower()
    except Exception:
        return False


# ────────────────────── public API ──────────────────────
def diagnose(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> Diagnosis:
    """Run the layered check and return a filled-in Diagnosis."""
    status, ctype, final_url, body, elapsed, err = fetch(url, timeout=timeout)
    diag = Diagnosis(url=url, final_url=final_url, status=status,
                     elapsed_ms=elapsed, error=err)

    # Hard failure fetching the page itself
    if err is not None and (status is None or status >= 400):
        diag.summary = _summary_for_page_failure(url, status, err)
        diag.detail = f"[web] fetch failed in {elapsed} ms: {err} (status={status})"
        return diag

    # If we have HTML, see which embedded hosts are reachable
    is_html = (ctype or "").lower().startswith("text/html") \
              or b"<html" in body[:4096].lower()
    if is_html and body:
        try:
            html = body.decode("utf-8", errors="replace")
        except Exception:
            html = ""
        resources = extract_resources(html, final_url)
        checks = check_hosts(resources, timeout=5.0) if resources else []
        diag.resources = checks

        broken = [c for c in checks if not c.ok]
        comix = _is_comix_chapter(url)

        if comix:
            # Special case: Comix.to is a React SPA. The chapter's PAGE
            # images are injected at runtime via JavaScript from a
            # separate CDN whose host doesn't appear in the initial
            # HTML. So "all hosts OK" means nothing for a reader that
            # won't render — it only means the shell loaded. Instead,
            # name that reality explicitly so the user knows this
            # isn't something the browser can fix.
            if broken:
                diag.summary = (
                    f"The comix shell loaded fine, but one of its hosts "
                    f"({broken[0].host}) failed with {broken[0].failure_reason}. "
                    f"That could be why the reader is broken."
                )
            else:
                diag.summary = (
                    f"The comix page itself loaded fine ({status} in "
                    f"{elapsed} ms), but comix is a single-page app — "
                    f"the actual chapter images come from a separate CDN "
                    f"that's fetched by JavaScript, which I can't see "
                    f"from the server side. If the reader shows 'Tap to "
                    f"retry' on every page, that CDN is almost certainly "
                    f"down. Not something you can fix from the browser. "
                    f"Try the MangaDex mirror instead."
                )
        elif broken:
            diag.summary = (
                f"The page loaded in {elapsed} ms with status {status}, but "
                f"{len(broken)} of its hosts failed to respond. The first "
                f"broken one is {broken[0].host} ({broken[0].failure_reason})."
            )
        else:
            diag.summary = (
                f"The page loaded fine: HTTP {status} in {elapsed} ms, "
                f"and every referenced host is reachable. The problem is "
                f"client-side or specific to a resource that lazy-loads later."
            )
        diag.detail = _render_detail(diag)
        return diag

    # Non-HTML (image, JSON, etc.) — just report the direct status
    diag.summary = f"HTTP {status} in {elapsed} ms. Content-type: {ctype or 'unknown'}."
    diag.detail = diag.summary
    return diag


def _summary_for_page_failure(url: str, status: Optional[int],
                              err: Optional[str]) -> str:
    host = urllib.parse.urlparse(url).hostname or url
    if err == "DNS failure":
        return (f"I can't resolve {host} — the domain's DNS is failing. "
                f"That usually means the site is dead or your DNS is down.")
    if err == "timeout":
        return (f"{host} isn't responding within the timeout. "
                f"The server is either overloaded or offline.")
    if err and "SSL" in err:
        return (f"{host} has an SSL error ({err}). The site's certificate "
                f"might be expired or misconfigured.")
    if err == "connection refused":
        return f"{host} refused the connection — the server is down."
    if status and 400 <= status < 500:
        return f"{host} returned HTTP {status}. The URL is wrong or no longer exists."
    if status and 500 <= status < 600:
        return f"{host} returned HTTP {status} — the site itself is broken."
    return f"{host} failed with: {err or f'HTTP {status}'}."


def _render_detail(diag: Diagnosis) -> str:
    lines = [f"[web] GET {diag.url}  →  {diag.status} in {diag.elapsed_ms} ms"]
    if diag.final_url != diag.url:
        lines.append(f"      final URL: {diag.final_url}")
    if diag.resources:
        lines.append(f"      {len(diag.resources)} unique hosts tested:")
        for c in diag.resources:
            tag = "OK " if c.ok else "FAIL"
            s = f"HTTP {c.status}" if c.status else (c.error or "?")
            lines.append(f"        {tag} {c.host:<48s} {s}")
    return "\n".join(lines)
