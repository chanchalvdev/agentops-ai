import uuid

import pytest

from app.ingestion.chunking import ChunkingConfig, chunk_text, stable_chunk_id

DOC = (
    """# Title

Intro paragraph about the system.

## Performance

"""
    + " ".join(f"Sentence number {i} talks about latency and throughput." for i in range(40))
    + """

## Recovery

RPO is 5 minutes. RTO is 15 minutes.
"""
)


@pytest.mark.parametrize("strategy", ["structured", "fixed"])
def test_chunks_are_exact_spans_within_size(strategy: str) -> None:
    cfg = ChunkingConfig(strategy=strategy, size=300, overlap=60)  # type: ignore[arg-type]
    chunks = chunk_text(DOC, cfg)
    assert len(chunks) > 3
    for c in chunks:
        assert DOC[c.char_start : c.char_end] == c.text  # offsets are exact
        assert len(c.text) <= cfg.size
        assert c.text == c.text.strip()
    assert [c.index for c in chunks] == list(range(len(chunks)))


@pytest.mark.parametrize("strategy", ["structured", "fixed"])
def test_full_text_is_covered(strategy: str) -> None:
    chunks = chunk_text(DOC, ChunkingConfig(strategy=strategy, size=300, overlap=60))  # type: ignore[arg-type]
    covered = set()
    for c in chunks:
        covered.update(range(c.char_start, c.char_end))
    missing = [i for i in range(len(DOC)) if i not in covered and not DOC[i].isspace()]
    assert missing == []


def test_overlap_repeats_trailing_content_within_a_section() -> None:
    chunks = chunk_text(DOC, ChunkingConfig(size=300, overlap=120))
    perf = [c for c in chunks if c.section == "Performance"]
    assert len(perf) >= 2
    assert any(b.char_start < a.char_end for a, b in zip(perf, perf[1:], strict=False))


def test_structured_starts_new_chunk_at_heading_and_labels_section() -> None:
    chunks = chunk_text(DOC, ChunkingConfig(size=300, overlap=60))
    recovery = [c for c in chunks if "RPO is 5 minutes" in c.text]
    assert len(recovery) == 1
    assert recovery[0].text.startswith("## Recovery")
    assert recovery[0].section == "Recovery"


def test_chunk_spanning_sections_lists_all_of_them() -> None:
    text = "# A\n\nalpha text.\n\n# B\n\nbeta text."
    (chunk,) = chunk_text(text, ChunkingConfig(size=1000, overlap=0))
    assert chunk.section == "A | B"


def test_pages_are_tracked_from_form_feeds() -> None:
    text = "page one text\fpage two text\fpage three text"
    chunks = chunk_text(text, ChunkingConfig(size=200, overlap=0))
    assert len(chunks) == 1 and (chunks[0].page_start, chunks[0].page_end) == (1, 3)
    small = chunk_text(text, ChunkingConfig(size=20, overlap=0))
    assert [c.page_start for c in small] == [1, 2, 3]


def test_no_pages_for_plain_text() -> None:
    assert chunk_text("hello world", ChunkingConfig())[0].page_start is None


def test_oversized_paragraph_without_punctuation_is_hard_split() -> None:
    text = "word " * 500
    chunks = chunk_text(text.strip(), ChunkingConfig(size=200, overlap=0))
    assert all(len(c.text) <= 200 for c in chunks)
    assert len(chunks) >= 12


def test_empty_text_yields_no_chunks() -> None:
    assert chunk_text("   \n\n ", ChunkingConfig()) == []


def test_invalid_overlap_rejected() -> None:
    with pytest.raises(ValueError):
        ChunkingConfig(size=100, overlap=100)


def test_chunk_ids_are_stable_and_config_sensitive() -> None:
    doc_id = uuid.uuid4()
    cfg_a, cfg_b = ChunkingConfig(size=300, overlap=60), ChunkingConfig(size=400, overlap=60)
    first = [stable_chunk_id(doc_id, cfg_a, c) for c in chunk_text(DOC, cfg_a)]
    again = [stable_chunk_id(doc_id, cfg_a, c) for c in chunk_text(DOC, cfg_a)]
    other = [stable_chunk_id(doc_id, cfg_b, c) for c in chunk_text(DOC, cfg_b)]
    assert first == again
    assert len(set(first)) == len(first)
    assert not set(first) & set(other)
