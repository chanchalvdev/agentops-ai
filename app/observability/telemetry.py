"""Telemetry: turn an execution's ledger into a trace and hand it to the configured sinks.

Why the ledger and not span code in every node: the harness already records every node step,
model call, tool call, rejection, approval, and correction with latency, tokens, and IDs. Building
the trace from it after the run means the trace and the persisted audit trail cannot disagree,
nodes stay free of vendor code, and no exporter can slow down or break a run.

Export is fire-and-forget on the event loop: `submit` never raises and never awaits the network.
`flush` (called at shutdown and by the CLI) waits a bounded time for pending exports.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

from app.agents import custom_loop, graph, nodes
from app.agents.result import AgentRunResult
from app.core.execution_context import ExecutionContext, LedgerEvent
from app.core.logging import redact
from app.observability.interfaces import Level, Observation, Score, TraceRecord, TraceSink

logger = logging.getLogger(__name__)

_MAX_OBSERVATIONS = 500
_MAX_TEXT = 2000
_MAX_PENDING = 100
_STEP_STATUS_LEVEL: dict[str, Level] = {"error": "ERROR", "rejected": "WARNING"}
_GENERATION_KEYS = ("role", "provider", "attempts", "pricing_source", "tool_calls")
_TOOL_KEYS = ("call_id", "attempts")


def _level(event: LedgerEvent) -> Level:
    return _STEP_STATUS_LEVEL.get(event.status, "DEFAULT")


def _retrieval_spans(
    parent: Observation, timings: dict[str, float], oid: Callable[[str], uuid.UUID], prefix: str
) -> list[Observation]:
    """Child spans for the query embedding and the two searches inside document_search.
    HybridRetriever.search runs them in sequence: embed, then vector, then keyword."""
    embed = timedelta(milliseconds=timings.get("embed", 0.0))
    spans = [
        Observation(
            oid(f"{prefix}:embed"),
            "span",
            "embed_query",
            parent.start,
            parent.start + embed,
            parent.id,
        )
    ]
    begin = parent.start + embed
    for key, name in (("vector", "vector_search"), ("keyword", "keyword_search")):
        if timings.get(key):
            end = begin + timedelta(milliseconds=timings[key])
            spans.append(Observation(oid(f"{prefix}:{key}"), "span", name, begin, end, parent.id))
            begin = end
    return spans


def build_trace(
    ctx: ExecutionContext,
    result: AgentRunResult,
    *,
    question: str | None = None,
    capture_io: bool = True,
) -> TraceRecord:
    """Pure function of the ledger and the result. Observation ids are derived from the
    execution id and ledger sequence, so re-exporting after a resume updates, not duplicates."""
    ns = ctx.execution_id

    def oid(key: str) -> uuid.UUID:
        return uuid.uuid5(ns, key)

    observations: list[Observation] = []
    step: Observation | None = None
    for e in ctx.ledger[:_MAX_OBSERVATIONS]:
        if e.kind == "step":
            if step is not None and step.end is None:
                step.end = e.at
            step = Observation(
                oid(f"step:{e.seq}"),
                "span",
                e.name,
                e.at,
                e.at if e.status != "ok" else None,
                level=_level(e),
                status_message=e.error_code,
                metadata={"step": e.data.get("step")},
            )
            observations.append(step)
            continue
        parent = step.id if step is not None else None
        started = e.at - timedelta(milliseconds=e.latency_ms or 0.0)
        if step is not None:
            started = max(started, step.start)  # clock rounding must not put a child first
        if e.kind == "model_call" and e.status != "rejected":
            observations.append(
                Observation(
                    oid(f"event:{e.seq}"),
                    "generation",
                    e.name,
                    started,
                    e.at,
                    parent,
                    level=_level(e),
                    status_message=e.error_code,
                    metadata={k: e.data[k] for k in _GENERATION_KEYS if k in e.data},
                    model=e.data.get("model"),
                    input_tokens=e.input_tokens,
                    output_tokens=e.output_tokens,
                )
            )
        elif e.kind == "tool_call" and e.status != "rejected":
            summary = e.data.get("result") or {}
            span = Observation(
                oid(f"event:{e.seq}"),
                "span",
                f"tool:{e.name}",
                started,
                e.at,
                parent,
                level=_level(e),
                status_message=e.error_code,
                metadata=redact({k: e.data[k] for k in _TOOL_KEYS if k in e.data}),
                input=redact(e.data.get("arguments")) if capture_io else None,
                output=redact({k: v for k, v in summary.items() if k != "timings_ms"}),
            )
            observations.append(span)
            if summary.get("timings_ms"):
                observations.extend(_retrieval_spans(span, summary["timings_ms"], oid, str(e.seq)))
        else:  # rejections, policy stops, approvals, corrections, the final decision
            observations.append(
                Observation(
                    oid(f"event:{e.seq}"),
                    "event",
                    f"{e.kind}:{e.name}",
                    e.at,
                    parent_id=parent,
                    level=_level(e),
                    # Messages can echo user/model text and approval events carry arguments:
                    # both are content, so capture_io=False drops them.
                    status_message=e.error_code or (e.message if capture_io else None),
                    metadata=redact(
                        {
                            "status": e.status,
                            **{
                                k: v
                                for k, v in e.data.items()
                                if capture_io or k not in ("arguments", "message")
                            },
                        }
                    ),
                )
            )
    if step is not None and step.end is None:
        step.end = ctx.ledger[-1].at

    now = datetime.now(UTC)
    start = min((o.start for o in observations), default=now)
    end = max((o.end or o.start for o in observations), default=now)
    prompt_version = nodes.PROMPT_VERSION if result.mode == "graph" else custom_loop.PROMPT_VERSION
    workflow_version = (
        graph.WORKFLOW_VERSION if result.mode == "graph" else custom_loop.WORKFLOW_VERSION
    )
    return TraceRecord(
        id=ctx.execution_id,
        name=f"agent.{result.mode}",
        start=start,
        end=end,
        source=ctx.source,
        terminal=result.status != "awaiting_approval",
        input={"question": question[:_MAX_TEXT]} if capture_io and question else None,
        output=(
            {
                "status": result.status,
                "answer": result.answer[:_MAX_TEXT] if capture_io else None,
                "citations": [f"{c.source_id}:{c.filename}" for c in result.citations],
            }
        ),
        metadata={
            "request_id": ctx.request_id,
            "tenant_id": ctx.tenant_id,
            "mode": result.mode,
            "status": result.status,
            "error_code": result.error_code,
            "verification": result.verification,
            "steps": result.steps,
            "model_calls": result.model_calls,
            "tool_calls": result.tool_calls,
            "corrections": result.corrections,
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "cost_usd": None if result.cost_usd is None else str(result.cost_usd),
            "total_ms": result.total_ms,
            "prompt_version": prompt_version,
            "workflow_version": workflow_version,
            # Only the first _MAX_OBSERVATIONS ledger events become observations; say so.
            "ledger_events": len(ctx.ledger),
            "observations_truncated": len(ctx.ledger) > _MAX_OBSERVATIONS,
            "cited_chunk_ids": [c.chunk_id for c in result.citations],
        },
        tags=[result.mode, result.status, ctx.source],
        observations=observations,
    )


class LogSink:
    """The local implementation: one structured log line per trace and per score."""

    name = "log"

    def accepts(self, trace: TraceRecord) -> bool:
        return True

    async def export(self, trace: TraceRecord) -> None:
        kinds = [o.kind for o in trace.observations]
        logger.info(
            "trace",
            extra={
                "trace_id": trace.id.hex,
                "trace_name": trace.name,
                "source": trace.source,
                "duration_ms": round((trace.end - trace.start).total_seconds() * 1000, 1),
                "spans": kinds.count("span"),
                "generations": kinds.count("generation"),
                "events": kinds.count("event"),
                **{k: trace.metadata[k] for k in ("status", "request_id", "prompt_version")},
            },
        )

    async def score(self, score: Score, source: str) -> None:
        logger.info(
            "score",
            extra={"trace_id": score.trace_id.hex, "score": score.name, "value": score.value},
        )


class Telemetry:
    def __init__(
        self, sinks: list[TraceSink], *, capture_io: bool = True, timeout_s: float = 10.0
    ) -> None:
        self.sinks = sinks
        self._capture_io, self._timeout_s = capture_io, timeout_s
        self._pending: set[asyncio.Task[None]] = set()

    @property
    def sink_names(self) -> list[str]:
        return [s.name for s in self.sinks]

    def submit(
        self, ctx: ExecutionContext, result: AgentRunResult, question: str | None = None
    ) -> None:
        """Schedule the export of this execution's trace. Never raises, never blocks."""
        if not self.sinks:
            return
        try:
            trace = build_trace(ctx, result, question=question, capture_io=self._capture_io)
        except Exception:
            logger.exception("could not build trace", extra={"execution_id": str(ctx.execution_id)})
            return
        for sink in self.sinks:
            if sink.accepts(trace):
                self._spawn(sink.name, sink.export(trace))

    def score(self, score: Score, source: str) -> None:
        for sink in self.sinks:
            self._spawn(sink.name, sink.score(score, source))

    def _spawn(self, sink: str, coro: Coroutine[Any, Any, None]) -> None:
        if len(self._pending) >= _MAX_PENDING:
            coro.close()  # backpressure: drop rather than grow without bound
            logger.warning("telemetry backlog full; dropping export", extra={"sink": sink})
            return
        task = asyncio.create_task(self._guarded(sink, coro))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _guarded(self, sink: str, coro: Coroutine[Any, Any, None]) -> None:
        try:
            async with asyncio.timeout(self._timeout_s):
                await coro
        except Exception as exc:  # observability must never take the application down
            logger.warning("telemetry export failed", extra={"sink": sink, "error": repr(exc)})

    async def flush(self, timeout_s: float | None = None) -> None:
        if self._pending:
            await asyncio.wait(set(self._pending), timeout=timeout_s or self._timeout_s)
