"""Workspace API for people (the UI file browser and terminal panel).

The same jail, command rules, executor, and audit log as the agent's workspace_* tools. A user
issuing a command is the human, so nothing waits for approval here; the permission check is
what separates read-only from write/exec access.
"""

import asyncio
import contextlib
import logging
import shlex
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError, model_validator

from app.api.deps import PrincipalDep, ServicesDep, rate_limited
from app.core.errors import (
    AppError,
    AuthenticationError,
    InvalidRequestError,
    PermissionDeniedError,
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
    return await services.workspace_audit.list(principal.tenant_id, limit)


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
    with contextlib.suppress(WebSocketDisconnect):
        while True:
            try:
                message = await websocket.receive_json()
            except ValueError:  # not JSON: tell the client, keep the session
                await websocket.send_json(
                    {"type": "error", "code": "invalid_request", "message": "frames must be JSON"}
                )
                continue
            try:
                argv = ExecRequest.model_validate(message).to_argv()
                await _exec_permission(services, principal, argv)
                async for item in ws.stream(argv, _actor(principal)):
                    if isinstance(item, ExecResult):
                        await websocket.send_json({"type": "exit", **item.model_dump()})
                    else:
                        await websocket.send_json({"type": "output", "data": item})
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
