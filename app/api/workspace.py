"""Workspace API for people (the UI file browser and terminal panel).

The same jail, command rules, executor, and audit log as the agent's workspace_* tools. A user
issuing a command is the human, so nothing waits for approval here; the permission check is
what separates read-only from write/exec access.
"""

import asyncio
import contextlib
import json
import logging
import shlex
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError, model_validator

from app.api.deps import PrincipalDep, ServicesDep, rate_limited
from app.core.errors import (
    AppError,
    AuthenticationError,
    InvalidRequestError,
    PermissionDeniedError,
    TooManyRequestsError,
)
from app.core.security import Principal
from app.services import Services
from app.workspace.audit import AuditOut
from app.workspace.executor import ExecResult
from app.workspace.jail import Actor, FileContent, ListResult, SearchResult, WriteResult

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/workspace", tags=["workspace"])
_OPERATOR_CHARS = frozenset("();<>|&")


def _require(principal: Principal, permission: str) -> None:
    if permission not in principal.permissions:
        raise PermissionDeniedError(f"This API key lacks the {permission} permission")


def _actor(principal: Principal) -> Actor:
    return Actor("user", key_id=principal.key_id)


class FileWrite(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=5_000_000)  # the configured write limit applies on top


class ExecRequest(BaseModel):
    """Either argv, or a command line split like a shell would split words (quotes work) but
    with no shell features: pipes, redirection, `;`, `&&`, and substitution are rejected."""

    argv: list[str] | None = Field(default=None, min_length=1, max_length=32)
    line: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _one_of(self) -> "ExecRequest":
        if (self.argv is None) == (self.line is None):
            raise ValueError("send exactly one of argv or line")
        return self

    def to_argv(self) -> list[str]:
        if self.argv is not None:
            return self.argv
        assert self.line is not None
        lexer = shlex.shlex(self.line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True  # unquoted operators still become their own tokens
        try:
            argv = list(lexer)
        except ValueError:
            raise InvalidRequestError("Unbalanced quotes in command line") from None
        if any(set(token) <= _OPERATOR_CHARS for token in argv):
            # Nothing would interpret them, but silently passing "|" to ls as a filename
            # would hide the mistake. `$` needs no check: no shell, so no substitution.
            raise InvalidRequestError("Pipes, redirects, ; and && are not supported (no shell)")
        if not argv:
            raise InvalidRequestError("Empty command")
        return argv


async def _exec_permission(services: Services, principal: Principal, argv: list[str]) -> None:
    """Plan the command (allowlist + jail on its arguments), then check the permission it needs.
    Refusals here never reach Workspace.stream, so they are audited explicitly."""
    ws = services.workspaces.for_tenant(principal.tenant_id)
    try:
        plan = ws.plan(argv)
        _require(principal, "workspace:exec" if plan.writes else "workspace:read")
    except AppError as exc:
        await ws.record_rejection(_actor(principal), "exec", shlex.join(argv), exc)
        raise


@router.get("/tree", response_model=ListResult, summary="List a workspace directory")
async def tree(
    services: ServicesDep, principal: PrincipalDep, path: str = Query(default=".", max_length=500)
) -> ListResult:
    _require(principal, "workspace:read")
    ws = services.workspaces.for_tenant(principal.tenant_id)
    return await ws.list_dir(path, _actor(principal))


@router.get("/file", response_model=FileContent, summary="Read a text file")
async def read_file(
    services: ServicesDep,
    principal: PrincipalDep,
    path: str = Query(min_length=1, max_length=500),
    max_bytes: int | None = Query(default=None, ge=1),
) -> FileContent:
    _require(principal, "workspace:read")
    ws = services.workspaces.for_tenant(principal.tenant_id)
    return await ws.read(path, _actor(principal), max_bytes)


@router.put(
    "/file",
    response_model=WriteResult,
    summary="Create or overwrite a text file",
    dependencies=[rate_limited("workspace")],
)
async def write_file(
    body: FileWrite, services: ServicesDep, principal: PrincipalDep
) -> WriteResult:
    _require(principal, "workspace:write")
    ws = services.workspaces.for_tenant(principal.tenant_id)
    return await ws.write(body.path, body.content, _actor(principal))


@router.get("/search", response_model=SearchResult, summary="Find exact text in workspace files")
async def search(
    services: ServicesDep,
    principal: PrincipalDep,
    q: str = Query(min_length=1, max_length=200),
    glob: str = Query(default="**/*", max_length=200),
    case_sensitive: bool = False,
) -> SearchResult:
    _require(principal, "workspace:read")
    ws = services.workspaces.for_tenant(principal.tenant_id)
    return await ws.search(q, _actor(principal), glob=glob, case_sensitive=case_sensitive)


@router.post(
    "/exec",
    response_model=ExecResult,
    summary="Run an allowlisted command",
    dependencies=[rate_limited("workspace")],
)
async def exec_command(
    body: ExecRequest, services: ServicesDep, principal: PrincipalDep
) -> ExecResult:
    argv = body.to_argv()
    await _exec_permission(services, principal, argv)
    ws = services.workspaces.for_tenant(principal.tenant_id)
    return await ws.exec(argv, _actor(principal))


@router.get("/audit", response_model=list[AuditOut], summary="Workspace audit log, newest first")
async def audit(
    services: ServicesDep, principal: PrincipalDep, limit: int = Query(default=100, ge=1, le=500)
) -> Any:
    _require(principal, "workspace:read")
    return await services.workspace_audit.recent(principal.tenant_id, limit)


def _terminal_rate_limit(services: Services, principal: Principal) -> None:
    """Each terminal command takes a token from the same bucket as POST /workspace/exec, so
    the WebSocket is not a way around the HTTP rate limit."""
    if not services.settings.rate_limit_enabled:
        return
    wait = services.rate_limiter.acquire(principal.tenant_id, "workspace")
    if wait is not None:
        raise TooManyRequestsError(
            f"Rate limit exceeded for workspace commands; retry in {wait:.0f} s",
            retry_after_s=wait,
        )


async def _run_watched(
    websocket: WebSocket, items: AsyncIterator[str | ExecResult]
) -> tuple[bool, Any]:
    """Forward one command's output while also listening to the socket, so a client that
    disconnects during a silent command (e.g. a long test run) kills it at once instead of
    when the next output arrives. Frames sent while the command runs get a "busy" error (one
    command at a time); a frame sent after the exit frame is the next command and is returned.
    Returns (client still connected, next frame or None)."""
    exited = asyncio.Event()

    async def forward() -> None:
        async for item in items:
            if isinstance(item, ExecResult):
                await websocket.send_json({"type": "exit", **item.model_dump()})
                exited.set()
            else:
                await websocket.send_json({"type": "output", "data": item})

    command = asyncio.create_task(forward())
    pending: Any = None
    try:
        while not command.done():
            incoming = asyncio.ensure_future(websocket.receive())
            done, _ = await asyncio.wait({command, incoming}, return_when=asyncio.FIRST_COMPLETED)
            if incoming not in done:
                incoming.cancel()
                await asyncio.gather(incoming, return_exceptions=True)
                break
            frame = incoming.result()
            if frame.get("type") == "websocket.disconnect":
                return False, None
            if exited.is_set():
                pending = frame  # the next command: handle it once this one is wrapped up
                break
            await websocket.send_json(
                {"type": "error", "code": "busy", "message": "a command is still running"}
            )
        await command  # re-raises AppError / disconnect from the command itself
        return True, pending
    finally:
        if not command.done():
            command.cancel()  # closes the stream: the executor kills the process group
            await asyncio.gather(command, return_exceptions=True)


@router.websocket("/terminal")
async def terminal(websocket: WebSocket) -> None:
    """Terminal panel protocol (JSON frames). Not a PTY: one allowlisted command at a time.

    client -> {"api_key": "..."}            first frame, unless an X-API-Key header was sent
    server -> {"type": "ready", "tenant": ...}
    client -> {"line": "git status"} or {"argv": ["git", "status"]}
    server -> {"type": "output", "data": "..."}* then {"type": "exit", ...ExecResult}
           or {"type": "error", "code": ..., "message": ...}
    Close codes: 4401 unauthenticated, 4403 no workspace permission.
    """
    services: Services = websocket.app.state.services
    await websocket.accept()
    try:
        key = websocket.headers.get("x-api-key")
        if not key:
            async with asyncio.timeout(10):
                first = await websocket.receive_json()
            key = first.get("api_key") if isinstance(first, dict) else None
        principal = services.auth.authenticate(key)
    except (AuthenticationError, TimeoutError, ValueError):
        await websocket.close(code=4401, reason="unauthenticated")
        return
    except WebSocketDisconnect:
        return
    if "workspace:read" not in principal.permissions:
        await websocket.close(code=4403, reason="no workspace permission")
        return
    ws = services.workspaces.for_tenant(principal.tenant_id)
    await websocket.send_json({"type": "ready", "tenant": principal.tenant_id})
    next_frame: Any = None
    with contextlib.suppress(WebSocketDisconnect):
        while True:
            try:
                if next_frame is not None:
                    raw, next_frame = next_frame, None
                    if raw.get("text") is None:
                        raise ValueError("binary frame")
                    message = json.loads(raw["text"])
                else:
                    message = await websocket.receive_json()
            except ValueError:  # not JSON: tell the client, keep the session
                await websocket.send_json(
                    {"type": "error", "code": "invalid_request", "message": "frames must be JSON"}
                )
                continue
            try:
                argv = ExecRequest.model_validate(message).to_argv()
                _terminal_rate_limit(services, principal)
                await _exec_permission(services, principal, argv)
                connected, next_frame = await _run_watched(
                    websocket, ws.stream(argv, _actor(principal))
                )
                if not connected:
                    return  # client went away mid-command: the command was killed
            except ValidationError:
                await websocket.send_json(
                    {
                        "type": "error",
                        "code": "invalid_request",
                        "message": "send exactly one of argv or line",
                    }
                )
            except AppError as exc:
                await websocket.send_json(
                    {"type": "error", "code": exc.code, "message": exc.message}
                )
