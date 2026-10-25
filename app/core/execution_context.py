"""Typed state for one agent execution: identity, limits, counters, permissions, and the ledger.

The context is created per request and owned by the harness. Agents and graph nodes read it but
change it only through the harness, so every limit is enforced in one place.
"""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.budgets import BudgetCounters, ExecutionLimits
from app.llm.schemas import Usage

EventKind = Literal["step", "model_call", "tool_call", "correction", "policy", "approval", "final"]
EventStatus = Literal["ok", "error", "rejected", "pending"]

DEFAULT_PERMISSIONS = frozenset({"documents:read"})


class LedgerEvent(BaseModel):
    """One attempted operation: decisions, references, and validated tool arguments (user-scoped
    data such as a search query, never secrets). Raw prompts and model reasoning are not stored."""

    seq: int
    kind: EventKind
    name: str
    status: EventStatus
    error_code: str | None = None
    message: str | None = None
    latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


@dataclass
class ExecutionContext:
    tenant_id: str
    limits: ExecutionLimits
    allowed_tools: frozenset[str]
    permissions: frozenset[str] = DEFAULT_PERMISSIONS
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    execution_id: uuid.UUID = field(default_factory=uuid.uuid4)
    clock: Callable[[], float] = time.monotonic
    workflow_state: str = "created"
    counters: BudgetCounters = field(default_factory=BudgetCounters)
    ledger: list[LedgerEvent] = field(default_factory=list)
    trace_ids: list[str] = field(default_factory=list)
    approved_call_ids: set[str] = field(default_factory=set)
    cancel_reason: str | None = None
    # Called for every ledger event, e.g. to stream progress over SSE. Must not raise or block.
    listeners: list[Callable[[LedgerEvent], None]] = field(default_factory=list)
    elapsed_before_s: float = 0.0  # active time spent before a pause (graph resume)
    started_at: float = field(init=False)

    def __post_init__(self) -> None:
        self.started_at = self.clock() - self.elapsed_before_s

    @property
    def deadline_at(self) -> float:
        return self.started_at + self.limits.deadline_s

    def remaining_s(self) -> float:
        return self.deadline_at - self.clock()

    @property
    def cancelled(self) -> bool:
        return self.cancel_reason is not None

    def cancel(self, reason: str = "cancelled by caller") -> None:
        self.cancel_reason = reason

    def record(self, kind: EventKind, name: str, status: EventStatus, **fields: Any) -> LedgerEvent:
        event = LedgerEvent(seq=len(self.ledger), kind=kind, name=name, status=status, **fields)
        self.ledger.append(event)
        for listener in self.listeners:
            listener(event)
        return event

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe state for a checkpoint. Time waiting for a human does not count against
        the deadline: only elapsed active time is saved, and the clock restarts on resume."""
        c = self.counters
        return {
            "tenant_id": self.tenant_id,
            "request_id": self.request_id,
            "execution_id": str(self.execution_id),
            "limits": self.limits.model_dump(mode="json"),
            "allowed_tools": sorted(self.allowed_tools),
            "permissions": sorted(self.permissions),
            "approved_call_ids": sorted(self.approved_call_ids),
            "elapsed_s": self.clock() - self.started_at,
            "counters": {
                "steps": c.steps,
                "model_calls": c.model_calls,
                "tool_calls": c.tool_calls,
                "input_tokens": c.usage.input_tokens,
                "output_tokens": c.usage.output_tokens,
                "cost_usd": str(c.cost_usd),
                "cost_known": c.cost_known,
                "corrections": dict(c.corrections),
            },
            "ledger": [e.model_dump(mode="json") for e in self.ledger],
        }

    @classmethod
    def restore(
        cls, snap: dict[str, Any], clock: Callable[[], float] = time.monotonic
    ) -> "ExecutionContext":
        c = snap["counters"]
        return cls(
            tenant_id=snap["tenant_id"],
            request_id=snap["request_id"],
            execution_id=uuid.UUID(snap["execution_id"]),
            limits=ExecutionLimits.model_validate(snap["limits"]),
            allowed_tools=frozenset(snap["allowed_tools"]),
            permissions=frozenset(snap["permissions"]),
            approved_call_ids=set(snap["approved_call_ids"]),
            clock=clock,
            elapsed_before_s=snap["elapsed_s"],
            counters=BudgetCounters(
                steps=c["steps"],
                model_calls=c["model_calls"],
                tool_calls=c["tool_calls"],
                usage=Usage(input_tokens=c["input_tokens"], output_tokens=c["output_tokens"]),
                cost_usd=Decimal(c["cost_usd"]),
                cost_known=c["cost_known"],
                corrections=dict(c["corrections"]),
            ),
            ledger=[LedgerEvent.model_validate(e) for e in snap["ledger"]],
        )
