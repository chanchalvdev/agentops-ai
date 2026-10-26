"""Workspace endpoints and the terminal WebSocket on a real database (audit rows included)."""

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import Settings
from app.main import create_app
from tests.conftest import KEY_A, KEY_B

pytestmark = pytest.mark.integration


async def test_file_browser_round_trip(client: httpx.AsyncClient) -> None:
    put = await client.put("/api/v1/workspace/file", json={"path": "docs/a.md", "content": "hi\n"})
    assert put.status_code == 200 and put.json()["created"] is True
    tree = (await client.get("/api/v1/workspace/tree", params={"path": "docs"})).json()
    assert [e["name"] for e in tree["entries"]] == ["a.md"]
    file = (await client.get("/api/v1/workspace/file", params={"path": "docs/a.md"})).json()
    assert file["content"] == "hi\n"
    found = (await client.get("/api/v1/workspace/search", params={"q": "HI"})).json()
    assert found["matches"][0]["path"] == "docs/a.md"


async def test_jail_errors_are_clean_api_errors(client: httpx.AsyncClient) -> None:
    escape = await client.get("/api/v1/workspace/file", params={"path": "../../etc/passwd"})
    assert escape.status_code == 403
    assert escape.json()["error"]["code"] == "workspace_access_denied"
    missing = await client.get("/api/v1/workspace/file", params={"path": "nope.md"})
    assert missing.status_code == 404
    bad = await client.post("/api/v1/workspace/exec", json={"argv": ["rm", "-rf", "."]})
    assert bad.status_code == 403 and bad.json()["error"]["code"] == "command_not_allowed"
    piped = await client.post("/api/v1/workspace/exec", json={"line": "ls | rm x"})
    assert piped.status_code == 422


async def test_exec_runs_allowlisted_commands(client: httpx.AsyncClient) -> None:
    await client.put("/api/v1/workspace/file", json={"path": "n.md", "content": "RPO 5 min\n"})
    resp = await client.post("/api/v1/workspace/exec", json={"line": "grep -n 'RPO 5' n.md"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["exit_code"] == 0 and resp.json()["output"] == "1:RPO 5 min\n"


async def test_tenant_without_workspace_permission_is_refused(client: httpx.AsyncClient) -> None:
    headers = {"X-API-Key": KEY_B}
    for method, url, kw in [
        ("GET", "/api/v1/workspace/tree", {}),
        ("PUT", "/api/v1/workspace/file", {"json": {"path": "x", "content": "y"}}),
        ("POST", "/api/v1/workspace/exec", {"json": {"argv": ["ls"]}}),
    ]:
        resp = await client.request(method, url, headers=headers, **kw)
        assert resp.status_code == 403, url
        assert resp.json()["error"]["code"] == "permission_denied"


async def test_audit_log_records_rejections_and_is_tenant_scoped(
    client: httpx.AsyncClient,
) -> None:
    await client.get("/api/v1/workspace/file", params={"path": "/etc/passwd"})
    await client.post("/api/v1/workspace/exec", json={"argv": ["ls"]})
    await client.post("/api/v1/workspace/exec", json={"argv": ["curl", "x"]})  # refused at planning
    rows = (await client.get("/api/v1/workspace/audit")).json()
    assert [(r["operation"], r["status"]) for r in rows] == [
        ("exec", "rejected"),
        ("exec", "ok"),
        ("read", "rejected"),
    ]
    assert rows[0]["error_code"] == "command_not_allowed"
    rows = rows[1:]
    assert rows[1]["error_code"] == "workspace_access_denied" and rows[0]["actor"] == "user"
    assert rows[0]["key_id"] and rows[0]["exit_code"] == 0


def _terminal_app(settings: Settings) -> Any:
    # TestClient runs the app on its own event loop, so the app builds (and closes) its own
    # services there through the lifespan instead of sharing the session-loop fixtures.
    return create_app(settings.model_copy(update={"agent_checkpointer": "memory"}))


def test_terminal_streams_output_and_enforces_policy(
    database: str, test_settings: Settings
) -> None:
    with TestClient(_terminal_app(test_settings)) as tc:
        with tc.websocket_connect("/api/v1/workspace/terminal") as ws:
            ws.send_json({"api_key": KEY_A})
            assert ws.receive_json() == {"type": "ready", "tenant": "tenant_a"}
            ws.send_json({"line": "ls -a"})
            frames = []
            while (frame := ws.receive_json())["type"] != "exit":
                frames.append(frame)
            assert frame["exit_code"] == 0 and all(f["type"] == "output" for f in frames)
            ws.send_json({"argv": ["cat", "../../etc/passwd"]})
            assert ws.receive_json()["code"] == "workspace_access_denied"
            ws.send_json({"line": "curl http://example.com"})
            assert ws.receive_json()["code"] == "command_not_allowed"
            ws.send_text("not json")
            assert ws.receive_json()["code"] == "invalid_request"  # session survives
            ws.send_json({"argv": ["ls"]})
            while ws.receive_json()["type"] != "exit":
                pass

        with tc.websocket_connect("/api/v1/workspace/terminal") as ws:
            ws.send_json({"api_key": "wrong-key-wrong-key"})
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 4401

        with (
            tc.websocket_connect("/api/v1/workspace/terminal", headers={"X-API-Key": KEY_B}) as ws,
            pytest.raises(WebSocketDisconnect) as closed,
        ):
            ws.receive_json()
        assert closed.value.code == 4403
