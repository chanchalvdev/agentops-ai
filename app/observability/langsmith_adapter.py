"""Optional LangSmith sink over its REST API (POST /runs/batch, POST /feedback).

Off unless AGENTOPS_LANGSMITH_API_KEY is set. By default (scope "eval") it receives ONLY
evaluation runs: LangSmith and Langfuse can be compared on the same small evaluation subset
without sending production traffic to a second vendor. Only finished executions are sent (a run
paused for approval is sent once it completes), so every run id is posted exactly once.

Verified with mocked HTTP only; no live LangSmith project was available while building this.
"""

from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import SecretStr

from app.observability.interfaces import Observation, Score, TraceRecord

_RUN_TYPE = {"generation": "llm", "span": "chain", "event": "chain"}


def _fmt(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def to_runs(trace: TraceRecord, project: str) -> list[dict[str, Any]]:
    """Root run + one child run per observation. dotted_order encodes the tree for LangSmith:
    parent's dotted_order + "." + start time + run id."""
    ok = trace.metadata.get("status") in ("answered", "insufficient_evidence")
    dotted = {trace.id: f"{_fmt(trace.start)}{trace.id}"}
    runs: list[dict[str, Any]] = [
        {
            "id": str(trace.id),
            "trace_id": str(trace.id),
            "dotted_order": dotted[trace.id],
            "name": trace.name,
            "run_type": "chain",
            "start_time": _iso(trace.start),
            "end_time": _iso(trace.end),
            "inputs": trace.input or {},
            "outputs": trace.output or {},
            "error": None if ok else trace.metadata.get("error_code") or trace.metadata["status"],
            "session_name": project,
            "tags": trace.tags,
            "extra": {"metadata": trace.metadata},
        }
    ]
    for o in trace.observations:
        parent = o.parent_id or trace.id
        dotted[o.id] = f"{dotted.get(parent, dotted[trace.id])}.{_fmt(o.start)}{o.id}"
        run: dict[str, Any] = {
            "id": str(o.id),
            "trace_id": str(trace.id),
            "parent_run_id": str(parent),
            "dotted_order": dotted[o.id],
            "name": o.name,
            "run_type": "tool" if o.name.startswith("tool:") else _RUN_TYPE[o.kind],
            "start_time": _iso(o.start),
            "end_time": _iso(o.end or o.start),
            "inputs": o.input if isinstance(o.input, dict) else {},
            "outputs": o.output if isinstance(o.output, dict) else {},
            "error": o.status_message if o.level == "ERROR" else None,
            "session_name": project,
            "extra": {"metadata": _metadata(o)},
        }
        if o.kind == "generation":
            i, out = o.input_tokens or 0, o.output_tokens or 0
            run["outputs"] = {
                "usage_metadata": {"input_tokens": i, "output_tokens": out, "total_tokens": i + out}
            }
        runs.append(run)
    return runs


def _metadata(o: Observation) -> dict[str, Any]:
    meta = dict(o.metadata)
    if o.model:
        meta["ls_model_name"] = o.model
        meta["ls_provider"] = meta.get("provider", "ollama")
    if o.level != "DEFAULT":
        meta["level"] = o.level
    return meta


class LangSmithSink:
    name = "langsmith"

    def __init__(
        self,
        http: httpx.AsyncClient,
        endpoint: str,
        api_key: SecretStr,
        project: str,
        scope: Literal["eval", "all"] = "eval",
    ) -> None:
        self._http, self._base = http, endpoint.rstrip("/")
        self._headers = {"x-api-key": api_key.get_secret_value()}
        self._project, self._scope = project, scope

    def _in_scope(self, source: str) -> bool:
        return self._scope == "all" or source == "eval"

    def accepts(self, trace: TraceRecord) -> bool:
        return trace.terminal and self._in_scope(trace.source)

    async def export(self, trace: TraceRecord) -> None:
        resp = await self._http.post(
            f"{self._base}/runs/batch",
            json={"post": to_runs(trace, self._project)},
            headers=self._headers,
        )
        resp.raise_for_status()

    async def score(self, score: Score, source: str) -> None:
        if not self._in_scope(source):
            return
        resp = await self._http.post(
            f"{self._base}/feedback",
            json={
                "run_id": str(score.trace_id),
                "key": score.name,
                "score": score.value,
                "comment": score.comment,
            },
            headers=self._headers,
        )
        resp.raise_for_status()
