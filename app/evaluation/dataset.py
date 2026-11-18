"""Versioned evaluation dataset: one JSON object per line, validated strictly on load.

A case states only what a program can check: allowed final statuses, which documents must be
retrieved or cited, text the answer must (or must not) contain, which tools must run with which
results, which must never run, which actions must wait for a human, and what the workspace must
look like afterwards. The reference answer is context for the optional LLM judge only.
"""

import hashlib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.errors import InvalidRequestError

Category = Literal[
    "answerable", "multi_document", "unanswerable", "tool_use", "adversarial", "workspace"
]
Mode = Literal["graph", "loop"]


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a typo in a field name must not silently pass

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    category: Category
    question: str = Field(min_length=1, max_length=2000)
    dataset_version: str
    modes: list[Mode] = ["graph", "loop"]  # pydantic copies mutable defaults per instance
    reference_answer: str | None = None

    # expected behaviour (all optional; an evaluator with nothing to check is "not applicable")
    expected_status: list[str] = Field(default_factory=list)
    expected_sources: list[str] = Field(default_factory=list)  # filenames that must be retrieved
    required_citations: list[str] = Field(default_factory=list)  # filenames that must be cited
    must_contain: list[list[str]] = Field(default_factory=list)  # all groups; any alternative
    must_not_contain: list[str] = Field(default_factory=list)
    expected_tools: list[str] = Field(default_factory=list)  # each must succeed at least once
    expected_tools_any: list[str] = Field(default_factory=list)  # at least one must succeed
    expected_tool_args: dict[str, dict[str, Any]] = Field(default_factory=dict)
    expected_tool_results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    forbidden_tools: list[str] = Field(default_factory=list)  # never executed, never approved
    # Adversarial cases: the attack only counts as defended if one of these tools was actually
    # attempted (executed, refused, or sent for approval). A model that never tries leaves the
    # defence untested, so the case must not pass vacuously.
    must_attempt_any: list[str] = Field(default_factory=list)
    max_steps: int | None = Field(default=None, ge=1)

    # run setup
    allowed_tools: list[str] | None = None
    approval: Literal["approve", "reject"] | None = None  # decision the runner gives, if asked
    expect_approval_any: list[str] = Field(default_factory=list)
    workspace_files: dict[str, str] = Field(default_factory=dict)  # extra seed files
    expect_files: dict[str, list[str]] = Field(default_factory=dict)  # path -> required text
    expect_absent: list[str] = Field(default_factory=list)
    expect_unchanged: list[str] = Field(default_factory=list)

    @property
    def uses_workspace(self) -> bool:
        return bool(
            self.category == "workspace"
            or self.workspace_files
            or self.expect_files
            or self.expect_absent
            or self.expect_unchanged
            or "workspace" in self.question.lower()
        )


class Dataset(BaseModel):
    name: str
    version: str
    sha256: str
    cases: list[EvalCase]


def load_dataset(path: Path) -> Dataset:
    raw = path.read_bytes()
    cases: list[EvalCase] = []
    for n, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        try:
            cases.append(EvalCase.model_validate_json(line))
        except ValidationError as exc:
            raise InvalidRequestError(f"{path.name}:{n}: invalid case: {exc}") from None
    if not cases:
        raise InvalidRequestError(f"{path.name}: no cases")
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise InvalidRequestError(f"{path.name}: duplicate case ids")
    versions = {c.dataset_version for c in cases}
    if len(versions) != 1:
        raise InvalidRequestError(f"{path.name}: mixed dataset versions {sorted(versions)}")
    return Dataset(
        name=path.stem,
        version=versions.pop(),
        sha256=hashlib.sha256(raw).hexdigest(),
        cases=cases,
    )
