"""Native PDF text extraction with optional page-level OCR fallback."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import re
import unicodedata


@dataclass(frozen=True)
class PageText:
    page: int  # 1-based page number
    text: str
    method: str
    char_count: int


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    text = text.replace("\x00", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(re.sub(r"[\t\u00a0 ]+", " ", line).strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _extract_with_pymupdf(path: Path, ocr: bool, progress=None, cancel=None) -> list[PageText]:
    import fitz  # type: ignore[import-not-found]
    pages: list[PageText] = []
    with fitz.open(path) as pdf:
        if pdf.is_encrypted:
            raise ValueError("This PDF is encrypted. Unlock it in Acrobat, then try again.")
        for number, page in enumerate(pdf, start=1):
            if cancel and cancel():
                raise InterruptedError("Indexing cancelled; previous index is unchanged.")
            # Preserve PDF content-stream order. In the supplied RTL document,
            # sort=True reverses Hebrew word order on otherwise digital pages.
            native = normalize_text(page.get_text("text", sort=False))
            method = "native"
            text = native
            # Sparse pages are candidates; only adopt OCR when it adds meaningful text.
            if ocr and (len(native) < 80 or _looks_corrupt(native)):
                recognized = _ocr_page(page)
                if len(recognized) > max(len(native) * 1.4, len(native) + 40):
                    text, method = recognized, "ocr"
            pages.append(PageText(number, text, method, len(text)))
            if progress:
                progress("Reading pages", number, len(pdf))
    return pages


def _looks_corrupt(text: str) -> bool:
    if not text:
        return True
    bad = sum(ch == "\ufffd" or (ord(ch) < 32 and ch not in "\n\t") for ch in text)
    return bad / max(len(text), 1) > 0.02


def _ocr_page(page: object) -> str:
    try:
        import pytesseract  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]
        import io
        pix = page.get_pixmap(dpi=275, alpha=False)  # type: ignore[attr-defined]
        image = Image.open(io.BytesIO(pix.tobytes("png")))
        return normalize_text(pytesseract.image_to_string(image, lang="heb+eng"))
    except Exception:
        # Missing OCR binary/language data must not break native text indexing.
        return ""


def _extract_with_pypdf(path: Path, progress=None, cancel=None) -> list[PageText]:
    from pypdf import PdfReader
    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        raise ValueError("This PDF is encrypted. Unlock it in Acrobat, then try again.")
    results = []
    for i, page in enumerate(reader.pages, start=1):
        if cancel and cancel():
            raise InterruptedError("Indexing cancelled; previous index is unchanged.")
        text = normalize_text(page.extract_text() or "")
        results.append(PageText(i, text, "native", len(text)))
        if progress:
            progress("Reading pages", i, len(reader.pages))
    return results


def extract_pdf(path: str | Path, *, ocr: bool = True, progress=None, cancel=None) -> list[PageText]:
    """Extract each page, preferring PyMuPDF but supporting a pypdf fallback."""
    pdf_path = Path(path).expanduser().resolve(strict=True)
    if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        raise ValueError("Choose an existing PDF file.")
    try:
        return _extract_with_pymupdf(pdf_path, ocr, progress, cancel)
    except ImportError:
        return _extract_with_pypdf(pdf_path, progress, cancel)

