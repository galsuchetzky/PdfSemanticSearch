"""SQLite persistence, FTS5 retrieval, and optional semantic ranking."""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import struct
import threading
import time
import unicodedata
from .chunker import Chunk
from .paths import data_dir, model_dir, normalize_pdf_path


def default_data_dir() -> Path:
    return data_dir()


_fingerprint_cache: dict[str, tuple[tuple[int, int, int, int], str]] = {}
_fingerprint_lock = threading.Lock()


def fingerprint(path: str | Path, cached: bool = True) -> str:
    """SHA-256 of the file.

    With cached=True the hash is reused while the file's size, modification time,
    inode, and status-change time are all unchanged (a copy that preserves mtime
    still changes ctime/inode). Indexing passes cached=False to always re-read.
    """
    p = Path(path)
    stat = p.stat()
    key = str(p.resolve())
    signature = (stat.st_size, stat.st_mtime_ns, stat.st_ino, stat.st_ctime_ns)
    with _fingerprint_lock:
        previous = _fingerprint_cache.get(key)
    if cached and previous and previous[0] == signature:
        return previous[1]
    digest = hashlib.sha256()
    with p.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    value = digest.hexdigest()
    with _fingerprint_lock:
        _fingerprint_cache[key] = (signature, value)
    return value


def document_id(path: str | Path) -> str:
    return hashlib.sha256(normalize_pdf_path(path).encode("utf-8")).hexdigest()


def _pack(vector: list[float] | tuple[float, ...] | None) -> bytes | None:
    if vector is None:
        return None
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(blob: bytes | None) -> tuple[float, ...] | None:
    if blob is None or len(blob) % 4:
        return None
    return struct.unpack(f"<{len(blob) // 4}f", blob)


def _query_terms(query: str) -> list[str]:
    terms = re.findall(r"[\w\u0590-\u05ff]+", unicodedata.normalize("NFC", query), re.UNICODE)
    stop = {"איזה", "מה", "איך", "למה", "האם", "מומלץ", "גורם", "גורמת", "של", "את", "על", "עם", "הוא", "היא"}
    result: list[str] = []
    for term in terms:
        if term.lower() in stop:
            continue
        if term not in result:
            result.append(term)
        # Hebrew prefixes are attached to the following word. Keep both forms so
        # an inflected query such as "לפרפור" can match the base token "פרפור".
        if len(term) > 4 and term[0] in "ובכלמשה" and term[1:] not in result:
            result.append(term[1:])
    return result[:30]


def _base_terms(query: str) -> list[str]:
    """Question-free, prefix-light terms used to boost contiguous key phrases."""
    stop = {"איזה", "מה", "איך", "למה", "האם", "מומלץ", "גורם", "גורמת", "של", "את", "על", "עם", "הוא", "היא"}
    terms = re.findall(r"[\w\u0590-\u05ff]+", unicodedata.normalize("NFC", query).lower(), re.UNICODE)
    result = []
    for term in terms:
        if term in stop:
            continue
        if len(term) > 4 and term[0] in "ובכלמשה":
            term = term[1:]
        if term not in result:
            result.append(term)
    return result


class IndexStore:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else default_data_dir() / "search.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
              id TEXT PRIMARY KEY, path TEXT NOT NULL, name TEXT NOT NULL,
              fingerprint TEXT NOT NULL, page_count INTEGER NOT NULL,
              indexed_at REAL NOT NULL, model TEXT, state TEXT NOT NULL DEFAULT 'ready',
              schema_version INTEGER NOT NULL DEFAULT 1, ocr_pages INTEGER NOT NULL DEFAULT 0,
              ocr_candidates INTEGER NOT NULL DEFAULT 0, last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS chunks (
              id INTEGER PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
              page INTEGER NOT NULL, ordinal INTEGER NOT NULL, text TEXT NOT NULL,
              method TEXT NOT NULL, vector BLOB, UNIQUE(document_id,page,ordinal)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
              text, content='chunks', content_rowid='id', tokenize='unicode61 remove_diacritics 0'
            );
            CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
              INSERT INTO chunks_fts(rowid,text) VALUES(new.id,new.text);
            END;
            CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
              INSERT INTO chunks_fts(chunks_fts,rowid,text) VALUES('delete',old.id,old.text);
            END;
            CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
              INSERT INTO chunks_fts(chunks_fts,rowid,text) VALUES('delete',old.id,old.text);
              INSERT INTO chunks_fts(rowid,text) VALUES(new.id,new.text);
            END;
        """)
        existing = {row["name"] for row in self._db.execute("PRAGMA table_info(documents)")}
        migrations = {
            "schema_version": "INTEGER NOT NULL DEFAULT 1",
            "ocr_pages": "INTEGER NOT NULL DEFAULT 0",
            "ocr_candidates": "INTEGER NOT NULL DEFAULT 0",
            "last_error": "TEXT",
        }
        for name, declaration in migrations.items():
            if name not in existing:
                self._db.execute(f"ALTER TABLE documents ADD COLUMN {name} {declaration}")
        self._db.commit()

    def status(self, path: str | Path) -> dict | None:
        path = str(Path(path).expanduser().resolve())
        with self._lock:
            row = self._db.execute("SELECT * FROM documents WHERE id=?", (document_id(path),)).fetchone()
        if not row:
            return None
        # stale: True = changed or missing, False = verified, None = could not be
        # read (e.g. permission denied); the saved index stays searchable then.
        try:
            if not Path(path).is_file():
                return {**dict(row), "stale": True}
            current = fingerprint(path)
        except OSError:
            return {**dict(row), "stale": None}
        return {**dict(row), "stale": current != row["fingerprint"]}

    def replace_document(self, path: str | Path, fp: str, pages: int, chunks: list[Chunk],
                         vectors: list[list[float]] | None = None, model: str | None = None,
                         ocr_pages: int = 0, ocr_candidates: int = 0) -> None:
        path = str(Path(path).expanduser().resolve())
        docid = document_id(path)
        if vectors is not None and len(vectors) != len(chunks):
            raise ValueError("Embedding count must match chunk count.")
        with self._lock, self._db:
            # Single transaction: a failed reindex leaves the prior index intact.
            self._db.execute("DELETE FROM documents WHERE id=?", (docid,))
            self._db.execute("""INSERT INTO documents
                (id,path,name,fingerprint,page_count,indexed_at,model,state,schema_version,ocr_pages,ocr_candidates,last_error)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (docid, path, Path(path).name, fp, pages, time.time(), model, "ready", 1, ocr_pages, ocr_candidates, None))
            self._db.executemany("INSERT INTO chunks(document_id,page,ordinal,text,method,vector) VALUES(?,?,?,?,?,?)",
                [(docid, c.page, c.ordinal, c.text, c.method, _pack(vectors[i]) if vectors is not None else None)
                 for i, c in enumerate(chunks)])

    def search(self, path: str | Path, query: str, limit: int = 5,
               query_vector: list[float] | None = None) -> list[dict]:
        query = query.strip()
        if not query:
            return []
        docid = document_id(path)
        with self._lock:
            rows = self._db.execute("SELECT c.* FROM chunks c WHERE c.document_id=?", (docid,)).fetchall()
            lexical: dict[int, float] = {}
            # FTS5 query grammar is not exposed to the user; quote tokens and OR them.
            terms = _query_terms(query)
            if terms:
                match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms[:30])
                try:
                    found = self._db.execute("""SELECT c.id,bm25(chunks_fts) AS rank
                        FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid
                        WHERE chunks_fts MATCH ? AND c.document_id=? ORDER BY rank LIMIT 50""",
                        (match, docid)).fetchall()
                    lexical = {int(r["id"]): float(r["rank"]) for r in found}
                except sqlite3.OperationalError:
                    lexical = {}
        sem: dict[int, float] = {}
        if query_vector:
            qnorm = math.sqrt(sum(v*v for v in query_vector)) or 1.0
            for row in rows:
                vec = _unpack(row["vector"])
                if not vec or len(vec) != len(query_vector):
                    continue
                denom = qnorm * (math.sqrt(sum(v*v for v in vec)) or 1.0)
                sem[int(row["id"])] = sum(a*b for a,b in zip(query_vector,vec)) / denom
        # Reciprocal-rank fusion combines independent lexical and semantic rankings.
        lex_order = sorted(lexical, key=lexical.get)  # FTS5 bm25 is lower-is-better
        sem_order = sorted(sem, key=sem.get, reverse=True)
        base_terms = _base_terms(query)
        phrase_pairs = list(zip(base_terms, base_terms[1:]))
        phrase_matches: dict[int, int] = {}
        for row in rows:
            content = " ".join(row["text"].lower().split())
            count = sum(1 for left, right in phrase_pairs if f"{left} {right}" in content)
            if count:
                phrase_matches[int(row["id"])] = count
        phrase_order = sorted(phrase_matches, key=phrase_matches.get, reverse=True)
        ranks: dict[int, float] = {}
        for source_weight, ordered in ((2.0, lex_order), (4.0, phrase_order), (1.0, sem_order)):
            for rank, cid in enumerate(ordered, start=1):
                ranks[cid] = ranks.get(cid, 0.0) + source_weight / (60 + rank)
        by_id = {int(r["id"]): r for r in rows}
        # If embeddings are unavailable, lexical-only ranking still works.
        if not sem_order:
            ranks = {cid: 1.0 / (60 + rank) for rank,cid in enumerate(lex_order, start=1)}
        results: list[dict] = []
        seen_pages: set[int] = set()
        for cid, score in sorted(ranks.items(), key=lambda item:item[1], reverse=True):
            if cid not in by_id:
                continue
            row = by_id[cid]
            page = int(row["page"])
            if page in seen_pages:
                continue
            seen_pages.add(page)
            results.append({"text": row["text"], "page": page, "score": round(score, 6), "method": row["method"]})
            if len(results) >= limit:
                break
        return results

    def delete_document(self, path: str | Path) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM documents WHERE id=?", (document_id(path),))

    def list_documents(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT path,name,page_count,indexed_at,model FROM documents ORDER BY name COLLATE NOCASE").fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()


class Embedder:
    """Optional local FastEmbed adapter. Raises a useful error if unavailable."""
    MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    def __init__(self):
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise RuntimeError("Semantic embeddings need FastEmbed. Install packages from requirements.txt.") from exc
        import fastembed
        cache_dir = model_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Prefer an offline cache hit so warm local runs do not contact a model host.
        # If there is no cached model, allow FastEmbed's first-use download.
        try:
            self._model = TextEmbedding(model_name=self.MODEL, cache_dir=str(cache_dir), local_files_only=True)
        except ValueError:
            self._model = TextEmbedding(model_name=self.MODEL, cache_dir=str(cache_dir))
        self.model_id = f"{self.MODEL}|fastembed-{fastembed.__version__}|mean-pooling"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._model.embed(texts)]

