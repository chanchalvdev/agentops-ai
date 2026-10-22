"""Shared fixtures.

Integration tests use a dedicated database (agentops_test) migrated with Alembic, so every run
also proves the migrations work on a clean database. If PostgreSQL is not reachable those tests
are skipped locally; set AGENTOPS_REQUIRE_DB=1 (CI does) to make that a failure instead.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Callable

import asyncpg
import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.core.config import Settings
from app.ingestion.embeddings import FakeEmbedder
from app.llm.provider import FakeProvider
from app.llm.schemas import LLMRequest, LLMResponse
from app.main import create_app
from app.services import Services, build_services

ADMIN_DSN = os.getenv(
    "AGENTOPS_TEST_ADMIN_DSN", "postgresql://agentops:agentops@localhost:5433/agentops"
)
TEST_DB = "agentops_test"
TEST_DATABASE_URL = os.getenv(
    "AGENTOPS_TEST_DATABASE_URL", f"postgresql+asyncpg://agentops:agentops@localhost:5433/{TEST_DB}"
)
KEY_A, KEY_B = "dev-key-tenant-a", "dev-key-tenant-b"


def make_pdf(pages: list[str]) -> bytes:
    """Build a minimal valid PDF with one text page per entry (no PDF library needed)."""

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    n = len(pages)
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{4 + 2 * i} 0 R' for i in range(n))}] /Count {n} >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, page in enumerate(pages):
        body = " ".join(f"({esc(line)}) Tj T*" for line in page.split("\n"))
        stream = f"BT /F1 12 Tf 72 720 Td 14 TL {body} ET"
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>"
        )
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


def cite_first_source(request: LLMRequest) -> str:
    """Default fake model: answers by citing S1 when evidence exists."""
    return (
        '{"answer": "According to the evidence [S1].", "citations": ["S1"], '
        '"insufficient_evidence": false}'
    )


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        env="test",
        database_url=TEST_DATABASE_URL,
        llm_provider="fake",
        embedding_provider="fake",
        log_level="WARNING",
        chunk_size_chars=400,
        chunk_overlap_chars=80,
        llm_max_retries=0,
    )


async def _prepare_database() -> str | None:
    try:
        conn = await asyncpg.connect(ADMIN_DSN, timeout=3)
    except (OSError, asyncpg.PostgresError) as exc:
        return f"PostgreSQL not reachable at {ADMIN_DSN}: {exc}"
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    finally:
        await conn.close()

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    # Alembic's env.py calls asyncio.run, so run it off the event loop thread.
    await asyncio.to_thread(command.downgrade, cfg, "base")
    await asyncio.to_thread(command.upgrade, cfg, "head")
    return None


@pytest.fixture(scope="session")
async def database() -> str:
    reason = await _prepare_database()
    if reason:
        if os.getenv("AGENTOPS_REQUIRE_DB") == "1":
            pytest.fail(reason)
        pytest.skip(reason)
    return TEST_DATABASE_URL


@pytest.fixture
def fake_llm() -> FakeProvider:
    return FakeProvider(cite_first_source)


@pytest.fixture
async def services(
    database: str, test_settings: Settings, fake_llm: FakeProvider
) -> AsyncIterator[Services]:
    svc = build_services(
        test_settings, embedder=FakeEmbedder(test_settings.embedding_dim), llm_provider=fake_llm
    )
    async with svc.engine.begin() as conn:
        await conn.execute(text("TRUNCATE documents CASCADE"))
    yield svc
    await svc.aclose()


@pytest.fixture
async def client(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(services=services)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-API-Key": KEY_A}
    ) as c:
        yield c


@pytest.fixture
def ingest(services: Services) -> Callable[..., object]:
    """Create + process a document synchronously; returns the stored Document."""

    async def _ingest(
        name: str,
        content: str | bytes,
        tenant: str = "tenant_a",
        metadata: dict[str, object] | None = None,
    ) -> object:
        data = content.encode() if isinstance(content, str) else content
        doc, created = await services.ingestion.create_document(tenant, name, data, metadata)
        if created:
            await services.ingestion.process_document(doc.id)
        return await services.ingestion.get(tenant, doc.id)

    return _ingest


def response_with(content: str, **kw: object) -> Callable[[LLMRequest], LLMResponse]:
    def responder(request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content=content, model=request.model, provider="fake", latency_ms=1.0, **kw
        )  # type: ignore[arg-type]

    return responder
