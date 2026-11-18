"""Evidence bookkeeping shared by the custom loop and the LangGraph workflow.

Tools return raw results; this module turns them into what the model sees and what the user gets:
- stable labels across many tool calls: document passages are S1, S2...; other tool results T1...
- duplicate passage removal: a chunk seen before keeps its label and is not re-sent in full
- fencing: every result is wrapped and escaped as untrusted data, separate from instructions
- citation validation: an answer may only cite labels that were actually shown to the model
"""

import json
import re
import uuid
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.rag.answer import Citation, escape_untrusted

CITATION_MARKER = re.compile(r"\[([ST]\d+)\]")


class FinalAnswer(BaseModel):
    """The FINAL outcome contract, delivered through the `final_answer` tool's arguments."""

    answer: str = Field(max_length=6000, description="The answer, citing sources inline as [S1].")
    citations: list[str] = Field(
        default_factory=list, max_length=20, description="Every source or tool result ID used."
    )
    insufficient_evidence: bool = Field(
        default=False, description="True when the sources do not contain the answer."
    )


class ToolReference(BaseModel):
    source_id: str
    tool: str
    output: dict[str, Any]


def _text_key(p: dict[str, Any]) -> str:
    # Same text in two different documents (e.g. v1 and v2 sharing a paragraph) stays two
    # sources: merging them would make a "latest version" answer cite only the older document.
    return f"{p['document_id']}:{' '.join(str(p['text']).split()).lower()}"


class EvidenceStore:
    def __init__(self, max_passage_chars: int = 1500) -> None:
        self._max_chars = max_passage_chars
        self.passages: dict[str, dict[str, Any]] = {}  # S-label -> passage
        self.tool_results: dict[str, ToolReference] = {}  # T-label -> result
        self._label_by_chunk: dict[uuid.UUID | str, str] = {}
        self._label_by_text: dict[str, str] = {}

    @property
    def labels(self) -> set[str]:
        return set(self.passages) | set(self.tool_results)

    def add_passages(self, passages: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
        """Register passages; returns (new labels, labels of passages already seen)."""
        new, seen = [], []
        for p in passages:
            text_key = _text_key(p)
            label = self._label_by_chunk.get(p["chunk_id"]) or self._label_by_text.get(text_key)
            if label:
                if label not in new:
                    seen.append(label)
                continue
            label = f"S{len(self.passages) + 1}"
            self.passages[label] = p
            self._label_by_chunk[p["chunk_id"]] = label
            self._label_by_text[text_key] = label
            new.append(label)
        return new, list(dict.fromkeys(seen))

    def add_tool_result(self, tool: str, output: dict[str, Any]) -> str:
        label = f"T{len(self.tool_results) + 1}"
        self.tool_results[label] = ToolReference(source_id=label, tool=tool, output=output)
        return label

    def render_passages(self, new: list[str], seen: list[str]) -> str:
        blocks = []
        for label in new:
            p = self.passages[label]
            page = f' page="{p["page_start"]}"' if p.get("page_start") else ""
            section = f' section="{escape_untrusted(p["section"])}"' if p.get("section") else ""
            text = escape_untrusted(str(p["text"])[: self._max_chars])
            blocks.append(
                f'<source id="{label}" file="{escape_untrusted(p["filename"])}"'
                f' version="{p["document_version"]}" document_id="{p["document_id"]}"'
                f"{page}{section}>\n{text}\n</source>"
            )
        if seen:
            blocks.append(f"(Already shown above, not repeated: {', '.join(seen)})")
        if not blocks:
            blocks.append("(No matching passages.)")
        return "<evidence>\n" + "\n".join(blocks) + "\n</evidence>"

    def render_tool_result(self, label: str) -> str:
        ref = self.tool_results[label]
        body = escape_untrusted(json.dumps(ref.output, default=str))
        return f'<tool_result id="{label}" tool="{ref.tool}">\n{body}\n</tool_result>'

    def recap(self, labels: list[str], max_chars: int) -> str:
        """One short fenced line per label, for evidence whose full turn was trimmed away."""
        per = max(80, max_chars // max(1, len(labels)) - 60)
        lines = []
        for label in labels:
            if (p := self.passages.get(label)) is not None:
                body = f'file="{escape_untrusted(p["filename"])}"> ' + escape_untrusted(
                    " ".join(str(p["text"]).split())[:per]
                )
            elif (ref := self.tool_results.get(label)) is not None:
                body = f'tool="{ref.tool}"> ' + escape_untrusted(
                    json.dumps(ref.output, default=str)[:per]
                )
            else:
                continue
            lines.append(f'<recap id="{label}" {body}</recap>')
        return "\n".join(lines)[:max_chars]

    def citations(self, labels: list[str]) -> list[Citation]:
        out = []
        for label in labels:
            p = self.passages.get(label)
            if p is None:
                continue
            out.append(
                Citation(
                    source_id=label,
                    chunk_id=str(p["chunk_id"]),
                    document_id=str(p["document_id"]),
                    filename=p["filename"],
                    section=p.get("section"),
                    page_start=p.get("page_start"),
                    page_end=p.get("page_end"),
                    snippet=str(p["text"])[:300],
                )
            )
        return out

    def references(self, labels: list[str]) -> list[ToolReference]:
        return [self.tool_results[label] for label in labels if label in self.tool_results]

    def to_state(self) -> dict[str, Any]:
        """JSON-safe snapshot for a LangGraph checkpoint."""
        return {
            "passages": self.passages,
            "tool_results": {k: v.model_dump(mode="json") for k, v in self.tool_results.items()},
        }

    @classmethod
    def from_state(cls, state: dict[str, Any] | None) -> "EvidenceStore":
        store = cls()
        for label, p in (state or {}).get("passages", {}).items():
            store.passages[label] = p
            store._label_by_chunk[p["chunk_id"]] = label
            store._label_by_text[_text_key(p)] = label
        for label, ref in (state or {}).get("tool_results", {}).items():
            store.tool_results[label] = ToolReference.model_validate(ref)
        return store


def cited_labels(answer: FinalAnswer) -> list[str]:
    return list(dict.fromkeys([*answer.citations, *CITATION_MARKER.findall(answer.answer)]))


def validate_final(answer: FinalAnswer, store: EvidenceStore) -> list[str]:
    """Problems that make an answer unverifiable. Empty list = citations are well-formed."""
    errors = []
    cited = set(cited_labels(answer))
    unknown = cited - store.labels
    if unknown:
        errors.append(f"cites IDs that were never shown: {sorted(unknown)}")
    if not answer.insufficient_evidence and not cited:
        errors.append("answer makes claims without citing any source")
    if not answer.answer.strip():
        errors.append("answer is empty")
    return errors


_THINK = re.compile(r"^\s*<think>.*?</think>", re.S)
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def _strip_wrapping(content: str) -> str:
    content = _THINK.sub("", content)
    match = _FENCE.match(content)
    return match.group(1) if match else content.strip()


def parse_final(arguments: dict[str, Any] | None, content: str) -> FinalAnswer:
    """FINAL comes from final_answer tool arguments, or JSON content as a fallback."""
    try:
        if arguments is not None:
            return FinalAnswer.model_validate(arguments)
        return FinalAnswer.model_validate_json(_strip_wrapping(content))
    except ValidationError as exc:
        fields = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'answer'}: {e['type']}" for e in exc.errors()
        )
        raise ValueError(f"final answer does not match the schema ({fields})") from None
