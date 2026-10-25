"""Execution limits and the counters checked against them.

Limits are configuration, not model instructions: the harness enforces them in code before every
model or tool call. A request may tighten the server defaults but never loosen them.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import BaseModel, Field

from app.core.config import Settings
from app.llm.schemas import Usage


class ExecutionLimits(BaseModel):
    max_steps: int = Field(default=8, ge=1)
    max_model_calls: int = Field(default=12, ge=1)
    max_tool_calls: int = Field(default=10, ge=0)
    max_corrections: int = Field(default=2, ge=0)  # per failure kind, not per execution
    max_total_tokens: int = Field(default=48_000, ge=1)
    max_cost_usd: Decimal | None = Field(default=None, gt=0)
    deadline_s: float = Field(default=180.0, gt=0)
    # The graph runs more, smaller nodes than the loop has turns, so it gets its own step limit.
    max_graph_steps: int = Field(default=20, ge=1)

    @classmethod
    def from_settings(cls, settings: Settings) -> "ExecutionLimits":
        return cls(
            max_steps=settings.agent_max_steps,
            max_model_calls=settings.agent_max_model_calls,
            max_tool_calls=settings.agent_max_tool_calls,
            max_corrections=settings.agent_max_corrections,
            max_total_tokens=settings.agent_max_total_tokens,
            max_cost_usd=settings.agent_max_cost_usd,
            deadline_s=settings.agent_deadline_s,
            max_graph_steps=settings.agent_max_graph_steps,
        )

    def tightened(self, **overrides: object) -> "ExecutionLimits":
        """Apply request overrides, keeping each limit at or below the server default."""
        merged = self.model_dump()
        unknown = set(overrides) - set(merged)
        if unknown:
            raise ValueError(f"unknown execution limits: {sorted(unknown)}")
        for key, value in overrides.items():
            if value is None:
                continue
            current = merged[key]
            merged[key] = value if current is None else min(current, value)
        return ExecutionLimits.model_validate(merged)


@dataclass
class BudgetCounters:
    steps: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    usage: Usage = field(default_factory=Usage)
    cost_usd: Decimal = Decimal(0)
    cost_known: bool = True  # False once any call had unavailable pricing
    corrections: dict[str, int] = field(default_factory=dict)
