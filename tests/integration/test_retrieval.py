from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.retrieval.schemas import RetrievalFilters
from app.services import Services

pytestmark = pytest.mark.integration
Ingest = Callable[..., Any]
CORPUS = Path(__file__).parents[2] / "datasets" / "corpus"


@pytest.fixture
async def corpus(ingest: Ingest) -> dict[str, Any]:
    docs = {}
    for path in sorted(CORPUS.glob("*.md")):
        docs[path.stem] = await ingest(
            path.name, path.read_text(), metadata={"kind": path.stem[:3]}
        )
    return docs


@pytest.mark.parametrize("mode", ["vector", "keyword", "hybrid"])
async def test_each_mode_finds_the_runbook(
    services: Services, corpus: dict[str, Any], mode: str
) -> None:
    result = await services.retriever.search(
        "tenant_a", "patronictl failover standby promote", mode=mode, top_k=3
    )  # type: ignore[arg-type]
    assert result.results, f"{mode} returned nothing"
    assert result.results[0].filename == "runbook-database-failover.md"
    assert result.diagnostics.mode == mode


async def test_hybrid_reports_both_retrievers(services: Services, corpus: dict[str, Any]) -> None:
    result = await services.retriever.search("tenant_a", "Recovery Point Objective RPO minutes")
    d = result.diagnostics
    assert d.vector_candidates > 0 and d.keyword_candidates > 0 and d.total_ms > 0
    top = result.results[0]
    assert top.vector_rank is not None and top.keyword_rank is not None
    assert "payments-architecture" in top.filename


async def test_keyword_search_matches_natural_language_questions(
    services: Services, corpus: dict[str, Any]
) -> None:
    # Regression: AND-ing every term made questions match nothing ("how", "kept" are absent).
    result = await services.retriever.search(
        "tenant_a", "how long are transaction records kept", mode="keyword"
    )
    assert result.results
    assert "payments-architecture" in result.results[0].filename


async def test_keyword_search_handles_hostile_query_syntax(
    services: Services, corpus: dict[str, Any]
) -> None:
    for query in ["'; DROP TABLE documents; --", '"unclosed', "a & | ! ( )", "-", "🙂"]:
        await services.retriever.search("tenant_a", query, mode="keyword")


async def test_filters_by_document_and_metadata(services: Services, corpus: dict[str, Any]) -> None:
    adr = corpus["adr-003-vector-store"]
    only = await services.retriever.search(
        "tenant_a", "latency", filters=RetrievalFilters(document_ids=[adr.id])
    )
    assert {r.document_id for r in only.results} <= {adr.id}
    meta = await services.retriever.search(
        "tenant_a", "failover", filters=RetrievalFilters(metadata={"kind": "run"})
    )
    assert meta.results and all(r.filename.startswith("runbook") for r in meta.results)


async def test_tenant_isolation(services: Services, corpus: dict[str, Any], ingest: Ingest) -> None:
    await ingest(
        "secret.md",
        "Tenant B secret: the vault passphrase rotation is quarterly.",
        tenant="tenant_b",
    )
    for mode in ("vector", "keyword", "hybrid"):
        a = await services.retriever.search(
            "tenant_a", "vault passphrase rotation quarterly", mode=mode, top_k=50
        )  # type: ignore[arg-type]
        assert all(r.filename != "secret.md" for r in a.results)
        b = await services.retriever.search("tenant_b", "payments RPO", mode=mode, top_k=50)  # type: ignore[arg-type]
        assert all(r.filename == "secret.md" for r in b.results)
    # Even passing tenant A's document IDs as a filter cannot reach across tenants.
    leak = await services.retriever.search(
        "tenant_b",
        "payments",
        filters=RetrievalFilters(document_ids=[d.id for d in corpus.values()]),
    )
    assert leak.results == []


async def test_duplicate_text_across_documents_is_removed(
    services: Services, ingest: Ingest
) -> None:
    boilerplate = "Confidential. Contact the platform team for access to this system."
    filler = "Quarterly capacity review notes for storage growth and budgets. " * 6
    await ingest("one.md", boilerplate)
    await ingest("two.txt", f"{filler}\n\n{boilerplate}")  # different file, same passage
    result = await services.retriever.search("tenant_a", "contact platform team access")
    copies = [r for r in result.results if r.text == boilerplate]
    assert len(copies) == 1 and result.diagnostics.duplicates_removed == 1


async def test_irrelevant_query_against_empty_tenant(services: Services) -> None:
    result = await services.retriever.search("tenant_a", "anything at all")
    assert result.results == []


async def test_negated_terms_exclude_rather_than_match_everything(
    services: Services, corpus: dict[str, Any]
) -> None:
    result = await services.retriever.search(
        "tenant_a", "failover -patronictl", mode="keyword", top_k=50
    )
    assert result.results
    assert all("patronictl" not in r.text for r in result.results)
    assert all("failover" in r.text.lower() for r in result.results)
