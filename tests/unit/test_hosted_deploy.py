"""The hosted deployment path: OpenAI-compatible primary model, in-process embeddings, hosted
Postgres URLs, command execution switched off, and the UI served by the API."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.core.errors import CommandNotAllowedError, EmbeddingError, RateLimitError
from app.core.retry import retry_async
from app.db.session import asyncpg_url, psycopg_conninfo
from app.ingestion.embeddings import FastEmbedEmbedder
from app.llm.provider import (
    FakeProvider,
    LLMRouter,
    OpenAICompatibleProvider,
    Route,
    _retry_after,
    provider_label,
)
from app.llm.schemas import LLMRequest, Message
from app.main import create_app
from app.services import build_router
from app.tools.workspace import workspace_tools
from app.workspace.audit import MemoryAudit
from app.workspace.jail import Actor, WorkspaceManager

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
OK = {"model": "m", "choices": [{"message": {"content": '{"answer": "x"}'}}], "usage": {}}


def capture() -> tuple[list[dict[str, Any]], httpx.AsyncClient]:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=OK)

    return sent, httpx.AsyncClient(transport=httpx.MockTransport(handler))


def structured(system: str | None = "Be brief.") -> LLMRequest:
    messages = [Message(role="user", content="q")]
    if system is not None:
        messages.insert(0, Message(role="system", content=system))
    return LLMRequest(messages=messages, model="m", json_schema=SCHEMA)


# --- OpenAI-compatible provider ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "label"),
    [
        ("https://api.groq.com/openai/v1", "groq"),
        ("https://openrouter.ai/api/v1", "openrouter"),
        ("https://api.cerebras.ai/v1", "cerebras"),
    ],
)
def test_provider_label_names_the_vendor(url: str, label: str) -> None:
    assert provider_label(url) == label


async def test_schema_mode_sends_json_schema_response_format() -> None:
    sent, http = capture()
    await OpenAICompatibleProvider(http, "https://api.groq.com/openai/v1", "k").chat(structured())
    assert sent[0]["response_format"]["type"] == "json_schema"
    assert sent[0]["messages"][0]["content"] == "Be brief."


async def test_object_mode_puts_the_schema_in_the_system_prompt() -> None:
    sent, http = capture()
    provider = OpenAICompatibleProvider(http, "http://x/v1", "k", json_mode="object")
    await provider.chat(structured())
    assert sent[0]["response_format"] == {"type": "json_object"}
    system = sent[0]["messages"][0]
    assert system["role"] == "system" and system["content"].startswith("Be brief.")
    assert "JSON" in system["content"] and json.dumps(SCHEMA) in system["content"]
    assert len(sent[0]["messages"]) == 2


async def test_object_mode_adds_a_system_message_when_there_is_none() -> None:
    sent, http = capture()
    provider = OpenAICompatibleProvider(http, "http://x/v1", "k", json_mode="object")
    await provider.chat(structured(system=None))
    assert [m["role"] for m in sent[0]["messages"]] == ["system", "user"]


async def test_plain_requests_have_no_response_format_and_pass_reasoning_effort() -> None:
    sent, http = capture()
    provider = OpenAICompatibleProvider(http, "http://x/v1", "k", reasoning_effort="low")
    resp = await provider.chat(LLMRequest(messages=[Message(role="user", content="q")], model="m"))
    assert "response_format" not in sent[0] and sent[0]["reasoning_effort"] == "low"
    assert resp.provider == "x"


def hosted_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "llm_provider": "openai",
        "llm_base_url": "https://api.groq.com/openai/v1",
        "llm_api_key": SecretStr("gsk_test"),
        "llm_model_main": "big",
        "llm_model_small": "small",
    }
    return Settings(**(base | overrides))


def test_openai_provider_requires_url_and_key() -> None:
    with pytest.raises(ValidationError, match="AGENTOPS_LLM_BASE_URL"):
        Settings(llm_provider="openai")


async def test_router_uses_hosted_primary_and_per_role_fallback_models() -> None:
    settings = hosted_settings(
        fallback_base_url="https://openrouter.ai/api/v1",
        fallback_api_key=SecretStr("or_test"),
        fallback_model="fb-big",
        fallback_model_small="fb-small",
    )
    async with httpx.AsyncClient() as http:
        router = build_router(settings, http)
    routes = router._routes  # noqa: SLF001 - the wiring is what this test checks
    assert [(r.provider.name, r.model) for r in routes["main"]] == [
        ("groq", "big"),
        ("openrouter", "fb-big"),
    ]
    assert [(r.provider.name, r.model) for r in routes["small"]] == [
        ("groq", "small"),
        ("openrouter", "fb-small"),
    ]


# --- in-process embeddings ----------------------------------------------------------------------


class FakeOnnx:
    def __init__(self, dim: int = 4) -> None:
        self.dim, self.seen = dim, []  # type: ignore[var-annotated]

    def embed(self, texts: list[str], batch_size: int) -> list[list[float]]:
        self.seen.extend(texts)
        return [[1.0] * self.dim for _ in texts]


async def test_fastembed_uses_nomic_prefixes_and_loads_once() -> None:
    model, loads = FakeOnnx(), []

    def factory() -> FakeOnnx:
        loads.append(1)
        return model

    emb = FastEmbedEmbedder("nomic-ai/nomic-embed-text-v1.5", 4, model_factory=factory)
    assert await emb.embed_documents([]) == []
    assert len(await emb.embed_documents(["a", "b"])) == 2
    assert len(await emb.embed_query("q")) == 4
    assert model.seen == ["search_document: a", "search_document: b", "search_query: q"]
    assert loads == [1]


async def test_fastembed_rejects_a_dimension_that_does_not_match_the_schema() -> None:
    emb = FastEmbedEmbedder("other-model", 768, model_factory=lambda: FakeOnnx(dim=384))
    with pytest.raises(EmbeddingError, match="dimension"):
        await emb.embed_query("q")


async def test_fastembed_wraps_model_failures() -> None:
    def broken() -> FakeOnnx:
        raise OSError("download failed")

    with pytest.raises(EmbeddingError):
        await FastEmbedEmbedder("m", 4, model_factory=broken).embed_query("q")


# --- hosted Postgres URLs -----------------------------------------------------------------------

NEON = "postgresql://u:p@ep-x.aws.neon.tech/neondb?sslmode=require&channel_binding=require"


def test_libpq_url_becomes_an_asyncpg_url() -> None:
    assert asyncpg_url(NEON) == "postgresql+asyncpg://u:p@ep-x.aws.neon.tech/neondb?ssl=require"


def test_asyncpg_url_becomes_a_psycopg_conninfo() -> None:
    url = "postgresql+asyncpg://u:p@h:5432/db?ssl=require"
    assert psycopg_conninfo(url) == "postgresql://u:p@h:5432/db?sslmode=require"


def test_plain_local_urls_are_unchanged_apart_from_the_driver() -> None:
    local = "postgresql+asyncpg://agentops:agentops@localhost:5433/agentops"
    assert asyncpg_url(local) == local
    assert psycopg_conninfo(local) == "postgresql://agentops:agentops@localhost:5433/agentops"


# --- command execution switched off -------------------------------------------------------------


async def test_disabled_exec_refuses_every_command_and_drops_the_agent_tool(
    tmp_path: Path,
) -> None:
    manager = WorkspaceManager(tmp_path / "root", MemoryAudit(), exec_enabled=False)
    ws = manager.for_tenant("tenant_a")
    with pytest.raises(CommandNotAllowedError, match="disabled"):
        await ws.exec(["ls"], Actor("user", key_id="test"))
    names = {t.name for t in workspace_tools(manager, exec_enabled=False)}
    assert "workspace_exec" not in names and "workspace_read" in names
    assert "workspace_exec" in {t.name for t in workspace_tools(manager)}


def test_exec_backend_accepts_disabled() -> None:
    assert Settings(workspace_exec_backend="disabled").workspace_exec_backend == "disabled"


# --- the UI served by the API -------------------------------------------------------------------


async def test_api_serves_the_built_ui_without_shadowing_api_routes(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html>agentops ui</html>")
    app = create_app(Settings(web_dist_dir=tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        assert "agentops ui" in (await client.get("/")).text
        assert (await client.get("/health")).json() == {"status": "ok"}
        missing = await client.get("/api/v1/does-not-exist")
        assert missing.status_code == 404


def test_missing_ui_build_fails_at_startup(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="index.html"):
        create_app(Settings(web_dist_dir=tmp_path))


# --- CORS for a UI hosted on its own origin -----------------------------------------------------

UI = "https://agentops-ai.vercel.app"


async def test_cors_allows_only_the_configured_ui_origin() -> None:
    app = create_app(Settings(cors_origins=f"{UI}/, http://localhost:4173"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        preflight = await client.options(
            "/api/v1/documents",
            headers={
                "Origin": UI,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-api-key",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == UI
        # Error responses carry the header too, so the UI can read the API's error message
        # instead of an opaque CORS failure.
        missing = await client.get("/api/v1/no-such-route", headers={"Origin": UI})
        assert missing.status_code == 404
        assert missing.headers["access-control-allow-origin"] == UI
        other = await client.get("/health", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in other.headers


async def test_no_cors_headers_by_default() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(Settings())), base_url="http://t"
    ) as client:
        response = await client.get("/health", headers={"Origin": UI})
        assert "access-control-allow-origin" not in response.headers


def test_wildcard_cors_is_refused() -> None:
    with pytest.raises(ValidationError, match="explicitly"):
        Settings(cors_origins="*")


# --- waiting out a hosted rate limit ------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "seconds"),
    [
        ({"retry-after": "7"}, 7.0),
        ({"x-ratelimit-reset-tokens": "577ms"}, 0.577),
        # A tokens-per-minute 429 also reports the daily request bucket's (hours away) reset:
        # it only counts once that bucket is empty.
        (
            {
                "x-ratelimit-remaining-requests": "895",
                "x-ratelimit-reset-requests": "2h31m12s",
                "x-ratelimit-reset-tokens": "7.66s",
            },
            7.66,
        ),
        ({"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "1m26.4s"}, 86.4),
        ({}, None),
    ],
)
def test_retry_after_hint_is_read_from_rate_limit_headers(
    headers: dict[str, str], seconds: float | None
) -> None:
    assert _retry_after(httpx.Headers(headers)) == seconds


def limited_then_ok(hint: float | None) -> tuple[list[int], Any]:
    calls: list[int] = []

    async def fn() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise RateLimitError("limited", retry_after_s=hint)
        return "ok"

    return calls, fn


async def test_a_short_provider_hint_is_waited_out(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr("app.core.retry.asyncio.sleep", fake_sleep)
    calls, fn = limited_then_ok(5.0)
    assert await retry_async(fn, max_retries=1, max_retry_after_s=20) == "ok"
    assert len(calls) == 2 and 5.0 <= slept[0] <= 5.5


async def test_a_long_provider_hint_falls_through_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.core.retry.asyncio.sleep", pytest.fail)
    calls, fn = limited_then_ok(90.0)
    with pytest.raises(RateLimitError):
        await retry_async(fn, max_retries=3, max_retry_after_s=20)
    assert len(calls) == 1


async def test_groq_429_is_retried_on_the_same_route_before_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("app.core.retry.asyncio.sleep", no_sleep)
    hits: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(1)
        if len(hits) == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, text="slow down")
        return httpx.Response(200, json=OK)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    primary = OpenAICompatibleProvider(http, "https://api.groq.com/openai/v1", "k")
    fallback = FakeProvider()
    router = LLMRouter(
        {"main": [Route(primary, "m"), Route(fallback, "f")], "small": [Route(primary, "s")]},
        max_retries=1,
        max_retry_after_s=20,
    )
    resp = await router.chat("main", LLMRequest(messages=[Message(role="user", content="q")]))
    assert resp.provider == "groq" and not resp.fallback_used and fallback.requests == []


async def test_alternate_models_on_the_primary_come_before_the_fallback_provider() -> None:
    settings = hosted_settings(
        llm_model_main_alternates="mid, tiny",
        llm_model_small_alternates="tiny",
        fallback_base_url="https://openrouter.ai/api/v1",
        fallback_api_key=SecretStr("or_test"),
        fallback_model="fb",
    )
    async with httpx.AsyncClient() as http:
        router = build_router(settings, http)
    routes = router._routes  # noqa: SLF001 - the wiring is what this test checks
    assert [(r.provider.name, r.model) for r in routes["main"]] == [
        ("groq", "big"),
        ("groq", "mid"),
        ("groq", "tiny"),
        ("openrouter", "fb"),
    ]
    assert [r.model for r in routes["small"]] == ["small", "tiny", "fb"]
