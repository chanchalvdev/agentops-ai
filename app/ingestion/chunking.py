"""Chunking strategies.

`structured` (default) packs whole paragraphs, starts a new chunk at Markdown headings when the
current chunk is already substantial, and only splits a paragraph (by sentence, then by words)
when it alone exceeds the size. `fixed` is a naive sliding window kept as an experimental
baseline, so the benefit of structure-aware chunking is measured rather than assumed.

Chunks are character spans into the normalized text, so offsets, pages, and overlap stay exact.
"""

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Literal

from app.ingestion.parsers import PAGE_BREAK

_PARAGRAPH_SEP = re.compile(r"\n[ \t]*\n+|\f")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
CHUNK_ID_NAMESPACE = uuid.UUID("6f1f6a52-3c1e-4c55-9a43-0d2b5c8a7e10")


@dataclass(frozen=True)
class ChunkingConfig:
    strategy: Literal["structured", "fixed"] = "structured"
    size: int = 1200
    overlap: int = 200

    def __post_init__(self) -> None:
        if not 0 <= self.overlap < self.size:
            raise ValueError("overlap must be >= 0 and smaller than size")

    @property
    def key(self) -> str:
        return f"{self.strategy}:{self.size}:{self.overlap}"


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    char_start: int
    char_end: int
    section: str | None
    page_start: int | None
    page_end: int | None

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


@dataclass(frozen=True)
class _Unit:
    start: int
    end: int
    section: str | None
    is_heading: bool = False

    @property
    def length(self) -> int:
        return self.end - self.start


def stable_chunk_id(document_id: uuid.UUID, config: ChunkingConfig, chunk: Chunk) -> uuid.UUID:
    """Same document + config + content -> same ID, so re-ingestion and citations stay stable."""
    return uuid.uuid5(
        CHUNK_ID_NAMESPACE, f"{document_id}:{config.key}:{chunk.index}:{chunk.text_hash}"
    )


def _trimmed(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _paragraphs(text: str) -> list[_Unit]:
    units, section, pos = [], None, 0
    for sep in [*_PARAGRAPH_SEP.finditer(text), None]:
        end = sep.start() if sep else len(text)
        start, stop = _trimmed(text, pos, end)
        if start < stop:
            first_line = text[start:stop].split("\n", 1)[0]
            heading = _HEADING.match(first_line)
            if heading:
                section = heading.group(2)[:500]
            units.append(_Unit(start, stop, section, is_heading=bool(heading)))
        pos = sep.end() if sep else len(text)
    return units


def _split_large(text: str, unit: _Unit, size: int) -> list[_Unit]:
    """Split an oversized paragraph by sentences, then by word windows."""
    pieces: list[_Unit] = []
    pos = unit.start
    for boundary in [*_SENTENCE_END.finditer(text, unit.start, unit.end), None]:
        end = boundary.start() if boundary else unit.end
        if end > pos:
            pieces.append(_Unit(pos, end, unit.section))
        pos = boundary.end() if boundary else unit.end
    out: list[_Unit] = []
    for piece in pieces:
        start = piece.start
        while piece.end - start > size:
            cut = text.rfind(" ", start, start + size)
            cut = cut if cut > start else start + size
            out.append(_Unit(start, cut, unit.section))
            start, _ = _trimmed(text, cut, piece.end)
        if start < piece.end:
            out.append(_Unit(start, piece.end, unit.section))
    return out


def _page(text: str, pos: int, paged: bool) -> int | None:
    return text.count(PAGE_BREAK, 0, pos) + 1 if paged else None


def _to_chunks(text: str, spans: list[tuple[int, int, str | None]]) -> list[Chunk]:
    paged = PAGE_BREAK in text
    return [
        Chunk(
            index=i,
            text=text[s:e],
            char_start=s,
            char_end=e,
            section=section,
            page_start=_page(text, s, paged),
            page_end=_page(text, e - 1, paged),
        )
        for i, (s, e, section) in enumerate(spans)
    ]


def _structured(text: str, cfg: ChunkingConfig) -> list[Chunk]:
    units: list[_Unit] = []
    for unit in _paragraphs(text):
        units.extend(_split_large(text, unit, cfg.size) if unit.length > cfg.size else [unit])

    spans: list[tuple[int, int, str | None]] = []
    current: list[_Unit] = []
    new_from = 0  # index in `current` of the first unit not carried over as overlap

    def flush() -> None:
        if len(current) > new_from:
            sections = dict.fromkeys(u.section for u in current[new_from:] if u.section)
            label = " | ".join(sections)[:500] or None
            spans.append((current[0].start, current[-1].end, label))

    for unit in units:
        span = unit.end - current[0].start if current else unit.length
        heading_break = (
            unit.is_heading and current and current[-1].end - current[0].start >= cfg.size // 2
        )
        if current and (span > cfg.size or heading_break):
            flush()
            # Carry trailing units as overlap, unless that is a heading-aligned section break.
            carry: list[_Unit] = []
            if not unit.is_heading:
                for prev in reversed(current):
                    if (current[-1].end - prev.start) > cfg.overlap:
                        break
                    carry.insert(0, prev)
                if carry and unit.end - carry[0].start > cfg.size:
                    carry = []
            current, new_from = carry, len(carry)
        current.append(unit)
    flush()
    return _to_chunks(text, spans)


def _fixed(text: str, cfg: ChunkingConfig) -> list[Chunk]:
    spans: list[tuple[int, int, str | None]] = []
    start = 0
    while start < len(text):
        end = min(start + cfg.size, len(text))
        if end < len(text):
            cut = text.rfind(" ", start + cfg.size // 2, end)
            end = cut if cut > start else end
        s, e = _trimmed(text, start, end)
        if s < e:
            spans.append((s, e, None))
        if end >= len(text):
            break
        start = max(end - cfg.overlap, start + 1)
    return _to_chunks(text, spans)


def chunk_text(text: str, config: ChunkingConfig) -> list[Chunk]:
    if not text.strip():
        return []
    return _structured(text, config) if config.strategy == "structured" else _fixed(text, config)
