"""File validation, text extraction, and normalization.

Validation trusts content, not the client: the extension picks a parser, then the bytes must match
it (PDF magic number, no NUL bytes in text). Page breaks are kept as \\f so chunks can report pages.
"""

import io
import os
import re
import unicodedata
from dataclasses import dataclass

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.core.errors import DocumentParseError, EmptyDocumentError, UnsupportedFileTypeError

SUPPORTED_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".pdf": "application/pdf",
}
PAGE_BREAK = "\f"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")
# Zero-width space/word-joiner/BOM: invisible, so they only ever hide text (or injected prompts).
_INVISIBLE = re.compile("[\u200b\u2060\ufeff]")
_MANY_BLANK_LINES = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class ExtractedText:
    text: str  # normalized; pages joined with \f for PDFs
    mime_type: str
    page_count: int | None


def sanitize_filename(filename: str | None) -> str:
    name = os.path.basename((filename or "").replace("\\", "/"))
    name = _CONTROL.sub("", name).strip()
    if not name or name in {".", ".."}:
        raise UnsupportedFileTypeError("A file name is required")
    return name[:255]


def detect_mime_type(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext not in SUPPORTED_TYPES:
        allowed = ", ".join(sorted(SUPPORTED_TYPES))
        raise UnsupportedFileTypeError(
            f"Unsupported file type '{ext or 'none'}'. Allowed: {allowed}"
        )
    return SUPPORTED_TYPES[ext]


def normalize_text(text: str) -> str:
    """NFKC (fixes PDF ligatures like 'ﬁ'), unify newlines, drop control chars, trim whitespace."""
    pages = []
    for page in text.split(PAGE_BREAK):
        page = unicodedata.normalize("NFKC", page).replace("\r\n", "\n").replace("\r", "\n")
        page = _INVISIBLE.sub("", _CONTROL.sub("", page))
        page = "\n".join(line.rstrip() for line in page.split("\n"))
        pages.append(_MANY_BLANK_LINES.sub("\n\n", page).strip())
    return PAGE_BREAK.join(pages).strip(PAGE_BREAK + " \n")


def _extract_pdf(data: bytes) -> tuple[str, int]:
    if not data.startswith(b"%PDF-"):
        raise DocumentParseError("File has a .pdf extension but is not a PDF")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise DocumentParseError("Encrypted PDFs are not supported")
        pages = [page.extract_text() or "" for page in reader.pages]
    except (PdfReadError, ValueError, KeyError) as exc:
        raise DocumentParseError("Could not read PDF", detail=str(exc)) from exc
    return PAGE_BREAK.join(pages), len(pages)


def _extract_text_file(data: bytes) -> str:
    if b"\x00" in data:
        raise UnsupportedFileTypeError("File looks binary, not text")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DocumentParseError("Text files must be UTF-8 encoded") from exc


def extract_text(filename: str, data: bytes) -> ExtractedText:
    mime = detect_mime_type(filename)
    if mime == "application/pdf":
        raw, page_count = _extract_pdf(data)
    else:
        raw, page_count = _extract_text_file(data), None
    text = normalize_text(raw)
    if not text.replace(PAGE_BREAK, "").strip():
        hint = " (scanned PDF? OCR is not supported)" if page_count else ""
        raise EmptyDocumentError(f"Document contains no extractable text{hint}")
    return ExtractedText(text=text, mime_type=mime, page_count=page_count)
