"""Vendor-neutral trace model and the sink interface every backend implements.

A trace is one agent execution; its id is the execution id, the same correlation id the API
returns, the ledger stores, and the logs carry (with the request id). Observations nest:
graph node / loop turn spans contain the model generations and tool spans that ran inside them.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

ObservationKind = Literal["span", "generation", "event"]
Level = Literal["DEFAULT", "WARNING", "ERROR"]


@dataclass
class Observation:
    id: uuid.UUID
    kind: ObservationKind
    name: str
    start: datetime
    end: datetime | None = None
    parent_id: uuid.UUID | None = None
    level: Level = "DEFAULT"
    status_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    input: Any = None
    output: Any = None
    # generations only
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class TraceRecord:
    id: uuid.UUID  # = execution id
    name: str
    start: datetime
    end: datetime
    source: str  # api | cli | eval
    terminal: bool  # False while the run waits for a human approval
    input: Any
    output: Any
    metadata: dict[str, Any]
    tags: list[str]
    observations: list[Observation]


@dataclass
class Score:
    trace_id: uuid.UUID
    name: str
    value: float
    comment: str | None = None


class TraceSink(Protocol):
    name: str

    def accepts(self, trace: TraceRecord) -> bool:
        """Routing/sampling decision, e.g. LangSmith only for evaluation runs."""
        ...

    async def export(self, trace: TraceRecord) -> None: ...

    async def score(self, score: Score, source: str) -> None: ...
