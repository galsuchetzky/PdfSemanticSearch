"""Application services shared by CLI and desktop UI."""
from __future__ import annotations
from pathlib import Path
from typing import Callable
import threading
from .chunker import chunk_pages
from .extractor import extract_pdf
from .index_store import Embedder, IndexStore, fingerprint


class SearchService:
    def __init__(self, store: IndexStore | None = None, embedder: Embedder | None = None):
        self.store = store or IndexStore()
        self._embedder = embedder
        self._index_lock = threading.Lock()

    def index(self, path: str | Path, progress: Callable[[str, int, int], None] | None = None,
              cancel: Callable[[], bool] | None = None, semantic: bool = True) -> dict:
        if not self._index_lock.acquire(blocking=False):
            raise RuntimeError("An indexing job is already running. Wait for it to finish before starting another.")
        try:
            return self._index_document(path, progress, cancel, semantic)
        finally:
            self._index_lock.release()

    def _index_document(self, path: str | Path, progress: Callable[[str, int, int], None] | None = None,
              cancel: Callable[[], bool] | None = None, semantic: bool = True) -> dict:
        path = Path(path).expanduser().resolve(strict=True)
        before = fingerprint(path)
        pages = extract_pdf(path, progress=progress, cancel=cancel)
        chunks = chunk_pages(pages)
        vectors = None
        model = None
        warnings: list[str] = []
        ocr_candidates = sum(1 for page in pages if page.char_count < 80)
        ocr_pages = sum(p.method == "ocr" for p in pages)
        if ocr_candidates and not ocr_pages:
            warnings.append(f"{ocr_candidates} sparse page(s) may contain image text. Install Tesseract with heb and eng data for OCR.")
        if semantic:
            try:
                if progress:
                    progress("Loading local embedding model", 0, 0)
                if self._embedder is None:
                    self._embedder = Embedder()
                vectors, model = [], self._embedder.model_id
                batch_size = 32
                for start in range(0, len(chunks), batch_size):
                    if cancel and cancel():
                        raise InterruptedError("Indexing cancelled; previous index is unchanged.")
                    batch = self._embedder.embed([c.text for c in chunks[start:start+batch_size]])
                    vectors.extend(batch)
                    if progress:
                        progress("Embedding", min(start + len(batch), len(chunks)), len(chunks))
            except Exception as exc:
                # Keep local keyword search usable if first-run model setup fails.
                model = None
                vectors = None
                warnings.append(f"Semantic model unavailable; indexed for keyword search. {exc}")
        if cancel and cancel():
            raise InterruptedError("Indexing cancelled; previous index is unchanged.")
        if fingerprint(path) != before:
            raise RuntimeError("PDF changed while indexing. Please retry.")
        self.store.replace_document(path, before, len(pages), chunks, vectors, model, ocr_pages, ocr_candidates)
        if progress:
            progress("Ready", len(pages), len(pages))
        return {"path": str(path), "pages": len(pages), "chunks": len(chunks),
                "ocr_pages": ocr_pages, "ocr_candidates": ocr_candidates, "semantic": vectors is not None,
                "warning": " ".join(warnings) or None}

    def search(self, path: str | Path, query: str, limit: int = 5) -> list[dict]:
        status = self.store.status(path)
        if not status:
            raise ValueError("This PDF has not been indexed.")
        if status["stale"]:
            raise ValueError("This PDF changed since indexing. Reindex it before searching.")
        vector = None
        if status["model"]:
            try:
                if self._embedder is None:
                    self._embedder = Embedder()
            except Exception:
                # Stored text and FTS remain available if the model cache is removed.
                pass
            else:
                if status["model"] != self._embedder.model_id:
                    raise ValueError("The embedding model version changed. Reindex this PDF before searching.")
                try:
                    vector = self._embedder.embed([query])[0]
                except Exception:
                    pass
        return self.store.search(path, query, limit, vector)

