import pytest

from app.core.errors import (
    DocumentParseError,
    EmptyDocumentError,
    UnsupportedFileTypeError,
)
from app.ingestion.parsers import extract_text, normalize_text, sanitize_filename
from tests.conftest import make_pdf


def test_markdown_and_text_extraction() -> None:
    md = extract_text("notes.MD", b"# Title\r\n\r\nBody  \r\n")
    assert md.mime_type == "text/markdown" and md.text == "# Title\n\nBody"
    assert md.page_count is None
    assert extract_text("a.txt", "\ufeffcaf\u00e9".encode()).text == "café"  # BOM stripped


def test_pdf_pages_are_separated_by_form_feed() -> None:
    result = extract_text("doc.pdf", make_pdf(["First page", "Second page"]))
    assert result.mime_type == "application/pdf" and result.page_count == 2
    assert result.text.split("\f") == ["First page", "Second page"]


@pytest.mark.parametrize("name", ["x.exe", "x.docx", "noext", "x.pdf.sh"])
def test_unsupported_extensions(name: str) -> None:
    with pytest.raises(UnsupportedFileTypeError):
        extract_text(name, b"hello")


def test_pdf_extension_with_non_pdf_bytes() -> None:
    with pytest.raises(DocumentParseError):
        extract_text("fake.pdf", b"#!/bin/sh\nrm -rf /")


def test_corrupt_pdf() -> None:
    with pytest.raises(DocumentParseError):
        extract_text("broken.pdf", b"%PDF-1.4\ngarbage")


def test_binary_disguised_as_text() -> None:
    with pytest.raises(UnsupportedFileTypeError):
        extract_text("image.txt", b"\x89PNG\r\n\x1a\n\x00\x00")


def test_non_utf8_text() -> None:
    with pytest.raises(DocumentParseError):
        extract_text("latin1.txt", "café".encode("latin-1"))


@pytest.mark.parametrize("content", [b"", b"   \n\n\t ", "\u200b".encode()])
def test_empty_documents(content: bytes) -> None:
    with pytest.raises(EmptyDocumentError):
        extract_text("empty.txt", content)


def test_pdf_without_text_is_empty() -> None:
    with pytest.raises(EmptyDocumentError, match="OCR"):
        extract_text("scan.pdf", make_pdf([""]))


def test_normalization() -> None:
    raw = "ﬁle\x07name\r\n\n\n\n\nnext   \f  page two  "
    assert normalize_text(raw) == "filename\n\nnext\fpage two"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd.txt", "passwd.txt"),
        ("C:\\Users\\me\\doc.md", "doc.md"),
        ("re\x00port.pdf", "report.pdf"),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "..", "/"])
def test_sanitize_filename_rejects_empty(raw: str | None) -> None:
    with pytest.raises(UnsupportedFileTypeError):
        sanitize_filename(raw)
