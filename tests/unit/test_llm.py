import json
from decimal import Decimal

import httpx
import pytest

from app.core.errors import (
    InvalidModelOutputError,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    RateLimitError,
)
from app.llm.provider import (
    FakeProvider,
    LLMRouter,
    OllamaProvider,
    OpenAICompatibleProvider,
    Route,
)
from app.llm.schemas import LLMRequest, LLMResponse, Message, ToolSpec, Usage
from app.llm.usage import estimate_cost

REQ = LLMRequest(messages=[Message(role="user", content="hi")], model="qwen3:8b")


def ollama_with(handler: httpx.MockTransport) -> OllamaProvider:
    return OllamaProvider(httpx.AsyncClient(transport=handler), "http://ollama")


async def test_ollama_parses_content_usage_and_tool_calls() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "qwen3:8b",
                "done_reason": "stop",
                "prompt_eval_count": 42,
                "eval_count": 7,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "calculator", "arguments": {"expression": "2+2"}}}
                    ],
                },
            },
        )

    req = REQ.model_copy(
        update={
            "json_schema": {"type": "object"},
            "tools": [
                ToolSpec(name="calculator", description="math", parameters={"type": "object"})
            ],
        }
    )
    resp = await ollama_with(httpx.MockTransport(handler)).chat(req)
    assert resp.usage == Usage(input_tokens=42, output_tokens=7)
    assert resp.tool_calls[0].name == "calculator"
    assert resp.tool_calls[0].arguments == {"expression": "2+2"}
    assert seen["think"] is False and seen["format"] == {"type": "object"}
    assert seen["tools"][0]["function"]["name"] == "calculator"  # type: ignore[index]


async def test_ollama_non_dict_arguments_are_kept_raw_not_guessed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "calculator", "arguments": "17% of 2340"}}
                    ],
                }
            },
        )

    call = (await ollama_with(httpx.MockTransport(handler)).chat(REQ)).tool_calls[0]
    assert call.arguments == {} and call.raw_arguments == "17% of 2340"


@pytest.mark.parametrize(
    ("status", "error", "transient"),
    [
        (429, RateLimitError, True),
        (500, ProviderUnavailableError, True),
        (503, ProviderUnavailableError, True),
        (404, ProviderError, False),
        (400, ProviderError, False),
    ],
)
async def test_http_status_mapping(status: int, error: type[Exception], transient: bool) -> None:
    provider = ollama_with(httpx.MockTransport(lambda r: httpx.Response(status, text="boom")))
    with pytest.raises(error) as info:
        await provider.chat(REQ)
    assert info.value.transient is transient  # type: ignore[attr-defined]


async def test_connect_error_and_timeout_are_transient() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ProviderUnavailableError):
        await ollama_with(httpx.MockTransport(refuse)).chat(REQ)
    with pytest.raises(ProviderTimeoutError):
        await ollama_with(httpx.MockTransport(slow)).chat(REQ)


async def test_non_json_body_is_invalid_output() -> None:
    provider = ollama_with(httpx.MockTransport(lambda r: httpx.Response(200, text="<html>")))
    with pytest.raises(InvalidModelOutputError):
        await provider.chat(REQ)


async def test_openai_compatible_parses_string_arguments_and_auth() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-test"
        return httpx.Response(
            200,
            json={
                "model": "gpt-x",
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "function": {
                                        "name": "calc",
                                        "arguments": '{"expression": "1+1"}',
                                    },
                                },
                                {"id": "c2", "function": {"name": "calc", "arguments": "not json"}},
                            ],
                        },
                    }
                ],
            },
        )

    provider = OpenAICompatibleProvider(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), "http://api/v1", "sk-test"
    )
    resp = await provider.chat(REQ)
    assert resp.tool_calls[0].arguments == {"expression": "1+1"}
    assert resp.tool_calls[1].arguments == {} and resp.tool_calls[1].raw_arguments == "not json"
    assert resp.usage.total_tokens == 13


class Flaky:
    """Fails with `error` for the first `failures` calls, then succeeds."""

    name = "flaky"

    def __init__(self, failures: int, error: Exception) -> None:
        self.calls, self.failures, self.error = 0, failures, error

    async def chat(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return LLMResponse(content="ok", model=request.model, provider=self.name, latency_ms=1)


def router(main: list[Route], retries: int = 1) -> LLMRouter:
    return LLMRouter({"main": main, "small": main}, max_retries=retries, retry_base_delay_s=0)


async def test_router_retries_transient_then_succeeds() -> None:
    flaky = Flaky(1, ProviderUnavailableError())
    resp = await router([Route(flaky, "m")]).chat("main", REQ)
    assert resp.content == "ok" and flaky.calls == 2 and resp.attempts == 2
    assert not resp.fallback_used


async def test_router_retries_are_bounded() -> None:
    flaky = Flaky(100, ProviderTimeoutError())
    with pytest.raises(ProviderTimeoutError):
        await router([Route(flaky, "m")], retries=2).chat("main", REQ)
    assert flaky.calls == 3


async def test_router_falls_back_on_transient_error() -> None:
    primary, backup = Flaky(100, ProviderUnavailableError()), Flaky(0, ProviderError())
    resp = await router([Route(primary, "local"), Route(backup, "hosted")]).chat("main", REQ)
    assert resp.fallback_used and resp.model == "hosted" and resp.attempts == 3


async def test_router_does_not_retry_or_fall_back_on_permanent_error() -> None:
    primary, backup = Flaky(100, ProviderError("model not found")), Flaky(0, ProviderError())
    with pytest.raises(ProviderError, match="model not found"):
        await router([Route(primary, "a"), Route(backup, "b")]).chat("main", REQ)
    assert primary.calls == 1 and backup.calls == 0


async def test_router_substitutes_model_per_role() -> None:
    fake = FakeProvider()
    r = LLMRouter({"main": [Route(fake, "big")], "small": [Route(fake, "tiny")]})
    await r.chat("small", REQ)
    assert fake.requests[-1].model == "tiny" and r.model_for("main") == "big"


def test_cost_local_is_zero_and_unknown_hosted_is_unavailable() -> None:
    usage = Usage(input_tokens=1000, output_tokens=500)
    local = estimate_cost("ollama", "qwen3:8b", usage)
    assert local.cost_usd == Decimal(0) and local.pricing_source == "local"
    hosted = estimate_cost("openai_compatible", "unknown-model", usage)
    assert hosted.cost_usd is None and hosted.pricing_source == "unavailable"
