"""Conservative page-aware chunking that preserves source page references."""
from __future__ import annotations
from dataclasses import dataclass
import re
from .extractor import PageText


@dataclass(frozen=True)
class Chunk:
    page: int
    ordinal: int
    text: str
    method: str


def chunk_pages(pages: list[PageText], *, max_chars: int = 2200, overlap: int = 160) -> list[Chunk]:
    if max_chars < 200 or overlap < 0 or overlap >= max_chars:
        raise ValueError("Require max_chars >= 200 and 0 <= overlap < max_chars.")
    chunks: list[Chunk] = []
    for page in pages:
        text = page.text.strip()
        if not text:
            continue
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if not paragraphs:
            paragraphs = [text]
        units: list[str] = []
        for paragraph in paragraphs:
            while len(paragraph) > max_chars:
                cut = paragraph.rfind(" ", 0, max_chars)
                cut = cut if cut > max_chars // 2 else max_chars
                units.append(paragraph[:cut].strip())
                paragraph = paragraph[max(0, cut - overlap):].strip()
            if paragraph:
                units.append(paragraph)
        packed: list[str] = []
        current = ""
        for paragraph in units:
            candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
            if len(candidate) > max_chars and current:
                packed.append(current)
                prefix = current[-overlap:] if overlap else ""
                current = f"{prefix}\n{paragraph}".strip() if prefix else paragraph
                # A near-limit paragraph plus overlap can exceed the cap; preserve the
                # paragraph intact rather than emitting an oversized combined chunk.
                if len(current) > max_chars:
                    packed.append(current[:max_chars])
                    current = current[max_chars-overlap:] if overlap else ""
            else:
                current = candidate
        if current:
            packed.append(current)
        for ordinal, unit in enumerate(packed):
            if unit:
                chunks.append(Chunk(page.page, ordinal, unit, page.method))
    return chunks

