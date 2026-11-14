"""Day 5: the two read endpoints the web UI needs and the API lacked: the caller's recent
executions (Executions screen) and one source chunk by id (clicking a citation)."""

import uuid
from typing import Any

import httpx
import pytest

from tests.conftest import KEY_B

pytestmark = pytest.mark.integration

DR = "# Disaster recovery\n\nThe recovery point objective (RPO) is 5 minutes."


async def test_executions_list_is_newest_first_and_tenant_scoped(
    client: httpx.AsyncClient, ingest: Any
) -> None:
    await ingest("dr.md", DR)
    first = (await client.post("/api/v1/agent/run", json={"question": "What is the RPO?"})).json()
    second = (await client.post("/api/v1/agent/run", json={"question": "RPO again?"})).json()

    listed = (await client.get("/api/v1/executions")).json()
    assert [e["id"] for e in listed[:2]] == [second["execution_id"], first["execution_id"]]
    assert listed[0]["question"] == "RPO again?" and listed[0]["status"] == second["status"]

    limited = (await client.get("/api/v1/executions", params={"limit": 1})).json()
    assert len(limited) == 1
    by_status = (await client.get("/api/v1/executions", params={"status": "nope"})).json()
    assert by_status == []

    theirs = (await client.get("/api/v1/executions", headers={"X-API-Key": KEY_B})).json()
    assert theirs == []
    bad = await client.get("/api/v1/executions", params={"limit": 0})
    assert bad.status_code == 422


async def test_citation_chunk_can_be_opened_by_its_owner_only(
    client: httpx.AsyncClient, ingest: Any
) -> None:
    await ingest("dr.md", DR)
    run = (await client.post("/api/v1/agent/run", json={"question": "What is the RPO?"})).json()
    assert run["status"] == "answered", run
    citation = run["citations"][0]
    path = f"/api/v1/documents/{citation['document_id']}/chunks/{citation['chunk_id']}"

    chunk = await client.get(path)
    assert chunk.status_code == 200, chunk.text
    body = chunk.json()
    assert body["filename"] == "dr.md" and "5 minutes" in body["text"]
    assert body["text"].startswith(citation["snippet"][:50])
    assert body["char_end"] > body["char_start"]

    other = await client.get(path, headers={"X-API-Key": KEY_B})
    assert other.status_code == 404  # same answer as a missing chunk: no existence leak
    wrong_doc = await client.get(f"/api/v1/documents/{uuid.uuid4()}/chunks/{citation['chunk_id']}")
    assert wrong_doc.status_code == 404


async def test_operators_see_evaluation_runs_started_from_the_cli(
    client: httpx.AsyncClient, services: Any
) -> None:
    from app.evaluation.dataset import Dataset, EvalCase
    from app.evaluation.store import EvaluationStore

    store = EvaluationStore(services.sessions)
    case = EvalCase.model_validate(
        {"id": "c1", "category": "answerable", "question": "q?", "dataset_version": "t1"}
    )
    ds = Dataset(name="golden_questions", version="t1", sha256="u" * 64, cases=[case])
    run = await store.start_run(await store.upsert_dataset(ds), "cli", "graph", {"mode": "graph"})

    runs = (await client.get("/api/v1/evaluations/runs")).json()  # tenant_a is an operator
    assert any(r["id"] == str(run.id) and r["requested_by"] == "cli" for r in runs)
    detail = await client.get(f"/api/v1/evaluations/runs/{run.id}")
    assert detail.status_code == 200, detail.text
    denied = await client.get("/api/v1/evaluations/runs", headers={"X-API-Key": KEY_B})
    assert denied.status_code == 403
