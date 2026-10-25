"""Document tools through the harness, on real PostgreSQL: tenant isolation is enforced by the
execution context, not by anything the model passes in."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest

from app.core.budgets import ExecutionLimits
from app.core.errors import NotFoundError
from app.core.execution_context import ExecutionContext
from app.llm.schemas import ToolCall
from app.services import Services

pytestmark = pytest.mark.integration
Ingest = Callable[..., Any]


def ctx_for(services: Services, tenant: str) -> ExecutionContext:
    return ExecutionContext(
        tenant_id=tenant, limits=ExecutionLimits(), allowed_tools=services.tools.names
    )


async def test_document_search_returns_passages_with_sources(
    services: Services, ingest: Ingest
) -> None:
    doc = await ingest("rpo.md", "# Payments\n\nThe Recovery Point Objective (RPO) is 5 minutes.")
    outcome = await services.harness.call_tool(
        ctx_for(services, "tenant_a"),
        ToolCall(id="1", name="document_search", arguments={"query": "RPO minutes"}),
    )
    passages = outcome.output["passages"]
    assert passages and passages[0]["document_id"] == str(doc.id)
    assert passages[0]["filename"] == "rpo.md" and "5 minutes" in passages[0]["text"]


async def test_document_search_is_tenant_scoped(services: Services, ingest: Ingest) -> None:
    doc = await ingest("secret.md", "Tenant A secret: the vault code is 1234.", tenant="tenant_a")
    outcome = await services.harness.call_tool(
        ctx_for(services, "tenant_b"),
        # Even naming tenant A's document ID explicitly must not reach it.
        ToolCall(
            id="1",
            name="document_search",
            arguments={"query": "vault code", "document_ids": [str(doc.id)]},
        ),
    )
    assert outcome.output["passages"] == []


async def test_document_metadata_enforces_ownership(services: Services, ingest: Ingest) -> None:
    doc = await ingest("a.md", "# A\n\nsome text", metadata={"team": "payments"})
    call = ToolCall(id="1", name="document_metadata", arguments={"document_id": str(doc.id)})

    own = await services.harness.call_tool(ctx_for(services, "tenant_a"), call)
    assert own.output["filename"] == "a.md" and own.output["metadata"] == {"team": "payments"}
    assert own.output["is_latest"] is True

    with pytest.raises(NotFoundError):
        await services.harness.call_tool(ctx_for(services, "tenant_b"), call)
    with pytest.raises(NotFoundError):
        await services.harness.call_tool(
            ctx_for(services, "tenant_a"),
            ToolCall(
                id="2", name="document_metadata", arguments={"document_id": str(uuid.uuid4())}
            ),
        )


async def test_tool_specs_expose_only_allowed_tools(services: Services) -> None:
    specs = services.tools.specs({"calculator"})
    assert [s.name for s in specs] == ["calculator"]
    assert "expression" in specs[0].parameters["properties"]
