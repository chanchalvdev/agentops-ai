"""Opt-in: the custom agent loop with real qwen3 tool calling, real embeddings, real Postgres.

`uv run pytest -m live tests/live/test_agent_live.py -s` prints the ledger so the run can be read.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from app.core.budgets import ExecutionLimits
from app.core.config import Settings
from app.core.execution_context import ExecutionContext
from app.services import Services, build_services
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.live
CORPUS = Path(__file__).parents[2] / "datasets" / "corpus"


@pytest.fixture
async def live_services(database: str) -> AsyncIterator[Services]:
    settings = Settings(env="test", database_url=TEST_DATABASE_URL, log_level="WARNING")
    try:
        async with httpx.AsyncClient() as c:
            (await c.get(f"{settings.ollama_base_url}/api/version", timeout=2)).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("Ollama is not running")
    svc = build_services(settings)
    async with svc.engine.begin() as conn:
        await conn.execute(text("TRUNCATE documents, agent_executions CASCADE"))
    for path in sorted(CORPUS.glob("*.md")):
        doc, _ = await svc.ingestion.create_document("tenant_a", path.name, path.read_bytes(), None)
        await svc.ingestion.process_document(doc.id)
    yield svc
    await svc.aclose()


def show(result: object) -> None:
    for e in result.ledger:  # type: ignore[attr-defined]
        extra = e.data.get("arguments") or e.data.get("tool_calls") or ""
        print(
            f"  {e.seq:>2} {e.kind:<10} {e.name:<16} {e.status:<8} {e.latency_ms or '':>8} {extra}"
        )
    print("  ->", result.status, "|", result.answer)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("question", "expect_status", "expect_in_answer", "expect_files"),
    [
        (
            "What is the RPO in the latest payments architecture, and how many seconds is that?",
            "answered",
            "300",
            {"payments-architecture-v2.md"},
        ),
        ("What is the CEO's favourite colour?", "insufficient_evidence", None, set()),
    ],
)
async def test_loop_live(
    live_services: Services,
    question: str,
    expect_status: str,
    expect_in_answer: str | None,
    expect_files: set[str],
) -> None:
    ctx = ExecutionContext(
        tenant_id="tenant_a",
        limits=ExecutionLimits.from_settings(live_services.settings),
        allowed_tools=live_services.tools.names,
    )
    await live_services.executions.start(ctx, "loop", question)
    result = await live_services.agent_loop.run(ctx, question)
    await live_services.executions.save(ctx, result)
    print(f"\n{question}  ({result.total_ms / 1000:.1f}s, {result.usage.total_tokens} tokens)")
    show(result)
    assert result.status == expect_status
    if expect_in_answer:
        assert expect_in_answer in result.answer.replace(",", "")
    assert expect_files <= {c.filename for c in result.citations}
