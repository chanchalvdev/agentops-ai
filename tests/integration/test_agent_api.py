"""Agent, execution, and approval endpoints end to end (fake model, real Postgres)."""

import asyncio
import json
from collections.abc import Callable
from typing import Any, ClassVar

import httpx
import pytest

from app.llm.provider import FakeProvider
from app.llm.schemas import LLMRequest, LLMResponse, ToolCall
from app.services import Services
from tests.conftest import KEY_B
from tests.integration.test_agent_loop_db import agent_script
from tests.unit.test_agent_graph import GraphModel, SaveNote

pytestmark = pytest.mark.integration
Ingest = Callable[..., Any]
DOC = "# DR\n\nThe Recovery Point Objective (RPO) is 5 minutes."


class OpenSaveNote(SaveNote):
    permissions: ClassVar[frozenset[str]] = frozenset()  # any tenant may request it; still gated


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


async def test_loop_run_execution_and_trace(
    client: httpx.AsyncClient, ingest: Ingest, fake_llm: FakeProvider
) -> None:
    fake_llm._responder = agent_script
    await ingest("dr.md", DOC)
    resp = await client.post("/api/v1/agent/run", json={"question": "RPO?", "mode": "loop"})
    assert resp.status_code == 200, resp.text
    run = resp.json()
    assert run["status"] == "answered" and run["citations"][0]["filename"] == "dr.md"

    execution = (await client.get(f"/api/v1/executions/{run['execution_id']}")).json()
    assert execution["status"] == "answered" and execution["tool_calls"] == 1
    assert execution["request_id"] == run["request_id"]
    trace = (await client.get(f"/api/v1/executions/{run['execution_id']}/trace")).json()
    assert [e["kind"] for e in trace][-1] == "final"

    other = await client.get(
        f"/api/v1/executions/{run['execution_id']}", headers={"X-API-Key": KEY_B}
    )
    assert other.status_code == 404


async def test_graph_run(client: httpx.AsyncClient, ingest: Ingest, fake_llm: FakeProvider) -> None:
    fake_llm._responder = GraphModel()
    await ingest("dr.md", DOC)
    run = (await client.post("/api/v1/agent/run", json={"question": "RPO?"})).json()
    assert run["mode"] == "graph" and run["status"] == "answered"
    assert run["verification"] == "supported"


async def test_request_limits_only_tighten_and_unknown_tools_rejected(
    client: httpx.AsyncClient, fake_llm: FakeProvider
) -> None:
    fake_llm._responder = agent_script
    run = (
        await client.post(
            "/api/v1/agent/run",
            json={"question": "?", "mode": "loop", "limits": {"max_steps": 1}},
        )
    ).json()
    assert run["status"] == "budget_exhausted" and run["steps"] == 1

    huge = (
        await client.post(
            "/api/v1/agent/run",
            json={"question": "?", "mode": "loop", "limits": {"max_steps": 10_000}},
        )
    ).json()
    execution = (await client.get(f"/api/v1/executions/{huge['execution_id']}")).json()
    assert execution["limits"]["max_steps"] == 8  # server default wins

    bad = await client.post("/api/v1/agent/run", json={"question": "?", "tools": ["rm_rf"]})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_request"


async def test_stream_emits_progress_citations_and_final(
    client: httpx.AsyncClient, ingest: Ingest, fake_llm: FakeProvider
) -> None:
    fake_llm._responder = agent_script
    await ingest("dr.md", DOC)
    resp = await client.post("/api/v1/agent/run/stream", json={"question": "RPO?", "mode": "loop"})
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(resp.text)
    names = [name for name, _ in events]
    assert names[0] == "start" and names[-1] == "final"
    assert {"step", "model_call", "tool_call", "citation"} <= set(names)
    assert names.index("tool_call") < names.index("citation") < names.index("final")
    final = events[-1][1]
    assert final["status"] == "answered" and "ledger" not in final
    stored = (await client.get(f"/api/v1/executions/{final['execution_id']}")).json()
    assert stored["status"] == "answered"


async def test_client_disconnect_cancels_and_persists(
    services: Services, fake_llm: FakeProvider
) -> None:
    async def slow(request: LLMRequest) -> LLMResponse:
        await asyncio.sleep(5)
        raise AssertionError("unreachable")

    fake_llm.chat = slow  # type: ignore[method-assign]
    ctx = services.agents.new_context("tenant_a", "req-1")
    stream = services.agents.stream(ctx, "loop", "?")
    first = await anext(stream)  # "step" event: the run is in flight
    assert first[0] == "event"
    await stream.aclose()  # what Starlette does when the client disconnects

    row = await services.executions.get("tenant_a", ctx.execution_id)
    assert row.status == "cancelled" and row.finished_at is not None


# --- approvals --------------------------------------------------------------------------------


def write_model() -> GraphModel:
    return GraphModel(
        classification={"intent": "document_question", "needs_tools": True},
        tool_calls=[[ToolCall(id="w1", name="save_note", arguments={"text": "RPO=5"})]],
        answers=[{"answer": "RPO 5 min [S1]; note [T1].", "citations": ["S1", "T1"]}],
    )


async def test_approve_resumes_and_double_approve_conflicts(
    client: httpx.AsyncClient, services: Services, ingest: Ingest, fake_llm: FakeProvider
) -> None:
    note = OpenSaveNote()
    services.tools.register(note)
    fake_llm._responder = write_model()
    await ingest("dr.md", DOC)

    paused = (await client.post("/api/v1/agent/run", json={"question": "Save RPO"})).json()
    assert paused["status"] == "awaiting_approval" and note.saved == []

    pending = (await client.get("/api/v1/approvals")).json()
    assert len(pending) == 1 and pending[0]["arguments"] == {"text": "RPO=5"}
    assert pending[0]["execution_id"] == paused["execution_id"]
    approval_id = pending[0]["id"]

    assert (
        await client.post(f"/api/v1/approvals/{approval_id}/approve", headers={"X-API-Key": KEY_B})
    ).status_code == 404

    done = await client.post(f"/api/v1/approvals/{approval_id}/approve", json={"reason": "ok"})
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "answered" and note.saved == ["RPO=5"]
    again = await client.post(f"/api/v1/approvals/{approval_id}/approve")
    assert again.status_code == 409 and note.saved == ["RPO=5"]

    assert (await client.get("/api/v1/approvals")).json() == []
    decided = (await client.get("/api/v1/approvals", params={"status": "approved"})).json()
    assert decided[0]["reason"] == "ok"
    execution = (await client.get(f"/api/v1/executions/{paused['execution_id']}")).json()
    assert execution["status"] == "answered"


async def test_concurrent_approvals_resume_once(
    client: httpx.AsyncClient, services: Services, ingest: Ingest, fake_llm: FakeProvider
) -> None:
    note = OpenSaveNote()
    services.tools.register(note)
    fake_llm._responder = write_model()
    await ingest("dr.md", DOC)
    await client.post("/api/v1/agent/run", json={"question": "Save RPO"})
    approval_id = (await client.get("/api/v1/approvals")).json()[0]["id"]
    results = await asyncio.gather(
        *(client.post(f"/api/v1/approvals/{approval_id}/approve") for _ in range(4))
    )
    assert sorted(r.status_code for r in results) == [200, 409, 409, 409]
    assert note.saved == ["RPO=5"]


async def test_reject_performs_no_write(
    client: httpx.AsyncClient, services: Services, ingest: Ingest, fake_llm: FakeProvider
) -> None:
    note = OpenSaveNote()
    services.tools.register(note)
    model = write_model()
    model.answers = [{"answer": "Not saved [T1].", "citations": ["T1"]}]
    fake_llm._responder = model
    await ingest("dr.md", DOC)
    await client.post("/api/v1/agent/run", json={"question": "Save RPO"})
    approval_id = (await client.get("/api/v1/approvals")).json()[0]["id"]
    done = (
        await client.post(f"/api/v1/approvals/{approval_id}/reject", json={"reason": "no"})
    ).json()
    assert note.saved == [] and done["status"] == "answered"
    assert done["tool_references"][0]["output"]["rejected_by_reviewer"] is True
