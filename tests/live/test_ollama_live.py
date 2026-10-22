"""Opt-in tests against the real local Ollama server: `uv run pytest -m live`.

They verify the integration contracts the fakes assume (JSON-schema output, tool-call shape,
embedding dimension and task prefixes). They are excluded from the default run and from CI.
"""

import httpx
import pytest

from app.core.config import Settings
from app.ingestion.embeddings import OllamaEmbedder
from app.llm.provider import OllamaProvider
from app.llm.schemas import LLMRequest, Message, ToolSpec
from app.rag.answer import AnswerDraft

pytestmark = pytest.mark.live
S = Settings()


@pytest.fixture
async def http() -> httpx.AsyncClient:
    async with httpx.AsyncClient() as client:
        try:
            (await client.get(f"{S.ollama_base_url}/api/version", timeout=2)).raise_for_status()
        except httpx.HTTPError:
            pytest.skip("Ollama is not running")
        yield client


async def test_structured_output_matches_schema(http: httpx.AsyncClient) -> None:
    resp = await OllamaProvider(http, S.ollama_base_url).chat(
        LLMRequest(
            model=S.llm_model_main,
            json_schema=AnswerDraft.model_json_schema(),
            messages=[
                Message(role="system", content="Answer from the evidence. Cite as [S1]."),
                Message(
                    role="user",
                    content='<source id="S1">The RPO is 5 minutes.</source>\n'
                    "Question: What is the RPO?",
                ),
            ],
        )
    )
    draft = AnswerDraft.model_validate_json(resp.content)
    assert "5" in draft.answer and resp.usage.output_tokens > 0


async def test_tool_call_shape(http: httpx.AsyncClient) -> None:
    resp = await OllamaProvider(http, S.ollama_base_url).chat(
        LLMRequest(
            model=S.llm_model_main,
            messages=[Message(role="user", content="Use the calculator to compute 12*7.")],
            tools=[
                ToolSpec(
                    name="calculator",
                    description="Evaluate an arithmetic expression",
                    parameters={
                        "type": "object",
                        "required": ["expression"],
                        "properties": {"expression": {"type": "string"}},
                    },
                )
            ],
        )
    )
    assert resp.tool_calls and resp.tool_calls[0].name == "calculator"
    assert isinstance(resp.tool_calls[0].arguments, dict)


async def test_embeddings_dimension_and_relevance(http: httpx.AsyncClient) -> None:
    embedder = OllamaEmbedder(http, S.ollama_base_url, S.embedding_model, S.embedding_dim)
    docs = await embedder.embed_documents(
        [
            "The database failover runbook uses patronictl.",
            "Lunch menu: pasta and salad on Fridays.",
        ]
    )
    query = await embedder.embed_query("How do I fail over the database?")

    def cos(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True)) / (
            sum(x * x for x in a) ** 0.5 * sum(y * y for y in b) ** 0.5
        )

    assert len(query) == S.embedding_dim
    assert cos(query, docs[0]) > cos(query, docs[1])
