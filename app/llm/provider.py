"""Chat model providers and the router that adds retries and fallback.

Providers translate between our contracts (schemas.py) and one vendor API, and map transport
failures onto typed errors. They do not retry: retry and fallback policy lives in LLMRouter so it
is applied identically to every provider.
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.core.errors import (
    AppError,
    InvalidModelOutputError,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    RateLimitError,
)
from app.core.retry import is_transient, retry_async
from app.llm.schemas import LLMRequest, LLMResponse, Message, ModelRole, ToolCall, Usage

logger = logging.getLogger(__name__)


class LLMProvider(Protocol):
    name: str

    async def chat(self, request: LLMRequest) -> LLMResponse: ...


def _map_http_error(provider: str, exc: Exception) -> AppError:
    if isinstance(exc, httpx.TimeoutException):
        return ProviderTimeoutError(f"{provider} timed out", detail=str(exc))
    if isinstance(exc, httpx.TransportError):  # connection refused, DNS, reset
        return ProviderUnavailableError(f"{provider} is unreachable", detail=str(exc))
    if isinstance(exc, httpx.HTTPStatusError):
        status, body = exc.response.status_code, exc.response.text[:500]
        if status == 429:
            return RateLimitError(f"{provider} rate limited the request", detail=body)
        if status >= 500:
            return ProviderUnavailableError(f"{provider} returned {status}", detail=body)
        return ProviderError(f"{provider} rejected the request ({status})", detail=body)
    return ProviderError(f"{provider} call failed", detail=repr(exc))


async def _post_json(
    client: httpx.AsyncClient,
    provider: str,
    url: str,
    payload: dict[str, Any],
    timeout_s: float,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        resp = await client.post(url, json=payload, timeout=timeout_s, headers=headers)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data
    except (httpx.HTTPError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise InvalidModelOutputError(f"{provider} returned non-JSON", detail=str(exc)) from exc
        raise _map_http_error(provider, exc) from exc


def _tool_payload(request: LLMRequest) -> list[dict[str, Any]]:
    return [{"type": "function", "function": t.model_dump()} for t in request.tools]


class OllamaProvider:
    """Native Ollama /api/chat. Supports JSON-schema `format`, tools, and `think`."""

    name = "ollama"

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._client, self._base = client, base_url.rstrip("/")

    @staticmethod
    def _message(m: Message) -> dict[str, Any]:
        out: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            out["tool_calls"] = [
                {"function": {"name": c.name, "arguments": c.arguments}} for c in m.tool_calls
            ]
        if m.role == "tool" and m.name:
            out["tool_name"] = m.name
        return out

    async def chat(self, request: LLMRequest) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [self._message(m) for m in request.messages],
            "stream": False,
            "think": request.think,
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_output_tokens,
            },
        }
        if request.tools:
            payload["tools"] = _tool_payload(request)
        if request.json_schema:
            payload["format"] = request.json_schema
        start = time.perf_counter()
        data = await _post_json(
            self._client, self.name, f"{self._base}/api/chat", payload, request.timeout_s
        )
        latency_ms = (time.perf_counter() - start) * 1000
        msg = data.get("message") or {}
        calls = []
        for i, call in enumerate(msg.get("tool_calls") or []):
            fn = call.get("function") or {}
            args = fn.get("arguments")
            calls.append(
                ToolCall(
                    id=call.get("id") or f"call_{i}",
                    name=fn.get("name", ""),
                    arguments=args if isinstance(args, dict) else {},
                    raw_arguments=None if isinstance(args, dict) else str(args),
                )
            )
        return LLMResponse(
            content=msg.get("content") or "",
            tool_calls=calls,
            usage=Usage(
                input_tokens=data.get("prompt_eval_count") or 0,
                output_tokens=data.get("eval_count") or 0,
            ),
            model=data.get("model", request.model),
            provider=self.name,
            latency_ms=latency_ms,
            finish_reason=data.get("done_reason"),
        )


class OpenAICompatibleProvider:
    """Any /v1/chat/completions endpoint (OpenAI, Groq, OpenRouter, vLLM, LM Studio...)."""

    name = "openai_compatible"

    def __init__(self, client: httpx.AsyncClient, base_url: str, api_key: str) -> None:
        self._client, self._base, self._key = client, base_url.rstrip("/"), api_key

    @staticmethod
    def _message(m: Message) -> dict[str, Any]:
        out: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            out["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                }
                for c in m.tool_calls
            ]
        if m.role == "tool":
            out["tool_call_id"] = m.tool_call_id
        return out

    async def chat(self, request: LLMRequest) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [self._message(m) for m in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
        }
        if request.tools:
            payload["tools"] = _tool_payload(request)
        if request.json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": request.json_schema},
            }
        start = time.perf_counter()
        data = await _post_json(
            self._client,
            self.name,
            f"{self._base}/chat/completions",
            payload,
            request.timeout_s,
            headers={"Authorization": f"Bearer {self._key}"},
        )
        latency_ms = (time.perf_counter() - start) * 1000
        try:
            choice = data["choices"][0]
        except (KeyError, IndexError) as exc:
            raise InvalidModelOutputError("provider returned no choices") from exc
        msg = choice.get("message") or {}
        calls = []
        for call in msg.get("tool_calls") or []:
            raw = (call.get("function") or {}).get("arguments") or "{}"
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            calls.append(
                ToolCall(
                    id=call.get("id", ""),
                    name=(call.get("function") or {}).get("name", ""),
                    arguments=parsed if isinstance(parsed, dict) else {},
                    raw_arguments=None if isinstance(parsed, dict) else raw,
                )
            )
        usage = data.get("usage") or {}
        return LLMResponse(
            content=msg.get("content") or "",
            tool_calls=calls,
            usage=Usage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
            ),
            model=data.get("model", request.model),
            provider=self.name,
            latency_ms=latency_ms,
            finish_reason=choice.get("finish_reason"),
        )


class FakeProvider:
    """Deterministic provider for tests and CI. `responder` builds each reply from the request."""

    name = "fake"

    def __init__(self, responder: Callable[[LLMRequest], LLMResponse | str] | None = None) -> None:
        self._responder = responder or (lambda req: "fake response")
        self.requests: list[LLMRequest] = []

    async def chat(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        out = self._responder(request)
        if isinstance(out, LLMResponse):
            return out
        prompt_chars = sum(len(m.content) for m in request.messages)
        return LLMResponse(
            content=out,
            model=request.model,
            provider=self.name,
            latency_ms=1.0,
            usage=Usage(input_tokens=prompt_chars // 4, output_tokens=len(out) // 4),
        )


@dataclass(frozen=True)
class Route:
    provider: LLMProvider
    model: str


class LLMRouter:
    """Resolves a model role to an ordered list of routes and applies retry + fallback.

    Fallback happens only on transient errors (unreachable, timeout, 5xx, 429). A bad answer is
    a quality problem, not an availability problem, so it never triggers fallback.
    """

    def __init__(
        self,
        routes: dict[ModelRole, list[Route]],
        max_retries: int = 1,
        retry_base_delay_s: float = 0.5,
    ) -> None:
        if not all(routes.get(r) for r in ("main", "small")):
            raise ValueError("routes must define at least one route for 'main' and 'small'")
        self._routes, self._max_retries = routes, max_retries
        self._base_delay = retry_base_delay_s

    def model_for(self, role: ModelRole) -> str:
        return self._routes[role][0].model

    async def chat(self, role: ModelRole, request: LLMRequest) -> LLMResponse:
        attempts = 0
        last_exc: BaseException | None = None
        for index, route in enumerate(self._routes[role]):
            routed = request.model_copy(update={"model": route.model})

            async def call(route: Route = route, routed: LLMRequest = routed) -> LLMResponse:
                nonlocal attempts
                attempts += 1
                return await route.provider.chat(routed)

            try:
                response = await retry_async(
                    call,
                    max_retries=self._max_retries,
                    base_delay_s=self._base_delay,
                    operation=f"llm.{route.provider.name}",
                )
            except Exception as exc:
                if not is_transient(exc):
                    raise
                last_exc = exc
                logger.warning(
                    "llm route failed, trying next",
                    extra={
                        "provider": route.provider.name,
                        "model": route.model,
                        "error": type(exc).__name__,
                    },
                )
                continue
            return response.model_copy(update={"attempts": attempts, "fallback_used": index > 0})
        assert last_exc is not None
        raise last_exc
