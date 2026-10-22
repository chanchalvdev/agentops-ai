"""Provider-neutral request/response contracts for chat models."""

from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]
ModelRole = Literal["main", "small"]  # resolved to concrete model names by the router


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    # Set when the provider returned arguments we could not parse as a JSON object. The harness
    # rejects such calls instead of guessing.
    raw_arguments: str | None = None


class Message(BaseModel):
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None  # tool name for role="tool"


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class LLMRequest(BaseModel):
    messages: list[Message]
    model: str = ""  # filled in by LLMRouter from the requested role
    tools: list[ToolSpec] = Field(default_factory=list)
    json_schema: dict[str, Any] | None = None  # constrain output to this schema when supported
    temperature: float = 0.0
    max_output_tokens: int = 1024
    think: bool = False  # qwen3 reasoning mode; off by default to save tokens and latency
    timeout_s: float = 120.0


class LLMResponse(BaseModel):
    content: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    model: str
    provider: str
    latency_ms: float
    finish_reason: str | None = None
    attempts: int = 1  # total provider attempts incl. retries and fallbacks
    fallback_used: bool = False
