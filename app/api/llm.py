from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import PrincipalDep, ServicesDep
from app.llm.schemas import LLMRequest, Message, ModelRole, Usage
from app.llm.usage import estimate_cost

router = APIRouter(prefix="/api/v1", tags=["llm"])


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=20_000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=50)
    role: ModelRole = "main"
    json_schema: dict[str, Any] | None = Field(
        default=None, description="Constrain the reply to this JSON Schema"
    )
    max_output_tokens: int = Field(default=512, ge=1, le=2048)
    think: bool = False


class ChatResponse(BaseModel):
    content: str
    model: str
    provider: str
    usage: Usage
    cost_usd: Decimal | None
    pricing_source: str
    latency_ms: float
    attempts: int
    fallback_used: bool


class ModelInfo(BaseModel):
    role: str
    name: str
    installed: bool


@router.post(
    "/llm/chat",
    response_model=ChatResponse,
    summary="Direct, bounded model call with usage, cost, and latency metadata",
)
async def chat(body: ChatRequest, services: ServicesDep, principal: PrincipalDep) -> ChatResponse:
    s = services.settings
    response = await services.llm.chat(
        body.role,
        LLMRequest(
            messages=[Message(role=m.role, content=m.content) for m in body.messages],
            json_schema=body.json_schema,
            max_output_tokens=body.max_output_tokens,
            think=body.think,
            timeout_s=s.llm_timeout_s,
        ),
    )
    cost = estimate_cost(response.provider, response.model, response.usage)
    return ChatResponse(
        content=response.content,
        model=response.model,
        provider=response.provider,
        usage=response.usage,
        cost_usd=cost.cost_usd,
        pricing_source=cost.pricing_source,
        latency_ms=round(response.latency_ms, 1),
        attempts=response.attempts,
        fallback_used=response.fallback_used,
    )


@router.get(
    "/models",
    response_model=list[ModelInfo],
    summary="Configured model per role and whether Ollama has it installed",
)
async def models(services: ServicesDep, principal: PrincipalDep) -> list[ModelInfo]:
    s = services.settings
    installed: set[str] = set()
    try:
        resp = await services.http.get(f"{s.ollama_base_url}/api/tags", timeout=3)
        resp.raise_for_status()
        installed = {m["name"] for m in resp.json().get("models", [])}
    except Exception:  # noqa: S110 - report as not installed rather than failing the listing
        pass
    roles = {"main": s.llm_model_main, "small": s.llm_model_small, "embedding": s.embedding_model}
    return [
        ModelInfo(role=r, name=n, installed=n in installed or f"{n}:latest" in installed)
        for r, n in roles.items()
    ]
