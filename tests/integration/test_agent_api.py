"""Agent, execution, and approval endpoints end to end (fake model, real Postgres)."""

import asyncio
import json
import uuid
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


async def test_sse_events_carry_ids_and_a_finished_run_can_be_replayed(
    client: httpx.AsyncClient, ingest: Ingest, fake_llm: FakeProvider
) -> None:
    fake_llm._responder = agent_script
    await ingest("dr.md", DOC)
    resp = await client.post("/api/v1/agent/run/stream", json={"question": "RPO?", "mode": "loop"})
    blocks = [
        dict(ln.split(": ", 1) for ln in b.splitlines()) for b in resp.text.strip().split("\n\n")
    ]
    ledger_ids = [int(b["id"]) for b in blocks if b.get("id", "end") != "end"]
    assert ledger_ids == sorted(ledger_ids) and ledger_ids[0] == 0
    assert blocks[-1]["id"] == "end" and blocks[-1]["event"] == "final"
    execution_id = parse_sse(resp.text)[0][1]["execution_id"]

    # The connection dropped after event 2: reconnecting replays 3.. and the stored outcome.
    again = await client.get(
        f"/api/v1/executions/{execution_id}/stream", headers={"Last-Event-ID": "2"}
    )
    replay = parse_sse(again.text)
    assert replay[0][0] == "start" and replay[-1][0] == "final"
    assert [e[1]["seq"] for e in replay[1:] if "seq" in e[1]][0] == 3
    assert replay[-1][1]["status"] == "answered" and replay[-1][1]["replayed"] is True
    other = await client.get(
        f"/api/v1/executions/{execution_id}/stream", headers={"X-API-Key": KEY_B}
    )
    assert other.status_code == 404


async def test_a_disconnected_stream_can_reattach_within_the_grace_period(
    services: Services, fake_llm: FakeProvider
) -> None:
    gate = asyncio.Event()

    async def held(request: LLMRequest) -> LLMResponse:
        await gate.wait()
        return LLMResponse(
            content='{"answer": "n/a", "insufficient_evidence": true}',
            model=request.model,
            provider="fake",
            latency_ms=1,
        )

    fake_llm.chat = held  # type: ignore[method-assign]
    ctx = services.agents.new_context("tenant_a", "req-re")
    first = services.agents.stream(ctx, "loop", "?", reconnect_grace_s=5)
    seen = await anext(first)
    await first.aclose()  # connection lost; the run keeps going during the grace period
    assert services.agents.live_run("tenant_a", ctx.execution_id) is not None
    assert services.agents.live_run("tenant_b", ctx.execution_id) is None  # tenant-scoped

    resumed = services.agents.follow("tenant_a", ctx.execution_id, seen[1]["seq"])
    gate.set()
    items = [item async for item in resumed]
    assert items[-1][0] == "result" and items[-1][1]["status"] == "insufficient_evidence"
    seqs = [d["seq"] for k, d in items if k == "event"]
    assert seqs and seqs[0] == seen[1]["seq"] + 1  # nothing replayed twice


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

    done = await client.post(
        f"/api/v1/approvals/{approval_id}/approve", params={"wait": "true"}, json={"reason": "ok"}
    )
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
    assert sorted(r.status_code for r in results) == [202, 409, 409, 409]
    await asyncio.gather(*services.agents._resumes)
    assert note.saved == ["RPO=5"]


async def test_approve_returns_202_and_the_run_finishes_in_the_background(
    client: httpx.AsyncClient, services: Services, ingest: Ingest, fake_llm: FakeProvider
) -> None:
    note = OpenSaveNote()
    services.tools.register(note)
    fake_llm._responder = write_model()
    await ingest("dr.md", DOC)
    paused = (await client.post("/api/v1/agent/run", json={"question": "Save RPO"})).json()
    approval_id = (await client.get("/api/v1/approvals")).json()[0]["id"]
    accepted = await client.post(f"/api/v1/approvals/{approval_id}/approve")
    assert accepted.status_code == 202, accepted.text
    body = accepted.json()
    assert body == {
        "approval_id": approval_id,
        "execution_id": paused["execution_id"],
        "status": "resuming",
    }
    assert accepted.headers["Location"] == f"/api/v1/executions/{paused['execution_id']}"
    await asyncio.gather(*services.agents._resumes)
    execution = (await client.get(accepted.headers["Location"])).json()
    assert execution["status"] == "answered" and note.saved == ["RPO=5"]


async def test_unanswered_approvals_expire_and_cancel_their_run(
    client: httpx.AsyncClient,
    services: Services,
    ingest: Ingest,
    fake_llm: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services.tools.register(OpenSaveNote())
    fake_llm._responder = write_model()
    await ingest("dr.md", DOC)
    paused = (await client.post("/api/v1/agent/run", json={"question": "Save RPO"})).json()
    approval_id = (await client.get("/api/v1/approvals")).json()[0]["id"]
    monkeypatch.setattr(services.settings, "approval_ttl_s", 0.001)
    assert (await client.get("/api/v1/approvals")).json() == []  # listing runs the sweep
    expired = (await client.get("/api/v1/approvals", params={"status": "expired"})).json()
    assert expired[0]["id"] == approval_id and "no decision" in expired[0]["reason"]
    execution = (await client.get(f"/api/v1/executions/{paused['execution_id']}")).json()
    assert execution["status"] == "cancelled" and execution["error_code"] == "approval_expired"
    late = await client.post(f"/api/v1/approvals/{approval_id}/approve")
    assert late.status_code == 409


async def test_a_decision_whose_resume_died_is_reopened(
    client: httpx.AsyncClient,
    services: Services,
    ingest: Ingest,
    fake_llm: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = OpenSaveNote()
    services.tools.register(note)
    fake_llm._responder = write_model()
    await ingest("dr.md", DOC)
    await client.post("/api/v1/agent/run", json={"question": "Save RPO"})
    approval_id = (await client.get("/api/v1/approvals")).json()[0]["id"]
    # The process dies right after claiming the decision: approved, run still paused.
    await services.approvals.decide("tenant_a", uuid.UUID(approval_id), approved=True, reason="x")
    monkeypatch.setattr(services.settings, "approval_resume_grace_s", 0.001)
    reopened = (await client.get("/api/v1/approvals")).json()
    assert [a["id"] for a in reopened] == [approval_id]
    assert "did not complete" in reopened[0]["reason"]
    done = await client.post(f"/api/v1/approvals/{approval_id}/approve", params={"wait": "true"})
    assert done.json()["status"] == "answered" and note.saved == ["RPO=5"]


async def test_a_resume_that_cannot_start_releases_the_claim(
    client: httpx.AsyncClient, services: Services, ingest: Ingest, fake_llm: FakeProvider
) -> None:
    services.tools.register(OpenSaveNote())
    fake_llm._responder = write_model()
    await ingest("dr.md", DOC)
    await client.post("/api/v1/agent/run", json={"question": "Save RPO"})
    approval_id = (await client.get("/api/v1/approvals")).json()[0]["id"]
    graph = services.graph

    async def gone(execution_id: Any) -> None:
        return None

    original = graph.restore_context
    graph.restore_context = gone  # type: ignore[method-assign]
    try:
        failed = await client.post(f"/api/v1/approvals/{approval_id}/approve")
        assert failed.status_code == 404
    finally:
        graph.restore_context = original  # type: ignore[method-assign]
    assert [a["id"] for a in (await client.get("/api/v1/approvals")).json()] == [approval_id]


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
        await client.post(
            f"/api/v1/approvals/{approval_id}/reject",
            params={"wait": "true"},
            json={"reason": "no"},
        )
    ).json()
    assert note.saved == [] and done["status"] == "answered"
    assert done["tool_references"][0]["output"]["rejected_by_reviewer"] is True


async def test_stream_sends_heartbeats_while_a_model_call_is_slow(
    services: Services, fake_llm: FakeProvider
) -> None:
    async def slow_then_final(request: LLMRequest) -> LLMResponse:
        await asyncio.sleep(0.25)
        return LLMResponse(
            content='{"answer": "n/a", "insufficient_evidence": true}',
            model=request.model,
            provider="fake",
            latency_ms=250,
        )

    fake_llm.chat = slow_then_final  # type: ignore[method-assign]
    ctx = services.agents.new_context("tenant_a", "req-hb")
    kinds = [k async for k, _ in services.agents.stream(ctx, "loop", "?", heartbeat_s=0.05)]
    assert "heartbeat" in kinds and kinds[-1] == "result"


async def test_stream_reports_errors_after_start(
    services: Services, fake_llm: FakeProvider
) -> None:
    async def broken_start(*args: Any) -> None:
        raise RuntimeError("db down")

    services.executions.start = broken_start  # type: ignore[method-assign]
    ctx = services.agents.new_context("tenant_a", "req-err")
    events = [e async for e in services.agents.stream(ctx, "loop", "?")]
    assert events == [("error", {"code": "internal_error", "execution_id": str(ctx.execution_id)})]


async def test_list_all_approval_statuses(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/approvals", params={"status": "all"})).status_code == 200
