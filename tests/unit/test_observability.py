"""Observability: traces built from the ledger, the Langfuse and LangSmith adapters (mocked HTTP),
routing by source, redaction, and the guarantee that exporters can never break a run."""

import asyncio
import base64
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.agents import nodes
from app.agents.service import AgentService
from app.core.config import Settings
from app.core.execution_context import ExecutionContext
from app.observability.interfaces import Score, TraceRecord
from app.observability.langfuse_adapter import LangfuseSink
from app.observability.langsmith_adapter import LangSmithSink, to_runs
from app.observability.telemetry import LogSink, Telemetry, build_trace
from app.services import build_telemetry
from app.tools.document_search import DocumentSearchInput, DocumentSearchOutput
from tests.unit.test_agent_graph import GraphModel, make_ctx, make_runner
from tests.unit.test_agent_loop import FakeSearch

QUESTION = "What is the RPO?"


class TimedSearch(FakeSearch):
    async def run(self, args: DocumentSearchInput, ctx: ExecutionContext) -> DocumentSearchOutput:
        out = await super().run(args, ctx)
        return out.model_copy(update={"timings_ms": {"embed": 40.0, "vector": 5.0, "keyword": 3.0}})


async def graph_run() -> tuple[ExecutionContext, Any]:
    runner, _, _ = make_runner(GraphModel(), TimedSearch())
    ctx = make_ctx()
    return ctx, await runner.run(ctx, QUESTION)


class Recorder:
    def __init__(self, name: str = "rec", accept: bool = True, fail: bool = False) -> None:
        self.name, self._accept, self._fail = name, accept, fail
        self.traces: list[TraceRecord] = []
        self.scores: list[tuple[Score, str]] = []

    def accepts(self, trace: TraceRecord) -> bool:
        return self._accept

    async def export(self, trace: TraceRecord) -> None:
        if self._fail:
            raise RuntimeError("backend down")
        self.traces.append(trace)

    async def score(self, score: Score, source: str) -> None:
        self.scores.append((score, source))


def mock_http(responses: list[httpx.Response] | None = None) -> tuple[httpx.AsyncClient, list[Any]]:
    seen: list[httpx.Request] = []
    queue = list(responses or [])

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0) if queue else httpx.Response(200, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), seen


# --- trace building --------------------------------------------------------------------------


async def test_trace_mirrors_the_graph_with_nested_observations() -> None:
    ctx, result = await graph_run()
    trace = build_trace(ctx, result, question=QUESTION)
    assert trace.id == ctx.execution_id and trace.name == "agent.graph" and trace.terminal
    spans = {o.name: o for o in trace.observations if o.kind == "span"}
    assert {"classify_request", "plan_workflow", "retrieve_evidence", "verify_answer"} <= set(spans)
    generations = [o for o in trace.observations if o.kind == "generation"]
    assert [g.name for g in generations] == ["classify", "plan", "generate_answer", "verify"]
    assert generations[0].parent_id == spans["classify_request"].id
    assert all(g.model and g.input_tokens is not None for g in generations)
    search = spans["tool:document_search"]
    assert search.parent_id == spans["retrieve_evidence"].id
    assert search.output["chunk_ids"] and "timings_ms" not in search.output
    children = [o for o in trace.observations if o.parent_id == search.id]
    assert [o.name for o in children] == ["embed_query", "vector_search", "keyword_search"]
    embed, vector, keyword = children  # sequential, as HybridRetriever.search runs them
    assert embed.end == vector.start and vector.end == keyword.start
    assert any(o.kind == "event" and o.name == "final:graph" for o in trace.observations)
    meta = trace.metadata
    assert meta["prompt_version"] == nodes.PROMPT_VERSION and meta["workflow_version"]
    assert meta["request_id"] == ctx.request_id and meta["status"] == "answered"
    assert trace.input == {"question": QUESTION} and trace.output["citations"]


async def test_trace_ids_are_stable_so_reexport_upserts() -> None:
    ctx, result = await graph_run()
    first, second = build_trace(ctx, result), build_trace(ctx, result)
    assert [o.id for o in first.observations] == [o.id for o in second.observations]
    for o in first.observations:  # children never start before their parent
        parent = next((p for p in first.observations if p.id == o.parent_id), None)
        assert parent is None or o.start >= parent.start


async def test_capture_io_off_and_redaction() -> None:
    ctx, result = await graph_run()
    ctx.record(
        "tool_call",
        "echo",
        "ok",
        data={"arguments": {"api_key": "sk-abcdefghijklmnop", "q": "Bearer abc.def"}},
    )
    trace = build_trace(ctx, result, question=QUESTION, capture_io=False)
    assert trace.input is None and trace.output["answer"] is None
    assert all(o.input is None for o in trace.observations if o.kind == "span")
    redacted = build_trace(ctx, result, capture_io=True).observations[-1].input
    assert redacted == {"api_key": "[REDACTED]", "q": "[REDACTED]"}


# --- Langfuse ---------------------------------------------------------------------------------


async def test_langfuse_ingestion_batch() -> None:
    ctx, result = await graph_run()
    http, seen = mock_http()
    sink = LangfuseSink(http, "https://lf.example/", "pk-lf-1", SecretStr("sk-lf-2"))
    await sink.export(build_trace(ctx, result, question=QUESTION))
    request = seen[0]
    assert str(request.url) == "https://lf.example/api/public/ingestion"
    assert (
        request.headers["authorization"] == "Basic " + base64.b64encode(b"pk-lf-1:sk-lf-2").decode()
    )
    batch = json.loads(request.content)["batch"]
    assert batch[0]["type"] == "trace-create" and batch[0]["body"]["id"] == ctx.execution_id.hex
    assert len({e["id"] for e in batch}) == len(batch)  # unique envelope ids
    gen = next(e["body"] for e in batch if e["type"] == "generation-create")
    assert gen["traceId"] == ctx.execution_id.hex and gen["parentObservationId"]
    assert gen["usage"]["unit"] == "TOKENS" and gen["model"]
    assert {e["type"] for e in batch} == {
        "trace-create",
        "span-create",
        "generation-create",
        "event-create",
    }

    await sink.score(Score(ctx.execution_id, "citation_validity", 1.0), "eval")
    score = json.loads(seen[1].content)["batch"][0]
    assert score["type"] == "score-create" and score["body"]["value"] == 1.0


async def test_langfuse_partial_failure_is_reported() -> None:
    ctx, result = await graph_run()
    http, _ = mock_http([httpx.Response(207, json={"successes": [], "errors": [{"id": "x"}]})])
    with pytest.raises(RuntimeError, match="rejected 1"):
        await LangfuseSink(http, "https://lf", "pk", SecretStr("sk")).export(
            build_trace(ctx, result)
        )


# --- LangSmith --------------------------------------------------------------------------------


async def test_langsmith_receives_only_finished_eval_runs_by_default() -> None:
    ctx, result = await graph_run()
    http, seen = mock_http()
    sink = LangSmithSink(http, "https://ls.example", SecretStr("ls-key"), "proj")
    api_trace = build_trace(ctx, result)
    assert not sink.accepts(api_trace)  # production traffic stays out of the second vendor
    ctx.source = "eval"
    eval_trace = build_trace(ctx, result)
    assert sink.accepts(eval_trace)
    paused = result.model_copy(update={"status": "awaiting_approval"})
    assert not sink.accepts(build_trace(ctx, paused))  # sent once, when it finishes
    assert LangSmithSink(http, "x", SecretStr("k"), "p", scope="all").accepts(api_trace)

    await sink.export(eval_trace)
    assert seen[0].headers["x-api-key"] == "ls-key"
    runs = json.loads(seen[0].content)["post"]
    root = runs[0]
    assert root["id"] == str(ctx.execution_id) and root["session_name"] == "proj"
    by_id = {r["id"]: r for r in runs}
    for run in runs[1:]:
        assert run["dotted_order"].startswith(by_id[run["parent_run_id"]]["dotted_order"] + ".")
    assert {r["run_type"] for r in runs} == {"chain", "llm", "tool"}
    llm = next(r for r in runs if r["run_type"] == "llm")
    assert llm["outputs"]["usage_metadata"]["total_tokens"] >= 0
    assert llm["extra"]["metadata"]["ls_model_name"]

    await sink.score(Score(ctx.execution_id, "x", 1.0), "api")
    assert len(seen) == 1  # feedback for non-eval runs is not sent either
    await sink.score(Score(ctx.execution_id, "x", 1.0), "eval")
    assert seen[1].url.path == "/feedback"


def test_to_runs_marks_failed_runs() -> None:
    now = datetime.now(UTC)
    trace = TraceRecord(
        uuid.uuid4(), "agent.loop", now, now, "eval", True, None, None,
        {"status": "budget_exhausted", "error_code": "budget_exhausted"}, [], [],
    )  # fmt: skip
    assert to_runs(trace, "p")[0]["error"] == "budget_exhausted"


# --- Telemetry ---------------------------------------------------------------------------------


async def test_exporter_failures_and_timeouts_never_reach_the_caller() -> None:
    class Hang(Recorder):
        async def export(self, trace: TraceRecord) -> None:
            await asyncio.sleep(10)

    good, bad, slow = Recorder("good"), Recorder("bad", fail=True), Hang("slow")
    telemetry = Telemetry([bad, slow, good], timeout_s=0.05)
    ctx, result = await graph_run()
    telemetry.submit(ctx, result, QUESTION)  # returns immediately
    await telemetry.flush(1.0)
    assert len(good.traces) == 1


async def test_routing_and_scores_fan_out() -> None:
    accepted, skipped = Recorder("a"), Recorder("b", accept=False)
    telemetry = Telemetry([accepted, skipped])
    ctx, result = await graph_run()
    telemetry.submit(ctx, result)
    telemetry.score(Score(ctx.execution_id, "groundedness", 0.8), "eval")
    await telemetry.flush()
    assert len(accepted.traces) == 1 and skipped.traces == []
    assert accepted.scores[0][1] == "eval" and skipped.scores  # scores go to every sink


async def test_agent_service_submits_every_finished_run() -> None:
    class Store:
        async def start(self, *a: Any) -> None: ...
        async def save(self, *a: Any) -> None: ...

    sink = Recorder()
    telemetry = Telemetry([sink])
    runner, _, _ = make_runner(GraphModel())
    service = AgentService(
        None,  # type: ignore[arg-type]
        lambda: runner,
        runner.harness.tools,
        Store(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        make_ctx().limits,
        telemetry,
    )
    ctx = make_ctx()
    ctx.source = "eval"
    result = await service.run(ctx, "graph", QUESTION)
    await telemetry.flush()
    assert sink.traces[0].id == result.execution_id and sink.traces[0].source == "eval"
    assert sink.traces[0].input == {"question": QUESTION}


def test_sinks_follow_configuration() -> None:
    http = httpx.AsyncClient()
    assert build_telemetry(Settings(env="test"), http).sink_names == ["log"]
    full = Settings(
        env="test",
        telemetry_log=False,
        langfuse_public_key="pk",
        langfuse_secret_key=SecretStr("sk"),
        langsmith_api_key=SecretStr("ls"),
    )
    assert build_telemetry(full, http).sink_names == ["langfuse", "langsmith"]


async def test_log_sink_writes_one_line(caplog: pytest.LogCaptureFixture) -> None:
    ctx, result = await graph_run()
    with caplog.at_level("INFO", logger="app.observability.telemetry"):
        await LogSink().export(build_trace(ctx, result))
    record = caplog.records[-1]
    assert record.getMessage() == "trace" and record.__dict__["trace_id"] == ctx.execution_id.hex


async def test_source_survives_a_checkpoint_round_trip() -> None:
    ctx = make_ctx()
    ctx.source = "eval"
    assert ExecutionContext.restore(ctx.snapshot()).source == "eval"
