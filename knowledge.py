"""
Jarvis knowledge base — RAG over the user's own documents.

Flow:
  1. Ingest: user says "learn <path>" with a .pdf / .docx / .xlsx /
     .txt / .md file. We extract text, split into overlapping chunks,
     embed each chunk with Gemini's text-embedding-004 model, and
     persist to knowledge_store.json next to config.
  2. Query: dispatcher calls `search(query, k)` BEFORE sending to the
     chat LLM. If top matches clear the similarity threshold, we
     return them so the LLM gets the user's own document text as
     context — grounding answers in facts the user already uploaded.

Design decisions:
  * No vector DB. For the tens-to-hundreds of chunks a single user
    accumulates, a plain in-memory numpy matrix is faster than any
    client/server DB and ships with zero infra.
  * No sentence-transformers. We already pay for Gemini; its 768-dim
    embedding model is good enough and keeps the dependency surface
    tiny. (Local fallback could be added later.)
  * JSON persistence. 768 floats × 4 bytes ≈ 3 KB per chunk. 500
    chunks ≈ 1.5 MB — JSON is fine, no need for pickle.
  * Document identity = source file basename. If you re-ingest the
    same filename it replaces the previous version (automatic
    "updated protocol" behaviour).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np


EMBED_MODEL = "gemini-embedding-001"
# Model's native dim is 3072. We request 768 via outputDimensionality
# to keep the JSON store ~4x smaller — plenty of resolution for RAG
# over personal documents, and faster cosine-sim at query time.
# NOTE: ollama's nomic-embed-text is also 768-dim so switching backends
# doesn't invalidate any pre-existing vectors of the same length.
EMBED_DIM = 768
STORE_FILENAME = "knowledge_store.json"
DEFAULT_CHUNK_SIZE = 900     # chars
DEFAULT_CHUNK_OVERLAP = 150
MAX_CHUNKS_WARN = 2000
SIM_THRESHOLD = 0.45         # cosine-sim floor for "relevant enough"
# NOTE: local embed models (nomic-embed-text) tend to score ~0.1
# lower than Gemini's for the same semantic pair, so we start a bit
# more permissive. The LLM's "answer only from excerpts, don't invent"
# instruction prevents false-positives from this looser threshold.
TOP_K = 4


# ────────────────────── text extraction ──────────────────────
def _extract_pdf(path: Path) -> str:
    import pypdf
    reader = pypdf.PdfReader(str(path))
    parts: List[str] = []
    for i, page in enumerate(reader.pages, 1):
        try:
            txt = page.extract_text() or ""
        except Exception as exc:
            logging.warning("[kb] pdf page %d extract failed: %s", i, exc)
            continue
        txt = txt.strip()
        if txt:
            parts.append(f"[Page {i}]\n{txt}")
    return "\n\n".join(parts)


def _extract_docx(path: Path) -> str:
    import docx
    doc = docx.Document(str(path))
    parts: List[str] = []
    for para in doc.paragraphs:
        t = para.text.strip()
        if t:
            parts.append(t)
    # Tables too — medical protocols often use them.
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _extract_xlsx(path: Path) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
    parts: List[str] = []
    for sheet in wb.worksheets:
        parts.append(f"[Sheet: {sheet.title}]")
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None and str(c).strip()]
            if cells:
                parts.append(" | ".join(cells))
    wb.close()
    return "\n".join(parts)


def _extract_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


EXTRACTORS = {
    ".pdf":  _extract_pdf,
    ".docx": _extract_docx,
    ".xlsx": _extract_xlsx,
    ".xlsm": _extract_xlsx,
    ".txt":  _extract_text,
    ".md":   _extract_text,
    ".csv":  _extract_text,
    ".log":  _extract_text,
    ".json": _extract_text,
    ".yaml": _extract_text,
    ".yml":  _extract_text,
}


def extract_text(path: Path) -> str:
    """Dispatch to the right extractor by file extension. Raises
    ValueError for unsupported types with a helpful message so the
    user hears exactly what went wrong."""
    ext = path.suffix.lower()
    if ext not in EXTRACTORS:
        raise ValueError(
            f"I don't know how to read {ext} files. Supported: "
            + ", ".join(sorted(EXTRACTORS)))
    return EXTRACTORS[ext](path)


# ────────────────────── chunking ──────────────────────
_PARA_SPLIT = re.compile(r"\n\s*\n+")


def chunk(text: str, *, size: int = DEFAULT_CHUNK_SIZE,
          overlap: int = DEFAULT_CHUNK_OVERLAP) -> List[str]:
    """Paragraph-aware sliding window. We try to keep paragraphs whole
    when they fit; if a paragraph is huge, slide inside it with
    character overlap so no fact gets split across a chunk boundary
    without appearing in at least one chunk in full context."""
    text = text.strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in _PARA_SPLIT.split(text) if p.strip()]
    chunks: List[str] = []
    buf = ""
    for para in paragraphs:
        if len(para) > size:
            # Flush current buffer, then slide inside the giant para.
            if buf:
                chunks.append(buf.strip()); buf = ""
            start = 0
            step = max(1, size - overlap)
            while start < len(para):
                chunks.append(para[start:start + size])
                start += step
            continue
        if len(buf) + 2 + len(para) <= size:
            buf = f"{buf}\n\n{para}" if buf else para
        else:
            chunks.append(buf.strip())
            # Carry a tail of the previous chunk as overlap for context.
            tail = buf[-overlap:] if overlap and len(buf) > overlap else ""
            buf = (tail + "\n\n" + para).strip() if tail else para
    if buf.strip():
        chunks.append(buf.strip())
    return chunks


# ────────────────────── embeddings via Gemini ──────────────────────
class EmbeddingError(RuntimeError):
    pass


def embed_batch(texts: List[str], *, api_key: str = "",
                backend: str = "gemini",
                ollama_host: str = "http://localhost:11434",
                ollama_model: str = "nomic-embed-text",
                timeout: float = 30.0) -> np.ndarray:
    """Embed a list of strings. Routes to the right backend:
      backend='gemini'  → gemini-embedding-001 REST, 768-dim via
                          outputDimensionality. Needs an api_key.
      backend='ollama'  → local nomic-embed-text (or whatever model
                          name is passed), 768-dim native, zero RPM.
    Returns a (len(texts), EMBED_DIM) float32 matrix."""
    if not texts:
        return np.zeros((0, EMBED_DIM), dtype=np.float32)

    if backend == "ollama":
        import llm_ollama as _ol
        try:
            vecs = _ol.embed(texts, model=ollama_model,
                             host=ollama_host, timeout=timeout)
        except Exception as exc:
            raise EmbeddingError(
                f"local embed failed via Ollama ({ollama_model}): {exc}") from exc
        if any(len(v) != EMBED_DIM for v in vecs):
            got = {len(v) for v in vecs}
            raise EmbeddingError(
                f"ollama returned vectors with wrong dim: {got} "
                f"(expected {EMBED_DIM}). Try `ollama pull nomic-embed-text`.")
        return np.asarray(vecs, dtype=np.float32)

    # Gemini (default)
    url = (f"https://generativelanguage.googleapis.com/v1beta/"
           f"models/{EMBED_MODEL}:embedContent?key={api_key}")
    out = np.zeros((len(texts), EMBED_DIM), dtype=np.float32)
    for i, text in enumerate(texts):
        payload = {
            "content": {"parts": [{"text": text}]},
            "outputDimensionality": EMBED_DIM,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    body = json.loads(resp.read())
                vec = body.get("embedding", {}).get("values")
                if not vec or len(vec) != EMBED_DIM:
                    raise EmbeddingError(
                        f"unexpected embed shape: {len(vec) if vec else 0}")
                out[i] = np.asarray(vec, dtype=np.float32)
                break
            except Exception as exc:
                if attempt == 2:
                    raise EmbeddingError(
                        f"embed failed on chunk {i+1}/{len(texts)}: {exc}") from exc
                time.sleep(0.5 * (attempt + 1))
    return out


# ────────────────────── store ──────────────────────
@dataclass
class Chunk:
    doc: str              # document key (basename)
    chunk_id: int
    text: str
    embedding: List[float]


@dataclass
class SearchHit:
    doc: str
    chunk_id: int
    text: str
    score: float


@dataclass
class KnowledgeBase:
    """In-memory index + JSON persistence. Load once at dispatcher init,
    mutate during ingest/forget, flush on each mutation."""
    store_path: Path
    api_key: str = ""
    backend: str = "gemini"
    ollama_host:  str = "http://localhost:11434"
    ollama_model: str = "nomic-embed-text"
    docs: dict = field(default_factory=dict)
    # Matrix built lazily — invalidated on any mutation.
    _matrix: Optional[np.ndarray] = None
    _rows: List[Tuple[str, int]] = field(default_factory=list)

    def _embed(self, texts: List[str]) -> np.ndarray:
        return embed_batch(texts, api_key=self.api_key, backend=self.backend,
                           ollama_host=self.ollama_host,
                           ollama_model=self.ollama_model)

    # ── persistence ──
    @classmethod
    def load(cls, store_path: Path, api_key: str = "", **kwargs
             ) -> "KnowledgeBase":
        kb = cls(store_path=store_path, api_key=api_key, **kwargs)
        if store_path.exists():
            try:
                data = json.loads(store_path.read_text(encoding="utf-8"))
                kb.docs = data.get("docs", {})
            except Exception as exc:
                logging.warning("[kb] store unreadable (%s), starting fresh", exc)
                kb.docs = {}
        return kb

    def save(self) -> None:
        payload = {"docs": self.docs}
        self.store_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    # ── index ──
    def _invalidate(self) -> None:
        self._matrix = None
        self._rows = []

    def _build_matrix(self) -> None:
        rows: List[Tuple[str, int]] = []
        vecs: List[List[float]] = []
        for doc_key, doc in self.docs.items():
            for ch in doc.get("chunks", []):
                rows.append((doc_key, ch["chunk_id"]))
                vecs.append(ch["embedding"])
        if vecs:
            self._matrix = np.asarray(vecs, dtype=np.float32)
            # L2-normalise rows so cosine sim is just a dot product.
            norms = np.linalg.norm(self._matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self._matrix = self._matrix / norms
        else:
            self._matrix = np.zeros((0, EMBED_DIM), dtype=np.float32)
        self._rows = rows

    # ── mutations ──
    def ingest(self, path: Path, *, chunk_size: int = DEFAULT_CHUNK_SIZE,
               chunk_overlap: int = DEFAULT_CHUNK_OVERLAP) -> dict:
        """Extract, chunk, embed, persist. Returns a stats dict with
        {doc, pages_or_chars, chunks, bytes_added}."""
        path = Path(path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"{path} doesn't exist")
        text = extract_text(path)
        if not text.strip():
            raise ValueError(f"{path.name} looks empty after extraction")
        chunks = chunk(text, size=chunk_size, overlap=chunk_overlap)
        if len(chunks) > MAX_CHUNKS_WARN:
            logging.warning("[kb] %d chunks for %s — this will take a bit",
                            len(chunks), path.name)
        vecs = self._embed(chunks)
        doc_key = path.name
        self.docs[doc_key] = {
            "path": str(path),
            "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "char_count": len(text),
            "chunks": [
                {"chunk_id": i, "text": c, "embedding": vecs[i].tolist()}
                for i, c in enumerate(chunks)
            ],
        }
        self._invalidate()
        self.save()
        return {
            "doc": doc_key,
            "chars": len(text),
            "chunks": len(chunks),
        }

    def forget(self, doc_key: str) -> bool:
        """Remove a doc. `doc_key` can be the basename or a path.
        Returns True iff something was removed."""
        key = Path(doc_key).name
        removed = False
        for k in list(self.docs.keys()):
            if k.lower() == key.lower() or doc_key.lower() in k.lower():
                del self.docs[k]
                removed = True
        if removed:
            self._invalidate()
            self.save()
        return removed

    def clear(self) -> int:
        n = len(self.docs)
        self.docs.clear()
        self._invalidate()
        self.save()
        return n

    # ── queries ──
    def list_docs(self) -> List[dict]:
        return [{
            "doc": k,
            "chunks": len(v.get("chunks", [])),
            "chars": v.get("char_count", 0),
            "ingested_at": v.get("ingested_at", ""),
        } for k, v in self.docs.items()]

    def search(self, query: str, *, k: int = TOP_K,
               threshold: float = SIM_THRESHOLD) -> List[SearchHit]:
        """Cosine-sim top-K over all chunks. Threshold filters out
        matches too weak to be worth citing."""
        if self._matrix is None:
            self._build_matrix()
        if self._matrix is None or self._matrix.shape[0] == 0:
            return []
        q = self._embed([query])[0]
        qn = q / (np.linalg.norm(q) or 1.0)
        scores = self._matrix @ qn    # cosine similarity
        order = np.argsort(-scores)[:max(k, 1)]
        hits: List[SearchHit] = []
        for idx in order:
            s = float(scores[idx])
            if s < threshold:
                break
            doc_key, chunk_id = self._rows[idx]
            chunk_data = next(c for c in self.docs[doc_key]["chunks"]
                              if c["chunk_id"] == chunk_id)
            hits.append(SearchHit(doc=doc_key, chunk_id=chunk_id,
                                   text=chunk_data["text"], score=s))
        return hits


# ────────────────────── factory ──────────────────────
def build_from_config(config: dict) -> Optional[KnowledgeBase]:
    """Return a KB wired to the right embedding backend.
      backend='ollama' (local)  → no api_key needed; just needs the
                                   Ollama service running + the embed
                                   model pulled. Zero rate limits.
      backend='gemini'          → needs llm.api_key / GEMINI_API_KEY.
    Returns None only if the backend genuinely can't work (no key
    for gemini, which is the only hard requirement)."""
    llm_cfg = config.get("llm") or {}
    backend = str(llm_cfg.get("backend", "gemini")).lower()
    config_dir = Path(config.get("_config_dir", "."))
    store_path = config_dir / STORE_FILENAME

    if backend == "ollama":
        return KnowledgeBase.load(
            store_path,
            backend="ollama",
            ollama_host=llm_cfg.get("ollama_host", "http://localhost:11434"),
            ollama_model=llm_cfg.get("ollama_embed_model", "nomic-embed-text"),
        )

    api_key = llm_cfg.get("api_key") or os.environ.get("GEMINI_API_KEY", "")
    api_key = (api_key or "").strip()
    if not api_key:
        return None
    return KnowledgeBase.load(store_path, api_key=api_key, backend="gemini")


# ────────────────────── context formatting ──────────────────────
def format_context(hits: List[SearchHit]) -> str:
    """Compact citation block to prepend to the LLM user turn."""
    if not hits:
        return ""
    parts = []
    for h in hits:
        parts.append(f"[source: {h.doc} #{h.chunk_id} · similarity {h.score:.2f}]\n"
                     f"{h.text}")
    return "\n\n".join(parts)
