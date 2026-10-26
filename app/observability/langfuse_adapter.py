"""Langfuse sink over its public ingestion HTTP API (POST /api/public/ingestion).

Plain HTTP instead of the SDK: the SDK (v3) brings an OpenTelemetry pipeline and background
threads; this project needs one batched POST per finished execution, under our own timeout and
redaction. Ids are deterministic, so a re-export after an approval resume upserts the same trace.

Verified against Langfuse's documented ingestion schema with mocked HTTP only; no live Langfuse
project was available while building this.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import SecretStr

from app.observability.interfaces import Observation, Score, TraceRecord

_TYPES = {"span": "span-create", "generation": "generation-create", "event": "event-create"}


def _ts(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _envelope(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    # The envelope id must be unique per send (Langfuse de-duplicates on it); the body id is the
    # stable object id, so a second send updates the object instead of being dropped.
    return {
        "id": uuid.uuid4().hex,
        "timestamp": _ts(datetime.now(UTC)),
        "type": kind,
        "body": {k: v for k, v in body.items() if v is not None},
    }


def _observation(trace_id: str, o: Observation) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": str(o.id),
        "traceId": trace_id,
        "parentObservationId": str(o.parent_id) if o.parent_id else None,
        "name": o.name,
        "startTime": _ts(o.start),
        "metadata": o.metadata or None,
        "level": o.level,
        "statusMessage": o.status_message,
        "input": o.input,
        "output": o.output,
    }
    if o.kind != "event":
        body["endTime"] = _ts(o.end or o.start)
    if o.kind == "generation":
        body["model"] = o.model
        if o.input_tokens is not None or o.output_tokens is not None:
            i, out = o.input_tokens or 0, o.output_tokens or 0
            body["usage"] = {"input": i, "output": out, "total": i + out, "unit": "TOKENS"}
    return _envelope(_TYPES[o.kind], body)


def to_batch(trace: TraceRecord) -> list[dict[str, Any]]:
    trace_id = trace.id.hex  # 32 lowercase hex: also a valid W3C / OpenTelemetry trace id
    head = _envelope(
        "trace-create",
        {
            "id": trace_id,
            "timestamp": _ts(trace.start),
            "name": trace.name,
            "input": trace.input,
            "output": trace.output,
            "metadata": trace.metadata,
            "tags": trace.tags,
            "version": trace.metadata.get("workflow_version"),
            "release": trace.metadata.get("prompt_version"),
        },
    )
    return [head, *(_observation(trace_id, o) for o in trace.observations)]


class LangfuseSink:
    name = "langfuse"

    def __init__(
        self, http: httpx.AsyncClient, host: str, public_key: str, secret_key: SecretStr
    ) -> None:
        self._http, self._url = http, host.rstrip("/") + "/api/public/ingestion"
        self._auth = (public_key, secret_key.get_secret_value())

    def accepts(self, trace: TraceRecord) -> bool:
        return True

    async def export(self, trace: TraceRecord) -> None:
        await self._post(to_batch(trace))

    async def score(self, score: Score, source: str) -> None:
        body = {
            "id": str(uuid.uuid5(score.trace_id, f"score:{score.name}")),
            "traceId": score.trace_id.hex,
            "name": score.name,
            "value": score.value,
            "comment": score.comment,
            "dataType": "NUMERIC",
        }
        await self._post([_envelope("score-create", body)])

    async def _post(self, batch: list[dict[str, Any]]) -> None:
        resp = await self._http.post(self._url, json={"batch": batch}, auth=self._auth)
        resp.raise_for_status()
        errors = resp.json().get("errors") if resp.status_code == 207 else None
        if errors:
            raise RuntimeError(f"langfuse rejected {len(errors)} event(s): {errors[:3]}")
