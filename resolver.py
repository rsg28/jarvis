"""
Target resolver — turns "open my resume", "open spotify", "read the todo
list" into an actual filesystem path or launchable target.

Strategy
────────
1. If the target already looks like a URL   -> return it as `{'kind':'url'}`.
2. If it's an existing absolute or relative path -> return it directly.
3. Otherwise walk a set of search roots:
      · Desktop  (regular + OneDrive)
      · Documents (regular + OneDrive)
      · Downloads
      · Start Menu shortcuts (user + all-users) -> installed apps
      · Any extra roots supplied via config
   and pick the best matches by fuzzy string ratio (stdlib difflib).

Safety
──────
This module never launches anything — it only *finds* the target. The
dispatcher decides what to do with the result (os.startfile / webbrowser
/ read-text). Nothing here writes, deletes, or executes arbitrary shell
strings.
"""
from __future__ import annotations

import difflib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set


TEXT_SUFFIXES: Set[str] = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv",
    ".json", ".jsonc", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf",
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".html", ".htm", ".css", ".scss", ".sass",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".rs", ".go", ".java", ".kt",
    ".sh", ".ps1", ".bat", ".cmd",
    ".sql", ".env",
    ".gitignore", ".gitattributes",
    ".vue", ".svelte",
    "",  # extensionless (README, LICENSE, Makefile, …)
}

# File suffixes we search when resolving generic "open X" targets.
LAUNCHABLE_SUFFIXES: Set[str] = {
    ".lnk", ".url",                       # Windows shortcuts
    ".exe", ".bat", ".cmd", ".ps1",       # executables/scripts
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
    ".csv", ".txt", ".md",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    ".mp3", ".mp4", ".mkv", ".wav", ".flac",
    ".zip", ".rar", ".7z",
    ".html", ".htm",
    ".py", ".js", ".ts",
    ".json", ".toml", ".yaml", ".yml",
}

URL_RE = re.compile(
    r"^(?:https?://\S+|www\.\S+|[a-zA-Z0-9-]+\.(?:com|org|net|io|dev|app|co|ai|xyz|es|fr|ca|us|gov|edu)(?:/\S*)?)$",
    re.IGNORECASE,
)


@dataclass
class Match:
    kind: str          # 'url' | 'app' | 'file' | 'folder'
    name: str          # display name (basename without shortcut suffix)
    path: str          # absolute path or URL
    score: float       # 0..1, higher is better

    def __repr__(self) -> str:  # pragma: no cover
        return f"Match({self.kind}, {self.name!r}, score={self.score:.2f})"


# ────────────────── search-root discovery ──────────────────

def _default_roots() -> List[Path]:
    """Reasonable defaults for a Windows user. Silently drops missing
    paths so the function is safe on non-Windows too."""
    home = Path.home()
    candidates: List[Path] = []

    # Desktop + Documents + Downloads (both regular and OneDrive variants)
    for sub in ("Desktop", "Documents", "Downloads"):
        candidates.append(home / sub)
    for onedrive_root in _onedrive_roots():
        for sub in ("Desktop", "Documents", "Documentos", "Downloads",
                    "Archivos personales", "Escritorio"):
            candidates.append(onedrive_root / sub)

    # Start Menu shortcuts (installed applications live here as .lnk files)
    appdata = os.environ.get("APPDATA")
    programdata = os.environ.get("ProgramData")
    if appdata:
        candidates.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    if programdata:
        candidates.append(Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")

    # De-dup and keep only existing dirs.
    seen: Set[Path] = set()
    out: List[Path] = []
    for c in candidates:
        try:
            resolved = c.resolve()
        except Exception:
            continue
        if resolved in seen:
            continue
        if resolved.is_dir():
            seen.add(resolved)
            out.append(resolved)
    return out


def _onedrive_roots() -> List[Path]:
    """Every OneDrive-* env var that points to an existing folder."""
    out: List[Path] = []
    for key, val in os.environ.items():
        if key.startswith("OneDrive"):
            p = Path(val)
            if p.is_dir():
                out.append(p)
    return out


# ────────────────── URL / path helpers ──────────────────

def looks_like_url(target: str) -> bool:
    return bool(URL_RE.match(target.strip()))


def normalize_url(target: str) -> str:
    t = target.strip()
    if t.lower().startswith(("http://", "https://")):
        return t
    return "https://" + t.lstrip("/")


def looks_like_path(target: str) -> bool:
    t = target.strip().strip('"').strip("'")
    if len(t) >= 3 and t[1:3] == ":\\":          # C:\ ...
        return True
    if t.startswith(("\\\\", "/", "~")):
        return True
    if t.startswith(".") and (os.sep in t or "/" in t):
        return True
    return False


def resolve_path(target: str) -> Optional[Path]:
    t = target.strip().strip('"').strip("'")
    if t.startswith("~"):
        t = os.path.expanduser(t)
    p = Path(os.path.expandvars(t))
    if p.exists():
        try:
            return p.resolve()
        except Exception:
            return p
    return None


# ────────────────── main search ──────────────────

def find_targets(
    query: str,
    roots: Optional[Sequence[Path]] = None,
    *,
    kinds: Iterable[str] = ("app", "file", "folder"),
    limit: int = 5,
    text_only: bool = False,
    min_score: float = 0.55,
    max_scan: int = 8000,
) -> List[Match]:
    """Return up to `limit` matches ranked by fuzzy score.

    Parameters
    ----------
    query     : natural-language target ("my resume", "spotify", "todo").
    roots     : additional roots to search on top of the defaults.
    kinds     : which kinds of hits to keep ('app'|'file'|'folder').
    text_only : when True, only text-ish files (for the `read` command).
    max_scan  : safety cap on how many entries we walk. Prevents a runaway
                scan on huge OneDrive folders. Increase if you keep a very
                deep vault of files.
    """
    query = (query or "").strip().lower()
    if not query:
        return []

    q_tokens = _tokenize(query)
    scan_roots = list(_default_roots())
    if roots:
        for r in roots:
            try:
                p = Path(r).expanduser().resolve()
            except Exception:
                continue
            if p.is_dir() and p not in scan_roots:
                scan_roots.append(p)

    matches: List[Match] = []
    seen_paths: Set[str] = set()
    scanned = 0

    for root in scan_roots:
        for path in _walk(root, max_depth=4):
            scanned += 1
            if scanned > max_scan:
                logging.info("resolver: hit max_scan=%d, stopping", max_scan)
                break

            name = path.name
            if not name:
                continue

            kind = _classify(path)
            if kind not in kinds:
                continue
            if text_only and kind == "file" and path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if kind == "file" and not text_only:
                # Filter out random junk (thumbs.db, etc.).
                if path.suffix.lower() not in LAUNCHABLE_SUFFIXES and path.suffix != "":
                    continue

            spath = str(path)
            if spath in seen_paths:
                continue
            seen_paths.add(spath)

            score = _score(name, q_tokens, query)
            if score < min_score:
                continue

            display = path.stem if path.suffix.lower() in (".lnk", ".url") else name
            matches.append(Match(kind=kind, name=display, path=spath, score=score))

        if scanned > max_scan:
            break

    matches.sort(key=lambda m: (-m.score, len(m.name)))
    return matches[:limit]


def find_best(query: str, **kwargs) -> Optional[Match]:
    """Convenience wrapper — top match or None."""
    results = find_targets(query, limit=1, **kwargs)
    return results[0] if results else None


# ────────────────── walking / scoring internals ──────────────────

def _walk(root: Path, max_depth: int) -> Iterable[Path]:
    """os.walk with a depth guard and PermissionError swallow."""
    root_parts = len(root.parts)
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            depth = len(Path(dirpath).parts) - root_parts
            if depth > max_depth:
                dirnames[:] = []
                continue
            # Prune noisy folders that never hold user targets.
            dirnames[:] = [d for d in dirnames if not _skip_dir(d)]

            # Yield the folder itself so `open <folder-name>` works
            p = Path(dirpath)
            if depth <= max_depth:
                yield p

            for f in filenames:
                yield Path(dirpath) / f
    except (PermissionError, OSError) as exc:
        logging.debug("walk skipped %s: %s", root, exc)


_SKIP_DIRS: Set[str] = {
    "node_modules", ".git", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".idea", ".vscode", "build", "dist", ".next", ".turbo",
    "$RECYCLE.BIN", "System Volume Information",
}


def _skip_dir(name: str) -> bool:
    if not name:
        return True
    if name.startswith("."):
        return name in {".git", ".venv"}
    return name in _SKIP_DIRS


def _classify(path: Path) -> str:
    try:
        if path.is_dir():
            return "folder"
    except OSError:
        return "file"
    suffix = path.suffix.lower()
    if suffix in (".lnk", ".url", ".exe"):
        return "app"
    return "file"


def _tokenize(s: str) -> List[str]:
    return [t for t in re.split(r"[\s_\-.]+", s.lower()) if t]


def _score(name: str, q_tokens: List[str], q_raw: str) -> float:
    """Blend of stem-substring match + token overlap + full ratio."""
    stem = re.sub(r"\.(lnk|url|exe)$", "", name.lower())
    stem_tokens = _tokenize(stem)

    # Direct substring on the stem is a strong signal.
    substring = 1.0 if q_raw in stem else 0.0

    # Token overlap: how many query tokens appear in the stem tokens.
    if q_tokens:
        overlap = sum(1 for t in q_tokens if any(t in s for s in stem_tokens)) / len(q_tokens)
    else:
        overlap = 0.0

    # Fuzzy full ratio for typos.
    ratio = difflib.SequenceMatcher(None, stem, q_raw).ratio()

    # Weighted blend, clipped to [0, 1].
    score = 0.55 * max(substring, overlap) + 0.45 * ratio
    return round(min(1.0, max(0.0, score)), 3)


# ────────────────── safe file reader ──────────────────

def read_text_file(
    path: Path,
    *,
    max_bytes: int = 200_000,
    encoding: str = "utf-8",
) -> str:
    """Return up to `max_bytes` of a text file, decoded best-effort.
    Raises ValueError if the suffix is not text-ish."""
    p = Path(path)
    if p.suffix.lower() not in TEXT_SUFFIXES:
        raise ValueError(f"refusing to read non-text file: {p.suffix or 'unknown'}")
    with p.open("rb") as f:
        raw = f.read(max_bytes + 1)
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    # Try a small chain of common encodings before giving up. Windows
    # tools often produce cp1252 / latin-1 while cross-platform tools use
    # UTF-8; we don't want a stray em-dash to mangle the whole excerpt.
    text: Optional[str] = None
    for enc in (encoding, "utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    if truncated:
        text += f"\n\n… (truncated at {max_bytes // 1000} KB)"
    return text
