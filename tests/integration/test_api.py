import asyncio
import json
import logging
from typing import Any

import httpx
import pytest

from app.llm.provider import FakeProvider
from tests.conftest import KEY_B

pytestmark = pytest.mark.integration

DOC = b"# Failover\n\nPromote the standby with patronictl failover. RPO is 5 minutes."


async def upload(
    client: httpx.AsyncClient, name: str = "runbook.md", content: bytes = DOC, **kw: Any
) -> httpx.Response:
    return await client.post("/api/v1/documents", files={"file": (name, content)}, **kw)


async def wait_ready(client: httpx.AsyncClient, doc_id: str) -> dict[str, Any]:
    for _ in range(50):
        body: dict[str, Any] = (await client.get(f"/api/v1/documents/{doc_id}")).json()
        if body["status"] in ("ready", "failed"):
            return body
        await asyncio.sleep(0.05)
    raise AssertionError("document never finished processing")


async def test_health_and_openapi(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health")).json() == {"status": "ok"}
    paths = (await client.get("/openapi.json")).json()["paths"]
    for path in (
        "/api/v1/documents",
        "/api/v1/retrieval/search",
        "/api/v1/rag/answer",
        "/api/v1/llm/chat",
    ):
        assert path in paths


async def test_ready_reports_database(client: httpx.AsyncClient) -> None:
    resp = await client.get("/ready")
    assert resp.status_code == 200 and resp.json()["checks"]["database"] == "ok"


async def test_upload_search_answer_flow(client: httpx.AsyncClient) -> None:
    resp = await upload(client)
    assert resp.status_code == 202
    doc_id = resp.json()["document"]["id"]
    assert resp.headers["location"] == f"/api/v1/documents/{doc_id}"
    assert (await wait_ready(client, doc_id))["status"] == "ready"

    again = await upload(client, name="copy.md")
    assert again.status_code == 200 and again.json()["duplicate"] is True

    search = (
        await client.post("/api/v1/retrieval/search", json={"query": "patronictl failover"})
    ).json()
    assert search["results"][0]["document_id"] == doc_id

    answer = (
        await client.post("/api/v1/rag/answer", json={"question": "How to fail over?"})
    ).json()
    assert answer["status"] == "answered" and answer["model_calls"] == 1
    assert answer["citations"][0]["document_id"] == doc_id
    assert answer["pricing_source"] == "local" and answer["prompt_version"] == "rag-answer-v1"

    listing = (await client.get("/api/v1/documents?limit=10")).json()
    assert listing["total"] == 1

    assert (await client.post(f"/api/v1/documents/{doc_id}/reindex")).status_code == 202
    assert (await wait_ready(client, doc_id))["status"] == "ready"
    assert (await client.delete(f"/api/v1/documents/{doc_id}")).status_code == 204
    assert (await client.get(f"/api/v1/documents/{doc_id}")).status_code == 404


async def test_metadata_is_validated(client: httpx.AsyncClient) -> None:
    ok = await upload(
        client,
        name="t.md",
        content=b"tagged doc",
        data={"metadata": json.dumps({"team": "payments"})},
    )
    assert ok.status_code == 202 and ok.json()["document"]["metadata"] == {"team": "payments"}
    bad = await client.post(
        "/api/v1/documents",
        files={"file": ("u.md", b"x y")},
        data={"metadata": '{"nested": {"no": 1}}'},
    )
    assert bad.status_code == 422


async def test_authentication_required(client: httpx.AsyncClient) -> None:
    for key in (None, "wrong-key-wrong-key"):
        headers = {"X-API-Key": key} if key else {}
        resp = await httpx.AsyncClient(
            transport=client._transport, base_url="http://test", headers=headers
        ).get("/api/v1/documents")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthenticated"


async def test_cross_tenant_access_is_not_found(client: httpx.AsyncClient) -> None:
    doc_id = (await upload(client)).json()["document"]["id"]
    await wait_ready(client, doc_id)
    other = {"X-API-Key": KEY_B}
    assert (await client.get(f"/api/v1/documents/{doc_id}", headers=other)).status_code == 404
    assert (await client.delete(f"/api/v1/documents/{doc_id}", headers=other)).status_code == 404
    assert (
        await client.post(f"/api/v1/documents/{doc_id}/reindex", headers=other)
    ).status_code == 404
    search = await client.post(
        "/api/v1/retrieval/search", headers=other, json={"query": "patronictl failover"}
    )
    assert search.json()["results"] == []


@pytest.mark.parametrize(
    ("name", "data", "status", "code"),
    [
        ("malware.exe", b"MZ\x90", 415, "unsupported_file_type"),
        ("fake.pdf", b"not a pdf", 422, "document_parse_error"),
        ("empty.md", b"   ", 422, "empty_document"),
    ],
)
async def test_bad_uploads_have_consistent_errors(
    client: httpx.AsyncClient, name: str, data: bytes, status: int, code: str
) -> None:
    resp = await upload(client, name=name, content=data, headers={"X-Request-ID": "req-123"})
    assert resp.status_code == status
    assert resp.json() == {
        "error": {"code": code, "message": resp.json()["error"]["message"], "request_id": "req-123"}
    }
    assert resp.headers["x-request-id"] == "req-123"


async def test_upload_too_large(client: httpx.AsyncClient) -> None:
    resp = await upload(client, name="big.txt", content=b"a" * (10 * 1024 * 1024 + 1))
    assert resp.status_code == 413


async def test_validation_errors_do_not_echo_input(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/retrieval/search", json={"query": "", "top_k": 999})
    body = resp.json()["error"]
    assert resp.status_code == 422 and body["code"] == "invalid_request"
    assert all(set(d) == {"loc", "msg"} for d in body["details"])


async def test_answer_with_no_documents_skips_the_model(
    client: httpx.AsyncClient, fake_llm: FakeProvider
) -> None:
    resp = (await client.post("/api/v1/rag/answer", json={"question": "What is the RPO?"})).json()
    assert resp["status"] == "insufficient_evidence" and resp["model_calls"] == 0
    assert fake_llm.requests == []


async def test_invalid_citations_get_one_correction_then_fail(
    client: httpx.AsyncClient, fake_llm: FakeProvider
) -> None:
    doc_id = (await upload(client)).json()["document"]["id"]
    await wait_ready(client, doc_id)
    fake_llm._responder = lambda req: '{"answer": "Made up [S42]", "citations": ["S42"]}'
    resp = (await client.post("/api/v1/rag/answer", json={"question": "failover?"})).json()
    assert resp["status"] == "failed_validation"
    assert resp["model_calls"] == 2 and resp["correction_attempts"] == 1
    assert resp["citations"] == [] and "S42" not in resp["answer"]
    assert "Valid source IDs" in fake_llm.requests[-1].messages[-1].content


async def test_correction_can_recover(client: httpx.AsyncClient, fake_llm: FakeProvider) -> None:
    doc_id = (await upload(client)).json()["document"]["id"]
    await wait_ready(client, doc_id)
    replies = iter(
        [
            '{"answer": "no cite", "citations": []}',
            '{"answer": "Use patronictl [S1].", "citations": ["S1"]}',
        ]
    )
    fake_llm._responder = lambda req: next(replies)
    resp = (await client.post("/api/v1/rag/answer", json={"question": "failover?"})).json()
    assert resp["status"] == "answered" and resp["correction_attempts"] == 1


async def test_llm_chat_endpoint_reports_usage(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/llm/chat",
        json={"messages": [{"role": "user", "content": "hello"}], "role": "small"},
    )
    body = resp.json()
    assert resp.status_code == 200 and body["provider"] == "fake"
    assert body["model"] == "qwen3:1.7b" and body["cost_usd"] == "0"
    assert body["usage"]["input_tokens"] >= 1 and body["attempts"] == 1


async def test_unknown_route_uses_error_shape(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/nope")
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"


async def test_access_log_includes_tenant(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="app")
    await client.get("/api/v1/documents")
    access = [r for r in caplog.records if r.getMessage() == "request"]
    assert access and access[-1].tenant_id == "tenant_a"  # type: ignore[attr-defined]


async def test_logs_inside_endpoints_carry_tenant(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    from app.core.logging import tenant_id_var

    seen: list[str | None] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.getMessage() == "document created":
                seen.append(tenant_id_var.get())

    caplog.set_level("INFO", logger="app.ingestion.pipeline")
    handler = Capture()
    logging.getLogger("app.ingestion.pipeline").addHandler(handler)
    try:
        await upload(client, name="ctx.md", content=b"tenant context check")
    finally:
        logging.getLogger("app.ingestion.pipeline").removeHandler(handler)
    assert seen == ["tenant_a"]
