"""workspace_* tools: the agent's view of its tenant's sandboxed directory (product.txt 9.1).

Policy lives in app/workspace (path jail, command rules, audit); these classes only adapt it to
the tool interface. Read-only tools run directly. workspace_write and workspace_edit always need a
human approval; workspace_exec needs one unless the command is on the read-only allowlist.
"""

import hashlib
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from app.core.execution_context import ExecutionContext
from app.tools.base import Tool
from app.workspace.executor import ExecResult
from app.workspace.jail import (
    Actor,
    FileContent,
    ListResult,
    SearchResult,
    Workspace,
    WorkspaceManager,
    WriteResult,
)

PATH = "Path relative to the workspace root, e.g. notes/todo.md"
_AGENT_OUTPUT_CHARS = 6000  # what the model sees of a command's output; the audit keeps size


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class _WorkspaceTool[I: BaseModel, O: BaseModel](Tool[I, O]):
    def __init__(self, workspaces: WorkspaceManager) -> None:
        self._workspaces = workspaces

    def _ws(self, ctx: ExecutionContext) -> Workspace:
        return self._workspaces.for_tenant(ctx.tenant_id)  # tenant from context, not the model

    @staticmethod
    def _actor(ctx: ExecutionContext) -> Actor:
        return Actor("agent", execution_id=ctx.execution_id, call_id=ctx.current_call_id)


class ListInput(BaseModel):
    path: str = Field(default=".", max_length=500, description=PATH)


class WorkspaceList(_WorkspaceTool[ListInput, ListResult]):
    name = "workspace_list"
    description = "List files and directories at a path in the user's workspace."
    input_model, output_model = ListInput, ListResult
    permissions: ClassVar[frozenset[str]] = frozenset({"workspace:read"})
    timeout_s = 10.0

    async def run(self, args: ListInput, ctx: ExecutionContext) -> ListResult:
        return await self._ws(ctx).list_dir(args.path, self._actor(ctx))

    def ledger_output(self, output: ListResult) -> dict[str, Any]:
        return {"entries": len(output.entries)}


class ReadInput(BaseModel):
    path: str = Field(max_length=500, description=PATH)
    max_bytes: int = Field(default=8000, ge=100, le=20_000)


class WorkspaceRead(_WorkspaceTool[ReadInput, FileContent]):
    name = "workspace_read"
    description = "Read a UTF-8 text file from the user's workspace."
    input_model, output_model = ReadInput, FileContent
    permissions: ClassVar[frozenset[str]] = frozenset({"workspace:read"})
    timeout_s = 10.0

    async def run(self, args: ReadInput, ctx: ExecutionContext) -> FileContent:
        return await self._ws(ctx).read(args.path, self._actor(ctx), args.max_bytes)

    def ledger_output(self, output: FileContent) -> dict[str, Any]:
        return {"path": output.path, "size": output.size, "truncated": output.truncated}


class SearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=200, description="Exact text to find.")
    glob: str = Field(default="**/*", max_length=200, description="Limit to matching paths.")
    case_sensitive: bool = False
    max_results: int = Field(default=30, ge=1, le=100)


class WorkspaceSearch(_WorkspaceTool[SearchInput, SearchResult]):
    name = "workspace_search"
    description = (
        "Find lines containing exact text in the user's workspace files (like grep -rF). "
        "Returns path, line number, and the line."
    )
    input_model, output_model = SearchInput, SearchResult
    permissions: ClassVar[frozenset[str]] = frozenset({"workspace:read"})
    timeout_s = 20.0

    async def run(self, args: SearchInput, ctx: ExecutionContext) -> SearchResult:
        return await self._ws(ctx).search(
            args.query,
            self._actor(ctx),
            glob=args.glob,
            case_sensitive=args.case_sensitive,
            max_results=args.max_results,
        )

    def ledger_output(self, output: SearchResult) -> dict[str, Any]:
        return {"matches": len(output.matches), "files_scanned": output.files_scanned}


class WriteInput(BaseModel):
    path: str = Field(max_length=500, description=PATH)
    content: str = Field(max_length=200_000, description="The complete new file content.")


class WorkspaceWrite(_WorkspaceTool[WriteInput, WriteResult]):
    name = "workspace_write"
    description = (
        "Create or overwrite a text file in the user's workspace with the full content given. "
        "A human must approve it before it runs."
    )
    input_model, output_model = WriteInput, WriteResult
    permissions: ClassVar[frozenset[str]] = frozenset({"workspace:write"})
    timeout_s = 10.0
    idempotent = False
    requires_approval = True

    async def run(self, args: WriteInput, ctx: ExecutionContext) -> WriteResult:
        return await self._ws(ctx).write(args.path, args.content, self._actor(ctx))

    def ledger_arguments(self, args: WriteInput) -> dict[str, Any]:
        return {"path": args.path, "chars": len(args.content), "sha256": _sha(args.content)}

    def ledger_output(self, output: WriteResult) -> dict[str, Any]:
        return output.model_dump(include={"path", "bytes", "sha256", "created", "replayed"})


class EditInput(BaseModel):
    path: str = Field(max_length=500, description=PATH)
    old: str = Field(min_length=1, max_length=20_000, description="Exact text to replace once.")
    new: str = Field(max_length=20_000, description="Replacement text.")


class WorkspaceEdit(_WorkspaceTool[EditInput, WriteResult]):
    name = "workspace_edit"
    description = (
        "Replace one exact occurrence of `old` with `new` in a workspace file. Read the file "
        "first. A human must approve it before it runs."
    )
    input_model, output_model = EditInput, WriteResult
    permissions: ClassVar[frozenset[str]] = frozenset({"workspace:write"})
    timeout_s = 10.0
    idempotent = False
    requires_approval = True

    async def run(self, args: EditInput, ctx: ExecutionContext) -> WriteResult:
        return await self._ws(ctx).edit(args.path, args.old, args.new, self._actor(ctx))

    def ledger_arguments(self, args: EditInput) -> dict[str, Any]:
        return {
            "path": args.path,
            "old_chars": len(args.old),
            "new_chars": len(args.new),
            "old_sha256": _sha(args.old),
        }

    def ledger_output(self, output: WriteResult) -> dict[str, Any]:
        return output.model_dump(include={"path", "bytes", "sha256", "replayed"})


class ExecInput(BaseModel):
    argv: list[str] = Field(
        min_length=1,
        max_length=32,
        description='Command as a list, no shell: ["git", "status"], ["python", "-m", "pytest"].',
    )


class WorkspaceExec(_WorkspaceTool[ExecInput, ExecResult]):
    name = "workspace_exec"
    description = (
        "Run an allowlisted command in the workspace (no shell, no pipes). Read-only: ls, cat, "
        "head, tail, wc, grep, find, git status/diff/log. Need human approval: python -m pytest, "
        "pytest, git add, mkdir."
    )
    input_model, output_model = ExecInput, ExecResult
    permissions: ClassVar[frozenset[str]] = frozenset({"workspace:exec"})
    idempotent = False
    requires_approval = True  # may need it; approval_needed decides per command
    timeout_s = 610.0  # above any allowed exec timeout (<= 600 s): the executor's fires first

    def approval_needed(self, args: ExecInput, ctx: ExecutionContext) -> bool:
        # Raises CommandNotAllowedError / WorkspaceAccessError: rejected before anyone is asked.
        return self._ws(ctx).plan(args.argv).writes

    async def run(self, args: ExecInput, ctx: ExecutionContext) -> ExecResult:
        result = await self._ws(ctx).exec(args.argv, self._actor(ctx))
        if len(result.output) > _AGENT_OUTPUT_CHARS:
            result = result.model_copy(
                update={"output": result.output[:_AGENT_OUTPUT_CHARS], "truncated": True}
            )
        return result

    def ledger_output(self, output: ExecResult) -> dict[str, Any]:
        return output.model_dump(include={"exit_code", "timed_out", "truncated", "duration_ms"})


def workspace_tools(workspaces: WorkspaceManager) -> list[Tool[Any, Any]]:
    return [
        WorkspaceList(workspaces),
        WorkspaceRead(workspaces),
        WorkspaceSearch(workspaces),
        WorkspaceWrite(workspaces),
        WorkspaceEdit(workspaces),
        WorkspaceExec(workspaces),
    ]
