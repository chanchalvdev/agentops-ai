"""Deterministic routing rules: decisions made in code so they need no model call.

These replace the small model's `needs_tools` flag, which was true on nearly every plain lookup in
the Day 3 baseline and cost one extra qwen3:8b call per question. A rule that misses falls back to
the normal path, so a false negative costs quality on that question, never safety.
"""

import re

_UNITS = r"(?:milli)?seconds?|ms|minutes?|hours?|days?|weeks?|months?|years?"
_CALCULATION = re.compile(
    rf"\b(?:in|to|into)\s+(?:{_UNITS})\s*[?.!]*$"  # "..., in seconds?"
    rf"|\bexpressed\s+in\s+(?:{_UNITS})\b"
    r"|\bhow\s+(?:much|many\s+\w+)\s+(?:shorter|longer|faster|slower|more|less|fewer|higher|lower)\b"
    r"|\bby\s+what\s+(?:factor|percent(?:age)?|ratio)\b"
    r"|\bwhat\s+(?:is|was)\s+the\s+(?:ratio|sum|percentage)\b"
    r"|\d\s*[-+*/×x]\s*\d",
    re.IGNORECASE,
)
_METADATA = re.compile(
    r"\b(?:metadata|document\s+id|which\s+version|latest\s+version\s+of|indexed\s+status)\b",
    re.IGNORECASE,
)
_COMPARISON = re.compile(
    r"\b(?:compare|comparison|change[ds]?|differ\w*|conflict\w*|between|versus|vs\.?)\b"
    r"|\bv\d+\b.*\bv\d+\b",
    re.IGNORECASE,
)


def needs_calculation(question: str) -> bool:
    return _CALCULATION.search(question) is not None


def needs_metadata(question: str) -> bool:
    return _METADATA.search(question) is not None


def is_comparison(question: str) -> bool:
    """Across documents or versions: the tool-selection step adds `document_metadata`, which tells
    the model which passage belongs to which version. Measured: with rule routing skipping it,
    `multi-availability` failed verification 3/3 (baseline passed 3/3, always with that call)."""
    return _COMPARISON.search(question) is not None


def is_simple_lookup(question: str) -> bool:
    """One fact from one place: safe to try on the small model, because verify_answer still
    checks the draft on the main model and a rejected draft is revised there."""
    return not (needs_calculation(question) or is_comparison(question))
